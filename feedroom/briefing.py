"""Build a daily Threads post from sourced headlines."""

import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

from .collector import parse_date, title_id
from .evidence import group_stories, story_keys

HKT = timezone(timedelta(hours=8), "HKT")
MAX_HEADLINES = 6


@dataclass(frozen=True)
class Draft:
    candidate_id: str
    title_id: str
    text: str
    related_ids: tuple[str, ...] = ()
    made_with_ai: bool = False
    source_url: str | None = None
    source_urls: tuple[str, ...] = ()


def preview_day(collected_at: str | None) -> str:
    collected = parse_date(collected_at) if isinstance(collected_at, str) else None
    return (collected or datetime.now(UTC)).astimezone(HKT).date().isoformat()


def format_post(headlines: list[str], day: str) -> str | None:
    text = "\n".join([day, *(f"• {headline}" for headline in headlines)])
    # Meta counts emoji by UTF-8 bytes. Count symbol code points conservatively.
    length = sum(len(char.encode()) if unicodedata.category(char).startswith("S") else 1 for char in text)
    return text if length <= 500 else None


def build_draft(
    selected: list[tuple[str, list[dict[str, Any]]]], day: str, *, made_with_ai: bool = False,
) -> Draft | None:
    if not selected:
        return None
    text = format_post([headline for headline, _ in selected], day)
    if text is None:
        return None
    lead = selected[0][1][0]
    source_urls = tuple(group[0]["url"] for _, group in selected)
    related_ids = {key for _, group in selected for item in group for key in story_keys(item)} - story_keys(lead)
    return Draft(
        lead["id"], title_id(lead["title"]), text,
        related_ids=tuple(sorted(related_ids)), made_with_ai=made_with_ai,
        source_url=source_urls[0], source_urls=source_urls,
    )


def select_draft(result: dict[str, Any], used: set[str], *, day: str | None = None) -> Draft | None:
    day = day or preview_day(result.get("collected_at"))
    candidates = sorted(result["candidates"], key=lambda item: item["section"] != "latest")
    selected = []
    blocked = set(used)
    for group in group_stories(candidates):
        keys = {key for item in group for key in story_keys(item)}
        if blocked & keys:
            continue
        headlines = [headline for headline, _ in selected]
        for item in group:
            title = " ".join(item["title"].split())
            if title and item["summary_or_snippet"].strip() and format_post([*headlines, title], day):
                selected.append((title, [item, *(other for other in group if other is not item)]))
                blocked.update(keys)
                break
        if len(selected) == MAX_HEADLINES:
            break
    return build_draft(selected, day)
