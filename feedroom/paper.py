"""Reuse the daily reading shortlist and write one paper post from its abstract."""

import json
import re
from datetime import datetime
from importlib.resources import files
from pathlib import Path
from tempfile import TemporaryDirectory

from .briefing import HKT, Draft, thread_length
from .collector import DAILY_PAPERS, DAIR_AI_PAPERS, TextExtractor, item_id, parse_date, title_id
from .evidence import ArticleReader, URL_TEXT, story_keys
from .writer import EditorialError, UNSAFE_TEXT, complete_json, validate_summary

ARXIV_PAPER = re.compile(r"https://arxiv\.org/abs/(\d{4}\.\d{4,5})(?:v\d+)?")
ABSTRACT = re.compile(r'''<blockquote\b[^>]*\bclass=["'][^"']*\babstract\b[^"']*["'][^>]*>(.*?)</blockquote>''', re.S)
PAPER_SOURCES = (DAILY_PAPERS, DAIR_AI_PAPERS.name)


def save_reading(path: Path, result: dict) -> None:
    snapshot = {
        "collected_at": result["collected_at"],
        "candidates": [item for item in result["candidates"] if item["section"] == "reading"],
        "collection_errors": [error for error in result["collection_errors"] if error["source"] in PAPER_SOURCES],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=path.parent) as directory:
        temporary = Path(directory) / path.name
        temporary.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
        temporary.replace(path)


def load_reading(path: Path, now: datetime) -> dict | None:
    try:
        snapshot = json.loads(path.read_text(encoding="utf-8"))
        collected_at = parse_date(snapshot["collected_at"])
        if (collected_at is not None and collected_at <= now
                and collected_at.astimezone(HKT).date() == now.astimezone(HKT).date()
                and isinstance(snapshot["candidates"], list) and isinstance(snapshot["collection_errors"], list)):
            return snapshot
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        pass
    return None


def select_paper(cached: dict | None, refresh, used: set[str], *, provided: bool = False) -> dict:
    reader = ArticleReader(limit=16)
    checked = set()
    errors = []

    def find(result: dict, source: str) -> dict:
        if not isinstance(result["candidates"], list) or not isinstance(result["collection_errors"], list):
            raise ValueError("Expected candidate and collection error lists in collector JSON.")
        errors.extend(result["collection_errors"])
        for item in result["candidates"]:
            if not isinstance(item, dict) or item.get("section") != "reading":
                continue
            publisher = item.get("source")
            upvotes = item.get("upvotes")
            if publisher not in PAPER_SOURCES or (publisher == DAILY_PAPERS and (not isinstance(upvotes, int) or upvotes < 100)):
                continue
            # shortcut: DAIR-only URLs need a verified publisher link before they can be used.
            url, title = item.get("url"), item.get("title")
            if not isinstance(url, str) or not isinstance(title, str) or not title.strip():
                continue
            match = ARXIV_PAPER.fullmatch(url)
            if not match or UNSAFE_TEXT.search(title) or item.get("id") != item_id(url, title):
                continue
            if thread_length(f"YYYY-MM-DD\n今日讀一篇\n{title}\n\n{url}") > 498:
                continue
            keys = story_keys(item)
            if keys & used or url in checked:
                continue
            checked.add(url)
            match = ABSTRACT.search(reader.page(url).decode("utf-8", errors="replace"))
            parser = TextExtractor()
            if match:
                parser.feed(match[1])
                parser.close()
            abstract = parser.text().removeprefix("Abstract:").strip()
            if abstract:
                return {"paper": (item, url, keys, URL_TEXT.sub("[link]", abstract[:5000])),
                        "candidate_source": source, "collection_errors": errors}
            errors.append({"source": url, "error": "No original paper abstract could be verified."})
        return {"paper": None, "candidate_source": source, "collection_errors": errors}

    if cached is not None:
        result = find(cached, "provided" if provided else "cached")
        if result["paper"] or provided:
            return result
    return find(refresh(used), "fresh")


def write_paper(result: dict, _used: set[str], *, day: str) -> Draft | None:
    if result["paper"] is None:
        return None
    item, url, keys, abstract = result["paper"]
    prompt = files("feedroom").joinpath("prompts/paper.v1.md").read_text(encoding="utf-8")
    schema = {"type": "object", "additionalProperties": False, "required": ["summary"],
              "properties": {"summary": {"type": "string"}}}
    budget = 500 - thread_length(f"{day}\n今日讀一篇\n{item['title']}\n\n{url}")
    payload = {"title": item["title"], "abstract": abstract, "summary_budget": min(budget, 160)}
    feedback = None
    for _ in range(2):
        reply = complete_json(prompt, payload, feedback, schema_name="daily_paper", schema=schema)
        try:
            if not isinstance(reply, dict) or set(reply) != {"summary"} or not isinstance(reply["summary"], str):
                raise EditorialError("Return a required summary in the requested JSON format.")
            summary = validate_summary(reply["summary"], {"abstract": abstract}, item["title"])
            text = "\n".join((day, "今日讀一篇", item["title"], summary, url))
            if thread_length(text) > 500:
                raise EditorialError("Shorten the summary so the entire post fits within 500 characters.")
        except EditorialError as error:
            feedback = str(error)
            continue
        lead_title_id = title_id(item["title"])
        return Draft(item["id"], lead_title_id, text, tuple(sorted(keys - {item["id"], lead_title_id})),
                     made_with_ai=True, source_url=url, source_urls=(url,))
    raise EditorialError(f"Paper draft failed validation. {feedback}")
