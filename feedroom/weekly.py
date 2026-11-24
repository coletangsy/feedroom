"""Build a Hong Kong weekly shortlist from daily publication history and sources."""

from collections.abc import Collection
from contextlib import closing
from datetime import datetime, timedelta
import json
from pathlib import Path
import sqlite3
from urllib.parse import urlsplit

from .briefing import HKT
from .collector import (
    DAILY_PAPERS, DAIR_AI_PAPERS, TLDR_AI_FEED, canonical_url, in_weekly_window, is_article_url, item_id,
    matches_daily, title_id,
)
from .evidence import ARTICLE_HOSTS, group_stories
from .threads import parse_publication_time

MAX_WEEKLY_EVENTS = 12
MAX_SUPPLEMENT_CANDIDATES = 4


def weekly_window(value: str) -> tuple[datetime, datetime]:
    cutoff = datetime.fromisoformat(value)
    if cutoff.tzinfo is None:
        raise ValueError("--as-of needs an ISO timestamp with a time zone, such as 2026-11-22T18:00:00+08:00.")
    cutoff = cutoff.astimezone(HKT)
    start = cutoff.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=cutoff.weekday())
    return start, cutoff


def daily_posts(path: Path, account: str) -> dict:
    if not path.is_file():
        raise ValueError(f"Daily SQLite journal does not exist: {path}")
    with closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)) as db:
        row = db.execute("SELECT body FROM journals WHERE account = ?", (account,)).fetchone()
    posts = json.loads(row[0]) if row else {}
    if not isinstance(posts, dict):
        raise ValueError("Expected daily posts by Hong Kong date in the SQLite journal.")
    return posts


def has_original_source(item: dict) -> bool:
    # Issue, coverage and submission dates do not establish the article's date.
    if item["source"] in {DAIR_AI_PAPERS.name, TLDR_AI_FEED.name, "Hacker News"}:
        return False
    return item["section"] == "latest" or (item["source"] == DAILY_PAPERS and item.get("upvotes", 0) >= 100)


def daily_story_references(posts: dict, start: datetime, cutoff: datetime) -> tuple[set[str], set[str]]:
    keys = set()
    links = set()
    for key, post in posts.items():
        if ":" in key or not isinstance(post, dict) or post.get("status") != "published":
            continue
        published_at = parse_publication_time(post.get("published_at"))
        if published_at is None or not start <= published_at <= cutoff:
            continue
        urls = post.get("source_urls")
        if not isinstance(urls, (list, tuple)):
            urls = []
        links.update(canonical_url(url) for url in [post.get("source_url"), *urls]
                     if isinstance(url, str) and is_article_url(url))
        related = post.get("related_ids")
        if not isinstance(related, (list, tuple)):
            related = []
        keys.update(key for key in [post.get("candidate_id"), post.get("title_id"), *related]
                    if isinstance(key, str) and key)
    keys.update(item_id(url, "") for url in links)
    return keys, links


def weekly_groups(
    candidates: list[dict], posts: dict, start: datetime, cutoff: datetime,
    *, listed_daily_links: Collection[str] | None = None,
) -> list[tuple[list[dict], bool]]:
    keys, links = daily_story_references(posts, start, cutoff)

    def from_daily(item: dict) -> bool:
        return matches_daily(item, keys)

    eligible = [
        item for item in candidates
        if has_original_source(item) and in_weekly_window(item, start, cutoff, keys)
    ]
    eligible.sort(key=lambda item: (-item["score"], not from_daily(item)))
    daily, repeats = [], []
    supplements_by_source = {}
    for group in group_stories(eligible):
        covered = any(from_daily(item) for item in group)
        if covered:
            group.sort(key=lambda item: not from_daily(item))
            daily.append((group, True))
        elif group[0]["source"] in supplements_by_source:
            repeats.append((group, False))
        else:
            supplements_by_source[group[0]["source"]] = (group, False)
    if listed_daily_links is not None:
        for url in sorted(links.difference(listed_daily_links)):
            address = urlsplit(url)
            if address.scheme == "https" and address.netloc in ARTICLE_HOSTS:
                # The writer must retrieve original article text for these links.
                daily.append(([{
                    "url": url, "id": item_id(url, ""), "title": url, "title_id": title_id(url),
                    "source": address.netloc, "published_at": None, "summary_or_snippet": "",
                }], True))
    supplemental = [*supplements_by_source.values(), *repeats][:MAX_SUPPLEMENT_CANDIDATES]
    return daily[:MAX_WEEKLY_EVENTS - len(supplemental)] + supplemental
