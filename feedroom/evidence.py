"""Group duplicate candidates and read a bounded amount of public article text."""

import re
from time import monotonic, sleep
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener
from urllib.robotparser import RobotFileParser

from trafilatura import extract

from .collector import FEEDS, USER_AGENT, is_article_url, item_id, title_id
from .http import NoRedirect

ARTICLE_HOSTS = {urlsplit(feed.url).hostname for feed in FEEDS} | {"arxiv.org"}
STOP_WORDS = {"about", "after", "from", "into", "more", "that", "their", "this", "what", "when", "with", "will", "your"}
URL_TEXT = re.compile(r"(?:https?://|www\.)[^\s<>]+", re.IGNORECASE)


def story_keys(item: dict) -> set[str]:
    return {item["id"], title_id(item["title"])}


def group_stories(candidates: list[dict]) -> list[list[dict]]:
    groups: list[list[dict]] = []
    indexed_titles: list[list[tuple[str, set[str]]]] = []
    for item in candidates:
        title, url = item["title"], item["url"]
        if item["section"] not in {"latest", "reading"} or not is_article_url(url) or item["id"] != item_id(url, title):
            continue
        words = set(re.findall(r"[a-z0-9][a-z0-9.-]{2,}", title.lower())) - STOP_WORDS
        for group, titles in zip(groups, indexed_titles):
            if item["section"] == group[0]["section"] and any(
                url == other_url or (len(words & other_words) >= 3 and len(words & other_words) / len(words | other_words) >= 0.7)
                for other_url, other_words in titles
            ):
                group.append(item)
                titles.append((url, words))
                break
        else:
            groups.append([item])
            indexed_titles.append([(url, words)])
    return groups


class ArticleReader:
    def __init__(self):
        self.opener = build_opener(NoRedirect())
        self.robots: dict[str, RobotFileParser] = {}
        self.last_request: dict[str, float] = {}
        self.remaining = 8

    def fetch(self, url: str, limit: int, *, html_only: bool = False) -> bytes:
        host = urlsplit(url).hostname
        pause = self.last_request.get(host, 0) + 1 - monotonic()
        if pause > 0:
            sleep(pause)
        self.last_request[host] = monotonic()
        request = Request(url, headers={"User-Agent": USER_AGENT})
        with self.opener.open(request, timeout=8) as response:
            if html_only and response.headers.get_content_type() not in {"text/html", "application/xhtml+xml"}:
                return b""
            data = response.read(limit + 1)
        return data if len(data) <= limit else b""

    def text(self, url: str) -> str:
        address = urlsplit(url)
        if not self.remaining or address.scheme != "https" or address.netloc not in ARTICLE_HOSTS:
            return ""
        host = address.netloc
        self.remaining -= 1
        try:
            if host not in self.robots:
                rules = RobotFileParser()
                try:
                    body = self.fetch(f"https://{host}/robots.txt", 500_000)
                    rules.parse(body.decode("utf-8", errors="replace").splitlines() or ["User-agent: *", "Disallow: /"])
                except HTTPError as error:
                    if error.code not in {404, 410}:
                        raise
                    rules.parse([])
                self.robots[host] = rules
            if not self.robots[host].can_fetch(USER_AGENT, url):
                return ""
            page = self.fetch(url, 2_000_000, html_only=True)
            article = extract(page, include_comments=False, favor_precision=True) if page else None
            return " ".join((article or "").split())
        except (OSError, ValueError):
            return ""


def source_evidence(item: dict, reader: ArticleReader) -> dict:
    snippet = item["summary_or_snippet"].strip()
    article = reader.text(item["url"])
    return {
        "publisher": item["source"],
        "title": URL_TEXT.sub("[link]", item["title"]),
        "source_date": item["published_at"],
        "snippet": URL_TEXT.sub("[link]", snippet),
        "article_excerpt": URL_TEXT.sub("[link]", article[:2500]) if len(article) >= 500 else "",
        **({"hugging_face_upvotes": item["upvotes"]} if item.get("upvotes") else {}),
    }
