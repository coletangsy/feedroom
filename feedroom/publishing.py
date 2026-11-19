"""Coordinate a single public post per account and Hong Kong run day."""

from dataclasses import asdict
from datetime import datetime
from time import sleep

from .briefing import HKT
from .threads import ThreadsError


def wait_for_container(threads, container_id: str) -> str:
    # Give Threads time to process the upload, then poll at most once per minute.
    for delay in (30, 60, 60, 60, 60):
        sleep(delay)
        status = threads.status(container_id)
        if status != "IN_PROGRESS":
            return status
    return "IN_PROGRESS"


def deliver(journal, threads, collect, now: datetime, *, write) -> dict:
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
                post_id = threads.find_post(post["text"], post["prepared_at"], link_attachment=post.get("source_url"))
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
            container_id = threads.create(
                post["text"], made_with_ai=post.get("made_with_ai", False), link_attachment=post.get("source_url"),
            )
            journal.write({**posts, day: {**post, "status": "ready", "container_id": container_id}}, revision)
        elif post["status"] == "ready":
            container_id = post["container_id"]
            status = wait_for_container(threads, container_id)
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
