import io
import json
from unittest import TestCase
from unittest.mock import patch
from urllib.robotparser import RobotFileParser

from feedroom.collector import candidate
from feedroom.evidence import ArticleReader
from feedroom.writer import EditorialError, complete, write_draft


def story(title, url, summary):
    return candidate("Fixture", title, url, "2026-11-19T00:00:00Z", summary, 8, section="latest")


ARTICLE = story("Open model inference benchmark release", "https://example.com/story", "Researchers report 72% accuracy.")
ALTERNATE = story("Open model inference benchmark release today", "https://example.org/alternate", "Independent coverage of the benchmark.")
OTHER = story("Database provider announces backup tool", "https://example.net/other", "The project reports 95% accuracy.")
DRAFT = {
    "event_id": "E1",
    "headline": "研究團隊發佈新模型",
    "facts": "團隊指準確率有 72%，結果由作者自己量度",
    "so_what": "做推論嘅團隊可以用呢個測試方法比較自己嘅模型",
}


class WriterTests(TestCase):
    def test_links_come_from_collector_and_duplicate_sources_are_grouped(self):
        with patch("feedroom.writer.complete", return_value=DRAFT) as model:
            result = write_draft({"candidates": [ARTICLE, ALTERNATE, OTHER]}, set())

        payload, feedback = model.call_args.args
        self.assertIsNone(feedback)
        self.assertEqual([len(event["sources"]) for event in payload["events"]], [2, 1])
        self.assertNotIn("https://", json.dumps(payload))
        self.assertTrue(result.text.endswith(ARTICLE["url"]))
        self.assertIn(ALTERNATE["id"], result.related_ids)
        self.assertTrue(result.made_with_ai)

    def test_retries_grounding_errors_without_using_numbers_from_other_events(self):
        wrong = {**DRAFT, "facts": "團隊指準確率有 95%"}
        with patch("feedroom.writer.complete", side_effect=[wrong, DRAFT]) as model:
            result = write_draft({"candidates": [ARTICLE, OTHER]}, set())
        self.assertIn("72%", result.text)
        self.assertIn("selected event", model.call_args.args[1])

    def test_refuses_unverified_link_and_does_not_resubmit_a_seen_cluster(self):
        with patch("feedroom.writer.complete", return_value={**DRAFT, "facts": "睇 https://example.net"}) as model:
            with self.assertRaises(EditorialError):
                write_draft({"candidates": [ARTICLE]}, set())
            self.assertEqual(model.call_count, 2)
            model.reset_mock()
            self.assertIsNone(write_draft({"candidates": [ARTICLE, ALTERNATE]}, {ALTERNATE["id"]}))
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

        with (patch("feedroom.cli.collect_from_args", return_value={"collection_errors": [], "candidates": [ARTICLE]}),
              patch("feedroom.writer.complete", return_value=DRAFT), patch("feedroom.cli.open_journal") as journal,
              patch("builtins.print") as output):
            self.assertEqual(main(["preview", "--editorial"]), 0)
        journal.assert_not_called()
        self.assertIn("72%", output.call_args.args[0])

    def test_openrouter_request_uses_packaged_prompt_and_strict_schema(self):
        response = json.dumps({"choices": [{"message": {"content": json.dumps(DRAFT)}}]}).encode()
        with (patch.dict("os.environ", {"OPENROUTER_API_KEY": "fixture", "OPENROUTER_MODEL": "fixture/model"}),
              patch("feedroom.writer.build_opener") as opener):
            opener.return_value.open.return_value = io.BytesIO(response)
            self.assertEqual(complete({"events": []}, None), DRAFT)
        request = opener.return_value.open.call_args.args[0]
        self.assertEqual(request.full_url, "https://openrouter.ai/api/v1/chat/completions")
        body = json.loads(request.data)
        self.assertTrue(body["response_format"]["json_schema"]["strict"])
        self.assertIn("香港粵語", body["messages"][0]["content"])
