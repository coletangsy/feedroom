#!/usr/bin/env python3
"""Collect high-signal current and durable AI reading into deterministic JSON."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import html
import io
import json
import re
import sys
import tempfile
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections.abc import Set
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from http.client import HTTPException
from pathlib import Path
from typing import Any

from .anthropic_sources import anthropic_entries
from .http import NoRedirect
from .uber_sources import uber_entries


LOOKBACK_HOURS = 36
READING_LOOKBACK_DAYS = 90
STATE_DAYS = 14
SNIPPET_LENGTH = 1200
USER_AGENT = "feedroom/0.1 (+https://github.com/coletangsy/feedroom)"
SCOPED_TERMS = (
    "ai", "artificial intelligence", "llm", "language model", "gpt", "gemini",
    "claude", "agent", "machine learning", "deep learning", "data science",
    "mlops", "inference", "embedding", "benchmark", "transformer", "rag",
    "hugging face", "openai", "anthropic", "deepmind", "model",
    "data engineering", "data platform", "recommendation", "recommender",
    "experimentation", "a/b test", "forecasting",
)
IMPACT_TERMS = (
    "release", "launch", "api", "model", "benchmark", "open source", "security",
    "safety", "privacy", "policy", "regulation", "governance", "inference",
    "training", "research", "agent", "breaking", "deprecat", "performance",
)
READING_TERMS = (
    "analysis", "architecture", "case study", "deep dive", "explained", "guide",
    "lessons", "postmortem", "tutorial", "evaluation", "benchmark", "research",
    "paper", "scaling", "inference", "training", "dataset", "reproducibility",
    "interpretability",
    "study", "report", "methodology", "findings",
)


@dataclass(frozen=True)
class Feed:
    name: str
    url: str
    weight: int
    format: str = "rss"


FEEDS = (
    Feed("OpenAI", "https://openai.com/news/rss.xml", 5),
    Feed("Anthropic News", "https://www.anthropic.com/news", 5, "anthropic"),
    Feed("Anthropic Research", "https://www.anthropic.com/research", 3, "anthropic"),
    Feed("Google AI", "https://blog.google/technology/ai/rss/", 5),
    Feed("Google DeepMind", "https://deepmind.google/blog/rss.xml", 3),
    Feed("Google Research", "https://research.google/blog/rss/", 3),
    Feed("Google Innovation & AI", "https://blog.google/innovation-and-ai/rss/", 4),
    Feed("Meta Engineering AI Research", "https://engineering.fb.com/category/ai-research/feed/", 5),
    Feed("Microsoft Research", "https://www.microsoft.com/en-us/research/feed/", 3),
    Feed("Netflix TechBlog", "https://netflixtechblog.com/feed", 4),
    Feed("Spotify Engineering", "https://engineering.atspotify.com/feed/", 4),
    Feed("Uber Engineering", "https://www.uber.com/us/en/blog/engineering/", 4, "uber"),
    Feed("Hugging Face", "https://huggingface.co/blog/feed.xml", 5),
    Feed("Apple ML Research", "https://machinelearning.apple.com/rss.xml", 3),
    Feed("AWS Machine Learning Blog", "https://aws.amazon.com/blogs/machine-learning/feed/", 4),
    Feed("Databricks", "https://www.databricks.com/feed", 4),
    Feed("PyTorch", "https://pytorch.org/blog/feed.xml", 5),
    Feed("The Verge AI", "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml", 2),
    Feed("TechCrunch AI", "https://techcrunch.com/category/artificial-intelligence/feed/", 2),
    Feed("NVIDIA Blog", "https://blogs.nvidia.com/feed/", 3),
    Feed("MIT Technology Review AI", "https://www.technologyreview.com/topic/artificial-intelligence/feed/", 3),
)
TLDR_AI_FEED = Feed("TLDR AI", "https://tldr.tech/api/rss/ai", 2)
DAILY_PAPERS = "Hugging Face Daily Papers"
DAIR_AI_PAPERS = Feed("DAIR.AI Papers", "https://academy.dair.ai/papers", 3)
GITHUB_RELEASES = (
    ("vLLM", "vllm-project/vllm"),
    ("Hugging Face Transformers", "huggingface/transformers"),
    ("OpenAI Python", "openai/openai-python"),
)


class TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def text(self) -> str:
        return " ".join(" ".join(self.parts).split())


def clean_text(value: str | None) -> str:
    parser = TextExtractor()
    parser.feed(html.unescape(value or ""))
    return parser.text()


def canonical_url(value: str) -> str:
    parsed = urllib.parse.urlsplit(value.strip())
    kept_query = urllib.parse.urlencode(
        [(key, val) for key, val in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
         if not key.lower().startswith(("utm_", "ref", "source"))],
        doseq=True,
    )
    path = parsed.path.rstrip("/") or "/"
    return urllib.parse.urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), path, kept_query, ""))


def item_id(url: str, title: str) -> str:
    basis = canonical_url(url) if url else "title:" + " ".join(title.lower().split())
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:20]


def title_id(title: str) -> str:
    return hashlib.sha256(("title:" + " ".join(title.lower().split())).encode("utf-8")).hexdigest()[:20]


def parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return parsed.replace(tzinfo=parsed.tzinfo or UTC).astimezone(UTC)


def is_article_url(url: str) -> bool:
    if any(character.isspace() or ord(character) < 32 for character in url):
        return False
    try:
        parsed = urllib.parse.urlsplit(url)
        return (parsed.scheme in {"http", "https"} and bool(parsed.hostname)
                and parsed.username is None and parsed.path not in {"", "/"})
    except ValueError:
        return False


def fetch(url: str, *, follow_redirects: bool = True) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json, application/atom+xml, application/rss+xml, text/xml, text/html, */*"})
    open_url = urllib.request.urlopen if follow_redirects else urllib.request.build_opener(NoRedirect()).open
    limit = 8_000_000
    try:
        with open_url(request, timeout=20) as response:
            content = response.read(limit + 1)
    except HTTPException as error:
        raise OSError(str(error)) from error
    if content.startswith(b"\x1f\x8b"):
        with gzip.GzipFile(fileobj=io.BytesIO(content)) as compressed:
            content = compressed.read(limit + 1)
    if len(content) > limit:
        raise ValueError("Source response exceeded the byte limit.")
    return content


def text_at(element: ET.Element, names: tuple[str, ...]) -> str:
    for name in names:
        found = element.find(name)
        if found is not None and found.text:
            return found.text.strip()
    return ""


def feed_entries(feed: Feed) -> list[dict[str, str]]:
    if feed.format == "anthropic":
        return anthropic_entries(feed.url, fetch)
    if feed.format == "uber":
        return uber_entries(feed.url, fetch)
    root = ET.fromstring(fetch(feed.url))
    entries: list[dict[str, str]] = []
    for element in root.findall(".//item") + root.findall("{http://www.w3.org/2005/Atom}entry"):
        atom = element.tag.endswith("entry")
        title = text_at(element, ("title", "{http://www.w3.org/2005/Atom}title"))
        if atom:
            link = next((node.attrib.get("href", "") for node in element.findall("{http://www.w3.org/2005/Atom}link") if node.attrib.get("rel", "alternate") == "alternate"), "")
            summary = text_at(element, ("{http://www.w3.org/2005/Atom}summary", "{http://www.w3.org/2005/Atom}content"))
            published = text_at(element, ("{http://www.w3.org/2005/Atom}published", "{http://www.w3.org/2005/Atom}updated"))
        else:
            link = text_at(element, ("link",))
            summary = text_at(element, ("description", "{http://purl.org/rss/1.0/modules/content/}encoded"))
            published = text_at(element, ("pubDate", "date"))
        entries.append({"title": clean_text(title), "url": link, "summary": clean_text(summary), "published_at": published})
    return entries


def tldr_issue_entries(value: str) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    for article in re.findall(r"<article\b[^>]*>(.*?)</article>", value, re.I | re.S):
        link_match = re.search(
            r'<a\b(?=[^>]*\bclass=["\'][^"\']*\bfont-bold\b[^"\']*["\'])(?=[^>]*\bhref=["\']([^"\']+)["\'])[^>]*>',
            article,
            re.I | re.S,
        )
        title_match = re.search(r"<h3>(.*?)</h3>", article, re.I | re.S)
        summary_match = re.search(r'<div\b[^>]*class=["\'][^"\']*\bnewsletter-html\b[^"\']*["\'][^>]*>(.*?)</div>', article, re.I | re.S)
        title = clean_text(title_match.group(1) if title_match else "")
        summary = clean_text(summary_match.group(1) if summary_match else "")
        if not link_match or not title or not summary or "sponsor" in title.lower():
            continue
        entries.append({"title": title, "url": html.unescape(link_match.group(1)), "summary": summary})
    return entries


def tldr_entries(cutoff: datetime, errors: list[dict[str, str]], as_of: datetime | None = None) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    for issue in feed_entries(TLDR_AI_FEED):
        if not in_window(issue["published_at"], cutoff, as_of or datetime.now(UTC)):
            continue
        published = parse_date(issue["published_at"])
        try:
            url = urllib.parse.urlsplit(issue["url"])
            if url.scheme != "https" or url.netloc != "tldr.tech" or not url.path.startswith("/ai/"):
                raise ValueError("Unexpected TLDR AI issue URL.")
            articles = tldr_issue_entries(fetch(issue["url"], follow_redirects=False).decode("utf-8", errors="replace"))
        except Exception as error:
            errors.append({"source": TLDR_AI_FEED.name, "error": f"Issue dated {published.date()}: {error}"[:300]})
            continue
        for article in articles:
            article["published_at"] = issue["published_at"]
            entries.append(article)
    return entries


def dair_entries(cutoff: datetime, as_of: datetime) -> list[dict[str, str]]:
    archive = fetch(DAIR_AI_PAPERS.url, follow_redirects=False).decode("utf-8")
    weeks = set()
    for path, start_date, month, day in re.findall(
        r"""<a\b[^>]*\shref=["'](/papers/week/(\d{4}-\d{2}-\d{2})-([a-z]+)-(\d{1,2}))["']""",
        archive,
    ):
        try:
            start = datetime.fromisoformat(start_date).date()
            end = datetime.strptime(f"{start.year} {month} {day}", "%Y %B %d").date()
            if end < start:
                end = end.replace(year=end.year + 1)
        except ValueError:
            continue
        if 0 <= (end - start).days <= 7:
            weeks.add((end, path))
    if not weeks:
        raise ValueError("No DAIR.AI weekly issues found.")
    issue = max((week for week in weeks if cutoff.date() <= week[0] < as_of.date()), default=None)
    if issue is None:
        return []
    end, path = issue
    url = urllib.parse.urljoin(DAIR_AI_PAPERS.url, path)
    page = fetch(url, follow_redirects=False).decode("utf-8")
    entries = []
    for path, card in re.findall(
        r"""<a\b[^>]*\shref=["'](/papers/[\w][\w.-]*)["'][^>]*>(.*?)</a>""", page, re.S,
    ):
        heading = re.search(r"<h3\b[^>]*>(.*?)</h3>", card, re.S)
        paragraph = re.search(r"<p\b[^>]*>(.*?)</p>", card, re.S)
        title = clean_text(heading[1]) if heading else ""
        summary = clean_text(paragraph[1]) if paragraph else ""
        if title and summary:
            arxiv = re.search(r"-(\d{4}\.\d{4,5})(?:v\d+)?$", path)
            url = f"https://arxiv.org/abs/{arxiv[1]}" if arxiv else urllib.parse.urljoin(DAIR_AI_PAPERS.url, path)
            entries.append({"title": title, "url": url, "summary": summary, "published_at": end.isoformat()})
    if not entries:
        raise ValueError(f"No paper summaries found for DAIR.AI issue ending {end}.")
    return entries


def paper_candidates(
    cutoff: datetime, as_of: datetime, errors: list[dict[str, str]], *, filter_dates: bool = True,
) -> list[dict[str, Any]]:
    start = cutoff.date() - timedelta(days=cutoff.weekday())
    papers = {}
    while start <= as_of.date():
        year, week, _ = start.isocalendar()
        label = f"{year}-W{week:02d}"
        start += timedelta(days=7)
        try:
            rows = json.loads(fetch(f"https://huggingface.co/api/daily_papers?week={label}&limit=100", follow_redirects=False))
            for row in rows:
                paper = row.get("paper") or {}
                paper_id = paper.get("id") or ""
                upvotes = int(paper.get("upvotes") or 0)
                published = paper.get("publishedAt") or ""
                if not re.fullmatch(r"\d{4}\.\d{4,5}", paper_id) or upvotes < 100:
                    continue
                if filter_dates and not in_window(published, cutoff, as_of):
                    continue
                title = clean_text(paper.get("title") or row.get("title"))
                summary = clean_text(paper.get("summary") or row.get("summary"))
                url = f"https://arxiv.org/abs/{paper_id}"
                score = score_reading_item(title, url, summary, 3) or 4
                item = candidate(DAILY_PAPERS, title, url, published, summary, score, section="reading", upvotes=upvotes)
                if item and summary and (paper_id not in papers or upvotes > papers[paper_id]["upvotes"]):
                    papers[paper_id] = item
        except (OSError, ValueError, AttributeError, TypeError) as error:
            errors.append({"source": DAILY_PAPERS, "error": f"{label}: {error}"[:300]})
    return list(papers.values())


def has_term(corpus: str, term: str) -> bool:
    if len(term) > 3:
        return term in corpus
    return re.search(rf"(?<![a-z0-9]){re.escape(term)}s?(?![a-z0-9])", corpus) is not None


def score_item(title: str, url: str, summary: str, source_weight: int, *, hn_points: int = 0, hn_comments: int = 0, github_release: bool = False) -> int | None:
    corpus = f"{title} {url} {summary[:SNIPPET_LENGTH]}".lower()
    scoped_hits = sum(has_term(corpus, term) for term in SCOPED_TERMS)
    if not scoped_hits:
        return None
    score = source_weight + min(scoped_hits, 3)
    score += min(sum(has_term(corpus, term) for term in IMPACT_TERMS), 3)
    if hn_points or hn_comments:
        score += 4 if hn_points >= 300 or hn_comments >= 100 else 2 if hn_points >= 100 or hn_comments >= 30 else 1 if hn_points >= 30 or hn_comments >= 10 else -2
    release_signals = ("breaking", "security", "performance", "api", "model", "inference", "deprecat", "support")
    if github_release and re.search(r"\bv?\d+(?:\.\d+){1,4}\b", title, re.I) and not any(term in corpus for term in release_signals):
        score -= 4
    return score


def score_reading_item(title: str, url: str, summary: str, source_weight: int) -> int | None:
    score = score_item(title, url, summary, source_weight)
    if score is None:
        return None
    corpus = f"{title} {url} {summary[:SNIPPET_LENGTH]}".lower()
    depth_hits = sum(has_term(corpus, term) for term in READING_TERMS)
    return score + min(depth_hits, 3) if depth_hits else None


def in_window(value: str, cutoff: datetime, as_of: datetime) -> bool:
    published = parse_date(value)
    if published is None or published > as_of:
        return False
    if value == published.date().isoformat():
        return published.date() >= cutoff.date()
    return published >= cutoff


def in_calendar_window(value: str, start: datetime | None, as_of: datetime) -> bool:
    published = parse_date(value)
    if published is None:
        return False
    if value == published.date().isoformat():
        return published.date() <= as_of.date() and (start is None or start.date() <= published.date())
    return published <= as_of and (start is None or start <= published)


def matches_daily(item: dict[str, Any], keys: Set[str]) -> bool:
    return bool(keys) and not keys.isdisjoint((item["id"], title_id(item["title"])))


def in_weekly_window(
    item: dict[str, Any], start: datetime, as_of: datetime, daily_keys: Set[str],
) -> bool:
    return in_calendar_window(item["published_at"], None if matches_daily(item, daily_keys) else start, as_of)


def candidate(source: str, title: str, url: str, published_at: str, summary: str, score: int, **extra: Any) -> dict[str, Any] | None:
    published = parse_date(published_at)
    if not title or not is_article_url(url) or not published:
        return None
    data: dict[str, Any] = {
        "id": item_id(url, title),
        "title_id": title_id(title),
        "title": title,
        "url": canonical_url(url),
        "source": source,
        "published_at": published_at if published_at == published.date().isoformat() else published.isoformat().replace("+00:00", "Z"),
        "summary_or_snippet": summary[:SNIPPET_LENGTH],
        "score": score,
    }
    data.update(extra)
    return data


def hn_entries(cutoff: datetime, as_of: datetime | None = None) -> list[dict[str, Any]]:
    bounds = f"created_at_i>={int(cutoff.timestamp())}"
    if as_of is not None:
        bounds += f",created_at_i<={int(as_of.timestamp())}"
    query = urllib.parse.urlencode({"tags": "story", "hitsPerPage": 1000, "numericFilters": bounds})
    payload = json.loads(fetch(f"https://hn.algolia.com/api/v1/search?{query}"))
    entries: list[dict[str, Any]] = []
    for hit in payload.get("hits", []):
        title = clean_text(hit.get("title") or hit.get("story_title"))
        original_url = hit.get("url") or hit.get("story_url") or ""
        discussion_url = f"https://news.ycombinator.com/item?id={hit['objectID']}"
        url = original_url if is_article_url(original_url) else discussion_url
        points = int(hit.get("points") or 0)
        comments = int(hit.get("num_comments") or 0)
        corpus = f"{title} {original_url}".lower()
        if not any(has_term(corpus, term) for term in SCOPED_TERMS):
            continue
        if url == discussion_url and points < 100 and comments < 30:
            continue
        entries.append({"title": title, "url": url, "summary": clean_text(hit.get("story_text") or ""), "published_at": hit.get("created_at"), "points": points, "comments": comments, "discussion_url": discussion_url})
    return entries


def github_entries(name: str, repository: str) -> list[dict[str, str]]:
    payload = json.loads(fetch(f"https://api.github.com/repos/{repository}/releases?per_page=10"))
    return [{
        "title": f"{name} {release.get('name') or release.get('tag_name')}",
        "url": release.get("html_url", ""),
        "summary": clean_text(release.get("body") or ""),
        "published_at": release.get("published_at") or release.get("created_at") or "",
    } for release in payload if not release.get("draft")]


def load_state(path: Path, now: datetime) -> set[str]:
    if not path.exists():
        return set()
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return set()
    cutoff = now - timedelta(days=STATE_DAYS)
    return {entry["id"] for entry in payload.get("items", []) if entry.get("id") and (seen_at := parse_date(entry.get("seen_at"))) and seen_at >= cutoff}


def save_seen(path: Path, ids: list[str], now: datetime) -> None:
    existing = []
    if path.exists():
        try:
            existing = json.loads(path.read_text()).get("items", [])
        except (OSError, json.JSONDecodeError):
            pass
    cutoff = now - timedelta(days=STATE_DAYS)
    retained = [entry for entry in existing if entry.get("id") and (seen_at := parse_date(entry.get("seen_at"))) and seen_at >= cutoff and entry["id"] not in ids]
    retained.extend({"id": value, "seen_at": now.isoformat().replace("+00:00", "Z")} for value in ids)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        json.dump({"items": retained}, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        temporary = Path(handle.name)
    temporary.replace(path)


def selected_ids(path: Path) -> list[str]:
    payload = json.loads(path.read_text())
    values = payload.get("selected_ids", []) if isinstance(payload, dict) else payload
    if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
        raise ValueError("expected a JSON list of IDs or an object with selected_ids")
    return values


def collect(
    state_path: Path | None, lookback_hours: int, max_items: int, reading_lookback_days: int = READING_LOOKBACK_DAYS,
    *, as_of: datetime | None = None, published_ids: Set[str] = frozenset(), include_aggregators: bool = True,
    week_start: datetime | None = None, daily_keys: Set[str] = frozenset(),
) -> dict[str, Any]:
    now = as_of or datetime.now(UTC)
    cutoff = now - timedelta(hours=lookback_hours)
    reading_cutoff = now - timedelta(days=reading_lookback_days)
    seen = load_state(state_path, now) if state_path is not None else set()
    errors: list[dict[str, str]] = []
    raw_candidates: list[dict[str, Any]] = []

    def add_feed_entries(feed: Feed, entries: list[dict[str, str]]) -> None:
        for entry in entries:
            if week_start is None and not in_window(entry["published_at"], cutoff, now):
                continue
            score = score_item(entry["title"], entry["url"], entry["summary"], feed.weight)
            if score is not None:
                item = candidate(feed.name, entry["title"], entry["url"], entry["published_at"], entry["summary"], score, section="latest")
                if item:
                    raw_candidates.append(item)

    for feed in FEEDS:
        try:
            add_feed_entries(feed, feed_entries(feed))
        except Exception as error:  # One feed must not prevent the rest of the briefing.
            errors.append({"source": feed.name, "error": str(error)[:300]})

    if include_aggregators:
        try:
            add_feed_entries(TLDR_AI_FEED, tldr_entries(cutoff, errors, now))
        except Exception as error:
            errors.append({"source": TLDR_AI_FEED.name, "error": str(error)[:300]})

        try:
            entries = hn_entries(cutoff, now)
            for entry in entries:
                score = score_item(entry["title"], entry["url"], entry["summary"], 1, hn_points=entry["points"], hn_comments=entry["comments"])
                if score is not None and in_window(entry["published_at"], cutoff, now):
                    item = candidate("Hacker News", entry["title"], entry["url"], entry["published_at"], entry["summary"], score, section="latest", points=entry["points"], comments=entry["comments"], discussion_url=entry["discussion_url"])
                    if item:
                        raw_candidates.append(item)
        except Exception as error:
            errors.append({"source": "Hacker News", "error": str(error)[:300]})

    for name, repository in GITHUB_RELEASES:
        try:
            for entry in github_entries(name, repository):
                score = score_item(entry["title"], entry["url"], entry["summary"], 4, github_release=True)
                if score is not None and (week_start is not None or in_window(entry["published_at"], cutoff, now)):
                    item = candidate(name, entry["title"], entry["url"], entry["published_at"], entry["summary"], score, section="latest", tags=["github-release"])
                    if item:
                        raw_candidates.append(item)
        except Exception as error:
            errors.append({"source": name, "error": str(error)[:300]})

    paper_start = reading_cutoff
    if week_start is not None:
        # A confirmed daily post can carry a paper over from the previous listing.
        paper_start = week_start - timedelta(days=7) if daily_keys else week_start
    raw_candidates.extend(paper_candidates(paper_start, now, errors, filter_dates=week_start is None))
    if include_aggregators:
        try:
            for entry in dair_entries(reading_cutoff, now):
                title, url, summary = entry["title"], entry["url"], entry["summary"]
                score = score_reading_item(title, url, summary, DAIR_AI_PAPERS.weight) or 4
                item = candidate(DAIR_AI_PAPERS.name, title, url, entry["published_at"], summary, score, section="reading")
                if item:
                    raw_candidates.append(item)
        except (OSError, ValueError) as error:
            errors.append({"source": DAIR_AI_PAPERS.name, "error": str(error)[:300]})
    listed_daily_links = set()
    if week_start is not None:
        listed_daily_links = {item["url"] for item in raw_candidates if matches_daily(item, daily_keys)}
        raw_candidates = [item for item in raw_candidates if in_weekly_window(item, week_start, now, daily_keys)]

    def rank(item: dict[str, Any]) -> tuple:
        return (not matches_daily(item, daily_keys), item["section"] != "latest",
                -item.get("upvotes", 0), -item["score"], item["published_at"])

    deduplicated: list[dict[str, Any]] = []
    known_titles: set[str] = set()
    known_ids: set[str] = set()
    for item in sorted(raw_candidates, key=rank):
        if item["id"] in seen or item["id"] in known_ids or item["title_id"] in seen or item["title_id"] in known_titles:
            continue
        if item["score"] < 4 and not matches_daily(item, daily_keys):
            continue
        known_titles.add(item["title_id"])
        known_ids.add(item["id"])
        deduplicated.append(item)
    daily = [item for item in deduplicated if matches_daily(item, daily_keys)]
    other = [item for item in deduplicated if not matches_daily(item, daily_keys)]
    latest = [item for item in other if item["section"] == "latest"]
    eligible_reading = [
        item for item in other
        if item["section"] == "reading" and published_ids.isdisjoint((item["id"], item["title_id"]))
    ]
    curated = [item for item in eligible_reading if item["source"] == DAIR_AI_PAPERS.name]
    popular = [item for item in eligible_reading if item["source"] != DAIR_AI_PAPERS.name]
    reading = [*popular[:1], *curated[:1], *popular[1:], *curated[1:]][:8]
    ordered = daily
    for index in range(max(len(latest), len(reading))):
        if index < len(latest):
            ordered.append(latest[index])
        if index < len(reading):
            ordered.append(reading[index])
    result = {"collected_at": now.isoformat().replace("+00:00", "Z"), "lookback_hours": lookback_hours,
              "reading_lookback_days": reading_lookback_days, "candidates": ordered[:max_items], "collection_errors": errors}
    if week_start is not None:
        result["listed_daily_links"] = sorted(listed_daily_links)
    return result


def add_arguments(parser: argparse.ArgumentParser, *, legacy: bool = False, replay: bool = True) -> None:
    parser.add_argument("--state", type=Path, default=Path("state/ai_news_seen.json"))
    parser.add_argument("--lookback-hours", type=int, default=LOOKBACK_HOURS)
    parser.add_argument("--reading-lookback-days", type=int, default=READING_LOOKBACK_DAYS)
    parser.add_argument("--max-items", type=int, default=40)
    if replay:
        parser.add_argument("--as-of", help="ISO timestamp or date at 08:00 Hong Kong time.")
    if legacy:
        parser.add_argument("--mark-seen", type=Path, metavar="JSON")


def collect_from_args(args: argparse.Namespace, *, published_ids: Set[str] = frozenset()) -> dict[str, Any]:
    value = getattr(args, "as_of", None)
    as_of = parse_date(value)
    if value and as_of is None:
        raise ValueError("--as-of must be an ISO date or timestamp.")
    return collect(args.state, args.lookback_hours, args.max_items, args.reading_lookback_days,
                   as_of=as_of, published_ids=published_ids)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    add_arguments(parser, legacy=True)
    args = parser.parse_args()
    if args.mark_seen:
        try:
            ids = selected_ids(args.mark_seen)
            save_seen(args.state, ids, datetime.now(UTC))
        except (OSError, ValueError, json.JSONDecodeError) as error:
            print(f"Cannot update seen state: {error}", file=sys.stderr)
            return 1
        print(json.dumps({"marked_seen": len(ids)}, ensure_ascii=False))
        return 0
    print(json.dumps(collect_from_args(args), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
