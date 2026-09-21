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

Every result this module returns *after* a successful `search` or `find_task`
carries `answered=True`, and the ones that never reached the service do not.
That is not decoration: "this service has nothing for this book" and "our own
disk refused this book before we dialed" are both `data` failures, and they are
opposite facts about the service. The breaker's half-open probe reads exactly
that difference, so a branch that moves across the free-space guard has to move
its `answered` with it. See `models.StageResult.answered`.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from .. import breaker, models
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
from ..health import canonical
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
        #
        # `busy` (408/425/429) is the same conclusion in a different message: a
        # rate-limited source is refusing work now, not refusing the book.
        if exc.status is not None and (exc.status >= 500 or exc.kind == "busy"):
            svc = canonical(exc.service)

            # Already held: hand back a hold and do *not* touch this book's own
            # grace clock. It would otherwise inflate a 24h budget on failures
            # that were never the book's, and a book held through a long outage
            # would hit its personal ceiling the moment the outage ended.
            if breaker.is_holding(svc):
                # The breaker is told *before* the hold is handed back, and that
                # ordering is the whole half-open probe.
                #
                # `hold()` deliberately clears `kind` — a held stage has not
                # failed — which leaves the result carrying no verdict about the
                # service at all, and `pipeline._advance` reads the probe's
                # verdict from exactly that field. Half-open counts as holding,
                # so without this line the probing book took this branch, came
                # back with a held result whose `kind` was `''`, and `_advance`
                # resolved the probe as *answered* — closing the breaker on the
                # very 503 that proves the service is still down. Then nothing
                # held the service, so the next sweep tried every book again:
                # ~7 searches a cycle against a dead source, a fresh
                # `opened_at` each cycle (so the 24h escalation could never
                # fire), and a "Shelfmark is answering again" line per cycle.
                #
                # `record_failure` is the right call for both cases this branch
                # covers: half-open re-opens on the doubled cooldown through
                # `_reopen`, after which `resolve_probe` is a no-op because the
                # state is no longer half_open; and genuinely open it only
                # refreshes `last_failure`, which is the message the operator's
                # one line prints.
                breaker.record_failure(svc, str(exc), retry_after=exc.retry_after)
                # `recorded=True` because the line above is this failure's
                # record: `_advance`'s probe handling must not send it again,
                # or the upstream's own words — which this call just stored —
                # are replaced by our wrapper around them. See
                # `StageResult.recorded`.
                return breaker.hold(
                    models.StageResult.failed(str(exc), kind=exc.kind,
                                              service=exc.service),
                    svc, recorded=True,
                )

            count, since = db().bump_transient(book_id, stage)
            down_for = db().transient_age_hours(since)
            if down_for < MAX_TRANSIENT_HOURS:
                # Tell the breaker *here*, at the first point the code knows
                # this is the service and not the book. This path returns
                # `blocked`, so `_advance` never sees a failure for it and
                # would never feed the breaker — counting only at the write-off
                # below let a whole day of an outage pass with the breaker
                # still closed. This is what makes it trip inside the first
                # sweep, and what stops the sweep grinding through 169 books.
                breaker.record_failure(svc, str(exc), retry_after=exc.retry_after)
                if breaker.is_holding(svc):
                    # `recorded=True` for the same reason as the branch above:
                    # this hold stands in for a failure the breaker has just
                    # been told about, and only the message half of that is at
                    # stake — the verdict is False either way, because a
                    # transient kind is never an answer.
                    return breaker.hold(
                        models.StageResult.failed(str(exc), kind=exc.kind,
                                                  service=exc.service),
                        svc, recorded=True,
                    )
                return models.StageResult.blocked(
                    f"the sources are unavailable ({str(exc)[:60]}) — down for "
                    f"{down_for:.1f}h of {MAX_TRANSIENT_HOURS}h before this counts "
                    f"as a real failure ({count} attempts so far)",
                    service=exc.service,
                )
            # Past the grace this becomes a real `failed` result, and
            # `_advance` feeds the breaker from that one — so it is not
            # recorded here as well, or a single failure would count twice
            # towards the three that are meant to be the trip evidence.
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
            # Deliberately *not* `answered`: this returns before the search, so
            # the service was never asked and this result is our own disk
            # talking. It carries the same `kind='data'` a successful empty
            # search carries, which is exactly why the two cannot be told apart
            # by kind and why `StageResult.answered` exists.
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
        # Everything below this line returns after a search that *succeeded*,
        # so it carries `answered=True`: the service was asked, and "nothing
        # for this book" is what it said. That is the whole of the F1 fix —
        # `kind` stays `data` (the issues panel buckets on it, and `cli`'s
        # `failure_kind='data'` queries would lose these rows otherwise), so
        # the breaker's half-open probe would otherwise read a healthy
        # service's own answer as silence and re-open on it, forever, for the
        # books that lead the rotation. See `StageResult.answered`.
        # Say which of the two it was: nothing found at all, or only releases
        # that are plainly the wrong format. "No releases found" when thirty
        # films were rejected reads as a search bug rather than a fact.
        rejected = getattr(client, "last_rejections", None) or []
        if rejected:
            sample = "; ".join(rejected[:2])
            # An audiobook search that returns only an epub has not *failed*: it
            # has established that no audiobook exists, which this codebase
            # already treats as a quiet fact about the world ("no-audiobook",
            # which `main._cause_bucket` keeps non-actionable). Leading with
            # "wrong format — not a book" made it read as a fault with a
            # rejection reason bolted on, and it arrived as one alarming row per
            # book — 1 book in the observed outage, but the same shape as the 18.
            #
            # The rejection detail is kept, because a human looking at *this*
            # book still wants to know what the search threw away; it just no
            # longer leads the sentence.
            return models.StageResult.failed(
                f"no {content_type} releases found for '{title}' — the "
                f"{len(rejected)} release(s) the search returned were not "
                f"{content_type}s. E.g. {sample}",
                kind="data", answered=True,
            )
        return models.StageResult.failed(
            f"no {content_type} releases found for '{title}'"
            + (f" by {author}" if author else ""),
            # Nothing is broken here — the release simply does not exist in any
            # configured source. Marking it "data" keeps it out of the pile of
            # failures that need a human.
            kind="data",
            # The search answered; it just had nothing. Not a fault, and not
            # silence either — see the note above the `rejected` branch.
            answered=True,
        )

    tried = _tried_releases(book_id, stage)
    ranked = client.rank(releases, title, author)
    remaining = [r for r in ranked if r.source_id not in tried]

    if not remaining:
        # Same shape as the two above: a search and a rank both came back from
        # the service, so it answered — with a list of releases it then failed
        # to download, which is this book's problem and not the service's
        # availability. `answered` only says the service is talking.
        return models.StageResult.failed(
            f"tried all {len(tried)} candidate release(s) for '{title[:50]}' and "
            f"every download failed",
            kind="data", answered=True,
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
            # Named, because this is the `ok` that proves Shelfmark is answering
            # again: `pipeline._advance` feeds it to `breaker.record_success`,
            # which is what clears a part-built failure streak.
            return models.StageResult.ok(
                f"Shelfmark completed task {task_id}", artifact=task_id,
                service=client.name,
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
            # `find_task` returned a task and its status, so this is a service
            # answering about a release that failed — the same false negative
            # as the empty search, one branch over, and it wedges the breaker
            # the same way for a book whose download died.
            return models.StageResult.failed(
                f"Shelfmark reported {status}: {reason} — after "
                f"{len(tried)} different releases",
                kind="data", answered=True,
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
        # An `ok` for a book we had queued with Shelfmark. The file is what
        # proves it, so it counts as this service answering.
        return models.StageResult.ok(
            f"found on disk ({score:.2f} match): {path.name}", artifact=str(path),
            service=client.name,
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
