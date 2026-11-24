"""Collect sources, preview, or publish Feedroom posts for Threads."""

import argparse
from dataclasses import asdict
from datetime import UTC, datetime
from functools import partial
import json
from math import ceil
import os
from pathlib import Path
import re
import sqlite3
import sys

from .briefing import HKT, CardDraft, Draft, select_draft
from .card_render import render_cards
from .collector import add_arguments, collect, collect_from_args, parse_date
from .journal import JournalError, LocalJournal, R2Journal
from .media import MediaError, R2Media
from .paper import load_reading, save_reading, select_paper, write_paper
from .publishing import deliver, used_story_keys
from .threads import Threads, ThreadsError, threads_id
from .weekly import daily_posts, daily_story_references, weekly_groups, weekly_window
from .weekly_writer import write_weekly_draft
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


def preview_output(result: dict, draft: Draft | None, cards_dir: Path | None, *, weekly: bool = False) -> dict:
    output = {"status": "draft" if draft else "collection_incomplete" if result["collection_errors"] else "no_story",
              "draft": asdict(draft) if draft else None, "collection_errors": result["collection_errors"]}
    if cards_dir is not None:
        images = render_cards(draft.cards, cards_dir, weekly=weekly) if isinstance(draft, CardDraft) else ()
        output["card_images"] = [str(image) for image in images]
    return output


def weekly_preview(args: argparse.Namespace) -> dict:
    start, cutoff = weekly_window(args.as_of)
    account = (args.account or "").strip()
    if not account:
        raise ValueError("Specify --account or set THREADS_ACCOUNT_ID to read the daily journal.")
    posts = daily_posts(args.daily_journal, account)
    listed_daily_links = None
    if args.candidates:
        result = json.loads(args.candidates.read_text(encoding="utf-8"))
    else:
        hours = ceil((cutoff - start).total_seconds() / 3600)
        keys, _ = daily_story_references(posts, start, cutoff)
        result = collect(None, hours, 150, reading_lookback_days=7, include_aggregators=False, as_of=cutoff,
                         week_start=start, daily_keys=keys)
        listed_daily_links = result["listed_daily_links"]
    groups = weekly_groups(result["candidates"], posts, start, cutoff, listed_daily_links=listed_daily_links)
    draft = write_weekly_draft(groups, start=start, cutoff=cutoff)
    output = preview_output(result, draft, args.cards_dir, weekly=True)
    output["coverage"] = {"start": start.isoformat(), "cutoff": cutoff.isoformat()}
    return output


def reading_path(account: str) -> Path:
    state = Path(os.environ.get("FEEDROOM_STATE_DB", "state/threads.sqlite3"))
    return state.with_name(f"reading-{threads_id(account)}.json")


def collect_daily(args: argparse.Namespace, path: Path, used: set[str]) -> dict:
    result = collect_from_args(args, published_ids=used)
    try:
        save_reading(path, result)
    except OSError as error:
        print(f"Could not save the reading shortlist: {error}", file=sys.stderr)
    return result


def paper_input(now: datetime, account: str, used: set[str], provided: dict | None = None) -> dict:
    cached = provided
    if cached is None and account:
        cached = load_reading(reading_path(account), now)
    return select_paper(cached, lambda ids: collect(None, 0, 8, as_of=now, published_ids=ids, reading_only=True),
                        used, provided=provided is not None)


def paper_preview_history(account: str) -> dict:
    if not account:
        return {}
    account = threads_id(account)
    if os.environ.get("FEEDROOM_R2_BUCKET"):
        posts, _ = open_journal(account).read()
        return posts
    path = Path(os.environ.get("FEEDROOM_STATE_DB", "state/threads.sqlite3"))
    return daily_posts(path, account) if path.is_file() else {}


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
    weekly = commands.add_parser("weekly-preview", help="Preview a Cantonese weekly digest and optional PNG cards.")
    weekly.add_argument("--as-of", required=True, help="Cutoff as an ISO timestamp with a time zone.")
    weekly.add_argument("--account", default=os.environ.get("THREADS_ACCOUNT_ID"), help="Account in the daily SQLite journal.")
    weekly.add_argument("--daily-journal", type=Path, default=Path(os.environ.get("FEEDROOM_STATE_DB", "state/threads.sqlite3")),
                        help="Read the daily SQLite journal without updating it.")
    weekly.add_argument("--candidates", type=Path, help="Read an existing collector JSON file.")
    weekly.add_argument("--cards-dir", type=Path, help="Render weekly PNGs into a new directory.")
    paper = commands.add_parser("paper-preview", help="Preview today's Cantonese paper post.")
    paper.add_argument("--as-of", help="Preview as of an ISO date or timestamp.")
    paper.add_argument("--candidates", type=Path, help="Read a collector JSON file instead of collecting papers.")
    commands.add_parser("paper-publish", help="Publish the afternoon paper post to the configured Threads account.")
    args = parser.parse_args(argv)

    try:
        if args.command in {"preview", "weekly-preview"} and args.cards_dir is not None and os.path.lexists(args.cards_dir):
            raise FileExistsError(f"Card output directory already exists: {args.cards_dir}")
        if args.command == "collect":
            output = collect_from_args(args)
        elif args.command == "preview":
            result = json.loads(args.candidates.read_text(encoding="utf-8")) if args.candidates else collect_from_args(args)
            write = write_draft if args.editorial else select_draft
            draft = write(result, set(), include_cards=args.cards_dir is not None)
            output = preview_output(result, draft, args.cards_dir)
        elif args.command == "weekly-preview":
            output = weekly_preview(args)
        elif args.command == "paper-preview":
            provided = json.loads(args.candidates.read_text(encoding="utf-8")) if args.candidates else None
            if args.candidates and not isinstance(provided, dict):
                raise ValueError("--candidates must contain a collector JSON object.")
            now = parse_date(args.as_of or (provided or {}).get("collected_at"))
            if args.as_of and now is None:
                raise ValueError("--as-of must be an ISO date or timestamp.")
            now = now or datetime.now(UTC)
            account = os.environ.get("THREADS_ACCOUNT_ID", "").strip()
            day = now.astimezone(HKT).date().isoformat()
            used = used_story_keys(paper_preview_history(account), day)
            result = paper_input(now, account, used, provided)
            draft = write_paper(result, used, day=day)
            output = preview_output(result, draft, None)
            output["candidate_source"] = result["candidate_source"]
        else:
            now = datetime.now(UTC)
            threads = Threads(required_env("THREADS_ACCESS_TOKEN"), required_env("THREADS_ACCOUNT_ID"))
            threads.verify_account()
            media = open_media() if args.command == "publish" and args.with_cards else None
            journal = open_journal(threads.account_id)
            if args.command == "paper-publish":
                output = deliver(journal, threads, lambda used: paper_input(now, threads.account_id, used),
                                 now, write=write_paper, slot="paper")
            else:
                output = deliver(journal, threads, partial(collect_daily, args, reading_path(threads.account_id)), now,
                                 write=partial(write_draft, include_cards=True) if media is not None else write_draft,
                                 media=media)
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return int(output.get("status") == "collection_incomplete")
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error, JournalError, MediaError, ThreadsError, EditorialError) as error:
        print(f"Feedroom {args.command} failed: {error}", file=sys.stderr)
        return 1
