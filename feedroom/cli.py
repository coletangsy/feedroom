"""Run collection, preview, or a daily Threads publication."""

import argparse
from dataclasses import asdict
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import re
import sqlite3
import sys

from .briefing import select_draft
from .collector import add_arguments, collect_from_args
from .journal import JournalError, LocalJournal, R2Journal
from .publishing import deliver
from .threads import Threads, ThreadsError
from .writer import EditorialError, write_draft


def required_env(name: str) -> str:
    if value := os.environ.get(name, "").strip():
        return value
    raise ValueError(f"Set {name} to publish.")


def open_journal(account_id: str):
    if not os.environ.get("FEEDROOM_R2_BUCKET"):
        return LocalJournal(Path(os.environ.get("FEEDROOM_STATE_DB", "state/threads.sqlite3")), account_id)

    account = required_env("FEEDROOM_R2_ACCOUNT_ID")
    if not re.fullmatch(r"[0-9a-f]{32}", account):
        raise ValueError("FEEDROOM_R2_ACCOUNT_ID must be a Cloudflare account ID.")
    try:
        import boto3
        from botocore.config import Config
    except ImportError:
        raise ValueError("Install feedroom[r2] to use a Cloudflare journal.") from None
    client = boto3.client(
        "s3", endpoint_url=f"https://{account}.r2.cloudflarestorage.com", region_name="auto",
        aws_access_key_id=required_env("FEEDROOM_R2_ACCESS_KEY_ID"),
        aws_secret_access_key=required_env("FEEDROOM_R2_SECRET_ACCESS_KEY"),
        config=Config(connect_timeout=10, read_timeout=20, retries={"total_max_attempts": 2},
                      request_checksum_calculation="when_required", response_checksum_validation="when_required"),
    )
    return R2Journal(client, required_env("FEEDROOM_R2_BUCKET"), account_id)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("collect", "preview", "publish"):
        command = commands.add_parser(name)
        add_arguments(command, replay=name != "publish")
        if name == "preview":
            command.add_argument("--candidates", type=Path, help="Read an existing collector JSON file.")
            command.add_argument("--editorial", action="store_true", help="Write and validate a Cantonese draft through OpenRouter.")
    args = parser.parse_args(argv)

    try:
        if args.command == "collect":
            output = collect_from_args(args)
        elif args.command == "preview":
            result = json.loads(args.candidates.read_text(encoding="utf-8")) if args.candidates else collect_from_args(args)
            write = write_draft if args.editorial else select_draft
            draft = write(result, set())
            output = {"status": "draft" if draft else "collection_incomplete" if result["collection_errors"] else "no_story",
                      "draft": asdict(draft) if draft else None, "collection_errors": result["collection_errors"]}
        else:
            now = datetime.now(UTC)
            threads = Threads(required_env("THREADS_ACCESS_TOKEN"), required_env("THREADS_ACCOUNT_ID"))
            threads.verify_account()
            output = deliver(
                open_journal(threads.account_id), threads,
                lambda published_ids: collect_from_args(args, published_ids=published_ids), now, write=write_draft,
            )
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return int(output.get("status") == "collection_incomplete")
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error, JournalError, ThreadsError, EditorialError) as error:
        print(f"Feedroom {args.command} failed: {error}", file=sys.stderr)
        return 1
