"""Store rendered Threads cards in a private R2 bucket."""

from __future__ import annotations

from datetime import date
import hashlib
from pathlib import Path
import struct

from .briefing import MAX_HEADLINES
from .card_render import CARD_HEIGHT, CARD_WIDTH


_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_PNG_HEADER_SIZE = 24
_MAX_BYTES = 8 * 1024 * 1024
_URL_EXPIRY_SECONDS = 60 * 60


class MediaError(RuntimeError):
    """Raised when a card batch cannot be validated or stored safely."""


class R2Media:
    """Upload a validated batch of rendered PNG cards to an S3-compatible R2 client."""

    def __init__(self, client, bucket: str):
        self.client = client
        self.bucket = bucket

    def upload(self, day: str, paths: tuple[Path, ...]) -> tuple[str, ...]:
        """Upload cards and return private-object URLs signed for one hour.

        Card keys include the ordered position and complete SHA-256 content
        digest. A changed render therefore gets a new object key, while a
        retry of the same render addresses the same immutable content.
        """

        cards = self._validate(day, paths)
        try:
            urls = []
            for index, (content, digest) in enumerate(cards, 1):
                key = f"threads/{day}/{index:02d}-{digest}.png"
                self.client.put_object(
                    Bucket=self.bucket,
                    Key=key,
                    Body=content,
                    ContentType="image/png",
                )
                url = self.client.generate_presigned_url(
                    "get_object",
                    Params={"Bucket": self.bucket, "Key": key},
                    ExpiresIn=_URL_EXPIRY_SECONDS,
                )
                if not isinstance(url, str) or not url:
                    raise RuntimeError("R2 returned an invalid signed URL.")
                urls.append(url)
            return tuple(urls)
        except Exception:
            # Do not expose boto or credential-bearing endpoint details.
            raise MediaError("Could not store media cards.") from None

    @staticmethod
    def _validate(day: str, paths: tuple[Path, ...]) -> tuple[tuple[bytes, str], ...]:
        try:
            canonical_day = date.fromisoformat(day).isoformat()
        except (TypeError, ValueError):
            raise MediaError("The run day must be a valid YYYY-MM-DD date.") from None
        if day != canonical_day:
            raise MediaError("The run day must use the canonical YYYY-MM-DD format.")

        if not 1 <= len(paths) <= MAX_HEADLINES:
            raise MediaError("Provide between one and six PNG cards.")

        cards = []
        for path in paths:
            if path.suffix.lower() != ".png":
                raise MediaError("Media cards must be PNG files.")
            try:
                if not path.is_file():
                    raise MediaError("A media card is unavailable.")
                with path.open("rb") as stream:
                    content = stream.read(_MAX_BYTES + 1)
            except (OSError, ValueError):
                raise MediaError("A media card is unavailable.") from None

            if len(content) > _MAX_BYTES or not _supported_png(content):
                raise MediaError("Media cards must be 1080 × 1350 PNG files up to 8 MiB.")
            cards.append((content, hashlib.sha256(content).hexdigest()))
        return tuple(cards)


def _supported_png(content: bytes) -> bool:
    """Check the signature and dimensions of a first-party rendered PNG."""

    if len(content) < _PNG_HEADER_SIZE or not content.startswith(_PNG_SIGNATURE):
        return False
    if content[12:16] != b"IHDR":
        return False
    width, height = struct.unpack(">II", content[16:24])
    return (width, height) == (CARD_WIDTH, CARD_HEIGHT)
