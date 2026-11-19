"""Select one sourced Threads draft from a collector result."""

from dataclasses import dataclass
from typing import Any

from .collector import is_article_url, item_id, title_id


@dataclass(frozen=True)
class Draft:
    candidate_id: str
    title_id: str
    text: str
    related_ids: tuple[str, ...] = ()
    made_with_ai: bool = False


def select_draft(result: dict[str, Any], used: set[str]) -> Draft | None:
    candidates = sorted(result["candidates"], key=lambda item: item["section"] != "latest")
    for item in candidates:
        section = item["section"]
        title = " ".join(item["title"].split())
        url = item["url"]
        if section not in {"latest", "reading"} or not title or not item["summary_or_snippet"].strip():
            continue
        if not is_article_url(url) or any(character.isspace() for character in url):
            continue
        candidate_id = item_id(url, title)
        title_key = title_id(title)
        if item["id"] != candidate_id or used.intersection((candidate_id, title_key)):
            continue
        label = "AI 與資料科學｜最新動態" if section == "latest" else "AI 與資料科學｜值得閱讀"
        text = f"{label}\n\n原文標題：{title}\n\n來源：{url}"
        # Meta counts emoji by their UTF-8 size. The byte cap is conservative for all text.
        if len(text.encode("utf-8")) <= 500:
            return Draft(candidate_id, title_key, text)
    return None
