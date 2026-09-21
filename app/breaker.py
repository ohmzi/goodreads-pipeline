"""One piece of state per service: "this service is down right now".

The problem this exists for is not that an outage happens. It is that a
**service-level** condition was stored, aged and reported as a **book-level**
one, once per book. When Anna's Archive stopped resolving, every release search
through Shelfmark failed with `HTTP 503 Unable to reach download source`; each
of the 169 affected books ran its own acquire stage, waited out its own
`acquire.MAX_TRANSIENT_HOURS` clock and then parked, and the operator's panel
showed 155 book titles and a dashboard tile reading "169 books need a human"
for one unreachable host.

So this module owns the clock for a *service*, shared by every book: while a
service is down no book is attempted against it, no book is marked failed for
it, and the operator gets one row naming the service instead of a wall of book
titles. It deliberately does not touch the pipeline's structure, the stage
registry, or any existing decision about what a *book* failure means.

It measures something the 24-hour forgiveness in `pipeline` cannot, and the two
never both run for one failure:

    ===========  =========================  ==========================
                 breaker                    TRANSIENT_GRACE_HOURS
    ===========  =========================  ==========================
    scope        one service, all books    one book, one stage
    question     "is this service usable?"  "has this failed long enough
                                            to be the book's problem?"
    action       do not attempt the stage   attempt, then write BLOCKED
    ceiling      cooldown, capped at 1h     24h, then the failure stands
    ===========  =========================  ==========================

Dependency direction stays one-way: `health` -> `clients` -> `db`, and
`breaker` sits beside `health` reading it. `breaker` never imports `pipeline`
(which imports `breaker`), which is why `ADOPT_WINDOW_HOURS` is its own
constant here with a pointer back to the original rather than an import.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone

from . import models
from .db import db
from .health import SERVICE_STAGES, canonical, label_for

#: Consecutive service-level failures before the breaker opens.
#:
#: `pipeline.ADVANCE_WORKERS` is 6, so a dead service produces up to six
#: failures before anyone looks: a threshold above six would let a whole
#: sweep's worth of books park before anything reacted. Three is reached inside
#: the first batch of a real outage, while one book's unlucky 500 against an
#: otherwise healthy service leaves the counter at 1, and
#: `_forgive_transient` handles that exactly as it does today.
#:
#: What this number does *not* buy is the cost of the first sweep, and the
#: comment here used to claim it did ("the sweep stops early instead of
#: grinding through 169 books"). It does not stop early. `pipeline._sweep`
#: takes one `holding` snapshot before its workers start, so at the beginning
#: of an outage that snapshot is empty and it gates nothing; and
#: `acquire._run` can only check the breaker *after* its dial, because the
#: 503 that tells it the service is down is the same call that spent the
#: search. Every book the first sweep reaches is therefore dialed once —
#: measured at 155 in the operator's library and pinned at 24 in
#: `test_one_probe_per_cooldown_however_many_books_are_waiting` — and during a
#: rate-limit outage each of those is one more search into the 429 hole the
#: module's other comment warns about.
#:
#: That is tolerable, and deliberately not traded away. It is one dial per
#: book per *outage*, not per sweep: from the trip onwards the snapshot holds
#: and a book costs nothing, which is the whole saving. Those dials are also
#: the evidence — three of them are what trips the breaker, so a first sweep
#: that skipped them would not discover the outage at all. And the failures
#: they produce are downgraded to holds rather than written off, so the cost
#: is upstream requests and not a single book parked for a human.
#:
#: Not 1: one book's bad release would take the service out for every book.
#: Not 10: by then ten books already carry `failed` rows a human is being asked
#: to read, which is the thing being removed.
FAILURE_THRESHOLD = 3

#: First cooldown after a trip, and the ceiling it doubles up to.
#:
#: 300s is roughly twenty skipped sweeps (the sweep ticks every 15s): long
#: enough to stop hammering a rate-limited source, where every extra search
#: digs the 429 hole deeper, and short enough that a DNS blip or a container
#: restart is discovered within a couple of UI refreshes. Deliberately not
#: `TRANSIENT_GRACE_HOURS`: that answers "when is this the book's problem",
#: this answers "when do we look again". One number would either probe an
#: outage thousands of times or hold a blip for a day.
COOLDOWN_BASE_SECONDS = 300

#: Past an hour the marginal protection is nil — one request per hour is
#: nothing to a rate limiter — while the cost is real: a source that recovers
#: just after a probe would pay a whole backoff before anyone noticed. An hour
#: also keeps a sustained outage's silence well inside the 24h book-level
#: ceiling, so "nothing is happening" is never explained by the breaker alone.
COOLDOWN_MAX_SECONDS = 3600

#: How long a half-open claim may go unanswered before another book may probe.
#:
#: A claimed probe that never returns (process restart, a book deleted
#: mid-run) would otherwise wedge the breaker half-open forever. Comfortably
#: longer than the slowest legitimate call in the system —
#: `ShelfmarkClient.SEARCH_TIMEOUT` is 120s — and far shorter than anyone
#: would notice.
PROBE_STALE_SECONDS = 900

#: How far apart two failures may be and still be one streak.
#:
#: `failures` counts a *streak*, and without a window a streak was unbounded in
#: time: three failures for one service, days apart and from unrelated causes,
#: tripped the breaker exactly as three failures in one sweep do — because the
#: signature is the service name alone. Healthy traffic resets the counter now
#: (`record_success` is fed from every `ok` that names a service), and this is
#: the bound that holds even when no success ever arrives, which is the case
#: that matters: a stage that never runs leaves no `ok` to report.
#:
#: The bound is on the *gap*, applied to the newest failure, so the streak is
#: "N failures, each within a day of the one before it" — an hour, the original
#: number, was measured to be too tight in the one direction that matters. A
#: service failing once every 61 minutes, forever and never once succeeding,
#: restarted its own streak on every count and was *never held*: the failure
#: this module exists to remove, arriving by the slow road, with the per-book
#: grace parking those books as `failed` one at a time and `adopt_backlog` only
#: noticing once three had parked inside its own 24h window.
#:
#: A day is deliberately the same horizon the module already calls "plausibly
#: the same outage" (`ADOPT_WINDOW_HOURS`, and `pipeline.TRANSIENT_GRACE_HOURS`
#: underneath it), so both halves agree about what one failure means: three
#: failures with no success between them are one outage whether they arrive
#: seconds, an hour or a day apart.
#:
#: Being wrong this way is bounded and self-reverting. Three unrelated failures
#: for a live service hold it for one cooldown and the probe that follows
#: releases it, while three failures spread over more than a day are still not
#: a streak. The residual is a service failing more slowly than once a day,
#: forever: it is never held, and what that costs is one book at a time parking
#: on its own 24h grace — never the 169-book wall, which needs a *rate* of
#: failures, not a total. Any real outage has that rate: `ADVANCE_WORKERS` is 6,
#: so a dead service produces its three failures inside one sweep.
#:
#: Only the closed-state counter ages on this: `trips` and `opened_at` are the
#: *outage's* history and must not be reset by the passage of time (see
#: `_reopen`).
FAILURE_WINDOW_SECONDS = 24 * 3600

#: How recent a `failed` row must be to count as evidence of the *current*
#: outage, when adopting a backlog the breaker never witnessed.
#:
#: The same number, and the same meaning, as `pipeline.TRANSIENT_GRACE_HOURS`:
#: younger than the grace is plausibly the same outage, older than that and the
#: existing rule already says it is not a passing one and the breaker must not
#: resurrect it. Kept as its own constant so this module stays import-free of
#: `pipeline`; the two are meant to agree.
ADOPT_WINDOW_HOURS = 24

#: The kinds that say something about the *service* rather than the book.
#:
#: `network` and `server` are the transport saying "could not reach it" or "it
#: answered 5xx"; `busy` is it refusing work right now (408/425/429) — a
#: different message, the same conclusion. `auth` is absent on purpose: a
#: rejected credential is a misconfiguration, it needs a human, and it must
#: stay loud rather than be quietly held. `notfound` and `data` are facts about
#: the world and never trip anything.
TRANSIENT_KINDS: tuple[str, ...] = ("network", "server", "busy")

#: The failure kinds that only the service's own answer could have produced, so
#: a probe carrying one has heard from it.
#:
#: `auth` is the service rejecting our credentials and `notfound` is it saying
#: the thing does not exist: both are answers, and neither is a reason to hold
#: the service (`TRANSIENT_KINDS` leaves `auth` out for the same reason).
#: Nothing else belongs here. `network`, `server` and `busy` are the service
#: *failing*, which is not the answer a probe is waiting for; `data` is a fact
#: about a release, and is also what `acquire._queue` returns for a full disk
#: before it has asked anything; `''` is a stage that never said anything about
#: the service at all.
#:
#: This list is a whitelist of *kinds* and nothing more. It deliberately does
#: not try to cover the `data` case that is also an answer — "the search
#: succeeded and the service has nothing" — because no value of `kind`
#: distinguishes that from the free-space refusal, which shares it. That
#: distinction is carried by `models.StageResult.answered`, set by the stage
#: that dialed, and read in `probe_verdict` below.
ANSWER_KINDS: tuple[str, ...] = ("auth", "notfound")

#: The clock. A module-level indirection rather than `time.time()` inline so a
#: test can drive the state machine through a five-minute cooldown without
#: sleeping; nothing in production ever reassigns it.
_clock = time.time

#: Every transition is a read-modify-write of one row, so one lock is enough —
#: and there are two writers in the process (`ADVANCE_WORKERS` pipeline threads
#: plus the API). Reads used by API handlers do not take it: a single SQLite
#: row read cannot tear, and `effective_state` is pure.
_LOCK = threading.RLock()


# --------------------------------------------------------------------------
# clock helpers
# --------------------------------------------------------------------------
def now() -> float:
    return _clock()


def _iso(at: float | None = None) -> str:
    return datetime.fromtimestamp(
        _clock() if at is None else at, timezone.utc
    ).isoformat(timespec="seconds")


def _epoch(stamp: str | None) -> float:
    """Parse one of our ISO stamps back to seconds, or 0.0 if unreadable."""
    if not stamp:
        return 0.0
    try:
        when = datetime.fromisoformat(stamp)
    except ValueError:
        return 0.0
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.timestamp()


def cooldown(trips: int) -> float:
    """Backoff after `trips` consecutive trips: 300, 600, 1200, ... capped.

    Doubling is cheap to be wrong about because a probe costs one search, and
    the first probe that succeeds resets the count — so a flapping service
    backs off instead of hammering at a fixed interval, and a recovered one
    goes straight back to normal.
    """
    if trips < 1:
        trips = 1
    return float(min(COOLDOWN_BASE_SECONDS * 2 ** (trips - 1), COOLDOWN_MAX_SECONDS))


def wait_seconds(trips: int, retry_after: float | None) -> float:
    """How long to hold: the backoff, or what the upstream asked for, capped.

    The cap applies to the *result*, not just to the backoff half. `Retry-After`
    is upstream-controlled and is not a suggestion we can simply honour: a
    misconfigured or hostile peer has answered `86400` (a day) and `1e9` (the
    year 2058), and a hold that long is not a cooldown — it is a wedge. Nothing
    probes an open breaker, so the only other way out is a success, and no
    stage can report one while the breaker refuses to run it.

    Within the cap the header wins, because it is the one piece of timing
    evidence a rate limiter ever gives us: an upstream that says "300 seconds"
    is telling us more than our own doubling is.
    """
    asked = max(0.0, float(retry_after or 0.0))
    return min(max(cooldown(trips), asked), COOLDOWN_MAX_SECONDS)


def _clamped_note(retry_after: float | None, wait: float) -> str:
    """Say so in the log when the upstream asked for longer than we granted.

    The number is real information about a rate limiter's window even when we
    decline to obey it, and dropping it silently would leave the operator with
    an hour-long hold and no hint that the service asked for a day. Appended to
    the trip and re-probe lines rather than logged separately, so an outage
    stays one line per event.
    """
    asked = max(0.0, float(retry_after or 0.0))
    if asked > wait + 0.5:
        return f" (upstream asked for {asked / 3600:.1f}h; capped)"
    return ""


# --------------------------------------------------------------------------
# state
# --------------------------------------------------------------------------
def effective_state(state: str, open_until: str | None, at: float) -> str:
    """Apply the lazy open -> half-open flip. Pure; a reader may call it.

    The transition is on the clock rather than on a timer so that no reader has
    to write, and so a process that restarted mid-outage comes back holding.
    """
    if state != "open":
        return state
    if open_until and _epoch(open_until) <= at:
        return "half_open"
    return "open"


def _row(service: str) -> dict:
    row = db().query_one("SELECT * FROM service_breaker WHERE service = ?", (service,))
    return dict(row) if row else {}


def _ensure(service: str) -> dict:
    """Read a service's row, creating a closed one if it has never failed.

    Rows are created lazily, so an existing database needs no backfill.
    """
    row = _row(service)
    if row:
        return row
    stamp = _iso()
    db().execute(
        "INSERT OR IGNORE INTO service_breaker (service, state, failures, trips, "
        "changed_at) VALUES (?, 'closed', 0, 0, ?)",
        (service, stamp),
    )
    return _row(service)


def _refresh_locked(service: str) -> dict:
    """The row with both lazy transitions applied, and written if one fired.

    Called only under `_LOCK`. Handles the two time-based moves: open ->
    half-open once the cooldown has passed, and the release of a probe claim
    that was never answered.
    """
    row = _ensure(service)
    at = _clock()
    state = effective_state(row["state"], row.get("open_until"), at)
    if state != row["state"]:
        db().execute(
            "UPDATE service_breaker SET state=?, probe_book_id=NULL, "
            "probe_stage='', probe_at=NULL, changed_at=? WHERE service=?",
            (state, _iso(at), service),
        )
        row = _row(service)
    if row["state"] == "half_open" and row.get("probe_at"):
        if at - _epoch(row.get("probe_at")) > PROBE_STALE_SECONDS:
            db().execute(
                "UPDATE service_breaker SET probe_book_id=NULL, probe_stage='', "
                "probe_at=NULL, changed_at=? WHERE service=?",
                (_iso(at), service),
            )
            row = _row(service)
    return row


def states() -> dict[str, dict]:
    """Every known service's breaker, with the lazy transition applied.

    For `/api/health/services` and for the sweep summary: a script or a test can
    see the state without parsing prose.
    """
    at = _clock()
    out: dict[str, dict] = {}
    for row in db().query("SELECT * FROM service_breaker"):
        row = dict(row)
        row["state"] = effective_state(row["state"], row.get("open_until"), at)
        out[row["service"]] = row
    return out


def open_breakers() -> dict[str, dict]:
    """Service -> row, for the services holding work right now."""
    return {
        name: row
        for name, row in states().items()
        if row["state"] in ("open", "half_open")
    }


def holding() -> dict[str, bool]:
    """{service: True} for every service a stage must not be run against.

    A snapshot for one sweep, not a live per-stage read: the alternative is one
    SELECT per stage per book, ~2,800 of them a sweep. Being one sweep stale is
    safe in both directions — a service that recovers mid-sweep runs one book's
    stage early, which is what the probe would have done anyway, and a service
    that breaks mid-sweep lets one more book fail, whose result is downgraded
    to a hold before it is written.

    The cost of that staleness is not "one more book", which is how this used
    to read. A sweep takes its snapshot before its first worker starts, so on
    the sweep where the outage *begins* the snapshot says nothing is held and
    gates nothing at all: every book that sweep reaches is dialed, and only
    then does `acquire._run` see the breaker and hand back a hold instead of
    retrying. That is 155 dials measured in the operator's library (24 in
    `test_one_probe_per_cooldown_however_many_books_are_waiting`) on the first
    sweep of an outage, against one dial per cooldown on every sweep after it
    — see `FAILURE_THRESHOLD` for why that trade is kept rather than fixed by
    re-reading the breaker mid-sweep.
    """
    return {
        name: True
        for name, row in open_breakers().items()
        if row["state"] in ("open", "half_open")
    }


def is_holding(service: str) -> bool:
    return bool(holding().get(service))


# --------------------------------------------------------------------------
# transitions
# --------------------------------------------------------------------------
def _streak_live(row: dict, at: float) -> bool:
    """Whether the failures already counted are recent enough to be one streak.

    A missing or unreadable stamp counts as *not* live, so a row written before
    this column existed restarts its streak rather than tripping on failures
    of unknown age. That is the conservative direction: the cost is one extra
    failure's worth of evidence, not a trip on a week-old blip.
    """
    started = _epoch(row.get("failures_since"))
    return bool(started) and (at - started) <= FAILURE_WINDOW_SECONDS


def record_failure(service: str, detail: str, retry_after: float | None = None) -> bool:
    """Count one service-level failure. True when the service is now held.

    Fed from the two places that already know a fault is the service's and not
    the book's: `stages/acquire._run`'s own 5xx branch, and `pipeline._advance`
    on any stage result whose kind is transient. Counting only at the point a
    book is written off would let a full day of an outage pass with the breaker
    still closed.

    The count is a *streak*: `FAILURE_WINDOW_SECONDS` bounds how far apart its
    failures may be, and a success clears it outright. Both are needed —
    the window bounds the false-positive rate for a stage that never reports a
    success, and the success reset makes a healthy service recover its counter
    the moment it answers.
    """
    service = canonical(service)
    if not service:
        return False
    with _LOCK:
        row = _refresh_locked(service)
        state = row["state"]

        if state == "open":
            # Already holding. Keep the newest message for the operator's one
            # line and leave the counters alone — this failure is the same
            # outage, not a new one.
            db().execute(
                "UPDATE service_breaker SET last_failure=?, changed_at=? WHERE service=?",
                (str(detail)[:400], _iso(), service),
            )
            return True

        if state == "half_open":
            # A real search just failed while we were deciding whether the
            # service was back: it is not. Re-open on the doubled cooldown.
            _reopen(service, row, detail, retry_after)
            return True

        at = _clock()
        counted = int(row["failures"] or 0)
        # Old failures age out before this one is counted. Without this the
        # streak had no time bound at all, so three unrelated failures for one
        # service — however far apart, from however different causes — tripped
        # it exactly as three failures in one sweep do.
        failures = counted + 1 if (counted and _streak_live(row, at)) else 1
        since = _iso(at)
        if failures >= FAILURE_THRESHOLD:
            _trip(service, row, detail, retry_after)
            return True
        db().execute(
            "UPDATE service_breaker SET failures=?, failures_since=?, "
            "last_failure=?, changed_at=? WHERE service=?",
            (failures, since, str(detail)[:400], since, service),
        )
        return False


def record_success(service: str) -> None:
    """An `ok` came back from this service: it is usable again.

    Must only ever be fed by a stage result of `ok`. Never by
    `models.BLOCKED`: `pipeline._forgive_transient` deliberately keeps
    `service` on the blocked result it returns, so feeding that here would
    reset the counter on every forgiven failure and the breaker would never
    trip at all.
    """
    service = canonical(service)
    if not service:
        return
    with _LOCK:
        row = _ensure(service)
        if row["state"] == "closed" and not row.get("failures") and not row.get("trips"):
            db().execute(
                "UPDATE service_breaker SET last_ok_at=?, changed_at=? WHERE service=?",
                (_iso(), _iso(), service),
            )
            return
        _close(service, row)


def claim_probe(service: str, book_id: int, stage: str) -> bool:
    """Whether this book may run a stage against a service the breaker holds.

    True when the breaker is actually closed — the caller's snapshot was stale
    — or when this call has just taken the half-open probe. Exactly one book
    per service ever gets the latter.
    """
    service = canonical(service)
    if not service:
        return True
    with _LOCK:
        row = _refresh_locked(service)
        state = row["state"]
        if state == "closed":
            return True
        if state != "half_open":
            return False
        claimed = row.get("probe_book_id")
        if claimed is None:
            db().execute(
                "UPDATE service_breaker SET probe_book_id=?, probe_stage=?, "
                "probe_at=?, changed_at=? WHERE service=?",
                (book_id, stage, _iso(), _iso(), service),
            )
            return True
        # The same book asking again for the same stage is still its probe; a
        # different book is not.
        return int(claimed) == int(book_id) and (row.get("probe_stage") or "") == stage


def probe_verdict(result: models.StageResult, refused: bool = False) -> bool:
    """Whether a half-open probe's result is evidence the service answered.

    The verdict this replaces was `not was_transient and not refused_probe` —
    a statement about *failure kinds* read as a statement about the *service*.
    Everything that was not a transient failure closed the breaker, including
    results that never went near it. The one measured against the real system
    is `acquire._queue`'s free-space check, which runs before the search: an
    audiobook probe on a disk below `MIN_FREE_SPACE_GB` comes back
    `failed(kind='data')`, the breaker closed, and the next sweep ran every
    book against a service nobody had asked a question. The same hole swallowed
    `_advance`'s own result for a stage that raised something that was not a
    `ClientError` — `kind` and `service` are both `''` there, and `''` is not a
    transient failure either.

    So this reads a whitelist, but only over the *failures*: a probe has been
    answered when the stage came back `ok` (it ran to completion against the
    service), when it says it got an answer out of the service
    (`models.StageResult.answered`, or a kind only the service could have
    produced — `ANSWER_KINDS`), or when it came back `blocked`/`skipped`
    without a hold on the probed service. A failure is read strictly, because
    that is where the evidence was being manufactured out of nothing:

    * `refused` — the stage found the breaker holding and handed the book back
      instead of running (`acquire._run`). It never asked, so there is nothing
      to read; the caller owns this half because only `_advance` can see that
      the runner was not reached or chose not to dial.
    * a transient failure — the service failing is not the service answering,
      and reading it as one is the flap this module exists to remove. This is
      checked first and on its own, so that no value of `answered` can turn a
      503 into proof of life.
    * `data` or `''` — unanswerable *as kinds*, see `ANSWER_KINDS`; the
      disk-space result is the measured case and an unhandled stage error is
      the same shape.

    The `data` half is why `answered` exists, because it is the one kind that
    two opposite situations share. `acquire._queue`'s free-space guard returns
    `failed(kind='data')` before the dial — our disk refused the book and the
    service was never asked — and the same stage returns `failed(kind='data')`
    after a *successful* search that found no release. Reading the kind alone
    gets the second case wrong in the direction that cannot recover: the
    verdict is False, `resolve_probe` re-opens on the doubled cooldown, and a
    healthy service that has nothing to hand over is held forever. Measured:
    16.25 hours, 20 cooldowns, 25 successful dials, state still open, 5 books
    held with no way out from the UI and none from a restart, because the
    state lives in SQLite and the next probe does the same thing. It is the
    silent one — nothing failed, nothing logged, the library just stopped
    acquiring — and the dominant outstanding category in a real library ("no
    audiobook exists in any configured source") is exactly the case that
    triggers it, since `_last_touched` puts those books at the head of the
    rotation. The stage knows which of the two it did; `answered` is how it
    says so, and this is the only consumer.

    A stage that ran and came back without failing counts, and `blocked` is the
    one that has to be argued, since it is a wait rather than a verdict. The
    acquire stages are why it counts: their waits are work already handed to
    the service — `queued from <release>`, `Shelfmark: working`, `no matching
    task… re-queueing` — and a download that is queued and running is proof the
    service answered, which is exactly what a probe needs to close on. `skipped`
    counts for the neighbouring reason: a service-gated stage that ran and
    found nothing to do is not evidence about the service either way, and
    answering "no" to it would hold a working service for every book it has
    nothing to hand over — the unclosable breaker, which is worse than the flap.

    Neither is airtight: `shelve` returns `blocked` for a local switch that
    never opened a connection and for a request that failed in transport, and
    `skipped` when a book has nothing to shelve, and none of those asked
    either. `answered` is deliberately not consulted for them — it is a
    statement about a *failure*, and reading it as a requirement for a wait
    would answer "no" to every book whose download is merely in flight, which
    is the unclosable breaker above. Telling those two apart needs a stage to
    say, of a *wait*, whether it spoke to the service, which the vocabulary
    still does not carry; so the verdict stays on the side that lets a
    recovered service come back: a close that was not earned costs one sweep
    against a dead service and is corrected by the trip a few seconds later,
    while a close that never comes is every book delayed by a cooldown it did
    not need, under a headline claiming an outage that is not there.

    A verdict of False is not a claim that the service is down — it is the
    absence of proof that it is up, and the caller treats it as such by
    re-opening on the doubled cooldown rather than by writing any book off.
    """
    if refused:
        return False
    if result.status == models.FAILED:
        # Transient first and on its own, so that a flag set on the wrong
        # result can never close the breaker on a service that is failing.
        if result.kind in TRANSIENT_KINDS:
            return False
        # Then the stage's own word about whether it dialed, which is the only
        # thing that separates "no release exists" from "we never asked".
        return result.answered or result.kind in ANSWER_KINDS
    return result.status in (models.OK, models.BLOCKED, models.SKIPPED)


def resolve_probe(service: str, answered: bool, retry_after: float | None = None) -> None:
    """Record what the half-open probe proved.

    `answered` is the verdict about the *service*, and comes from
    `probe_verdict`. False means the probe did not establish that the service
    is answering — either it failed transiently, which is evidence the service
    is still down, or its result says nothing about the service at all. The
    hold simply continues on the doubled cooldown: no book is failed for it,
    and the probing book stays held rather than being written off.
    """
    service = canonical(service)
    if not service:
        return
    with _LOCK:
        row = _refresh_locked(service)
        if row["state"] != "half_open":
            # Already resolved — by another worker's success, or by the probe's
            # own `record_failure` having re-opened it a moment ago.
            return
        if answered:
            _close(service, row)
        else:
            _reopen(service, row, row.get("last_failure") or "still failing",
                    retry_after)


def _trip(service: str, row: dict, detail: str, retry_after: float | None) -> None:
    """Open the breaker: start the clock, reclaim what this fault explains."""
    trips = int(row["trips"] or 0) + 1
    wait = wait_seconds(trips, retry_after)
    opened = _iso()
    db().execute(
        "UPDATE service_breaker SET state='open', trips=?, failures=0, "
        "failures_since=NULL, opened_at=?, open_until=?, probe_book_id=NULL, "
        "probe_stage='', probe_at=NULL, last_failure=?, changed_at=? "
        "WHERE service=?",
        (trips, opened, _iso(_clock() + wait), str(detail)[:400], opened, service),
    )
    held = reclaim(service)
    db().log(
        f"{label_for(service)} cannot reach its sources — holding {held} "
        f"book(s), none failed; retrying in {wait / 60:.0f} min"
        f"{_clamped_note(retry_after, wait)}",
        level="error",
        stage="breaker",
        service=service,
    )


def _reopen(service: str, row: dict, detail: str,
            retry_after: float | None) -> None:
    """A probe answered badly: back to open on a longer cooldown.

    `opened_at` is left alone on purpose — it is the outage's age, which is
    what tells the operator this has stopped being a blip.
    """
    trips = int(row["trips"] or 0) + 1
    wait = wait_seconds(trips, retry_after)
    db().execute(
        "UPDATE service_breaker SET state='open', trips=?, open_until=?, "
        "probe_book_id=NULL, probe_stage='', probe_at=NULL, last_failure=?, "
        "changed_at=? WHERE service=?",
        (trips, _iso(_clock() + wait), str(detail)[:400], _iso(), service),
    )
    db().log(
        f"{label_for(service)} is still not answering — next probe in "
        f"{wait / 60:.0f} min{_clamped_note(retry_after, wait)}",
        level="warning",
        stage="breaker",
        service=service,
    )


def _reset_to_closed(service: str, last_ok: str | None = None) -> None:
    """The state half of closing a breaker: closed, history cleared, claim gone.

    Factored out of `_close` so that clearing by hand (`clear`) leaves the row
    in the shape a real recovery leaves it. Reimplementing it for the hand path
    would let the two drift, and the drift that matters is `trips`: a row left
    at 5 re-opens on a 3600s cooldown the next time it fails, for an outage the
    operator has just declared over.

    `last_ok` is the one field the two callers must not share. A close that a
    probe earned passes the current time, because the service really did
    answer; a hand-clear passes nothing and the column keeps whatever it had,
    because nothing has answered and a fresh stamp would read as "we saw it
    work, just now" — a lie the operator cannot see through later.
    """
    db().execute(
        "UPDATE service_breaker SET state='closed', failures=0, "
        "failures_since=NULL, trips=0, opened_at=NULL, open_until=NULL, "
        "probe_book_id=NULL, probe_stage='', probe_at=NULL, "
        "last_ok_at=COALESCE(?, last_ok_at), "
        "changed_at=? WHERE service=?",
        (last_ok, _iso(), service),
    )


def _close(service: str, row: dict) -> None:
    """The service answered: release everything it was holding.

    The per-book clock reset is the important half. It is not needed to make the
    books run again — a `blocked` stage is not satisfied, so `_advance` picks it
    up by itself on the very next sweep, and the hold only exists while the
    breaker refuses to run it. It is needed because that clock ages on the wall:
    a book whose `transient_since` was set just before an outage began would be
    at 25h the next time it failed after recovery and would park immediately,
    having "failed for 25 hours" without having been tried for 24 of them.
    """
    was_down = row["state"] != "closed"
    _reset_to_closed(service, _iso())
    if was_down:
        cleared = db().clear_transient_for_service(service, row.get("opened_at") or "")
        db().log(
            f"{label_for(service)} is answering again — resuming "
            f"{cleared} held book(s)",
            stage="breaker",
            service=service,
        )


def clear(service: str, reason: str = "") -> dict:
    """Release a service by hand: the operator's way out of a held breaker.

    The escape hatch, and the reason it exists is the shape of the failure this
    module can still have. A breaker holds a service until something proves it
    answered; if nothing ever gets to ask — no book is eligible for the probed
    stage, the only books left are the ones whose probe comes back with no
    evidence, a worker keeps re-opening it — then it holds forever, with no
    path out from the UI, none from a restart (the state is a row in SQLite,
    not a process's memory), and none from the cooldown, which by design never
    closes anything on its own. Measured, before F1 was fixed: 16.25 hours, 20
    cooldowns, 25 dials, still open, 5 books held. A mechanism that can
    silently wedge a library is not acceptable however unlikely the wedge, so
    there is a way out and it is honest about what it did:

    * the row goes `closed` and the books are released exactly as a real
      recovery releases them — `_reset_to_closed` plus the same per-book clock
      reset, so a book held through the outage does not park on a clock nobody
      spent;
    * it does **not** claim the service answered. `_close` logs "is answering
      again" because a probe proved it; this logs that it was cleared by hand
      and that the next sweep is what will find out, which is the truth: the
      whole premise of this call is that we do not know;
    * the log line is `warning` and carries the caller's `reason`, so nobody
      reading the activity log later can mistake it for a recovery;
    * `last_ok_at` is left where it was rather than stamped with the moment of
      the click, for the same reason: the row must not acquire a "seen
      answering" time that no answer produced;
    * clicking twice, or a stale page, cannot do anything more: clearing an
      already-closed breaker is a no-op that says so rather than an error,
      because the honest answer to "clear this" when nothing is held is "there
      was nothing held".

    `trips` and `opened_at` go with the state — "start over" is what the
    operator asked for. If the service really is down, the next sweep trips it
    again at the base cooldown on fresh evidence, which is exactly right: one
    sweep's failures are one sweep's failures, and holding a grudge on the
    operator's behalf is what got the library stuck in the first place.
    """
    service = canonical(service)
    if not service:
        return {"service": "", "label": "", "was": "closed", "cleared": False,
                "released": 0, "message": "no service named, nothing to clear"}
    with _LOCK:
        row = _refresh_locked(service)
        was = row["state"]
        if was == "closed":
            return {
                "service": service, "label": label_for(service), "was": was,
                "cleared": False, "released": 0,
                "message": f"{label_for(service)} was not holding anything",
            }
        _reset_to_closed(service)
        released = db().clear_transient_for_service(
            service, row.get("opened_at") or ""
        )
    why = f" ({reason})" if reason else ""
    db().log(
        f"{label_for(service)}'s breaker was cleared by hand{why} — "
        f"{released} held book(s) released. Nothing has proved the service is "
        f"answering; the next sweep will try it and re-trip if it is still "
        f"down.",
        level="warning",
        stage="breaker",
        service=service,
    )
    return {
        "service": service,
        "label": label_for(service),
        "was": was,
        "cleared": True,
        "released": released,
        # What the click ended, for the operator's own account of it: how long
        # the outage had been running (the same number the panel's `fix` line
        # escalates on) and when the cooldown would otherwise have expired.
        # `message` is the sentence the UI shows verbatim, and it is the part
        # that must not read as a recovery.
        "was_open_for_hours": round(age_hours(row.get("opened_at")), 1),
        "was_open_until": row.get("open_until"),
        "message": (
            f"{label_for(service)}'s breaker cleared by hand — {released} "
            f"book(s) released. The service has not been proven up; the next "
            f"sweep will re-trip it if it is still down."
        ),
    }


# --------------------------------------------------------------------------
# the backlog the breaker never witnessed
# --------------------------------------------------------------------------
def adopt_backlog() -> list[str]:
    """Open on an outage that parked books before the breaker saw it.

    A parked book is never attempted again, so it can never feed a failure in —
    which means a breaker that only counts live failures sits closed while the
    panel shows 169 rows. Recent transient `failed` rows are themselves the
    evidence, and they take the same trip path as a live failure, reclaim
    included.

    Idempotent by construction: the trip reclaims those rows, so the count is
    zero on the next sweep and this cannot re-trip a breaker a probe just
    closed. Returns the services it opened.
    """
    since = _iso(_clock() - ADOPT_WINDOW_HOURS * 3600)
    rows = db().failed_transient_rows(since, TRANSIENT_KINDS)
    by_service: dict[str, list[dict]] = {}
    for row in rows:
        svc = canonical(row.get("service") or "")
        if not svc or row.get("stage") not in SERVICE_STAGES.get(svc, ()):
            continue
        by_service.setdefault(svc, []).append(row)

    opened: list[str] = []
    with _LOCK:
        for svc, group in by_service.items():
            if len(group) < FAILURE_THRESHOLD:
                continue
            row = _refresh_locked(svc)
            if row["state"] != "closed":
                continue
            _trip(svc, row, group[0].get("detail") or "", None)
            opened.append(svc)
    return opened


# --------------------------------------------------------------------------
# what the trip explains, and what it does not
# --------------------------------------------------------------------------
def reclaim(service: str) -> int:
    """Convert the failures this trip explains into held rows. Returns how many.

    The predicate is `failure_kind IN ('network','server','busy')` and nothing
    else. A `data` failure is a fact about a release — "no audiobook exists",
    "every candidate was the wrong format", "Shelfmark reported error after 4
    releases" — and must stay exactly where it is: a fact about the world is
    reported, but it must not be confused with a fault, and silently absorbing
    an 18-book "Shelfmark failed to download these" group into an outage hold
    would be the same mistake in the other direction. Those groups keep their
    own fix text and simply stop growing while the breaker is open, because no
    new release is attempted.
    """
    marks = ",".join("?" * len(TRANSIENT_KINDS))
    candidates = db().query(
        f"SELECT id, stage, service FROM stage_runs "
        f"WHERE status = ? AND failure_kind IN ({marks})",
        [models.FAILED, *TRANSIENT_KINDS],
    )
    stages = SERVICE_STAGES.get(service, ())
    ids = [
        row["id"]
        for row in candidates
        if canonical(row["service"] or "") == service and row["stage"] in stages
    ]
    return db().hold_rows(ids, service, hold_detail(service))


def hold(result: models.StageResult, service: str,
         recorded: bool = False) -> models.StageResult:
    """Downgrade a service-caused failure to a hold.

    Mirrors `_forgive_transient`'s reasoning about which fields survive:
    `service` is kept, because "blocked because Shelfmark is unreachable" is
    exactly what the per-service view should show, and `kind` is cleared,
    because a held stage has not failed.

    `recorded` is the caller saying it has already fed the failure behind this
    hold to `record_failure`, and it travels on the result rather than being
    inferred from `kind` being empty — see `models.StageResult.recorded`, and
    `_advance`'s `refused_probe` block for what reads it.
    """
    return models.StageResult.held(
        f"held: {str(result.detail)[:150]} — {label_for(service)} is down; "
        f"retries by itself when it recovers",
        service,
        recorded=recorded,
    )


def hold_detail(service: str, said: str = "") -> str:
    """The sentence written on a held stage row.

    The service's own words are embedded verbatim, which is what makes the
    operator's "Open" link land on something that explains itself rather than
    on the phrase "held".
    """
    if not said:
        row = _row(service)
        said = (row.get("last_failure") or "").strip()
    text = f"held: {label_for(service)} cannot reach its sources"
    if said:
        text += f" ({said[:170]})"
    return text + " — retries by itself when it recovers"


def headline(service: str, count: int, hours: float = 0.0) -> str:
    """The one line the operator's panel shows for a held service."""
    books = "book" if count == 1 else "books"
    line = (
        f"{label_for(service)} cannot reach its download sources — holding "
        f"{count} {books}, none failed"
    )
    if hours >= 1:
        line += f", for {hours:.0f} hour{'s' if hours >= 2 else ''}"
    return line


def age_hours(opened_at: str | None) -> float:
    """How long this outage has been going, for the headline and the fix line."""
    if not opened_at:
        return 0.0
    return max(0.0, (_clock() - _epoch(opened_at)) / 3600)
