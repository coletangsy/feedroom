"""Build a source-grounded weekly Threads digest with paired story cards."""

from __future__ import annotations

from datetime import datetime
from importlib.resources import files

from .briefing import (
    Card,
    CardDraft,
    HKT,
    MAX_CARD_SUMMARY,
    SENTENCE_END,
    build_card,
    thread_length,
)
from .evidence import ArticleReader, source_evidence, story_keys
from .writer import (
    CANTONESE,
    MAX_EDITORIAL_HEADLINE,
    SIMPLIFIED,
    UNSAFE_TEXT,
    EditorialError,
    evidence_numbers,
    complete_json,
    numbers,
)


MAX_WEEKLY_STORIES = 3
MAX_POST_CHARS = 500


def _caption_heading(start: datetime, cutoff: datetime) -> str:
    cutoff_day = cutoff.astimezone(HKT).date().isoformat()
    period = f"{start.astimezone(HKT):%m/%d}–{cutoff.astimezone(HKT):%m/%d}（香港時間）"
    return f"{cutoff_day}\n每週 AI 摘要｜{period}"


def format_weekly_post(headlines: list[tuple[str, str]], *, cutoff: datetime, start: datetime) -> str | None:
    """Format the weekly caption and source links within Threads' text limit."""

    lines = [_caption_heading(start, cutoff)]
    for headline, url in headlines:
        lines.extend((f"• {headline}", url))
    text = "\n".join(lines)
    return text if thread_length(text) <= MAX_POST_CHARS else None


def _summary(value: object, supported_numbers: set[str], headline: str, *, label: str, required: bool = False) -> str | None:
    if value is None:
        if required:
            raise EditorialError(f"Each selected weekly story needs a non-empty {label}.")
        return None
    if not isinstance(value, str):
        raise EditorialError(f"{label} must be a Traditional Chinese string or null.")
    value = value.strip()
    if len(value) > MAX_CARD_SUMMARY:
        raise EditorialError(f"Shorten {label} from {len(value)} to at most {MAX_CARD_SUMMARY} characters.")
    if not CANTONESE.search(value) or SIMPLIFIED.intersection(value):
        raise EditorialError(f"Write {label} in Hong Kong Cantonese with Traditional Chinese characters.")
    if UNSAFE_TEXT.search(value) or "?" in value or "？" in value:
        raise EditorialError(f"Use a single line of plain text for {label}, with no question, link or markup.")
    endings = list(SENTENCE_END.finditer(value))
    if not 1 <= len(endings) <= 3 or endings[-1].end() != len(value):
        raise EditorialError(f"Use one to three complete {label} sentences, with a full stop at the end.")
    if not numbers(value) <= supported_numbers:
        raise EditorialError(f"Use only figures supported by the primary source for the {label}.")
    if value.rstrip("。！？.!?") == headline.rstrip("。！？.!?"):
        raise EditorialError(f"Use the {label} to add a concrete detail, or return null.")
    return value


def _weekly_evidence_numbers(source: dict) -> set[str]:
    # Selector metadata is context, not evidence for a card claim. Keep the
    # daily writer's historical evidence_numbers behaviour unchanged.
    claims = {key: source[key] for key in ("title", "snippet", "article_excerpt") if key in source}
    return evidence_numbers([claims])


def _complete_weekly(payload: dict, feedback: str | None) -> dict:
    prompt = files("feedroom").joinpath("prompts/weekly.v1.md").read_text(encoding="utf-8")
    item_schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["event_id", "headline", "summary", "detail"],
        "properties": {
            "event_id": {"type": "string"},
            "headline": {"type": "string"},
            "summary": {"type": "string"},
            "detail": {"type": ["string", "null"]},
        },
    }
    schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["items"],
        "properties": {
            "items": {
                "type": "array",
                "items": item_schema,
                "maxItems": MAX_WEEKLY_STORIES,
            }
        },
    }
    return complete_json(
        prompt,
        payload,
        feedback,
        schema_name="weekly_threads_post",
        schema=schema,
        missing_error="Set OPENROUTER_API_KEY and OPENROUTER_MODEL to generate a weekly post.",
        response_error="OpenRouter did not return a readable weekly draft.",
    )


def _prepare_groups(groups: list[tuple[list[dict], bool]], *, reader: ArticleReader) -> list[dict]:
    """Choose one link and its supporting evidence per event."""

    evidence_by_group: dict[int, tuple[dict, dict]] = {}
    # Reserve reads for supplements and try alternates only if the primary lacks evidence.
    read_order = sorted(range(len(groups)), key=lambda index: groups[index][1])
    for offset in range(3):
        for index in read_order:
            group, _ = groups[index]
            if index in evidence_by_group or offset >= len(group):
                continue
            item = group[offset]
            source = source_evidence(item, reader, excerpt_limit=5000)
            if source.get("snippet") or source.get("article_excerpt"):
                evidence_by_group[index] = item, source

    prepared = []
    for index, (group, daily_backed) in enumerate(groups):
        if index not in evidence_by_group:
            continue
        primary, source = evidence_by_group[index]
        prepared.append({
            "group": group,
            "daily_backed": bool(daily_backed),
            "primary": primary,
            "primary_source": {**source, "url": primary["url"]},
        })
    return prepared


def _validate(reply: dict, events: dict[str, dict], *, start: datetime, cutoff: datetime) -> CardDraft | None:
    if not isinstance(reply, dict) or set(reply) != {"items"} or not isinstance(reply["items"], list):
        raise EditorialError("Return a list of weekly items in the requested JSON format.")
    items = reply["items"]
    if len(items) > MAX_WEEKLY_STORIES:
        raise EditorialError(f"Select at most {MAX_WEEKLY_STORIES} weekly stories.")
    if not items:
        return None
    seen: set[str] = set()
    seen_keys: set[str] = set()
    selected = []
    for item in items:
        if not isinstance(item, dict) or set(item) != {"event_id", "headline", "summary", "detail"}:
            raise EditorialError("Return event_id, headline, summary and detail for each weekly story.")
        event_id = item["event_id"]
        if not isinstance(event_id, str) or event_id not in events:
            raise EditorialError("Select a weekly event ID from the supplied evidence.")
        if event_id in seen:
            raise EditorialError("Select each weekly event only once.")
        seen.add(event_id)
        event = events[event_id]
        event_keys = {key for article in event["group"] for key in story_keys(article)}
        if seen_keys & event_keys:
            raise EditorialError("Do not select the same story from multiple weekly events.")
        seen_keys.update(event_keys)
        headline = item["headline"].strip() if isinstance(item["headline"], str) else ""
        supported_numbers = _weekly_evidence_numbers(event["primary_source"])
        if (
            not headline
            or not CANTONESE.search(headline)
            or len(headline) > MAX_EDITORIAL_HEADLINE
            or UNSAFE_TEXT.search(headline)
            or SIMPLIFIED.intersection(headline)
            or not numbers(headline) <= supported_numbers
        ):
            raise EditorialError("Use a short, Traditional Chinese headline grounded in the selected event.")
        summary = _summary(item["summary"], supported_numbers, headline, label="summary", required=True)
        detail = _summary(item["detail"], supported_numbers, headline, label="detail")
        if detail is not None and detail == summary:
            raise EditorialError("Use a different concrete detail for the second weekly card, or return null.")
        selected.append((event, headline, summary, detail))

    if sum(not event["daily_backed"] for event, *_ in selected) > 1:
        raise EditorialError("Select at most one story outside the daily publication base.")
    included = []
    captions = []
    post = None
    for story in selected:
        event, headline, *_ = story
        entry = (headline, event["primary"]["url"])
        candidate_post = format_weekly_post([*captions, entry], cutoff=cutoff, start=start)
        if candidate_post is not None:
            included.append(story)
            captions.append(entry)
            post = candidate_post
    if post is None:
        raise EditorialError("Choose a shorter headline or a story with a shorter source link to fit the caption budget.")

    cards: list[Card] = []
    related: set[str] = set()
    source_urls: list[str] = []
    for event, headline, summary, detail in included:
        primary = event["primary"]
        cards.append(build_card(headline, primary, summary))
        if detail is not None:
            cards.append(build_card(headline, primary, detail))
        source_urls.append(primary["url"])
        related.update(key for item in event["group"] for key in story_keys(item))
    lead = included[0][0]["primary"]
    related.difference_update(story_keys(lead))
    return CardDraft(
        candidate_id=lead["id"],
        title_id=lead["title_id"],
        text=post,
        related_ids=tuple(sorted(related)),
        made_with_ai=True,
        source_url=source_urls[0],
        source_urls=tuple(source_urls),
        cards=tuple(cards),
    )


def write_weekly_draft(
    groups: list[tuple[list[dict], bool]], *, start: datetime, cutoff: datetime,
) -> CardDraft | None:
    """Select and write a weekly draft from daily-backed and supplement groups."""

    if start.tzinfo is None or cutoff.tzinfo is None or start > cutoff:
        raise ValueError("Weekly start and cutoff must be timezone-aware and ordered.")
    events = {f"W{index}": item for index, item in enumerate(
        _prepare_groups(groups, reader=ArticleReader(follow_article_slash=True)), 1,
    )}
    if not events:
        return None
    payload = {
        "period": {
            "start": start.astimezone(HKT).isoformat(),
            "cutoff": cutoff.astimezone(HKT).isoformat(),
        },
        "caption_budget": MAX_POST_CHARS - thread_length(_caption_heading(start, cutoff)),
        "events": [
            {
                "event_id": event_id,
                "daily_backed": event["daily_backed"],
                "source_link_chars": thread_length(event["primary"]["url"]),
                "primary_source": event["primary_source"],
            }
            for event_id, event in events.items()
        ],
    }
    feedback = None
    for _ in range(2):
        reply = _complete_weekly(payload, feedback)
        try:
            return _validate(reply, events, start=start, cutoff=cutoff)
        except EditorialError as error:
            feedback = str(error)
    raise EditorialError(f"Weekly draft failed validation. {feedback}")
