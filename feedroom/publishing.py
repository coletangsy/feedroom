"""Coordinate a single public post per account and Hong Kong run day."""

from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from time import sleep

from .briefing import HKT, Card
from .card_render import render_cards
from .threads import ThreadsError


def wait_for_containers(threads, *container_ids: str) -> str:
    # Give Threads time to process the upload, then poll at most once per minute.
    for delay in (30, 60, 60, 60, 60):
        sleep(delay)
        pending = []
        for container_id in container_ids:
            status = threads.status(container_id)
            if status == "IN_PROGRESS":
                pending.append(container_id)
            elif status != "FINISHED":
                return status
        if not pending:
            return "FINISHED"
        container_ids = tuple(pending)
    return "IN_PROGRESS"


def card_alt_text(card: Card) -> str:
    return "\n".join(filter(None, (card.headline, card.summary, card.source_name)))


def prepare_card_post(journal, threads, media, day: str, posts: dict, revision: str | None) -> None:
    post = posts[day]
    cards = tuple(Card(**card) for card in post["cards"])
    children = post.get("card_children", [])
    if len(children) > len(cards):
        raise ThreadsError(f"The saved image containers do not match the card draft for {day}.")
    urls = ()
    if len(cards) == 1 or len(children) < len(cards):
        with TemporaryDirectory() as temp:
            paths = render_cards(cards, Path(temp) / "cards")
            urls = media.upload(day, paths)

    if len(cards) == 1:
        container_id = threads.create_image(
            urls[0], alt_text=card_alt_text(cards[0]), text=post["text"], topic_tag="AI Threads",
        )
    else:
        # Keep acknowledged children so retries only create missing items.
        start = len(children)
        for card, url in zip(cards[start:], urls[start:], strict=True):
            child = threads.create_image(url, alt_text=card_alt_text(card), carousel_item=True)
            children = [*children, child]
            updated = {**post, "card_children": children}
            if not journal.write({**posts, day: updated}, revision):
                return
            posts, revision = journal.read()
            if posts.get(day) != updated:
                return
            post = updated

        status = wait_for_containers(threads, *children)
        if status in {"ERROR", "EXPIRED"}:
            if journal.write({**posts, day: {**post, "card_children": []}}, revision):
                raise ThreadsError(f"Image containers for {day} ended in {status}. Retry to recreate them.")
            return
        if status != "FINISHED":
            raise ThreadsError(f"Image containers for {day} are not ready to publish.")
        if journal.read()[1] != revision:
            return
        container_id = threads.create_carousel(post["text"], tuple(children), topic_tag="AI Threads")

    ready = {**post, "status": "ready", "container_id": container_id, "link_attachment": None}
    journal.write({**posts, day: ready}, revision)


def deliver(journal, threads, collect, now: datetime, *, write, media=None) -> dict:
    day = now.astimezone(HKT).date().isoformat()
    result = None
    draft = None
    written_for = None
    for _ in range(12):
        posts, revision = journal.read()
        pending = next((key for key, post in sorted(posts.items()) if post["status"] == "publishing"), None)
        if pending is not None:
            post = posts[pending]
            status = threads.status(post["container_id"])
            if status == "PUBLISHED":
                # Older containers used the lead source URL as their link attachment.
                attachment = post.get("link_attachment", post.get("source_url"))
                media_match = {}
                if cards := post.get("cards"):
                    media_match["media_type"] = "IMAGE" if len(cards) == 1 else "CAROUSEL_ALBUM"
                post_id = threads.find_post(post["text"], post["prepared_at"], link_attachment=attachment, **media_match)
                if journal.write({**posts, pending: {**post, "status": "published", "post_id": post_id}}, revision):
                    return {"status": "reconciled", "day": pending, "post_id": post_id}
            elif status in {"ERROR", "EXPIRED"}:
                if journal.write({**posts, pending: {**post, "status": "failed", "reason": status}}, revision):
                    raise ThreadsError(f"Threads container for {pending} ended in {status}.")
            else:
                raise ThreadsError(f"Publication for {pending} is unresolved. The existing container must be checked before sending again.")
            continue

        abandoned = {key: {**post, "status": "failed", "reason": "run_day_ended"}
                     for key, post in posts.items() if key < day and post["status"] in {"claimed", "ready"}}
        if abandoned:
            journal.write({**posts, **abandoned}, revision)
            continue

        post = posts.get(day)
        if post and post["status"] in {"claimed", "ready"}:
            if post.get("cards") and media is None:
                raise ThreadsError(f"Resume the card draft for {day} with publish --with-cards.")
            if not post.get("cards") and media is not None:
                raise ThreadsError(f"A text draft already exists for {day}. Resolve it before publishing cards.")
        if post is None:
            used = {key for post in posts.values() if post["status"] != "failed"
                    for key in (post["candidate_id"], post["title_id"], *post.get("related_ids", []))}
            if used != written_for:
                result = collect(used)
                draft = write(result, used, day=day)
                written_for = used
            if draft is None:
                status = "collection_incomplete" if result["collection_errors"] else "no_story"
                return {"status": status, "day": day, "collection_errors": result["collection_errors"]}
            post = {**asdict(draft), "status": "claimed", "prepared_at": str(int(now.timestamp()))}
            journal.write({**posts, day: post}, revision)
        elif post["status"] == "claimed":
            # Concurrent starts can create extra containers. Only the saved one can be published.
            if post.get("cards"):
                prepare_card_post(journal, threads, media, day, posts, revision)
                continue
            container_id = threads.create(post["text"], topic_tag="AI Threads")
            ready = {**post, "status": "ready", "container_id": container_id, "link_attachment": None}
            journal.write({**posts, day: ready}, revision)
        elif post["status"] == "ready":
            container_id = post["container_id"]
            status = wait_for_containers(threads, container_id)
            if status in {"ERROR", "EXPIRED"}:
                if journal.write({**posts, day: {**post, "status": "failed", "reason": status}}, revision):
                    raise ThreadsError(f"Threads container for {day} ended in {status}.")
                continue
            if status != "FINISHED":
                if journal.read()[1] != revision:
                    continue
                raise ThreadsError(f"Threads container for {day} is not ready to publish.")
            if not journal.write({**posts, day: {**post, "status": "publishing"}}, revision):
                continue
            post_id = threads.publish(container_id)
            for _ in range(12):
                posts, revision = journal.read()
                saved = posts[day]
                if saved["status"] == "published" and saved["post_id"] == post_id:
                    break
                if saved["status"] != "publishing":
                    raise ThreadsError(f"Journal for {day} changed while Threads was publishing.")
                if journal.write({**posts, day: {**saved, "status": "published", "post_id": post_id}}, revision):
                    break
            else:
                raise ThreadsError(f"Could not save the published post ID for {day}.")
            return {"status": "published", "day": day, "post_id": post_id,
                    "collection_errors": result["collection_errors"] if result else []}
        elif post["status"] == "published":
            return {"status": "already_published", "day": day, "post_id": post["post_id"]}
        elif post["status"] == "failed":
            raise ThreadsError(f"Publication for {day} was marked failed in the journal.")
        else:
            raise ThreadsError(f"Unknown publishing state for {day}.")
    raise ThreadsError(f"Could not update the publishing journal for {day}.")
