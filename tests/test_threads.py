import io
import json
import socket
import unittest
from unittest.mock import Mock, call, patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

from feedroom.threads import Threads, ThreadsError


def _response(payload):
    return io.BytesIO(json.dumps(payload).encode("utf-8"))


class ThreadsRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.threads = Threads("synthetic", "11")
        self.threads.opener = Mock()

    def test_account_verification_recovers_from_dns_failure(self):
        self.threads.opener.open.side_effect = [
            URLError(socket.gaierror(socket.EAI_AGAIN, "temporary DNS failure")),
            URLError(socket.gaierror(socket.EAI_AGAIN, "temporary DNS failure")),
            _response({"id": "11"}),
        ]
        with patch("feedroom.threads.sleep") as delay:
            self.threads.verify_account()

        self.assertEqual(delay.call_args_list, [call(2), call(5)])
        requests = [attempt.args[0] for attempt in self.threads.opener.open.call_args_list]
        self.assertEqual([(req.method, urlsplit(req.full_url).path) for req in requests], [("GET", "/v1.0/me")] * 3)

    def test_account_verification_stops_after_three_network_failures(self):
        self.threads.opener.open.side_effect = URLError(socket.gaierror(socket.EAI_AGAIN, "temporary DNS failure"))
        with (patch("feedroom.threads.sleep"),
              self.assertRaisesRegex(ThreadsError, "response was unavailable")):
            self.threads.verify_account()
        self.assertEqual(self.threads.opener.open.call_count, 3)

    def test_account_verification_does_not_retry_unauthorized(self):
        self.threads.opener.open.side_effect = HTTPError("https://graph.threads.com/", 401, "Unauthorized", {}, None)
        with self.assertRaisesRegex(ThreadsError, "HTTP 401"):
            self.threads.verify_account()
        self.threads.opener.open.assert_called_once()

    def test_publish_does_not_retry_after_a_lost_response(self):
        self.threads.opener.open.side_effect = TimeoutError("timed out")
        with self.assertRaisesRegex(ThreadsError, "response was unavailable"):
            self.threads.publish("22")
        self.threads.opener.open.assert_called_once()

    def test_reads_the_timestamp_from_the_published_media_id(self):
        self.threads.opener.open.return_value = _response({"timestamp": "2026-11-22T10:05:00+0000"})
        self.assertEqual(self.threads.publication_time("33"), "2026-11-22T10:05:00+00:00")
        request = self.threads.opener.open.call_args.args[0]
        url = urlsplit(request.full_url)
        self.assertEqual((request.method, url.path, parse_qs(url.query)), ("GET", "/v1.0/33", {"fields": ["timestamp"]}))

    def test_create_sends_the_topic_with_no_link_attachment_or_ai_disclosure(self):
        text = "2026-11-20\n• 研究團隊發佈新模型"
        self.threads.opener.open.return_value = _response({"id": "22"})

        self.assertEqual(self.threads.create(text, topic_tag="AI Threads"), "22")

        request = self.threads.opener.open.call_args.args[0]
        self.assertEqual(parse_qs(request.data.decode()), {
            "media_type": ["TEXT"], "text": [text], "topic_tag": ["AI Threads"],
        })

    def test_create_image_sends_card_url_and_alt_text(self):
        url = "https://example.org/01.png?token=fixture&expiry=900"
        self.threads.opener.open.return_value = _response({"id": "22"})
        self.assertEqual(self.threads.create_image(url, alt_text="圖卡標題", text="摘要", topic_tag="AI Threads"), "22")
        self.assertEqual(parse_qs(self.threads.opener.open.call_args.args[0].data.decode()), {
            "media_type": ["IMAGE"], "image_url": [url], "alt_text": ["圖卡標題"],
            "text": ["摘要"], "topic_tag": ["AI Threads"],
        })

    def test_create_carousel_sends_ordered_children_and_topic(self):
        self.threads.opener.open.return_value = _response({"id": "22"})
        self.assertEqual(self.threads.create_carousel("摘要", ("54", "13"), topic_tag="AI Threads"), "22")
        self.assertEqual(parse_qs(self.threads.opener.open.call_args.args[0].data.decode()), {
            "media_type": ["CAROUSEL"], "text": ["摘要"], "children": ["54,13"], "topic_tag": ["AI Threads"],
        })

    def test_find_post_matches_media_type_when_recovering_card_post(self):
        self.threads.opener.open.return_value = _response({"data": [
            {"id": "20", "text": "摘要", "media_type": "TEXT_POST"},
            {"id": "33", "text": "摘要", "media_type": "CAROUSEL_ALBUM"},
        ]})
        self.assertEqual(self.threads.find_post("摘要", "1700000000", media_type="CAROUSEL_ALBUM"), "33")

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
