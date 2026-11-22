import hashlib
import struct
import tempfile
import unittest
from pathlib import Path

from feedroom.media import MediaError, R2Media


PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def png_fixture(width=1080, height=1350, marker=b""):
    """Build the small PNG header the first-party asset validator needs."""

    ihdr = struct.pack(">II", width, height) + b"\x08\x06\x00\x00\x00"
    return PNG_SIGNATURE + struct.pack(">I", len(ihdr)) + b"IHDR" + ihdr + marker


class FakeR2Client:
    def __init__(self, *, put_error=None):
        self.put_error = put_error
        self.puts = []
        self.presigns = []

    def put_object(self, **kwargs):
        if self.put_error is not None:
            raise self.put_error
        self.puts.append(kwargs)

    def generate_presigned_url(self, method, *, Params, ExpiresIn):
        self.presigns.append((method, Params, ExpiresIn))
        return f"https://r2.example/{Params['Key']}?expires={ExpiresIn}"


class R2MediaTests(unittest.TestCase):
    def test_uploads_ordered_immutable_cards_and_returns_one_hour_urls(self):
        with tempfile.TemporaryDirectory() as temporary:
            first = Path(temporary) / "first.png"
            second = Path(temporary) / "second.png"
            first_bytes = png_fixture(marker=b"first")
            second_bytes = png_fixture(marker=b"second")
            first.write_bytes(first_bytes)
            second.write_bytes(second_bytes)
            client = FakeR2Client()

            urls = R2Media(client, "fixture-bucket").upload(
                "2026-11-22", (first, second)
            )

            first_hash = hashlib.sha256(first_bytes).hexdigest()
            second_hash = hashlib.sha256(second_bytes).hexdigest()
            expected_keys = (
                f"threads/2026-11-22/01-{first_hash}.png",
                f"threads/2026-11-22/02-{second_hash}.png",
            )
            self.assertEqual(
                urls,
                tuple(f"https://r2.example/{key}?expires=3600" for key in expected_keys),
            )
            self.assertEqual(
                [(item["Key"], item["Body"], item["ContentType"]) for item in client.puts],
                [
                    (expected_keys[0], first_bytes, "image/png"),
                    (expected_keys[1], second_bytes, "image/png"),
                ],
            )
            self.assertEqual(
                client.presigns,
                [
                    (
                        "get_object",
                        {"Bucket": "fixture-bucket", "Key": expected_keys[0]},
                        3600,
                    ),
                    (
                        "get_object",
                        {"Bucket": "fixture-bucket", "Key": expected_keys[1]},
                        3600,
                    ),
                ],
            )

    def test_rejects_noncanonical_or_impossible_dates_before_writing(self):
        with tempfile.TemporaryDirectory() as temporary:
            card = Path(temporary) / "01.png"
            card.write_bytes(png_fixture())
            client = FakeR2Client()

            for day in ("2026-1-22", "2026-02-30", " 2026-11-22"):
                with self.subTest(day=day):
                    with self.assertRaises(MediaError):
                        R2Media(client, "fixture-bucket").upload(day, (card,))
                    self.assertEqual(client.puts, [])

    def test_rejects_card_count_and_supported_png_size_before_writing(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            cards = []
            for index in range(7):
                card = directory / f"{index:02d}.png"
                card.write_bytes(png_fixture(marker=str(index).encode()))
                cards.append(card)
            client = FakeR2Client()

            for paths in ((), tuple(cards)):
                with self.subTest(count=len(paths)):
                    with self.assertRaises(MediaError):
                        R2Media(client, "fixture-bucket").upload("2026-11-22", paths)
                    self.assertEqual(client.puts, [])

            wrong_size = directory / "wrong-size.png"
            wrong_size.write_bytes(png_fixture(width=1441))
            with self.assertRaises(MediaError):
                R2Media(client, "fixture-bucket").upload("2026-11-22", (wrong_size,))
            self.assertEqual(client.puts, [])

    def test_validates_all_files_before_the_first_external_write(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            valid = directory / "01.png"
            invalid = directory / "02.jpg"
            valid.write_bytes(png_fixture())
            invalid.write_bytes(png_fixture())
            client = FakeR2Client()

            with self.assertRaises(MediaError):
                R2Media(client, "fixture-bucket").upload("2026-11-22", (valid, invalid))

            self.assertEqual(client.puts, [])
            self.assertEqual(client.presigns, [])

    def test_sanitizes_client_errors_as_media_error(self):
        with tempfile.TemporaryDirectory() as temporary:
            card = Path(temporary) / "01.png"
            card.write_bytes(png_fixture())
            client = FakeR2Client(put_error=RuntimeError("secret-access-key=fixture"))

            with self.assertRaises(MediaError) as raised:
                R2Media(client, "fixture-bucket").upload("2026-11-22", (card,))

            self.assertNotIn("secret-access-key", str(raised.exception))
            self.assertEqual(client.presigns, [])


if __name__ == "__main__":
    unittest.main()
