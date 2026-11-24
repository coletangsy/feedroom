from datetime import datetime
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.robotparser import RobotFileParser

from feedroom.briefing import CardDraft, build_card
from feedroom.cli import main
from feedroom.collector import DAILY_PAPERS, DAIR_AI_PAPERS, TLDR_AI_FEED, Feed, candidate, collect, item_id
from feedroom.evidence import ArticleReader
from feedroom.journal import LocalJournal
from feedroom.weekly import daily_posts, weekly_groups, weekly_window


def article(slug, published, *, source="Fixture", section="latest"):
    return candidate(source, f"Independent update {slug}", f"https://example.com/{slug}",
                     published, "The original source explains the release.", 8, section=section)


class WeeklyInputsTests(TestCase):
    def test_week_bounds_use_hong_kong_monday_even_while_utc_is_sunday(self):
        start, cutoff = weekly_window("2026-11-15T17:00:00Z")
        self.assertEqual(start.isoformat(), "2026-11-16T00:00:00+08:00")
        self.assertEqual(cutoff.isoformat(), "2026-11-16T01:00:00+08:00")

    def test_shortlist_respects_cutoff_and_excludes_issue_coverage_dates(self):
        start, cutoff = weekly_window("2026-11-22T18:00:00+08:00")
        before_week = article("before-week", "2026-11-15T15:59:00Z")
        at_start = article("at-start", "2026-11-15T16:00:00Z")
        at_cutoff = article("at-cutoff", "2026-11-22T10:00:00Z")
        after_cutoff = article("after-cutoff", "2026-11-22T10:01:00Z")
        undated_time = article("date-only", "2026-11-22")
        curated = article("curated", "2026-11-22", source=DAIR_AI_PAPERS.name, section="reading")
        invalid_lane = article("invalid-lane", "2026-11-22", source=DAIR_AI_PAPERS.name)
        groups = weekly_groups([before_week, at_start, at_cutoff, after_cutoff, undated_time, curated, invalid_lane], {}, start, cutoff)
        self.assertEqual({group[0]["id"] for group, _ in groups}, {item["id"] for item in (at_start, at_cutoff, undated_time)})

    def test_published_daily_records_match_legacy_urls_and_ids_within_the_week(self):
        start, cutoff = weekly_window("2026-11-22T18:00:00+08:00")
        by_url = article("url", "2026-11-15T15:59:00Z")
        by_id = article("id", "2026-11-15")
        failed, late, unknown = [article(slug, "2026-11-19") for slug in ("failed", "late", "unknown")]
        posts = {
            "2026-11-18": {"status": "published", "source_url": by_url["url"] + "/?utm_source=daily",
                           "published_at": "2026-11-18T09:01:00+08:00"},
            "2026-11-19": {"status": "published", "source_urls": [unknown["url"]]},
            "2026-11-20": {"status": "published", "related_ids": [by_id["id"]],
                           "published_at": "2026-11-20T09:01:00+08:00"},
            "2026-11-21": {"status": "failed", "source_urls": [failed["url"]]},
            "2026-11-21:paper": {"status": "published", "source_urls": [failed["url"]],
                                 "published_at": "2026-11-21T15:00:00+08:00"},
            "2026-11-22": {"status": "published", "source_urls": [late["url"]],
                           "published_at": "2026-11-22T18:05:00+08:00",
                           "prepared_at": str(int(datetime.fromisoformat("2026-11-22T17:55:00+08:00").timestamp()))},
        }
        groups = weekly_groups([failed, by_url, by_id, late, unknown], posts, start, cutoff)
        self.assertEqual({group[0]["id"]: daily for group, daily in groups}, {
            by_url["id"]: True, by_id["id"]: True, failed["id"]: False, late["id"]: False, unknown["id"]: False,
        })

    def test_weekly_replay_requires_original_publication_date_for_aggregated_articles(self):
        start, cutoff = weekly_window("2026-11-22T18:00:00+08:00")
        direct = article("direct", "2026-11-20")
        hn = article("hn", "2026-11-20", source="Hacker News")
        tldr = article("tldr", "2026-11-20", source=TLDR_AI_FEED.name)
        self.assertEqual(weekly_groups([hn, tldr, direct], {}, start, cutoff), [([direct], False)])

    def test_original_source_collection_skips_aggregators(self):
        _, cutoff = weekly_window("2026-11-22T18:00:00+08:00")
        with (patch("feedroom.collector.FEEDS", ()), patch("feedroom.collector.GITHUB_RELEASES", ()),
              patch("feedroom.collector.paper_candidates", return_value=[]), patch("feedroom.collector.dair_entries", return_value=[]),
              patch("feedroom.collector.hn_entries") as hn, patch("feedroom.collector.tldr_entries") as tldr):
            self.assertEqual(collect(None, 162, 150, as_of=cutoff, include_aggregators=False)["candidates"], [])
        hn.assert_not_called()
        tldr.assert_not_called()

    def test_collect_keeps_a_daily_paper_over_a_duplicate_feed_title(self):
        start, cutoff = weekly_window("2026-11-17T18:00:00+08:00")
        url = "https://arxiv.org/abs/2611.00001"
        paper = {"paper": {"id": "2611.00001", "title": "Research on model inference",
                           "summary": "The study measured inference latency.", "publishedAt": "2026-11-01",
                           "upvotes": 120}}
        report = {"url": "https://example.com/model", "title": paper["paper"]["title"],
                  "published_at": "2026-11-16", "summary": "A new model inference release."}
        with (patch("feedroom.collector.FEEDS", (Feed("Fixture", "https://example.com/feed", 5),)),
              patch("feedroom.collector.feed_entries", return_value=[report]),
              patch("feedroom.collector.GITHUB_RELEASES", ()),
              patch("feedroom.collector.fetch", side_effect=[json.dumps([paper]).encode(), b"[]"])):
            result = collect(None, 48, 150, as_of=cutoff, week_start=start,
                             daily_keys={item_id(url, "")}, include_aggregators=False)
        [selected] = result["candidates"]
        self.assertEqual(selected["url"], url)

    def test_holds_room_for_supplements_when_daily_shortlist_is_full(self):
        start, cutoff = weekly_window("2026-11-22T18:00:00+08:00")
        articles = [article(f"topic-{index}", "2026-11-19") for index in range(15)]
        posts = {"2026-11-20": {"status": "published", "published_at": "2026-11-20T09:01:00+08:00",
                                "source_urls": [item["url"] for item in articles[:-2]]}}
        groups = weekly_groups(articles, posts, start, cutoff)
        self.assertEqual(sum(daily for _, daily in groups), 10)
        self.assertEqual(len(groups), 12)

    def test_supplements_prefer_varied_publishers_before_reusing_one(self):
        start, cutoff = weekly_window("2026-11-22T18:00:00+08:00")
        repeated = [article(f"topic-{index}", "2026-11-19", source="Same publisher") for index in range(5)]
        other = article("another-topic", "2026-11-19", source="Another publisher")
        groups = weekly_groups([*repeated, other], {}, start, cutoff)
        self.assertEqual([group[0]["source"] for group, _ in groups], [
            "Same publisher", "Another publisher", "Same publisher", "Same publisher",
        ])

    def test_bad_entry_does_not_discard_other_published_daily_history(self):
        start, cutoff = weekly_window("2026-11-22T18:00:00+08:00")
        valid_item = article("valid", "2026-11-19")
        posts = {
            "2026-11-19": {}, "2026-11-20": {"status": "published", "prepared_at": "invalid"},
            "2026-11-21": {"status": "published", "published_at": "2026-11-21T09:01:00+08:00",
                           "source_urls": [valid_item["url"]]},
        }
        self.assertEqual(weekly_groups([valid_item], posts, start, cutoff), [([valid_item], True)])

    def test_reads_only_the_requested_account_from_existing_sqlite_without_creating_a_file(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "daily state.sqlite3"
            original = {"2026-11-20": {"status": "published", "source_url": "https://example.com/story"}}
            self.assertTrue(LocalJournal(path, "123").write(original, None))
            self.assertTrue(LocalJournal(path, "456").write({"unrelated": True}, None))
            self.assertEqual(daily_posts(path, "123"), original)
            self.assertEqual(daily_posts(path, "999"), {})
            missing = Path(directory) / "missing.sqlite3"
            with self.assertRaisesRegex(ValueError, "journal"):
                daily_posts(missing, "123")
            self.assertFalse(missing.exists())

    def test_weekly_reader_can_follow_only_the_same_article_trailing_slash(self):
        url = "https://openai.com/index/story"
        for location, allowed in (("/index/story/", True), ("https://other.example/index/story/", False)):
            with self.subTest(location=location):
                reader = ArticleReader(follow_article_slash=True)
                robots = RobotFileParser()
                robots.parse(["User-agent: *", "Disallow:"])
                reader.robots["openai.com"] = robots
                redirect = HTTPError(url, 301, "Moved", {"Location": location}, None)
                with (patch.object(reader, "fetch", side_effect=[redirect, b"source document"]) as fetch,
                      patch("feedroom.evidence.extract", return_value="Article text")):
                    self.assertEqual(reader.text(url), "Article text" if allowed else "")
                self.assertEqual([call.args[0] for call in fetch.call_args_list], [url, url + "/"] if allowed else [url])


class WeeklyPreviewTests(TestCase):
    def test_live_preview_recovers_a_daily_link_missing_from_publisher_listings(self):
        url = "https://openai.com/index/older-release"
        excerpt = "The team released a benchmark for long context models. " * 12
        with TemporaryDirectory() as directory:
            state_path = Path(directory) / "threads.sqlite3"
            LocalJournal(state_path, "123").write(
                {"2026-11-16": {"status": "published", "published_at": "2026-11-16T08:00:00+08:00",
                                "source_url": url, "text": "每日摘要：未經驗證嘅舊圖卡文案。"}}, None,
            )
            with (patch("feedroom.collector.FEEDS", ()), patch("feedroom.collector.GITHUB_RELEASES", ()),
                  patch("feedroom.collector.paper_candidates", return_value=[]),
                  patch.object(ArticleReader, "text", return_value=excerpt) as reader,
                  patch("feedroom.weekly_writer._complete_weekly", return_value={"items": []}) as model,
                  patch("builtins.print")):
                self.assertEqual(main(["weekly-preview", "--as-of", "2026-11-16T18:00:00+08:00",
                                       "--account", "123", "--daily-journal", str(state_path)]), 0)
        reader.assert_called_once_with(url)
        [event] = model.call_args.args[0]["events"]
        self.assertTrue(event["daily_backed"])
        self.assertEqual(event["primary_source"]["url"], url)
        self.assertEqual(event["primary_source"]["article_excerpt"], excerpt)
        self.assertIsNone(event["primary_source"]["source_date"])
        self.assertNotIn("未經驗證", json.dumps(event, ensure_ascii=False))

    def test_cli_previews_weekly_card_without_accessing_threads(self):
        item = article("release", "2026-11-19")
        card = build_card("開源模型有更新", item, "開發團隊公佈新版。")
        draft = CardDraft(item["id"], item["title_id"], "每週草稿", cards=(card,))
        with TemporaryDirectory() as directory:
            root = Path(directory)
            state_path = root / "threads.sqlite3"
            LocalJournal(state_path, "123").write(
                {"2026-11-20": {"status": "published", "published_at": "2026-11-20T09:01:00+08:00",
                                "source_urls": [item["url"]]}}, None,
            )
            candidates_path = root / "candidates.json"
            candidates_path.write_text(json.dumps({"candidates": [item], "collection_errors": []}), encoding="utf-8")
            cards_dir = root / "weekly-cards"
            with (patch("feedroom.cli.write_weekly_draft", return_value=draft) as writer,
                  patch("feedroom.cli.render_cards", return_value=(cards_dir / "01.png",)) as render,
                  patch("feedroom.cli.Threads") as threads,
                  patch("builtins.print") as output):
                self.assertEqual(main(["weekly-preview", "--as-of", "2026-11-22T18:00:00+08:00", "--account", "123",
                                       "--daily-journal", str(state_path), "--candidates", str(candidates_path),
                                       "--cards-dir", str(cards_dir)]), 0)
            self.assertEqual(writer.call_args.args, ([([item], True)],))
            render.assert_called_once_with((card,), cards_dir, weekly=True)
            threads.assert_not_called()
            result = json.loads(output.call_args.args[0])
            self.assertEqual(result["status"], "draft")
            self.assertEqual(result["coverage"]["cutoff"], "2026-11-22T18:00:00+08:00")
            self.assertEqual(result["card_images"], [str(cards_dir / "01.png")])

    def test_empty_week_leaves_no_output_directory(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            state_path = root / "threads.sqlite3"
            LocalJournal(state_path, "123")
            candidates_path = root / "candidates.json"
            candidates_path.write_text('{"candidates": [], "collection_errors": []}', encoding="utf-8")
            cards_dir = root / "weekly-cards"
            with patch("builtins.print") as output:
                self.assertEqual(main(["weekly-preview", "--as-of", "2026-11-22T18:00:00+08:00", "--account", "123",
                                       "--daily-journal", str(state_path), "--candidates", str(candidates_path),
                                       "--cards-dir", str(cards_dir)]), 0)
            result = json.loads(output.call_args.args[0])
            self.assertEqual(result["status"], "no_story")
            self.assertEqual(result["card_images"], [])
            self.assertFalse(cards_dir.exists())

    def test_live_collection_matches_replay_across_hong_kong_monday(self):
        before_start = article("before", "2026-11-15T15:50:00Z")
        carryover = article("carryover", "2026-11-15T15:50:00Z")
        on_monday = article("monday-date-only", "2026-11-16")
        after_cutoff = candidate("OpenAI", "Future AI release", "https://openai.com/index/future",
                                 "2026-11-15T17:01:00Z", "The original source explains the release.", 8,
                                 section="latest")
        items = [before_start, carryover, on_monday, after_cutoff]
        feed_entries = [dict(title=f"{item['title']} AI model release", url=item["url"],
                             summary=item["summary_or_snippet"], published_at=item["published_at"]) for item in items]
        with TemporaryDirectory() as directory:
            root = Path(directory)
            state_path = root / "threads.sqlite3"
            LocalJournal(state_path, "123").write(
                {"2026-11-16": {"status": "published", "published_at": "2026-11-16T00:30:00+08:00",
                                "source_urls": [carryover["url"], after_cutoff["url"]]}}, None,
            )
            candidates_path = root / "candidates.json"
            candidates_path.write_text(json.dumps({"candidates": items, "collection_errors": []}), encoding="utf-8")
            command = ["weekly-preview", "--as-of", "2026-11-16T01:00:00+08:00",
                       "--account", "123", "--daily-journal", str(state_path)]
            with (patch("feedroom.collector.feed_entries", return_value=feed_entries),
                  patch("feedroom.collector.GITHUB_RELEASES", ()),
                  patch("feedroom.collector.paper_candidates", return_value=[]),
                  patch("feedroom.cli.write_weekly_draft", return_value=None) as writer,
                  patch("builtins.print")):
                self.assertEqual(main(command), 0)
                self.assertEqual(main([*command, "--candidates", str(candidates_path)]), 0)
            for call in writer.call_args_list:
                self.assertEqual([(group[0]["url"], daily) for group, daily in call.args[0]],
                                 [(carryover["url"], True), (on_monday["url"], False)])

    def test_live_collection_applies_week_start_before_the_paper_limit(self):
        def paper(index, published_at, votes):
            return {"paper": {"id": f"2611.{index:05}", "title": f"Model research {index}",
                              "summary": "Findings from model research.", "publishedAt": published_at,
                              "upvotes": votes}}

        old = [paper(i, "2026-11-15T15:45:00Z", 200 + i) for i in range(8)]
        current = paper(9, "2026-11-16", 100)
        with TemporaryDirectory() as directory:
            state_path = Path(directory) / "threads.sqlite3"
            LocalJournal(state_path, "123")
            with (patch("feedroom.collector.FEEDS", ()), patch("feedroom.collector.GITHUB_RELEASES", ()),
                  patch("feedroom.collector.fetch", return_value=json.dumps([*old, current]).encode()) as fetch,
                  patch("feedroom.cli.write_weekly_draft", return_value=None) as writer,
                  patch("builtins.print")):
                self.assertEqual(main(["weekly-preview", "--as-of", "2026-11-17T18:00:00+08:00",
                                       "--account", "123", "--daily-journal", str(state_path)]), 0)
            fetch.assert_called_once_with("https://huggingface.co/api/daily_papers?week=2026-W47&limit=100",
                                          follow_redirects=False)
            [(group, backed)] = writer.call_args.args[0]
            self.assertFalse(backed)
            self.assertEqual(group[0]["source"], DAILY_PAPERS)
            self.assertEqual(group[0]["url"], "https://arxiv.org/abs/2611.00009")
