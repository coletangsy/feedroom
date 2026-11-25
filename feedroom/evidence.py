"""Group duplicate candidates and read a bounded amount of public article text."""

from http.client import HTTPException
import re
from time import monotonic, sleep
from urllib.error import HTTPError
from urllib.parse import urljoin, urlsplit
from urllib.request import Request, build_opener
from urllib.robotparser import RobotFileParser

from trafilatura import extract

from .collector import DAIR_AI_PAPERS, FEEDS, USER_AGENT, is_article_url, item_id, title_id
from .http import NoRedirect

ARTICLE_HOSTS = {urlsplit(feed.url).hostname for feed in (*FEEDS, DAIR_AI_PAPERS)} | {"arxiv.org"}
STOP_WORDS = {"about", "after", "from", "into", "more", "that", "their", "this", "what", "when", "with", "will", "your"}
URL_TEXT = re.compile(r"(?:https?://|www\.)[^\s<>]+", re.IGNORECASE)
ARXIV_PATH = re.compile(r"/(?:abs|pdf|html)/([0-9]{4}\.[0-9]{4,5})(?:v[0-9]+)?(?:\.pdf)?/?")


def arxiv_story_key(url: str) -> str | None:
    try:
        address = urlsplit(url)
    except ValueError:
        return None
    if address.scheme in {"http", "https"} and address.netloc.lower() in {"arxiv.org", "www.arxiv.org", "export.arxiv.org"}:
        if match := ARXIV_PATH.fullmatch(address.path):
            return item_id(f"https://arxiv.org/abs/{match[1]}", "")
    return None


def story_keys(item: dict) -> set[str]:
    keys = {item["id"], title_id(item["title"])}
    if key := arxiv_story_key(item["url"]):
        keys.add(key)
    return keys


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
    def __init__(self, *, follow_article_slash: bool = False, limit: int = 8):
        self.opener = build_opener(NoRedirect())
        self.follow_article_slash = follow_article_slash
        self.robots: dict[str, RobotFileParser] = {}
        self.last_request: dict[str, float] = {}
        self.remaining = limit

    def fetch(self, url: str, limit: int, *, html_only: bool = False) -> bytes:
        host = urlsplit(url).hostname
        rules = self.robots.get(host)
        delay = rules.crawl_delay(USER_AGENT) if rules else None
        pause = self.last_request[host] + max(1, delay or 0) - monotonic() if host in self.last_request else 0
        if pause > 0:
            sleep(pause)
        self.last_request[host] = monotonic()
        request = Request(url, headers={"User-Agent": USER_AGENT})
        with self.opener.open(request, timeout=8) as response:
            if html_only and response.headers.get_content_type() not in {"text/html", "application/xhtml+xml"}:
                return b""
            data = response.read(limit + 1)
        return data if len(data) <= limit else b""

    def _article_page(self, url: str, robots: RobotFileParser) -> bytes:
        limit = 2_000_000
        try:
            return self.fetch(url, limit, html_only=True)
        except HTTPError as error:
            address = urlsplit(url)
            if not self.follow_article_slash or error.code not in {301, 302, 307, 308} or address.path.endswith("/"):
                raise
            # Collection strips the slash that some publishers require on article URLs.
            variant = address._replace(path=address.path + "/").geturl()
            if urljoin(url, error.headers.get("Location", "")) != variant or not robots.can_fetch(USER_AGENT, variant):
                raise
            return self.fetch(variant, limit, html_only=True)

    def page(self, url: str) -> bytes:
        address = urlsplit(url)
        if not self.remaining or address.scheme != "https" or address.netloc not in ARTICLE_HOSTS:
            return b""
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
                return b""
            return self._article_page(url, self.robots[host])
        except (OSError, HTTPException, ValueError):
            return b""

    def text(self, url: str) -> str:
        try:
            page = self.page(url)
            article = extract(page, include_comments=False, favor_precision=True) if page else None
            return " ".join((article or "").split())
        except (OSError, HTTPException, ValueError):
            return ""


def source_evidence(item: dict, reader: ArticleReader, *, excerpt_limit: int = 2500) -> dict:
    snippet = item["summary_or_snippet"].strip()
    article = reader.text(item["url"])
    date_field = "coverage_end_date" if item["source"] == DAIR_AI_PAPERS.name else "source_date"
    return {
        "publisher": item["source"],
        "title": URL_TEXT.sub("[link]", item["title"]),
        date_field: item["published_at"],
        "snippet": URL_TEXT.sub("[link]", snippet),
        "article_excerpt": URL_TEXT.sub("[link]", article[:excerpt_limit]) if len(article) >= 500 else "",
        **({"hugging_face_upvotes": item["upvotes"]} if item.get("upvotes") else {}),
    }
