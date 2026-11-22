import io
import json
from unittest import TestCase
from unittest.mock import patch
from urllib.robotparser import RobotFileParser

from feedroom.collector import candidate, title_id
from feedroom.evidence import ArticleReader, story_keys
from feedroom.writer import EditorialError, complete, write_draft


def story(title, url, summary):
    return candidate("Fixture", title, url, "2026-11-19T00:00:00Z", summary, 8, section="latest")


ARTICLE = story("Open model inference benchmark release", "https://example.com/story", "Researchers report 72% accuracy.")
ALTERNATE = story("Open model inference benchmark release today", "https://example.org/alternate", "Independent coverage of the benchmark.")
OTHER = story("Database provider announces backup tool", "https://example.net/other", "The project reports 95% accuracy.")
COLLECTED_AT = "2026-11-19T17:00:00Z"
DRAFT = {
    "items": [{
        "event_id": "E1",
        "headline": "研究團隊發佈新模型，指準確率有 72%",
    }],
}
TEXT = "2026-11-20\n• 研究團隊發佈新模型，指準確率有 72%"
HEADLINES = ("甲項研究", "乙項研究", "丙項研究", "丁項研究", "戊項研究", "己項研究")


def collected(*articles):
    return {"collected_at": COLLECTED_AT, "collection_errors": [], "candidates": list(articles)}


def daily_stories(count):
    labels = ("alpha", "bravo", "charlie", "delta", "echo", "foxtrot")
    return [story(f"Research topic {labels[index]} report", f"https://example.com/{labels[index]}",
                  f"Source summary for {labels[index]}.") for index in range(count)]


def editorial(*headlines):
    return {"items": [{"event_id": f"E{index}", "headline": headline}
                       for index, headline in enumerate(headlines, 1)]}


class WriterTests(TestCase):
    def test_links_come_from_collector_and_duplicate_sources_are_grouped(self):
        with patch("feedroom.writer.complete", return_value=DRAFT) as model:
            result = write_draft(collected(ARTICLE, ALTERNATE, OTHER), set())

        payload, feedback = model.call_args.args
        self.assertIsNone(feedback)
        self.assertEqual([len(event["sources"]) for event in payload["events"]], [2, 1])
        self.assertNotIn("https://", json.dumps(payload))
        self.assertEqual(result.text, TEXT)
        self.assertEqual(result.source_url, ARTICLE["url"])
        self.assertEqual(result.source_urls, (ARTICLE["url"],))
        self.assertIn(ALTERNATE["id"], result.related_ids)
        self.assertIn(title_id(ALTERNATE["title"]), result.related_ids)
        self.assertTrue(result.made_with_ai)

    def test_retries_grounding_errors_without_using_numbers_from_other_events(self):
        valid = editorial(DRAFT["items"][0]["headline"], "資料庫工具")
        wrong = editorial("研究團隊指準確率有 95%", "資料庫工具")
        with patch("feedroom.writer.complete", side_effect=[wrong, valid]) as model:
            result = write_draft(collected(ARTICLE, OTHER), set())
        self.assertIn("72%", result.text)
        self.assertNotIn("95%", result.text)
        self.assertIn("selected event", model.call_args.args[1])

    def test_selects_five_or_six_ordered_headlines_and_tracks_each_source(self):
        for count in (5, 6):
            with self.subTest(count=count):
                stories = daily_stories(count)
                reply = editorial(*HEADLINES[:count])
                with patch("feedroom.writer.complete", return_value=reply):
                    result = write_draft(collected(*stories), set())

                expected_text = "2026-11-20\n" + "\n".join(f"• {headline}" for headline in HEADLINES[:count])
                self.assertEqual(result.text, expected_text)
                self.assertEqual(result.source_url, stories[0]["url"])
                self.assertEqual(result.source_urls, tuple(item["url"] for item in stories))
                expected_related = {
                    key
                    for item in stories[1:]
                    for key in story_keys(item)
                }
                self.assertEqual(set(result.related_ids), expected_related)

    def test_retries_duplicate_event_selection(self):
        stories = daily_stories(2)
        duplicate = {"items": [
            {"event_id": "E1", "headline": HEADLINES[0]},
            {"event_id": "E1", "headline": HEADLINES[1]},
        ]}
        valid = editorial(*HEADLINES[:2])
        with patch("feedroom.writer.complete", side_effect=[duplicate, valid]) as model:
            result = write_draft(collected(*stories), set())

        self.assertEqual(result.source_urls, tuple(item["url"] for item in stories))
        self.assertEqual(model.call_count, 2)
        self.assertTrue(model.call_args.args[1])

    def test_accepts_an_empty_selection_for_a_short_day(self):
        with patch("feedroom.writer.complete", return_value={"items": []}) as model:
            self.assertIsNone(write_draft(collected(ARTICLE), set()))
        model.assert_called_once()

    def test_refuses_unverified_link_and_does_not_resubmit_a_seen_cluster(self):
        with patch("feedroom.writer.complete", return_value={"items": [{
            "event_id": "E1", "headline": "睇 https://example.net",
        }]}) as model:
            with self.assertRaises(EditorialError):
                write_draft(collected(ARTICLE), set())
            self.assertEqual(model.call_count, 2)
            model.reset_mock()
            self.assertIsNone(write_draft(collected(ARTICLE, ALTERNATE), {ALTERNATE["id"]}))
            model.assert_not_called()

    def test_fetcher_honors_robots_and_skips_unconfigured_hosts(self):
        reader = ArticleReader()
        robots = RobotFileParser()
        robots.parse(["User-agent: *", "Disallow: /private"])
        reader.robots["openai.com"] = robots
        with patch.object(reader, "fetch") as fetch:
            self.assertEqual(reader.text("https://openai.com/private"), "")
            self.assertEqual(reader.text("https://example.com/article"), "")
            fetch.assert_not_called()

    def test_cli_editorial_preview_uses_writer_without_opening_the_publish_journal(self):
        from feedroom.cli import main

        with (patch("feedroom.cli.collect_from_args", return_value=collected(ARTICLE)),
              patch("feedroom.writer.complete", return_value=DRAFT), patch("feedroom.cli.open_journal") as journal,
              patch("builtins.print") as output):
            self.assertEqual(main(["preview", "--editorial"]), 0)
        journal.assert_not_called()
        draft = json.loads(output.call_args.args[0])["draft"]
        self.assertEqual(draft["text"], TEXT)
        self.assertEqual(draft["source_url"], ARTICLE["url"])
        self.assertEqual(draft["source_urls"], [ARTICLE["url"]])

    def test_openrouter_request_uses_packaged_prompt_and_strict_schema(self):
        response = json.dumps({"choices": [{"message": {"content": json.dumps(DRAFT)}}]}).encode()
        with (patch.dict("os.environ", {"OPENROUTER_API_KEY": "fixture", "OPENROUTER_MODEL": "fixture/model"}),
              patch("feedroom.writer.build_opener") as opener):
            opener.return_value.open.return_value = io.BytesIO(response)
            self.assertEqual(complete({"events": []}, None), DRAFT)
        request = opener.return_value.open.call_args.args[0]
        self.assertEqual(request.full_url, "https://openrouter.ai/api/v1/chat/completions")
        body = json.loads(request.data)
        self.assertNotIn("max_tokens", body)
        self.assertNotIn("max_completion_tokens", body)
        schema = body["response_format"]["json_schema"]
        self.assertTrue(schema["strict"])
        self.assertEqual(set(schema["schema"]["required"]), {"items"})
        items = schema["schema"]["properties"]["items"]
        self.assertEqual(items["maxItems"], 6)
        self.assertEqual(set(items["items"]["required"]), {"event_id", "headline"})
        self.assertIn("香港粵語", body["messages"][0]["content"])
