from contextlib import redirect_stdout
from datetime import UTC, datetime, timedelta
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import Mock, patch

from feedroom.briefing import select_draft, thread_length
from feedroom.cli import main
from feedroom.collector import DAILY_PAPERS, DAIR_AI_PAPERS, candidate, collect
from feedroom.journal import LocalJournal, R2Journal
from feedroom.paper import load_reading, save_reading, select_paper, write_paper
from feedroom.publishing import deliver
from feedroom.threads import Threads, ThreadsError

NOW = datetime(2026, 11, 24, 7, tzinfo=UTC)
DAY = "2026-11-24"
ABSTRACT = b'''<blockquote class="abstract mathjax"><span class="descriptor">Abstract:</span>
We investigate inference latency and introduce a cache that reuses intermediate representations.
</blockquote><div>Unrelated claim: 99% improvement.</div>'''
SUMMARY = "研究探討點樣縮短推論時間，作者提出重用中間表徵嘅快取方法。"


def paper(index=1, *, source=DAILY_PAPERS, version=""):
    return candidate(source, f"Efficient inference with reusable representations {index}",
                     f"https://arxiv.org/abs/2611.{index:05d}{version}", "2026-11-23",
                     "Curator says 99% improvement.", 8, section="reading", upvotes=125)


def collection(*items, now=NOW, errors=()):
    return {"collected_at": now.isoformat(), "candidates": list(items), "collection_errors": list(errors)}


class DailyPaperTests(TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory()))

    def test_morning_snapshot_reused_in_preview_and_same_day_publish(self):
        morning_paper, afternoon_paper = paper(1), paper(2)
        path = self.root / "threads.sqlite3"
        output = self.enterContext(redirect_stdout(StringIO()))
        threads = Mock(spec=Threads, account_id="11")
        threads.create.side_effect = ["22", "23"]
        threads.publish.side_effect = ["32", "33"]
        threads.status.return_value = "FINISHED"
        threads.publication_time.return_value = None
        morning = NOW - timedelta(hours=7)
        with (patch.dict("os.environ", {"THREADS_ACCOUNT_ID": "11", "THREADS_ACCESS_TOKEN": "synthetic",
                                       "FEEDROOM_STATE_DB": str(path)}, clear=True),
              patch("feedroom.cli.datetime") as clock,
              patch("feedroom.cli.Threads", return_value=threads),
              patch("feedroom.cli.collect_from_args", return_value=collection(morning_paper, afternoon_paper, now=morning)),
              patch("feedroom.cli.collect") as refresh,
              patch("feedroom.cli.write_draft", return_value=select_draft(collection(morning_paper), set())),
              patch("feedroom.paper.ArticleReader.page", return_value=ABSTRACT) as page,
              patch("feedroom.paper.complete_json", return_value={"summary": SUMMARY}),
              patch("feedroom.publishing.sleep")):
            clock.now.return_value = morning
            self.assertEqual(main(["publish"]), 0)
            self.assertEqual(load_reading(self.root / "reading-11.json", NOW)["candidates"], [morning_paper, afternoon_paper])
            journal = LocalJournal(path, "11")
            before_preview = journal.read()
            output.seek(0)
            output.truncate()
            clock.now.return_value = NOW
            self.assertEqual(main(["paper-preview"]), 0)
            preview = json.loads(output.getvalue())
            self.assertEqual(preview["candidate_source"], "cached")
            self.assertEqual(preview["draft"]["source_url"], afternoon_paper["url"])
            self.assertEqual(journal.read(), before_preview)
            self.assertEqual(threads.publish.call_count, 1)
            self.assertEqual(main(["paper-publish"]), 0)
            output.seek(0)
            output.truncate()
            self.assertEqual(main(["paper-publish"]), 0)
            self.assertEqual(json.loads(output.getvalue())["status"], "already_published")
            refresh.assert_not_called()
            self.assertEqual(page.call_count, 2)
            self.assertEqual(threads.verify_account.call_count, 3)
        posts, _ = LocalJournal(path, "11").read()
        self.assertEqual({key: post["post_id"] for key, post in posts.items()}, {DAY: "32", f"{DAY}:paper": "33"})
        self.assertEqual(posts[f"{DAY}:paper"]["text"], preview["draft"]["text"])
        self.assertEqual(threads.publish.call_count, 2)

    def test_snapshot_must_be_from_same_hong_kong_date_and_not_in_the_future(self):
        path = self.root / "reading.json"
        self.assertIsNone(load_reading(path, NOW))
        save_reading(path, collection(paper(), now=NOW - timedelta(hours=8)))
        self.assertIsNotNone(load_reading(path, NOW))  # 23:00 UTC is already 07:00 HKT.
        save_reading(path, collection(paper(), now=NOW - timedelta(hours=16)))
        self.assertIsNone(load_reading(path, NOW))
        save_reading(path, collection(paper(), now=NOW + timedelta(minutes=1)))
        self.assertIsNone(load_reading(path, NOW))
        path.write_text("not JSON", encoding="utf-8")
        self.assertIsNone(load_reading(path, NOW))

    def test_fallback_uses_paper_sources_only_and_skips_used_and_unverified_papers(self):
        used, unavailable, fresh = paper(1), paper(2), paper(3, source=DAIR_AI_PAPERS.name)
        cache = collection(paper(1, version="v2"), unavailable)
        curated = {"title": fresh["title"], "url": fresh["url"], "summary": fresh["summary_or_snippet"],
                   "published_at": fresh["published_at"]}
        with (patch("feedroom.collector.feed_entries") as feeds, patch("feedroom.collector.github_entries") as releases,
              patch("feedroom.collector.tldr_entries") as tldr, patch("feedroom.collector.hn_entries") as hn,
              patch("feedroom.collector.paper_candidates", return_value=[used, unavailable]),
              patch("feedroom.collector.dair_entries", return_value=[curated]),
              patch("feedroom.paper.ArticleReader.page", side_effect=[b"", ABSTRACT]) as page):
            result = select_paper(cache, lambda ids: collect(None, 0, 8, as_of=NOW, published_ids=ids,
                                                           reading_only=True), {used["id"]})
        self.assertEqual(result["candidate_source"], "fresh")
        self.assertEqual(result["paper"][1], fresh["url"])
        self.assertEqual(result["collection_errors"][0]["source"], unavailable["url"])
        self.assertEqual([call.args[0] for call in page.call_args_list], [unavailable["url"], fresh["url"]])
        for source in (feeds, releases, tldr, hn):
            source.assert_not_called()

    def test_writer_gets_only_original_abstract_and_revises_unsupported_claim(self):
        original = paper()
        with (patch("feedroom.paper.ArticleReader.page", return_value=ABSTRACT),
              patch("feedroom.paper.complete_json", side_effect=[{"summary": "研究報告提升咗99%。"},
                                                                 {"summary": SUMMARY}]) as complete):
            result = select_paper(None, Mock(return_value=collection(original)), set())
            draft = write_paper(result, set(), day=DAY)
        payload = complete.call_args.args[1]
        self.assertNotIn("99", payload["abstract"])
        self.assertIn("figures", complete.call_args.args[2])
        self.assertEqual(draft.text.splitlines(), [DAY, "今日讀一篇", original["title"], SUMMARY, original["url"]])
        self.assertLessEqual(thread_length(draft.text), 500)

    def test_original_abstract_keeps_mathematical_comparisons(self):
        abstract = b'<blockquote class="abstract">Abstract: $x&lt;y$ and $a&gt;b$ hold.</blockquote>'
        with patch("feedroom.paper.ArticleReader.page", return_value=abstract):
            result = select_paper(collection(paper()), Mock(), set())
        self.assertEqual(result["paper"][3], "$x<y$ and $a>b$ hold.")

    def test_legacy_versioned_source_url_excludes_the_same_paper_with_a_different_title(self):
        original = paper()
        morning = candidate("Hacker News", "The morning digest used a different title",
                            original["url"] + "v2?utm_source=hn", DAY, "Source excerpt.", 8, section="latest")
        journal = LocalJournal(self.root / "threads.sqlite3", "11")
        self.assertTrue(journal.write({DAY: {"status": "published", "candidate_id": morning["id"],
                                             "title_id": morning["title_id"], "source_urls": [morning["url"]]}}, None))
        threads = Mock(spec=Threads)
        with (patch("feedroom.paper.ArticleReader.page", return_value=ABSTRACT) as page,
              patch("feedroom.paper.complete_json", side_effect=AssertionError("The published paper must be skipped"))):
            result = deliver(journal, threads, lambda used: select_paper(collection(original), Mock(return_value=collection()),
                                                                        used), NOW, write=write_paper, slot="paper")
        self.assertEqual(result["status"], "no_story")
        page.assert_not_called()
        threads.create.assert_not_called()

    def test_preview_only_reads_the_configured_r2_journal(self):
        published, available = paper(1), paper(2)
        path = self.root / "candidates.json"
        path.write_text(json.dumps(collection(published, available)), encoding="utf-8")
        journal = Mock(spec=R2Journal)
        journal.read.return_value = ({DAY: {"status": "published", "source_url": published["url"] + "v2"}}, "etag")
        with (patch.dict("os.environ", {"THREADS_ACCOUNT_ID": "11", "FEEDROOM_R2_BUCKET": "fixture"}, clear=True),
              patch("feedroom.cli.open_journal", return_value=journal) as open_journal,
              patch("feedroom.cli.Threads") as threads,
              patch("feedroom.paper.ArticleReader.page", return_value=ABSTRACT),
              patch("feedroom.paper.complete_json", return_value={"summary": SUMMARY}),
              redirect_stdout(StringIO()) as output):
            self.assertEqual(main(["paper-preview", "--candidates", str(path)]), 0)
        self.assertEqual(json.loads(output.getvalue())["draft"]["source_url"], available["url"])
        open_journal.assert_called_once_with("11")
        journal.read.assert_called_once_with()
        journal.write.assert_not_called()
        threads.assert_not_called()

    def test_unverified_original_does_not_create_a_post(self):
        journal = LocalJournal(self.root / "threads.sqlite3", "11")
        threads = Mock(spec=Threads)
        with patch("feedroom.paper.ArticleReader.page", return_value=b"<div>Only a curator snippet</div>"):
            result = deliver(journal, threads, lambda used: select_paper(None, Mock(return_value=collection(
                paper(), errors=[{"source": DAILY_PAPERS, "error": "timeout"}])), used), NOW,
                write=write_paper, slot="paper")
        self.assertEqual(result["status"], "collection_incomplete")
        self.assertEqual(journal.read()[0], {})
        threads.create.assert_not_called()

    def test_paper_slot_recovers_lost_response_and_keeps_other_slots_independent(self):
        journal = LocalJournal(self.root / "threads.sqlite3", "11")
        threads = Mock(spec=Threads)
        threads.create.side_effect = ["22", "23", "24"]
        threads.status.return_value = "FINISHED"
        threads.publication_time.return_value = None
        threads.publish.side_effect = ["32", ThreadsError("connection lost"), "34"]
        threads.find_post.return_value = "33"
        with patch("feedroom.publishing.sleep"):
            self.assertEqual(deliver(journal, threads, Mock(return_value=collection(paper(1))), NOW,
                                     write=select_draft)["status"], "published")
            with self.assertRaisesRegex(ThreadsError, "connection lost"):
                deliver(journal, threads, Mock(return_value=collection(paper(2))), NOW, write=select_draft, slot="paper")
            threads.status.side_effect = ["PUBLISHED", "FINISHED"]
            self.assertEqual(deliver(journal, threads, Mock(return_value=collection(paper(3))), NOW,
                                     write=select_draft, slot="weekly")["status"], "published")
            self.assertEqual(deliver(journal, threads, Mock(), NOW, write=select_draft, slot="paper")["status"],
                             "already_published")
        self.assertEqual({key: post["post_id"] for key, post in journal.read()[0].items()},
                         {DAY: "32", f"{DAY}:paper": "33", f"{DAY}:weekly": "34"})
        self.assertEqual([call.args[0] for call in threads.publish.call_args_list], ["22", "23", "24"])

    def test_other_slot_pauses_the_paper_until_its_container_has_definitively_failed(self):
        for terminal in ("EXPIRED", "ERROR"):
            with self.subTest(terminal=terminal):
                journal = LocalJournal(self.root / "threads.sqlite3", terminal)
                self.assertTrue(journal.write({DAY: {"status": "publishing", "container_id": "22"}}, None))
                threads = Mock(spec=Threads)
                threads.create.return_value = "23"
                threads.publish.return_value = "33"
                threads.publication_time.return_value = None
                threads.status.side_effect = ["IN_PROGRESS", terminal, "FINISHED"]
                collect = Mock(return_value=collection(paper()))
                with patch("feedroom.publishing.sleep"):
                    with self.assertRaisesRegex(ThreadsError, "unresolved"):
                        deliver(journal, threads, collect, NOW, write=select_draft, slot="paper")
                    collect.assert_not_called()
                    self.assertEqual(deliver(journal, threads, collect, NOW, write=select_draft, slot="paper")["status"],
                                     "published")
                self.assertEqual(journal.read()[0][DAY]["reason"], terminal)
                threads.publish.assert_called_once_with("23")
