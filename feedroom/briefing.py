"""Build a daily Threads post from sourced headlines."""

import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

from .collector import TLDR_AI_FEED, parse_date, title_id
from .evidence import URL_TEXT, group_stories, story_keys

HKT = timezone(timedelta(hours=8), "HKT")
MAX_HEADLINES = 6
MAX_CARD_SUMMARY = 160
SENTENCE_END = re.compile(r"[。！？]+|[.!?]+(?=\s|$)")


@dataclass(frozen=True)
class Draft:
    candidate_id: str
    title_id: str
    text: str
    related_ids: tuple[str, ...] = ()
    made_with_ai: bool = False
    source_url: str | None = None
    source_urls: tuple[str, ...] = ()


@dataclass(frozen=True)
class SelectedStory:
    headline: str
    group: list[dict[str, Any]]
    summary: str | None = None


@dataclass(frozen=True)
class Card:
    headline: str
    summary: str | None
    source_name: str
    source_date: str | None
    source_url: str


@dataclass(frozen=True, kw_only=True)
class CardDraft(Draft):
    cards: tuple[Card, ...]


def source_summary(snippet: str) -> str | None:
    snippet = " ".join(URL_TEXT.sub("", snippet).split())
    endings = [match.end() for match in SENTENCE_END.finditer(snippet)]
    usable = [end for end in endings[:2] if end <= MAX_CARD_SUMMARY]
    return snippet[:usable[-1]] if usable else None


def build_card(headline: str, article: dict[str, Any], summary: str | None) -> Card:
    # Newsletter and aggregator dates describe the coverage, not the linked article.
    source = article["source"]
    published = None if source in {TLDR_AI_FEED.name, "Hacker News"} else parse_date(article.get("published_at"))
    return Card(headline, summary, source, published.date().isoformat() if published else None, article["url"])


def preview_day(collected_at: str | None) -> str:
    collected = parse_date(collected_at) if isinstance(collected_at, str) else None
    return (collected or datetime.now(UTC)).astimezone(HKT).date().isoformat()


def format_post(headlines: list[str], day: str) -> str | None:
    text = "\n".join([day, *(f"• {headline}" for headline in headlines)])
    # Meta counts emoji by UTF-8 bytes. Count symbol code points conservatively.
    length = sum(len(char.encode()) if unicodedata.category(char).startswith("S") else 1 for char in text)
    return text if length <= 500 else None


def build_draft(
    selected: list[SelectedStory], day: str, *, made_with_ai: bool = False, include_cards: bool = False,
) -> Draft | None:
    if not selected:
        return None
    text = format_post([story.headline for story in selected], day)
    if text is None:
        return None
    lead = selected[0].group[0]
    source_urls = tuple(story.group[0]["url"] for story in selected)
    related_ids = {key for story in selected for item in story.group for key in story_keys(item)} - story_keys(lead)
    draft = Draft(
        lead["id"], title_id(lead["title"]), text,
        related_ids=tuple(sorted(related_ids)), made_with_ai=made_with_ai,
        source_url=source_urls[0], source_urls=source_urls,
    )

    if not include_cards:
        return draft
    cards = tuple(build_card(story.headline, story.group[0], story.summary) for story in selected)
    return CardDraft(**vars(draft), cards=cards)


def select_draft(
    result: dict[str, Any], used: set[str], *, day: str | None = None, include_cards: bool = False,
) -> Draft | None:
    day = day or preview_day(result.get("collected_at"))
    candidates = sorted(result["candidates"], key=lambda item: item["section"] != "latest")
    selected = []
    blocked = set(used)
    for group in group_stories(candidates):
        keys = {key for item in group for key in story_keys(item)}
        if blocked & keys:
            continue
        headlines = [story.headline for story in selected]
        for item in group:
            title = " ".join(item["title"].split())
            if title and item["summary_or_snippet"].strip() and format_post([*headlines, title], day):
                primary_first = [item, *(other for other in group if other is not item)]
                summary = source_summary(item["summary_or_snippet"]) if include_cards else None
                selected.append(SelectedStory(title, primary_first, summary))
                blocked.update(keys)
                break
        if len(selected) == MAX_HEADLINES:
            break
    return build_draft(selected, day, include_cards=include_cards)
