"""Collect sources, preview, or publish the daily Threads headline digest."""

import argparse
from dataclasses import asdict
from datetime import UTC, datetime
from functools import partial
import json
import os
from pathlib import Path
import re
import sqlite3
import sys

from .briefing import CardDraft, select_draft
from .card_render import render_cards
from .collector import add_arguments, collect_from_args
from .journal import JournalError, LocalJournal, R2Journal
from .media import MediaError, R2Media
from .publishing import deliver
from .threads import Threads, ThreadsError
from .writer import EditorialError, write_draft


def required_env(name: str) -> str:
    if value := os.environ.get(name, "").strip():
        return value
    raise ValueError(f"Set {name} to publish.")


def r2_client(prefix: str):
    account_var = f"{prefix}_ACCOUNT_ID"
    account = required_env(account_var)
    if not re.fullmatch(r"[0-9a-f]{32}", account):
        raise ValueError(f"{account_var} must be a Cloudflare account ID.")
    try:
        import boto3
        from botocore.config import Config
    except ImportError:
        raise ValueError("Install feedroom[r2] to use Cloudflare R2.") from None
    return boto3.client(
        "s3", endpoint_url=f"https://{account}.r2.cloudflarestorage.com", region_name="auto",
        aws_access_key_id=required_env(f"{prefix}_ACCESS_KEY_ID"),
        aws_secret_access_key=required_env(f"{prefix}_SECRET_ACCESS_KEY"),
        config=Config(connect_timeout=10, read_timeout=20, retries={"total_max_attempts": 2},
                      signature_version="s3v4",
                      request_checksum_calculation="when_required", response_checksum_validation="when_required"),
    )


def open_journal(account_id: str):
    if not os.environ.get("FEEDROOM_R2_BUCKET"):
        return LocalJournal(Path(os.environ.get("FEEDROOM_STATE_DB", "state/threads.sqlite3")), account_id)
    return R2Journal(r2_client("FEEDROOM_R2"), required_env("FEEDROOM_R2_BUCKET"), account_id)


def open_media() -> R2Media:
    bucket = required_env("FEEDROOM_MEDIA_R2_BUCKET")
    if bucket == os.environ.get("FEEDROOM_R2_BUCKET", "").strip():
        raise ValueError("Use a separate R2 bucket for media and the publishing journal.")
    return R2Media(r2_client("FEEDROOM_MEDIA_R2"), bucket)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("collect", "preview", "publish"):
        command = commands.add_parser(name)
        add_arguments(command, replay=name != "publish")
        if name == "preview":
            command.add_argument("--cards-dir", type=Path, help="Render one PNG per selected headline into a new directory.")
            command.add_argument("--candidates", type=Path, help="Read an existing collector JSON file.")
            command.add_argument("--editorial", action="store_true", help="Write and validate a Cantonese headline digest through OpenRouter.")
        if name == "publish":
            command.add_argument("--with-cards", action="store_true", help="Render and publish news cards via a private R2 media bucket.")
    args = parser.parse_args(argv)

    try:
        if args.command == "collect":
            output = collect_from_args(args)
        elif args.command == "preview":
            if args.cards_dir is not None and os.path.lexists(args.cards_dir):
                raise FileExistsError(f"Card output directory already exists: {args.cards_dir}")
            result = json.loads(args.candidates.read_text(encoding="utf-8")) if args.candidates else collect_from_args(args)
            write = write_draft if args.editorial else select_draft
            draft = write(result, set(), include_cards=args.cards_dir is not None)
            output = {"status": "draft" if draft else "collection_incomplete" if result["collection_errors"] else "no_story",
                      "draft": asdict(draft) if draft else None, "collection_errors": result["collection_errors"]}
            if args.cards_dir is not None:
                images = render_cards(draft.cards, args.cards_dir) if isinstance(draft, CardDraft) else ()
                output["card_images"] = [str(image) for image in images]
        else:
            now = datetime.now(UTC)
            threads = Threads(required_env("THREADS_ACCESS_TOKEN"), required_env("THREADS_ACCOUNT_ID"))
            threads.verify_account()
            media = open_media() if args.with_cards else None
            output = deliver(
                open_journal(threads.account_id), threads,
                lambda published_ids: collect_from_args(args, published_ids=published_ids), now,
                write=partial(write_draft, include_cards=True) if media is not None else write_draft,
                media=media,
            )
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return int(output.get("status") == "collection_incomplete")
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error, JournalError, MediaError, ThreadsError, EditorialError) as error:
        print(f"Feedroom {args.command} failed: {error}", file=sys.stderr)
        return 1
