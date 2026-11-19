"""Send explicit two-step text publications to the configured Threads account."""

import json
import re
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, build_opener

from .http import NoRedirect


class ThreadsError(RuntimeError):
    pass


def threads_id(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,40}", value):
        raise ThreadsError("Expected a numeric Threads ID.")
    return value


class Threads:
    def __init__(self, token: str, account_id: str):
        self.token = token
        self.account_id = threads_id(account_id)
        self.opener = build_opener(NoRedirect())

    def request(self, path: str, fields: dict[str, str], *, method: str = "GET") -> dict:
        url = f"https://graph.threads.com/v1.0/{path}"
        encoded = urlencode(fields)
        headers = {"Authorization": f"Bearer {self.token}", "Accept": "application/json"}
        data = None
        if method == "GET":
            url += f"?{encoded}"
        else:
            data = encoded.encode("utf-8")
        try:
            with self.opener.open(Request(url, data, headers, method=method), timeout=20) as response:
                payload = json.load(response)
        except HTTPError as error:
            raise ThreadsError(f"Threads API returned HTTP {error.code}.") from None
        except (OSError, URLError, ValueError):
            raise ThreadsError("Threads API response was unavailable or invalid.") from None
        if not isinstance(payload, dict) or "error" in payload:
            raise ThreadsError("Threads API returned an invalid response.")
        return payload

    def verify_account(self) -> None:
        actual_id = threads_id(self.request("me", {"fields": "id"}).get("id"))
        if actual_id != self.account_id:
            raise ThreadsError("Threads access token does not belong to the configured account.")

    def create(self, text: str, *, made_with_ai: bool = False) -> str:
        fields = {"media_type": "TEXT", "text": text}
        if made_with_ai:
            fields["is_made_with_ai"] = "true"
        result = self.request(f"{self.account_id}/threads", fields, method="POST")
        return threads_id(result.get("id"))

    def publish(self, container_id: str) -> str:
        result = self.request(f"{self.account_id}/threads_publish", {"creation_id": threads_id(container_id)}, method="POST")
        return threads_id(result.get("id"))

    def status(self, container_id: str) -> str:
        return self.request(threads_id(container_id), {"fields": "status"}).get("status", "")

    def find_post(self, text: str, since: str) -> str:
        result = self.request(f"{self.account_id}/threads", {"fields": "id,text", "since": since, "limit": "25"})
        matches = [post for post in result.get("data", []) if post.get("text") == text]
        if len(matches) != 1:
            raise ThreadsError("Container is published, but the original post could not be identified uniquely.")
        return threads_id(matches[0].get("id"))
