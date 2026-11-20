from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime, timedelta
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier
import unittest
from unittest.mock import Mock, call, patch

from feedroom.briefing import Draft, select_draft
from feedroom.cli import main
from feedroom.collector import DAILY_PAPERS, candidate
from feedroom.journal import LocalJournal
from feedroom.publishing import deliver
from feedroom.threads import Threads, ThreadsError

NOW = datetime(2026, 11, 19, 17, tzinfo=UTC)
DAY = "2026-11-20"


def story(title="AI model research", url="https://example.com/story", summary="Source summary", section="latest"):
    return candidate("Source", title, url, NOW.isoformat(), summary, 8, section=section)


def result(*stories, errors=()):
    return {"candidates": list(stories), "collection_errors": list(errors)}


class PublishingTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(TemporaryDirectory())
        self.path = Path(directory) / "threads.sqlite3"
        self.journal = LocalJournal(self.path, "11")
        self.threads = Mock(spec=Threads)
        self.threads.account_id = "11"
        self.threads.create.return_value = "22"
        self.threads.publish.return_value = "33"
        self.threads.status.return_value = "FINISHED"
        self.sleep = self.enterContext(patch("feedroom.publishing.sleep"))

    def test_repeated_run_reuses_post_and_history_has_no_14_day_expiry(self):
        original = story()
        collect = Mock(return_value={**result(original), "collected_at": "2026-11-19T10:00:00Z"})
        post = deliver(self.journal, self.threads, collect, NOW, write=select_draft)
        self.assertEqual(post, {"status": "published", "day": DAY, "post_id": "33", "collection_errors": []})
        self.threads.create.assert_called_once_with(f"{DAY}\n• {original['title']}", topic_tag="AI Threads")

        reopened = LocalJournal(self.path, "11")
        self.assertEqual(deliver(reopened, self.threads, collect, NOW, write=select_draft)["status"], "already_published")
        candidates = result(story(), story(url="https://mirror.example/story"))
        self.assertEqual(deliver(reopened, self.threads, lambda _used: candidates, NOW + timedelta(days=20), write=select_draft)["status"], "no_story")
        collect.assert_called_once()
        self.threads.publish.assert_called_once_with("22")

    def test_one_daily_post_contains_six_distinct_headlines_and_tracks_each_story(self):
        lead = story("Open model inference benchmark", "https://example.com/lead")
        alternate = story("Open model inference benchmark released", "https://mirror.example/lead")
        other_stories = [story(f"研究消息 {index}", f"https://example.com/story-{index}") for index in range(1, 7)]
        collect = Mock(return_value=result(lead, alternate, *other_stories))

        published = deliver(self.journal, self.threads, collect, NOW, write=select_draft)

        selected = [lead, *other_stories[:5]]
        expected_text = "\n".join([DAY, *(f"• {item['title']}" for item in selected)])
        self.assertEqual(published["status"], "published")
        self.assertEqual(self.journal.read()[0][DAY]["source_urls"], [item["url"] for item in selected])
        self.threads.create.assert_called_once_with(expected_text, topic_tag="AI Threads")
        self.threads.publish.assert_called_once_with("22")

        next_day = Mock(return_value=result(alternate, *other_stories[:5]))
        skipped = deliver(self.journal, self.threads, next_day, NOW + timedelta(days=1), write=select_draft)
        self.assertEqual(skipped["status"], "no_story")
        self.threads.publish.assert_called_once_with("22")

    def test_waits_for_processing_container_before_publishing(self):
        statuses = iter(("IN_PROGRESS", "FINISHED"))

        def status(container_id):
            self.assertEqual(container_id, "22")
            self.assertEqual(self.journal.read()[0][DAY]["status"], "ready")
            self.threads.publish.assert_not_called()
            return next(statuses)

        self.threads.status.side_effect = status
        post = deliver(self.journal, self.threads, Mock(return_value=result(story())), NOW, write=select_draft)

        self.assertEqual(post["status"], "published")
        self.assertEqual(self.sleep.call_args_list, [call(30), call(60)])
        self.threads.publish.assert_called_once_with("22")

    def test_retry_checks_a_container_that_was_still_processing(self):
        collect = Mock(return_value=result(story()))
        self.threads.status.return_value = "IN_PROGRESS"

        with self.assertRaisesRegex(ThreadsError, "not ready"):
            deliver(self.journal, self.threads, collect, NOW, write=select_draft)

        self.assertEqual(self.journal.read()[0][DAY]["status"], "ready")
        self.assertEqual(self.threads.status.call_count, 5)
        self.threads.publish.assert_not_called()

        self.threads.status.return_value = "FINISHED"
        post = deliver(self.journal, self.threads, collect, NOW, write=select_draft)
        self.assertEqual(post["status"], "published")
        self.threads.create.assert_called_once()
        self.threads.publish.assert_called_once_with("22")
        collect.assert_called_once()

    def test_publish_backfills_the_reading_shortlist_from_account_history(self):
        papers = [
            candidate(DAILY_PAPERS, f"AI research paper {index}", f"https://arxiv.org/abs/2611.{10000 + index}",
                      NOW.isoformat(), "A source summary.", 8, section="reading", upvotes=200 - index)
            for index in range(9)
        ]
        posts = {
            (NOW.date() - timedelta(days=index)).isoformat(): {
                "status": "published", "candidate_id": paper["id"], "title_id": paper["title_id"],
            }
            for index, paper in enumerate(papers[:-1])
        }
        self.assertTrue(self.journal.write(posts, None))
        output = io.StringIO()
        with (
            patch.dict("os.environ", {"THREADS_ACCESS_TOKEN": "synthetic", "THREADS_ACCOUNT_ID": "11"}, clear=True),
            patch("feedroom.cli.datetime") as clock,
            patch("feedroom.cli.Threads", return_value=self.threads),
            patch("feedroom.cli.open_journal", return_value=self.journal),
            patch("feedroom.cli.write_draft", side_effect=select_draft),
            patch.multiple("feedroom.collector", FEEDS=(), GITHUB_RELEASES=()),
            patch("feedroom.collector.tldr_entries", return_value=[]),
            patch("feedroom.collector.hn_entries", return_value=[]),
            patch("feedroom.collector.paper_candidates", return_value=papers),
            redirect_stdout(output),
        ):
            clock.now.return_value = NOW
            self.assertEqual(main(["publish", "--state", str(self.path.with_name("seen.json"))]), 0)

        self.assertEqual(json.loads(output.getvalue())["status"], "published")
        self.assertEqual(self.journal.read()[0][DAY]["candidate_id"], papers[-1]["id"])

    def test_ambiguous_publish_reads_existing_container_and_never_resends(self):
        original = story()
        collect = Mock(return_value=result(original))
        self.threads.publish.side_effect = ThreadsError("Disconnected after POST")
        with self.assertRaisesRegex(ThreadsError, "Disconnected"):
            deliver(self.journal, self.threads, collect, NOW, write=select_draft)

        self.threads.status.return_value = "FINISHED"
        with self.assertRaisesRegex(ThreadsError, "unresolved"):
            deliver(self.journal, self.threads, collect, NOW + timedelta(days=1), write=select_draft)
        self.threads.status.return_value = "PUBLISHED"
        self.threads.find_post.return_value = "33"
        reconciled = deliver(self.journal, self.threads, collect, NOW + timedelta(days=1), write=select_draft)

        self.assertEqual(reconciled, {"status": "reconciled", "day": DAY, "post_id": "33"})
        self.threads.status.assert_called_with("22")
        self.threads.find_post.assert_called_once_with(
            f"{DAY}\n• {original['title']}", str(int(NOW.timestamp())), link_attachment=None,
        )
        self.threads.publish.assert_called_once_with("22")
        (posts, _) = self.journal.read()
        self.assertEqual(posts[DAY]["post_id"], "33")
        collect.assert_called_once()

    def test_reconciles_a_legacy_journal_entry_with_an_inline_source(self):
        text = "模型研究。\n\n原文：https://example.com/story"
        prepared_at = str(int(NOW.timestamp()))
        self.assertTrue(self.journal.write({DAY: {
            "status": "publishing", "container_id": "22", "text": text, "prepared_at": prepared_at,
        }}, None))
        self.threads.status.return_value = "PUBLISHED"
        self.threads.find_post.return_value = "33"

        reconciled = deliver(self.journal, self.threads, Mock(), NOW, write=select_draft)

        self.assertEqual(reconciled["status"], "reconciled")
        self.threads.find_post.assert_called_once_with(text, prepared_at, link_attachment=None)
        self.threads.publish.assert_not_called()

    def test_reconciles_a_legacy_journal_entry_with_a_link_attachment(self):
        text = f"{DAY}\n• 模型研究"
        url = "https://example.com/story"
        prepared_at = str(int(NOW.timestamp()))
        self.assertTrue(self.journal.write({DAY: {
            "status": "publishing", "container_id": "22", "text": text, "prepared_at": prepared_at,
            "source_url": url,
        }}, None))
        self.threads.status.return_value = "PUBLISHED"
        self.threads.find_post.return_value = "33"

        reconciled = deliver(self.journal, self.threads, Mock(), NOW, write=select_draft)

        self.assertEqual(reconciled["status"], "reconciled")
        self.threads.find_post.assert_called_once_with(text, prepared_at, link_attachment=url)
        self.threads.publish.assert_not_called()

    def test_generated_story_keeps_its_sources_and_publishes_with_the_topic(self):
        original = story()
        alternate = story(url="https://mirror.example/story")
        text = f"{DAY}\n• 模型研究"
        created = Draft(original["id"], original["title_id"], text, (alternate["id"],), made_with_ai=True,
                        source_url=original["url"], source_urls=(original["url"],))
        deliver(self.journal, self.threads, lambda _used: result(original), NOW, write=Mock(return_value=created))
        saved = self.journal.read()[0][DAY]
        self.assertEqual(saved["source_urls"], [original["url"]])
        self.assertTrue(saved["made_with_ai"])
        seen = Mock(return_value=None)
        collect = Mock(return_value=result(alternate))
        deliver(self.journal, self.threads, collect, NOW + timedelta(days=1), write=seen)
        self.assertIn(alternate["id"], collect.call_args.args[0])
        self.assertIn(alternate["id"], seen.call_args.args[1])
        self.threads.create.assert_called_once_with(text, topic_tag="AI Threads")

    def test_concurrent_runs_can_only_publish_from_one_saved_container(self):
        ready = Barrier(2, timeout=3)

        def create(text, *, topic_tag):
            return str(20 + ready.wait())

        self.threads.create.side_effect = create

        def run():
            try:
                return deliver(LocalJournal(self.path, "11"), self.threads, lambda _used: result(story()), NOW, write=select_draft)["status"]
            except ThreadsError as error:
                self.assertIn("unresolved", str(error))
                return "unresolved"

        with ThreadPoolExecutor(max_workers=2) as pool:
            first, second = [pool.submit(run) for _ in range(2)]
            self.assertIn("published", [first.result(), second.result()])

        posts, _ = self.journal.read()
        self.assertEqual(posts[DAY]["status"], "published")
        self.threads.publish.assert_called_once_with(posts[DAY]["container_id"])

    def test_draft_retains_source_and_skips_thin_or_oversize_stories(self):
        reading = story("閱讀一篇研究", "https://example.com/paper", section="reading")
        items = {**result(reading, story(summary="  "), story("🦀" * 200)), "collected_at": NOW.isoformat()}
        draft = select_draft(items, set())
        self.assertEqual(draft.text, f"{DAY}\n• 閱讀一篇研究")
        self.assertEqual(draft.source_url, reading["url"])

    def test_preview_skips_a_title_that_would_exceed_the_post_limit(self):
        stories = [
            story(f"研究題目{label}" + "文" * 95, f"https://example.com/{index}")
            for index, label in enumerate("甲乙丙丁戊")
        ]
        short = story("短篇研究", "https://example.com/short")
        items = {**result(*stories, short), "collected_at": NOW.isoformat()}

        draft = select_draft(items, set())

        self.assertEqual(draft.source_urls, tuple(item["url"] for item in [*stories[:4], short]))
        self.assertEqual(len(draft.text.splitlines()), 6)
        self.assertLessEqual(len(draft.text), 500)

    def test_preview_uses_no_credentials_or_journal_and_reports_collection_errors(self):
        output = io.StringIO()
        with patch.dict("os.environ", {}, clear=True), patch("feedroom.cli.open_journal") as journal:
            with patch("feedroom.cli.collect_from_args", return_value=result(errors=[{"source": "Feed", "error": "timeout"}])), redirect_stdout(output):
                self.assertEqual(main(["preview"]), 1)
            journal.assert_not_called()
        self.assertEqual(json.loads(output.getvalue())["status"], "collection_incomplete")

    def test_account_mismatch_stops_before_opening_publish_state(self):
        with patch.dict("os.environ", {"THREADS_ACCESS_TOKEN": "synthetic", "THREADS_ACCOUNT_ID": "11"}, clear=True):
            with patch.object(Threads, "request", return_value={"id": "12"}) as request, patch("feedroom.cli.open_journal") as journal, redirect_stderr(io.StringIO()):
                self.assertEqual(main(["publish"]), 1)
        request.assert_called_once_with("me", {"fields": "id"})
        journal.assert_not_called()

    def test_credentials_stay_out_of_request_url_and_errors(self):
        threads = Threads("synthetic", "11")
        threads.opener = Mock()
        threads.opener.open.return_value = io.BytesIO(b'{"id":"22"}')
        self.assertEqual(threads.create("新聞", topic_tag="AI Threads"), "22")
        request = threads.opener.open.call_args.args[0]
        self.assertEqual(request.full_url, "https://graph.threads.com/v1.0/11/threads")
        self.assertEqual(request.get_header("Authorization"), "Bearer synthetic")
        self.assertIn(b"text=", request.data)
        self.assertNotIn(b"auto_publish_text", request.data)
        threads.opener.open.side_effect = OSError("synthetic")
        with self.assertRaises(ThreadsError) as caught:
            threads.publish("22")
        self.assertNotIn("synthetic", str(caught.exception))
