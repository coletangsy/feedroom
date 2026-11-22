"""Write one grounded Cantonese Threads post through OpenRouter."""

import json
import os
import re
import unicodedata
from importlib.resources import files
from urllib.error import HTTPError
from urllib.request import Request, build_opener

from .briefing import (
    MAX_CARD_SUMMARY, MAX_HEADLINES, SENTENCE_END, Draft, SelectedStory, build_draft, preview_day,
)
from .collector import DAILY_PAPERS, DAIR_AI_PAPERS
from .evidence import ArticleReader, group_stories, source_evidence, story_keys
from .http import NoRedirect

NUMBER = re.compile(r"[+−-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?%?")
UNSAFE_TEXT = re.compile(r"https?\s*[:：]|www\.|[\x00-\x1f\x7f-\x9f\u2028\u2029\u202a-\u202e\u2066-\u2069<>\[\]`*@★•]", re.IGNORECASE)
SIMPLIFIED = set("风买开发这说数据实权写软稳进选览优项网络页领结论报术应转让识压测验学与现层显样众获类关读书质访")
CANTONESE = re.compile(r"[\u3400-\u9fff]")
MAX_EDITORIAL_HEADLINE = 40


class EditorialError(RuntimeError):
    pass


def complete(payload: dict, feedback: str | None, *, include_cards: bool = False) -> dict:
    token = os.environ.get("OPENROUTER_API_KEY", "").strip()
    model = os.environ.get("OPENROUTER_MODEL", "").strip()
    if not token or not model:
        raise EditorialError("Set OPENROUTER_API_KEY and OPENROUTER_MODEL to generate a post.")
    prompt = files("feedroom").joinpath("prompts/threads.v1.md").read_text(encoding="utf-8")
    item_schema = {
        "type": "object", "additionalProperties": False, "required": ["event_id", "headline"],
        "properties": {"event_id": {"type": "string"}, "headline": {"type": "string"}},
    }
    if include_cards:
        prompt += "\n\n" + files("feedroom").joinpath("prompts/cards.v1.md").read_text(encoding="utf-8")
        item_schema["required"].append("summary")
        item_schema["properties"]["summary"] = {"type": ["string", "null"]}
    schema = {
        "type": "object", "additionalProperties": False, "required": ["items"],
        "properties": {"items": {"type": "array", "items": item_schema, "maxItems": MAX_HEADLINES}},
    }
    if feedback:
        payload = {**payload, "previous_validation_error": feedback}
    body = {
        "model": model, "messages": [
            {"role": "system", "content": prompt},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ],
        "response_format": {"type": "json_schema", "json_schema": {"name": "threads_post", "strict": True, "schema": schema}},
        "provider": {"require_parameters": True, "data_collection": "deny"},
    }
    request = Request("https://openrouter.ai/api/v1/chat/completions", json.dumps(body).encode(),
                      {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}, method="POST")
    try:
        with build_opener(NoRedirect()).open(request, timeout=90) as response:
            result = json.load(response)
            return json.loads(result["choices"][0]["message"]["content"])
    except HTTPError as error:
        raise EditorialError(f"OpenRouter returned HTTP {error.code}.") from None
    except (OSError, KeyError, IndexError, TypeError, ValueError):
        raise EditorialError("OpenRouter did not return a readable draft.") from None


def numbers(text: str) -> set[str]:
    return set(NUMBER.findall(unicodedata.normalize("NFKC", text)))


def evidence_numbers(sources: list[dict]) -> set[str]:
    claims = [{key: value for key, value in source.items() if key != "coverage_end_date"} for source in sources]
    return numbers(json.dumps(claims, ensure_ascii=False))


def validate_summary(value: str | None, source: dict, headline: str) -> str | None:
    if value is None:
        return None
    summary = value.strip() if isinstance(value, str) else ""
    endings = list(SENTENCE_END.finditer(summary))
    if (len(summary) > MAX_CARD_SUMMARY or not 1 <= len(endings) <= 2
            or endings[-1].end() != len(summary) or not CANTONESE.search(summary)
            or UNSAFE_TEXT.search(summary) or SIMPLIFIED.intersection(summary)):
        raise EditorialError(f"Use one or two single-line Traditional Chinese summary sentences up to {MAX_CARD_SUMMARY} characters, or null.")
    if not numbers(summary) <= evidence_numbers([source]):
        raise EditorialError("Use only figures supported by the primary source for the card summary.")
    if summary.rstrip("。！？.!?") == headline.rstrip("。！？.!?"):
        raise EditorialError("Use the card summary to add a detail, or return null.")
    return summary


def validate(reply: dict, groups: dict, evidence: dict, day: str, *, include_cards: bool = False) -> Draft | None:
    if not isinstance(reply, dict) or set(reply) != {"items"} or not isinstance(reply["items"], list):
        raise EditorialError("Return a list of items in the requested JSON format.")
    items = reply["items"]
    if len(items) > MAX_HEADLINES:
        raise EditorialError(f"Select at most {MAX_HEADLINES} events.")
    if not items:
        return None

    selected = []
    selected_keys = set()
    fields = {"event_id", "headline", "summary"} if include_cards else {"event_id", "headline"}
    for item in items:
        if not isinstance(item, dict) or set(item) != fields or not isinstance(item["headline"], str):
            raise EditorialError("Return the requested fields for each item.")
        event = item["event_id"]
        if not isinstance(event, str) or event not in groups:
            raise EditorialError("Select an event ID from the supplied evidence.")
        group = groups[event]
        keys = {key for article in group for key in story_keys(article)}
        if selected_keys & keys:
            raise EditorialError("Select each event only once.")
        headline = item["headline"].strip()
        if not CANTONESE.search(headline) or UNSAFE_TEXT.search(headline) or SIMPLIFIED.intersection(headline):
            raise EditorialError("Use single-line Traditional Chinese headlines without links, bullets, markup or mentions.")
        if len(headline) > MAX_EDITORIAL_HEADLINE:
            raise EditorialError(f"Keep the opening main clause in each headline and use at most {MAX_EDITORIAL_HEADLINE} characters.")
        if not numbers(headline) <= evidence_numbers(evidence[event]):
            raise EditorialError("Copy figures exactly from the selected event or omit them.")
        summary = validate_summary(item["summary"], evidence[event][0], headline) if include_cards else None
        selected.append(SelectedStory(headline, group, summary))
        selected_keys.update(keys)

    draft = build_draft(selected, day, made_with_ai=True, include_cards=include_cards)
    if draft is None:
        raise EditorialError("Shorten the headlines to fit together within the Threads limit of 500 characters.")
    return draft


def write_draft(
    result: dict, used: set[str], *, day: str | None = None, include_cards: bool = False,
) -> Draft | None:
    groups = {}
    reader = ArticleReader()
    evidence = {}
    for group in group_stories(result["candidates"]):
        if any(used & story_keys(item) for item in group):
            continue
        group = [
            item for item in group
            if item["section"] == "latest" or item["source"] == DAIR_AI_PAPERS.name
            or (item["source"] == DAILY_PAPERS and item.get("upvotes", 0) >= 100)
        ]
        if not group:
            continue
        extracts = [source_evidence(item, reader) for item in group[:3]]
        usable = [(item, source) for item, source in zip(group, extracts) if source["snippet"] or source["article_excerpt"]]
        if not usable:
            continue
        event = f"E{len(groups) + 1}"
        primary = usable[0][0]
        groups[event] = [primary, *(item for item in group if item is not primary)]
        evidence[event] = [source for _, source in usable]
        if len(groups) >= 12:
            break
    if not groups:
        return None

    day = day or preview_day(result.get("collected_at"))
    payload = {"collected_at": result.get("collected_at"), "events": [
        {"event_id": event, "lane": groups[event][0]["section"], "sources": sources}
        for event, sources in evidence.items()
    ]}
    feedback = None
    for _ in range(2):
        reply = complete(payload, feedback, include_cards=include_cards)
        try:
            return validate(reply, groups, evidence, day, include_cards=include_cards)
        except EditorialError as error:
            feedback = str(error)
    raise EditorialError(f"Draft failed validation. {feedback}")
