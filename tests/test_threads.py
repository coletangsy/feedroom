import io
import json
import unittest
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit

from feedroom.threads import Threads, ThreadsError


def _response(payload):
    return io.BytesIO(json.dumps(payload).encode("utf-8"))


class ThreadsRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.threads = Threads("synthetic", "11")
        self.threads.opener = Mock()

    def test_create_attaches_the_source_outside_the_post_text(self):
        text = "2026-11-20\n• 研究團隊發佈新模型"
        url = "https://example.com/story"
        self.threads.opener.open.return_value = _response({"id": "22"})

        self.assertEqual(self.threads.create(text, link_attachment=url), "22")

        request = self.threads.opener.open.call_args.args[0]
        self.assertEqual(parse_qs(request.data.decode()), {
            "media_type": ["TEXT"], "text": [text], "link_attachment": [url],
        })

    def test_find_post_uses_the_source_attachment_to_disambiguate_identical_titles(self):
        text = "2026-11-20\n• 研究團隊發佈新模型"
        url = "https://example.com/story"
        self.threads.opener.open.return_value = _response({"data": [
            {"id": "20", "text": text, "link_attachment_url": "https://example.org/other"},
            {"id": "33", "text": text, "link_attachment_url": url},
        ]})

        self.assertEqual(self.threads.find_post(text, "1700000000", link_attachment=url), "33")

        request = self.threads.opener.open.call_args.args[0]
        self.assertEqual(parse_qs(urlsplit(request.full_url).query)["fields"], ["id,text,link_attachment_url"])

    def test_find_post_follows_pagination_until_the_unique_match(self):
        self.threads.opener.open.side_effect = [
            _response({
                "data": [{"id": "20", "text": "other"}],
                "paging": {"cursors": {"after": "AFTER_CURSOR"}},
            }),
            _response({
                "data": [{"id": "33", "text": "target"}],
                "paging": {},
            }),
        ]

        self.assertEqual(self.threads.find_post("target", "1700000000"), "33")
        first_request = self.threads.opener.open.call_args_list[0].args[0]
        second_request = self.threads.opener.open.call_args_list[1].args[0]
        self.assertEqual(
            first_request.full_url,
            "https://graph.threads.com/v1.0/11/threads?fields=id%2Ctext&since=1700000000&limit=25",
        )
        self.assertEqual(
            second_request.full_url,
            "https://graph.threads.com/v1.0/11/threads?fields=id%2Ctext&since=1700000000&limit=25&after=AFTER_CURSOR",
        )
        self.assertEqual(first_request.get_header("Authorization"), "Bearer synthetic")
        self.assertEqual(second_request.get_header("Authorization"), "Bearer synthetic")

    def test_find_post_rejects_duplicate_text_across_pages(self):
        self.threads.opener.open.side_effect = [
            _response({
                "data": [{"id": "20", "text": "target"}],
                "paging": {"cursors": {"after": "AFTER_CURSOR"}},
            }),
            _response({"data": [{"id": "33", "text": "target"}]}),
        ]

        with self.assertRaisesRegex(ThreadsError, "could not be identified uniquely"):
            self.threads.find_post("target", "1700000000")

    def test_find_post_rejects_an_incomplete_pagination_response(self):
        self.threads.opener.open.side_effect = [
            _response({"data": [], "paging": {"next": "https://graph.threads.com/v1.0/11/threads"}}),
        ]

        with self.assertRaisesRegex(ThreadsError, "invalid pagination response"):
            self.threads.find_post("target", "1700000000")
        self.assertEqual(self.threads.opener.open.call_count, 1)

    def test_find_post_rejects_a_repeated_cursor(self):
        self.threads.opener.open.side_effect = [
            _response({"data": [], "paging": {"cursors": {"after": "AFTER_CURSOR"}}}),
            _response({"data": [], "paging": {"cursors": {"after": "AFTER_CURSOR"}}}),
        ]

        with self.assertRaisesRegex(ThreadsError, "repeated pagination cursor"):
            self.threads.find_post("target", "1700000000")
        self.assertEqual(self.threads.opener.open.call_count, 2)

    def test_find_post_fails_closed_when_pagination_never_ends(self):
        self.threads.opener.open.side_effect = [
            _response({"data": [], "paging": {"cursors": {"after": f"cursor-{index}"}}})
            for index in range(20)
        ]

        with self.assertRaisesRegex(ThreadsError, "pagination limit"):
            self.threads.find_post("target", "1700000000")
        self.assertEqual(self.threads.opener.open.call_count, 20)


if __name__ == "__main__":
    unittest.main()
