from datetime import UTC, datetime, timedelta
from http.client import IncompleteRead
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from feedroom.collector import (
    FEEDS,
    TLDR_AI_FEED,
    canonical_url,
    collect,
    fetch as fetch_source,
    hn_entries,
    item_id,
    load_state,
    paper_candidates,
    save_seen,
    score_item,
    score_reading_item,
    tldr_entries,
    tldr_issue_entries,
)


class NewsCollectorTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch("feedroom.collector.dair_entries", return_value=[]))

    def test_curated_feeds_include_tldr_and_have_unique_urls(self):
        feeds = {feed.name: feed for feed in (*FEEDS, TLDR_AI_FEED)}

        self.assertEqual(feeds["AWS Machine Learning Blog"].url, "https://aws.amazon.com/blogs/machine-learning/feed/")
        self.assertEqual(feeds["Apple ML Research"].url, "https://machinelearning.apple.com/rss.xml")
        self.assertEqual(feeds["Microsoft Research"].url, "https://www.microsoft.com/en-us/research/feed/")
        self.assertEqual(feeds["Databricks"].url, "https://www.databricks.com/feed")
        self.assertEqual(feeds["PyTorch"].url, "https://pytorch.org/blog/feed.xml")
        self.assertEqual(feeds["MIT Technology Review AI"].url, "https://www.technologyreview.com/topic/artificial-intelligence/feed/")
        self.assertEqual(feeds["TLDR AI"].url, "https://tldr.tech/api/rss/ai")
        self.assertEqual(len(feeds), len({feed.url for feed in feeds.values()}))

    def test_tldr_issue_entries_keep_exact_links_and_drop_sponsors(self):
        entries = tldr_issue_entries('<article><a class="font-bold" href="https://example.com/story"><h3>New AI model</h3></a><div class="newsletter-html">A useful summary.</div></article><article><a class="font-bold" href="https://example.com/sponsor"><h3>AI platform (Sponsor)</h3></a><div class="newsletter-html">Sponsored.</div></article>')

        self.assertEqual(entries, [{"title": "New AI model", "url": "https://example.com/story", "summary": "A useful summary."}])

    def test_tldr_keeps_articles_from_successful_issues_after_a_timeout(self):
        now = datetime.now(UTC)
        issues = [{"url": f"https://tldr.tech/ai/{day}", "published_at": now.isoformat()} for day in ("first", "second")]
        html = b'<article><a class="font-bold" href="https://example.com/story"><h3>AI model</h3></a><div class="newsletter-html">Summary</div></article>'
        errors = []
        with patch("feedroom.collector.feed_entries", return_value=issues), patch("feedroom.collector.fetch", side_effect=[TimeoutError("timed out"), html]):
            entries = tldr_entries(now - timedelta(days=1), errors)

        self.assertEqual([entry["url"] for entry in entries], ["https://example.com/story"])
        self.assertEqual([error["source"] for error in errors], ["TLDR AI"])

    def test_canonical_url_drops_tracking_but_keeps_meaningful_query(self):
        self.assertEqual(
            canonical_url("https://Example.com/post/?utm_source=x&model=gpt#section"),
            "https://example.com/post?model=gpt",
        )

    def test_hn_discussion_heat_changes_signal(self):
        low_heat = score_item("New AI model", "https://example.com/post", "", 1, hn_points=2, hn_comments=1)
        active_discussion = score_item("New AI model", "https://example.com/post", "", 1, hn_points=100, hn_comments=30)
        most_discussed = score_item("New AI model", "https://example.com/post", "", 1, hn_points=300, hn_comments=100)

        self.assertLess(low_heat, 4)
        self.assertGreaterEqual(active_discussion, 4)
        self.assertGreater(most_discussed, active_discussion)

    def test_hn_uses_popularity_ranked_search_for_recent_stories(self):
        payload = b'{"hits": [{"title": "New AI model", "url": "https://example.com/", "points": 300, "num_comments": 100, "created_at": "2026-08-12T01:00:00Z", "objectID": "1"}]}'
        with patch("feedroom.collector.fetch", return_value=payload) as fetch:
            entries = hn_entries(datetime(2026, 8, 12, tzinfo=UTC))

        url = fetch.call_args.args[0]
        self.assertIn("/api/v1/search?", url)
        self.assertNotIn("query=", url)
        self.assertEqual(entries[0]["url"], "https://news.ycombinator.com/item?id=1")

    def test_ai_matches_a_term_not_part_of_an_unrelated_word(self):
        self.assertIsNone(score_item("Email training update", "https://example.com/post", "Paid leave changes", 2))
        self.assertIsNotNone(score_item("AI training update", "https://example.com/post", "", 2))

    def test_routine_version_bump_is_below_the_output_threshold(self):
        score = score_item("OpenAI Python v2.52.1", "https://github.com/openai/openai-python/releases/tag/v2.52.1", "Chores: pin a CI action", 4, github_release=True)
        self.assertLess(score, 4)

    def test_old_item_needs_a_reading_signal(self):
        self.assertIsNone(
            score_reading_item(
                "OpenAI model release",
                "https://example.com/release",
                "A routine model release.",
                2,
            )
        )
        self.assertIsNotNone(
            score_reading_item(
                "A deep dive into LLM evaluation",
                "https://example.com/deep-dive",
                "Research lessons from a benchmark study.",
                2,
            )
        )

    def test_hugging_face_papers_need_at_least_100_upvotes(self):
        now = datetime.now(UTC)
        entries = {
            "OpenAI": [
                {"title": "New AI model release", "url": "https://example.com/latest", "summary": "A new API model release.", "published_at": (now - timedelta(hours=1)).isoformat()},
                {"title": "A deep dive into LLM evaluation", "url": "https://example.com/reading", "summary": "Research lessons from a benchmark study.", "published_at": (now - timedelta(days=3)).isoformat()},
                {"title": "Future AI model release", "url": "https://example.com/future", "summary": "A new model.", "published_at": (now + timedelta(hours=1)).isoformat()},
            ],
        }

        def feed_entries(feed):
            return entries.get(feed.name, [])

        paper = {"id": "2609.12345", "title": "Paper on model inference", "summary": "Research on inference.",
                 "publishedAt": (now - timedelta(days=3)).isoformat(), "upvotes": 112}
        rows = [paper, {**paper, "id": "2609.12346", "upvotes": 99},
                {**paper, "id": "2609.12347", "publishedAt": (now + timedelta(days=1)).isoformat()}]
        with TemporaryDirectory() as directory:
            with patch("feedroom.collector.feed_entries", side_effect=feed_entries), patch("feedroom.collector.tldr_entries", return_value=[]), patch("feedroom.collector.hn_entries", return_value=[]), patch("feedroom.collector.github_entries", return_value=[]), patch("feedroom.collector.fetch", return_value=json.dumps([{"paper": row} for row in rows]).encode()):
                result = collect(Path(directory) / "seen.json", lookback_hours=36, max_items=10, reading_lookback_days=30, as_of=now)

        self.assertEqual([item["section"] for item in result["candidates"]], ["latest", "reading"])
        self.assertEqual(result["candidates"][1]["url"], "https://arxiv.org/abs/2609.12345")
        self.assertEqual(result["candidates"][1]["upvotes"], 112)

    def test_a_failed_paper_week_preserves_other_weeks(self):
        cutoff = datetime(2026, 9, 6, tzinfo=UTC)
        now = cutoff + timedelta(days=1)
        paper = {"id": "2609.12345", "title": "Research on inference", "summary": "A method.",
                 "publishedAt": now.isoformat(), "upvotes": 112}
        errors = []
        with patch("feedroom.collector.fetch", side_effect=[TimeoutError("timed out"), json.dumps([{"paper": paper}]).encode()]):
            result = paper_candidates(cutoff, now, errors)
        self.assertEqual(result[0]["upvotes"], 112)
        self.assertEqual(len(errors), 1)

    def test_malformed_story_url_does_not_stop_collection(self):
        entry = {"title": "AI model release", "url": "https://example.com/release", "summary": "A new API model release.", "published_at": datetime.now(UTC).isoformat()}
        entries = [{**entry, "url": "https://[invalid.example/story"}, entry]
        with TemporaryDirectory() as directory:
            with patch("feedroom.collector.feed_entries", return_value=entries), patch("feedroom.collector.tldr_entries", return_value=[]), patch("feedroom.collector.hn_entries", return_value=[]), patch("feedroom.collector.github_entries", return_value=[]), patch("feedroom.collector.paper_candidates", return_value=[]):
                result = collect(Path(directory) / "seen.json", 36, 40)

        self.assertEqual([item["url"] for item in result["candidates"]], [entry["url"]])

    def test_seen_state_expires_after_two_weeks(self):
        now = datetime.now(UTC)
        with TemporaryDirectory() as directory:
            path = Path(directory) / "seen.json"
            save_seen(path, ["current"], now)
            path.write_text('{"items": [{"id": "old", "seen_at": "2000-01-01T00:00:00Z"}, {"id": "current", "seen_at": "' + now.isoformat().replace("+00:00", "Z") + '"}]}')
            self.assertEqual(load_state(path, now + timedelta(days=1)), {"current"})

    def test_overlapping_sources_deduplicate_exact_article_with_different_titles(self):
        published = datetime.now(UTC).isoformat()
        entries = [
            {"title": "AI research report", "url": "https://example.com/study?utm_source=google", "summary": "Research findings.", "published_at": published},
            {"title": "New AI research findings", "url": "https://example.com/study/", "summary": "Research findings.", "published_at": published},
        ]
        with TemporaryDirectory() as directory:
            with patch("feedroom.collector.feed_entries", return_value=entries), patch("feedroom.collector.tldr_entries", return_value=[]), patch("feedroom.collector.hn_entries", return_value=[]), patch("feedroom.collector.github_entries", return_value=[]), patch("feedroom.collector.paper_candidates", return_value=[]):
                result = collect(Path(directory) / "seen.json", 36, 40)
        self.assertEqual(len(result["candidates"]), 1)

    def test_technical_reading_accepts_data_engineering_but_not_company_news(self):
        self.assertIsNotNone(score_reading_item("Scaling a data platform", "https://example.com/post", "Architecture lessons from data engineering.", 4))
        self.assertIsNone(score_item("Opening our new office", "https://example.com/post", "Meet our team.", 4))

    def test_signals_study_can_enter_reading_lane(self):
        self.assertIsNotNone(score_reading_item("How people use ChatGPT", "https://openai.com/index/study", "A study with findings on AI adoption.", 5))

    def test_scoring_ignores_terms_outside_the_emitted_snippet(self):
        summary = "Ordinary company news. " * 100 + " AI research training benchmark"
        self.assertIsNone(score_item("Office update", "https://example.com/post", summary, 4))
        self.assertIsNone(score_reading_item("AI update", "https://example.com/post", summary, 4))

    def test_date_only_articles_overlap_latest_window_without_invented_time(self):
        class MorningRun(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime(2026, 9, 6, tzinfo=UTC)

        # 08:00 HKT: cutoff is September 4 at noon UTC. No depth terms, so
        # treating the date as midnight would drop the announcement entirely.
        dates = ("2026-09-04", "2026-09-03", "2026-09-04T00:00:00Z", "2026-09-04T13:00:00Z")
        entries = [{"title": f"Claude announcement {index}", "url": f"https://example.com/{index}", "summary": "Claude is available.", "published_at": value} for index, value in enumerate(dates)]
        with TemporaryDirectory() as directory:
            with patch("feedroom.collector.datetime", MorningRun), patch("feedroom.collector.feed_entries", return_value=entries), patch("feedroom.collector.tldr_entries", return_value=[]), patch("feedroom.collector.hn_entries", return_value=[]), patch("feedroom.collector.github_entries", return_value=[]), patch("feedroom.collector.paper_candidates", return_value=[]):
                result = collect(Path(directory) / "seen.json", 36, 40)
        self.assertEqual({item["published_at"] for item in result["candidates"]}, {"2026-09-04", "2026-09-04T13:00:00Z"})
        self.assertTrue(all(item["section"] == "latest" for item in result["candidates"]))


class DairPapersTests(unittest.TestCase):
    now = datetime(2026, 11, 22, tzinfo=UTC)
    hub = "https://academy.dair.ai/papers"
    issue = f"{hub}/week/2026-11-09-november-15"
    archive = b"""
        <a href="/papers/week/2026-11-09-november-15">Completed issue</a>
        <a href="/papers/week/2026-11-16-november-22">Current issue</a>
        <a href="https://example.com/papers/week/2026-11-15-november-21">External link</a>
        <a href="/papers/week/2026-08-03-august-9">Old issue</a>
    """
    cards = b"""
        <a class="ph-card" href="/papers/model-inference-2611.12345">
            <h3>Faster Model Inference</h3><p>A research team studied model inference.</p></a>
        <a class="ph-card" href="/papers/ai-evaluation-2611.12346">
            <h3>AI &amp; Evaluation</h3><p>A study of <em>agent</em> evaluation.</p></a>
        <a class="ph-card" href="/papers/technical-report">
            <h3>Training Report</h3><p>A report on AI training.</p></a>
    """

    def collect_papers(self, fetch, *, days=90, published_ids=frozenset()):
        with (
            TemporaryDirectory() as directory,
            patch.multiple("feedroom.collector", FEEDS=(), GITHUB_RELEASES=()),
            patch("feedroom.collector.tldr_entries", return_value=[]),
            patch("feedroom.collector.hn_entries", return_value=[]),
            patch("feedroom.collector.fetch", side_effect=fetch),
        ):
            return collect(Path(directory) / "seen.json", 36, 40, days, as_of=self.now,
                           published_ids=published_ids)

    def test_curated_papers_use_completed_week_and_canonical_urls(self):
        def fetch(url, *, follow_redirects=False):
            return {self.hub: self.archive, self.issue: self.cards}.get(url, b"[]")

        result = self.collect_papers(fetch)
        candidates = {item["url"]: item for item in result["candidates"]}

        self.assertEqual(result["collection_errors"], [])
        self.assertEqual(set(candidates), {
            "https://arxiv.org/abs/2611.12345", "https://arxiv.org/abs/2611.12346",
            f"{self.hub}/technical-report",
        })
        paper = candidates["https://arxiv.org/abs/2611.12346"]
        self.assertEqual(paper["title"], "AI & Evaluation")
        self.assertEqual(paper["summary_or_snippet"], "A study of agent evaluation.")
        self.assertEqual(paper["source"], "DAIR.AI Papers")
        self.assertEqual(paper["section"], "reading")
        self.assertEqual(paper["published_at"], "2026-11-15")

    def test_curated_papers_share_shortlist_and_deduplicate_arxiv_ids(self):
        rows = [{"paper": {
            "id": f"2611.{index}", "title": f"Model evaluation {index}",
            "summary": "Research on inference.", "publishedAt": self.now.isoformat(),
            "upvotes": 201 if index == 12345 else 200,
        }} for index in range(12338, 12346)]

        def fetch(url, *, follow_redirects=False):
            return {self.hub: self.archive, self.issue: self.cards}.get(url, json.dumps(rows).encode())

        used_url = "https://arxiv.org/abs/2611.12346"
        result = self.collect_papers(fetch, published_ids={item_id(used_url, "")})
        candidates = result["candidates"]
        self.assertEqual(len(candidates), 8)
        self.assertEqual(candidates[0]["url"], "https://arxiv.org/abs/2611.12345")
        self.assertEqual(candidates[0]["source"], "Hugging Face Daily Papers")
        self.assertEqual(candidates[1]["url"], f"{self.hub}/technical-report")
        self.assertEqual(candidates[1]["source"], "DAIR.AI Papers")
        self.assertEqual(len({item["url"] for item in candidates}), 8)
        self.assertNotIn(used_url, [item["url"] for item in candidates])

    def test_expired_and_current_issues_are_not_fetched(self):
        fetches = []

        def fetch(url, *, follow_redirects=False):
            fetches.append(url)
            return self.archive if url == self.hub else b"[]"

        result = self.collect_papers(fetch, days=2)
        self.assertEqual([url for url in fetches if url.startswith(self.hub)], [self.hub])
        self.assertEqual(result["collection_errors"], [])
        self.assertEqual(result["candidates"], [])

    def test_dair_issue_failure_preserves_hugging_face_papers(self):
        rows = [{"paper": {
            "id": "2611.12345", "title": "Research on inference", "summary": "A method.",
            "publishedAt": self.now.isoformat(), "upvotes": 112,
        }}]

        def fetch(url, *, follow_redirects=False):
            if url == self.issue:
                return fetch_source(url, follow_redirects=follow_redirects)
            return self.archive if url == self.hub else json.dumps(rows).encode()

        for error in (TimeoutError("issue timed out"), IncompleteRead(b"partial")):
            with self.subTest(error=error), patch("feedroom.collector.urllib.request.build_opener") as opener:
                response = opener.return_value.open.return_value.__enter__.return_value
                response.read.side_effect = error
                result = self.collect_papers(fetch)

                self.assertEqual(result["candidates"][0]["source"], "Hugging Face Daily Papers")
                self.assertEqual(result["collection_errors"], [{"source": "DAIR.AI Papers", "error": str(error)}])


if __name__ == "__main__":
    unittest.main()
