"""Write one grounded Cantonese Threads post through OpenRouter."""

import json
import os
import re
import unicodedata
from importlib.resources import files
from urllib.error import HTTPError
from urllib.request import Request, build_opener

from .briefing import MAX_HEADLINES, Draft, build_draft, preview_day
from .collector import DAILY_PAPERS
from .evidence import ArticleReader, group_stories, source_evidence, story_keys
from .http import NoRedirect

NUMBER = re.compile(r"[+−-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?%?")
UNSAFE_TEXT = re.compile(r"https?\s*[:：]|www\.|[\x00-\x1f\x7f-\x9f\u2028\u2029\u202a-\u202e\u2066-\u2069<>\[\]`*@★•]", re.IGNORECASE)
SIMPLIFIED = set("风买开发这说数据实权写软稳进选览优项网络页领结论报术应转让识压测验学与现层显样众获类关读书质访")
CANTONESE = re.compile(r"[\u3400-\u9fff]")


class EditorialError(RuntimeError):
    pass


def complete(payload: dict, feedback: str | None) -> dict:
    token = os.environ.get("OPENROUTER_API_KEY", "").strip()
    model = os.environ.get("OPENROUTER_MODEL", "").strip()
    if not token or not model:
        raise EditorialError("Set OPENROUTER_API_KEY and OPENROUTER_MODEL to generate a post.")
    prompt = files("feedroom").joinpath("prompts/threads.v1.md").read_text(encoding="utf-8")
    item_schema = {
        "type": "object", "additionalProperties": False, "required": ["event_id", "headline"],
        "properties": {"event_id": {"type": "string"}, "headline": {"type": "string"}},
    }
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
        "max_tokens": 5000,
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


def validate(reply: dict, groups: dict, evidence: dict, day: str) -> Draft | None:
    if not isinstance(reply, dict) or set(reply) != {"items"} or not isinstance(reply["items"], list):
        raise EditorialError("Return a list of items in the requested JSON format.")
    items = reply["items"]
    if len(items) > MAX_HEADLINES:
        raise EditorialError(f"Select at most {MAX_HEADLINES} events.")
    if not items:
        return None

    selected = []
    selected_keys = set()
    numbers = lambda text: set(NUMBER.findall(unicodedata.normalize("NFKC", text)))
    for item in items:
        if not isinstance(item, dict) or set(item) != {"event_id", "headline"} or not isinstance(item["headline"], str):
            raise EditorialError("Return event_id and headline for each item.")
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
        source_text = json.dumps(evidence[event], ensure_ascii=False)
        if not numbers(headline) <= numbers(source_text):
            raise EditorialError("Copy figures exactly from the selected event or omit them.")
        selected.append((headline, group))
        selected_keys.update(keys)

    draft = build_draft(selected, day, made_with_ai=True)
    if draft is None:
        raise EditorialError("Shorten the headlines to fit together within the Threads limit of 500 characters.")
    return draft


def write_draft(result: dict, used: set[str], *, day: str | None = None) -> Draft | None:
    groups = {}
    reader = ArticleReader()
    evidence = {}
    for group in group_stories(result["candidates"]):
        if any(used & story_keys(item) for item in group):
            continue
        group = [item for item in group if item["section"] == "latest" or (item["source"] == DAILY_PAPERS and item.get("upvotes", 0) >= 100)]
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
        reply = complete(payload, feedback)
        try:
            return validate(reply, groups, evidence, day)
        except EditorialError as error:
            feedback = str(error)
    raise EditorialError(f"Draft failed validation. {feedback}")
