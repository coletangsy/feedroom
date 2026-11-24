import io
import json
from datetime import UTC, datetime
from unittest import TestCase
from unittest.mock import patch

from feedroom.collector import candidate
from feedroom.evidence import ArticleReader
from feedroom.weekly_writer import _complete_weekly, write_weekly_draft
from feedroom.writer import EditorialError


START = datetime(2026, 11, 16, tzinfo=UTC)
CUTOFF = datetime(2026, 11, 22, 10, tzinfo=UTC)


def story(label, *, day="2026-11-19", score=8, source="Fixture"):
    return candidate(
        source,
        f"{label} release report",
        f"https://example.com/{label.lower().replace(' ', '-')}",
        day,
        f"{label} introduced a new method with 72% accuracy.",
        score,
        section="latest",
    )


def evidence(item, *, article=""):
    return {
        "publisher": item["source"],
        "title": item["title"],
        "source_date": item["published_at"],
        "snippet": item["summary_or_snippet"],
        "article_excerpt": article,
    }


class WeeklyWriterTests(TestCase):
    def test_builds_adjacent_same_headline_cards_and_weekly_caption(self):
        first = story("Alpha")
        second = story("Bravo", day="2026-11-20")
        reply = {
            "items": [
                {
                    "event_id": "W1",
                    "headline": "Alpha 推出新方法",
                    "summary": "團隊介紹新方法，並報告準確率有 72%。",
                    "detail": "測試摘要交代方法表現，讀者可以按原文條件理解結果。",
                },
                {
                    "event_id": "W2",
                    "headline": "Bravo 更新模型",
                    "summary": "團隊公布模型更新，並補充實驗條件。",
                    "detail": None,
                },
            ]
        }
        with (
            patch("feedroom.weekly_writer.source_evidence", side_effect=[evidence(first), evidence(second)]),
            patch("feedroom.weekly_writer._complete_weekly", return_value=reply),
        ):
            draft = write_weekly_draft(
                [([first], True), ([second], True)], start=START, cutoff=CUTOFF,
            )

        self.assertIsNotNone(draft)
        self.assertEqual(
            draft.text,
            "2026-11-22\n每週 AI 摘要｜11/16–11/22（香港時間）\n"
            "• Alpha 推出新方法\nhttps://example.com/alpha\n"
            "• Bravo 更新模型\nhttps://example.com/bravo",
        )
        self.assertEqual([card.headline for card in draft.cards], [
            "Alpha 推出新方法", "Alpha 推出新方法", "Bravo 更新模型",
        ])
        self.assertEqual(draft.cards[1].summary, reply["items"][0]["detail"])
        self.assertEqual(draft.source_urls, (first["url"], second["url"]))
        self.assertIn(second["id"], draft.related_ids)
        self.assertIn(second["title_id"], draft.related_ids)

    def test_selects_at_most_three_stories_and_one_supplement(self):
        stories = [story("Alpha", score=12), story("Bravo", score=11), story("Charlie", score=10)]
        supplement = story("Supplement", score=20, source="Supplement")
        reply = {"items": [
            {"event_id": "W1", "headline": "Alpha 發布新方法", "summary": "團隊交代新方法。", "detail": None},
            {"event_id": "W2", "headline": "Bravo 更新系統", "summary": "團隊交代系統更新。", "detail": None},
            {"event_id": "W4", "headline": "Supplement 補充研究", "summary": "來源補充研究細節。", "detail": None},
        ]}
        with (
            patch("feedroom.weekly_writer.source_evidence", side_effect=lambda item, *_args, **_kw: evidence(item)),
            patch("feedroom.weekly_writer._complete_weekly", return_value=reply) as model,
        ):
            draft = write_weekly_draft(
                [([stories[0]], True), ([stories[1]], True), ([stories[2]], True), ([supplement], False)],
                start=START,
                cutoff=CUTOFF,
            )

        payload = model.call_args.args[0]
        self.assertEqual(len(payload["events"]), 4)
        self.assertEqual([event["event_id"] for event in payload["events"]], ["W1", "W2", "W3", "W4"])
        self.assertEqual(len(draft.cards), 3)

    def test_returns_none_when_a_shortlisted_story_has_no_evidence(self):
        old = story("Old", day="2026-11-01")
        with patch("feedroom.weekly_writer.source_evidence", return_value={
            **evidence(old), "snippet": "", "article_excerpt": "",
        }), patch("feedroom.weekly_writer._complete_weekly") as model:
            self.assertIsNone(write_weekly_draft([([old], True)], start=START, cutoff=CUTOFF))
        model.assert_not_called()

    def test_retries_invalid_weekly_summary_and_rejects_unsupported_numbers(self):
        item = story("Alpha")
        invalid = {"items": [{
            "event_id": "W1", "headline": "Alpha 推出新方法",
            "summary": "結果有 95%。", "detail": None,
        }]}
        valid = {"items": [{
            "event_id": "W1", "headline": "Alpha 推出新方法",
            "summary": "結果有 72%。", "detail": None,
        }]}
        with (
            patch("feedroom.weekly_writer.source_evidence", return_value=evidence(item)),
            patch("feedroom.weekly_writer._complete_weekly", side_effect=[invalid, valid]) as model,
        ):
            draft = write_weekly_draft([([item], True)], start=START, cutoff=CUTOFF)

        self.assertEqual(model.call_count, 2)
        self.assertEqual(draft.cards[0].summary, valid["items"][0]["summary"])
        self.assertIn("supported", model.call_args.args[1])

    def test_card_numbers_must_be_supported_by_the_linked_primary_source(self):
        primary = story("Alpha")
        alternate = story("Alpha alternate", source="Alternate")
        invalid = {"items": [{
            "event_id": "W1", "headline": "Alpha 推出新方法",
            "summary": "結果有 95%。", "detail": None,
        }]}
        valid = {"items": [{
            "event_id": "W1", "headline": "Alpha 推出新方法",
            "summary": "團隊交代新方法。", "detail": None,
        }]}
        with (
            patch("feedroom.weekly_writer.source_evidence", side_effect=lambda item, *_args, **_kw:
                  evidence(primary) if item is primary else
                  {**evidence(alternate), "snippet": "The method only works offline and achieves 95%."}),
            patch("feedroom.weekly_writer._complete_weekly", side_effect=[invalid, valid]) as model,
        ):
            draft = write_weekly_draft(
                [([primary, alternate], True)], start=START, cutoff=CUTOFF,
            )

        self.assertEqual(model.call_count, 2)
        event = model.call_args.args[0]["events"][0]
        self.assertEqual(event["primary_source"]["url"], primary["url"])
        self.assertNotIn("only works offline", json.dumps(event))
        self.assertEqual(draft.cards[0].summary, valid["items"][0]["summary"])
        self.assertIn("primary source", model.call_args.args[1])

    def test_publication_metadata_cannot_ground_a_quantity(self):
        item = story("Alpha", day="2026-11-19T05:22:00Z")
        bad = {"event_id": "W1", "headline": "Alpha 推出新方法",
               "summary": "團隊將部署時間減少 22 日。", "detail": None}
        good = {**bad, "summary": "團隊交代新方法。"}
        with (patch("feedroom.weekly_writer.source_evidence", return_value=evidence(item)),
              patch("feedroom.weekly_writer._complete_weekly", side_effect=[{"items": [bad]}, {"items": [good]}]) as model):
            draft = write_weekly_draft([([item], True)], start=START, cutoff=CUTOFF)
        self.assertEqual(draft.cards[0].summary, good["summary"])
        self.assertIn("primary source", model.call_args.args[1])

    def test_long_source_urls_keep_highest_priority_stories_that_fit(self):
        stories = [candidate("Example", f"Topic {index}", f"https://example.com/{index}/" + "x" * 135,
                             "2026-11-19", "The report explains a new method.", 8, section="latest") for index in range(3)]
        reply = {"items": [{"event_id": f"W{index}", "headline": f"{headline}嘅新研究",
                            "summary": "研究員公布評測方法。", "detail": "團隊公開測試條件。"}
                           for index, headline in enumerate(("甲", "乙", "丙"), 1)]}
        with (patch.object(ArticleReader, "text", return_value=""),
              patch("feedroom.weekly_writer._complete_weekly", return_value=reply) as model):
            draft = write_weekly_draft([([item], index < 2) for index, item in enumerate(stories)], start=START, cutoff=CUTOFF)

        self.assertEqual(draft.source_urls, tuple(item["url"] for item in stories[:2]))
        self.assertEqual(len(draft.cards), 4)
        self.assertNotIn(stories[2]["id"], draft.related_ids)
        self.assertGreater(model.call_args.args[0]["events"][0]["source_link_chars"], 150)
        model.assert_called_once()

    def test_supplement_primary_can_be_read_after_eight_daily_candidates_are_shortlisted(self):
        def article(index):
            return candidate("OpenAI", f"Topic {index}", f"https://openai.com/index/topic-{index}",
                             "2026-11-19", "", 8, section="latest")

        with (patch.object(ArticleReader, "fetch", return_value=b"User-agent: *\nDisallow:"),
              patch.object(ArticleReader, "_article_page", return_value=b"Article body") as fetch_article,
              patch("feedroom.evidence.extract", return_value="Source explains the method. " * 30),
              patch("feedroom.weekly_writer._complete_weekly", return_value={"items": []}) as model):
            write_weekly_draft([([article(index)], index < 8) for index in range(9)], start=START, cutoff=CUTOFF)

        self.assertEqual(fetch_article.call_count, 8)
        self.assertTrue(any(not event["daily_backed"] for event in model.call_args.args[0]["events"]))

    def test_rejects_question_lines_and_extra_stories(self):
        items = [
            {"event_id": "W1", "headline": "Alpha 推出新方法", "summary": "點解值得留意？", "detail": None},
            {"event_id": "W2", "headline": "Bravo 更新系統", "summary": "團隊交代更新。", "detail": None},
            {"event_id": "W3", "headline": "Charlie 發布研究", "summary": "團隊交代研究。", "detail": None},
            {"event_id": "W4", "headline": "Delta 發布研究", "summary": "團隊交代研究。", "detail": None},
        ]
        item = story("Alpha")
        with (
            patch("feedroom.weekly_writer.source_evidence", return_value=evidence(item)),
            patch("feedroom.weekly_writer._complete_weekly", return_value={"items": items}) as model,
        ):
            with self.assertRaises(EditorialError):
                write_weekly_draft([([item], True)], start=START, cutoff=CUTOFF)
        self.assertEqual(model.call_count, 2)

    def test_weekly_transport_uses_packaged_prompt_and_strict_schema(self):
        reply = {"items": []}
        response = json.dumps({"choices": [{"message": {"content": json.dumps(reply)}}]}).encode()
        with (patch.dict("os.environ", {"OPENROUTER_API_KEY": "fixture", "OPENROUTER_MODEL": "fixture/model"}),
              patch("feedroom.writer.build_opener") as opener):
            opener.return_value.open.return_value = io.BytesIO(response)
            self.assertEqual(_complete_weekly({"events": []}, None), reply)
        request = opener.return_value.open.call_args.args[0]
        body = json.loads(request.data)
        self.assertEqual(body["response_format"]["json_schema"]["name"], "weekly_threads_post")
        self.assertTrue(body["response_format"]["json_schema"]["strict"])
        item_schema = body["response_format"]["json_schema"]["schema"]["properties"]["items"]["items"]
        self.assertEqual(item_schema["properties"]["summary"]["type"], "string")
        self.assertEqual(item_schema["properties"]["detail"]["type"], ["string", "null"])
        self.assertEqual(item_schema["required"], ["event_id", "headline", "summary", "detail"])
        self.assertIn("每週", body["messages"][0]["content"])
        self.assertNotIn("max_tokens", body)
