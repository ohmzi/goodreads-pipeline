"""acquire — get the ebook and/or the audiobook from Shelfmark.

Each acquire stage is a two-phase state machine, because queueing and
finishing are separated by minutes:

  1. not queued   -> search, rank, hand the best release to Shelfmark
  2. queued       -> watch the task; finish when Shelfmark says complete

Three things make this more than a simple poll, all learned from watching it
run against the real services:

  * The "already queued" marker lives in `stage_runs.queued_at`. Without it,
    every scheduler sweep would queue another copy of the same book — which is
    exactly the duplication we are trying to avoid, only on the download side.
  * **Shelfmark does not persist its queue.** It lives in memory, so a
    container restart silently drops every queued task. Rather than waiting an
    hour for the abandon timeout, we notice that the whole queue is empty and
    re-queue immediately.
  * **A failed release is not a failed book.** If Shelfmark errors on the
    release we picked, the next-best one usually works, so the stages record
    what they have already tried and move down the list.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from .. import models
from ..clients.base import ClientError
from ..clients.shelfmark import (
    AUDIOBOOK,
    EBOOK,
    TASK_ACTIVE,
    TASK_FAILED,
    TASK_OK,
    ShelfmarkClient,
)
from ..config import settings
from ..db import db
from ..pathing import EBOOK_EXTS, AUDIO_EXTS, find_matching_entries, free_space_gb

# How long to wait before deciding a queued entry is gone. This only applies
# when Shelfmark has *no* matching task at all — a task that is genuinely
# queued is found by id or title and watches normally, however long it waits
# behind the concurrency limit.
#
# It used to be an hour, on the theory that a queue entry could simply be slow.
# Then a Shelfmark restart dropped 294 entries and goodreads sat on stale
# markers for the full hour: the earlier check only fired when Shelfmark's
# queue was *entirely* empty, which it rarely is. Ten minutes of a missing task
# is conclusive, and re-queueing is cheap and idempotent.
QUEUE_ABANDON_SECONDS = 600

#: How many different releases to try for one book before giving up. A dead
#: NZB is common; the same book usually has several working copies.
MAX_RELEASE_ATTEMPTS = 4

#: How long an upstream outage is forgiven before a book is written off.
#:
#: This is a *time* budget, not a count. Retrying is cheap — a failed search
#: costs a couple of seconds — so a count budget is exhausted in minutes by an
#: outage that lasts hours, and books would be marked permanently failed before
#: the source ever came back. What actually distinguishes "temporarily down"
#: from "genuinely broken" is how long it has been down.
MAX_TRANSIENT_HOURS = 24


def run_ebook(book: dict) -> models.StageResult:
    return _run(book, "acquire_ebook", EBOOK, settings.staging_dir, EBOOK_EXTS)


def run_audiobook(book: dict) -> models.StageResult:
    return _run(book, "acquire_audiobook", AUDIOBOOK, settings.audiobooks_root, AUDIO_EXTS)


def _run(
    book: dict,
    stage: str,
    content_type: str,
    output_root,
    extensions: set[str],
) -> models.StageResult:
    book_id = book["id"]
    title = book["title"]
    author = book.get("author") or ""
    row = db().stage(book_id, stage) or {}

    client = ShelfmarkClient()
    try:
        if row.get("queued_at"):
            return _watch(client, book, stage, content_type, output_root, extensions)
        return _queue(client, book, stage, content_type)
    except ClientError as exc:
        # A 5xx from the *search* is almost always "the sources are all busy or
        # briefly unreachable right now", not a fact about this book. Shelfmark
        # answers `503 every indexer failed` routinely, and marking 77 books
        # failed for it — when the identical search succeeds moments later —
        # filled the attention list with books that needed no attention.
        if exc.status is not None and exc.status >= 500:
            count, since = db().bump_transient(book_id, stage)
            down_for = db().transient_age_hours(since)
            if down_for < MAX_TRANSIENT_HOURS:
                return models.StageResult.blocked(
                    f"the sources are unavailable ({str(exc)[:60]}) — down for "
                    f"{down_for:.1f}h of {MAX_TRANSIENT_HOURS}h before this counts "
                    f"as a real failure ({count} attempts so far)",
                    service=exc.service,
                )
            return models.StageResult.failed(
                f"{exc} — the sources have been unavailable for {down_for:.0f} "
                f"hours, so this is not a passing outage",
                kind="server",
                service=exc.service,
            )
        return models.StageResult.failed(
            str(exc), kind=exc.kind, service=exc.service
        )
    finally:
        client.close()


def _tried_releases(book_id: int, stage: str) -> list[str]:
    """Source ids already attempted for this book."""
    row = db().stage(book_id, stage) or {}
    raw = row.get("output_path") or ""
    if not raw:
        return []
    try:
        value = json.loads(raw)
        return [str(v) for v in value] if isinstance(value, list) else []
    except ValueError:
        return []


def _remember_release(book_id: int, stage: str, source_id: str, tried: list[str]) -> None:
    tried = tried + [source_id]
    db().set_output(book_id, stage, json.dumps(tried[-MAX_RELEASE_ATTEMPTS:]))


def _queue(
    client: ShelfmarkClient, book: dict, stage: str, content_type: str
) -> models.StageResult:
    title = book["title"]
    author = book.get("author") or ""
    book_id = book["id"]

    if content_type == AUDIOBOOK:
        free = free_space_gb(settings.audiobooks_root)
        if free < settings.min_free_space_gb:
            return models.StageResult.failed(
                f"only {free:.1f} GB free where audiobooks land; floor is "
                f"{settings.min_free_space_gb} GB. Free space before retrying.",
                kind="data",
            )

    releases = client.search(title, author, content_type)
    if not releases:
        # Reaching the sources worked, so this is a fact about availability,
        # not a transient fault — reset the forgiveness counter.
        db().reset_transient(book_id, stage)
        # Say which of the two it was: nothing found at all, or only releases
        # that are plainly the wrong format. "No releases found" when thirty
        # films were rejected reads as a search bug rather than a fact.
        rejected = getattr(client, "last_rejections", None) or []
        if rejected:
            sample = "; ".join(rejected[:2])
            return models.StageResult.failed(
                f"found {len(rejected)} release(s) for '{title[:50]}' but every one "
                f"was the wrong format — not a book. E.g. {sample}",
                kind="data",
            )
        return models.StageResult.failed(
            f"no {content_type} releases found for '{title}'"
            + (f" by {author}" if author else ""),
            # Nothing is broken here — the release simply does not exist in any
            # configured source. Marking it "data" keeps it out of the pile of
            # failures that need a human.
            kind="data",
        )

    tried = _tried_releases(book_id, stage)
    ranked = client.rank(releases, title, author)
    remaining = [r for r in ranked if r.source_id not in tried]

    if not remaining:
        return models.StageResult.failed(
            f"tried all {len(tried)} candidate release(s) for '{title[:50]}' and "
            f"every download failed",
            kind="data",
        )

    best = remaining[0]
    if len(remaining) > 1:
        db().log(
            f"{len(remaining)} candidates left, chose {best.label}",
            level="debug", book_id=book_id, stage=stage,
        )

    try:
        result = client.download(best, title, content_type)
    except ClientError as exc:
        # Shelfmark answers HTTP 500 when the release is already queued. That is
        # not a failure — the download is running — but treating it as one
        # marked books failed that were downloading perfectly well.
        if "already in the download queue" in str(exc).lower():
            db().mark_queued(book_id, stage, f"queued {best.label}")
            _remember_release(book_id, stage, best.source_id, tried)
            return models.StageResult.blocked(f"already queued ({best.label})")
        raise
    db().mark_queued(book_id, stage, f"queued {best.label}")
    _remember_release(book_id, stage, best.source_id, tried)
    task_id = str(result.get("task_id") or "")
    return models.StageResult.blocked(
        f"queued from {best.source} ({best.label})"
        + (f" as task {task_id}" if task_id else "")
    )


def _watch(
    client: ShelfmarkClient,
    book: dict,
    stage: str,
    content_type: str,
    output_root,
    extensions: set[str],
) -> models.StageResult:
    title = book["title"]
    author = book.get("author") or ""
    book_id = book["id"]

    found = client.find_task(title, author, source_ids=_tried_releases(book_id, stage))
    if found:
        task_id, status, entry = found
        if status in TASK_OK:
            return models.StageResult.ok(
                f"Shelfmark completed task {task_id}", artifact=task_id
            )
        if status in TASK_FAILED:
            reason = entry.get("error") or entry.get("detail") or entry.get("status_message") or status
            tried = _tried_releases(book_id, stage)
            db().clear_queued(book_id, stage)
            if len(tried) < MAX_RELEASE_ATTEMPTS:
                # A dead release is not a dead book. Drop the marker and let the
                # next sweep pick the next-best candidate; several copies of the
                # same title usually exist and only some of them work.
                db().log(
                    f"{stage}: release failed ({str(reason)[:60]}) — trying another "
                    f"({len(tried)}/{MAX_RELEASE_ATTEMPTS} tried)",
                    level="warning", book_id=book_id, stage=stage,
                )
                return models.StageResult.blocked(
                    f"release failed ({str(reason)[:70]}); will try another"
                )
            return models.StageResult.failed(
                f"Shelfmark reported {status}: {reason} — after "
                f"{len(tried)} different releases",
                kind="data",
            )
        if status in TASK_ACTIVE or not status:
            return models.StageResult.blocked(f"Shelfmark: {status or 'working'}")
        # An unrecognised state is not a reason to give up.
        return models.StageResult.blocked(f"Shelfmark: {status}")

    # No matching task. Either it finished and was cleared from /api/status
    # before we looked, or it never took. Check the destination before
    # assuming the worst.
    hits = find_matching_entries(output_root, title, author)
    if hits:
        score, path = hits[0]
        return models.StageResult.ok(
            f"found on disk ({score:.2f} match): {path.name}", artifact=str(path)
        )

    # Shelfmark keeps its queue in memory, so restarting the container drops
    # every pending task. An entry whose task is nowhere in the status listing
    # is gone, whether or not Shelfmark happens to be busy with other books.
    age = _queue_age_seconds(book_id, stage)
    if age > QUEUE_ABANDON_SECONDS:
        db().clear_queued(book_id, stage)
        return models.StageResult.blocked(
            f"no matching task in Shelfmark after {int(age // 60)} min "
            f"(the queue is not persisted, so a restart loses it) — re-queueing"
        )

    return models.StageResult.blocked(
        f"queued, waiting for Shelfmark to start ({int(age // 60)} min)"
    )


def _queue_age_seconds(book_id: int, stage: str) -> float:
    row = db().stage(book_id, stage) or {}
    stamp = row.get("queued_at")
    if not stamp:
        return 0.0
    try:
        queued = datetime.fromisoformat(stamp)
    except ValueError:
        return 0.0
    if queued.tzinfo is None:
        queued = queued.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - queued).total_seconds()


def acquired_formats(book_id: int) -> tuple[bool, bool]:
    """(has_ebook, has_audiobook) based on completed acquire stages."""
    ebook = db().stage(book_id, "acquire_ebook") or {}
    audio = db().stage(book_id, "acquire_audiobook") or {}
    return ebook.get("status") == models.OK, audio.get("status") == models.OK
