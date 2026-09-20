"""The scheduler.

One background thread sweeps the book list, advancing each book as far as it
can go. Stages are independent so they are retried independently — a book
whose audiobook is stuck still gets its ebook classified, placed, indexed and
added to a notebook.

A stage is only run when its prerequisites have settled, which lets the two
acquire stages run concurrently (they are genuinely independent) while keeping
`place` pinned behind `classify`.

A stage that has exhausted its attempts parks the book rather than looping
forever. Parked books are visible in the UI and resume the moment someone hits
Retry, which resets the stage and everything downstream of it.
"""

from __future__ import annotations

import concurrent.futures
import threading
import time
from datetime import datetime, timezone

from . import models
from .clients.base import ClientError
from .db import db
from .stages import REGISTRY, discover

#: A stage may run once its prerequisites reach one of these.
SATISFIED = (models.OK, models.SKIPPED)

PREREQUISITES: dict[str, tuple[str, ...]] = {
    "classify": (),
    "acquire_ebook": (),
    "acquire_audiobook": (),
    "place": ("classify",),
    "index": ("place",),
    "notebook": ("place",),
    # Verification is only meaningful once every writer has had its turn.
    "verify": ("place", "index", "notebook"),
    # The Goodreads shelf move is irreversible, so it waits for proof.
    "shelve": ("place", "verify"),
}

#: Discovering more often than this is pointless and rude to Goodreads.
DISCOVER_INTERVAL_SECONDS = 15 * 60

#: How often to re-probe service credentials. Frequent enough that a broken
#: key surfaces while you are still looking at the screen, rare enough not to
#: hammer six services.
HEALTH_INTERVAL_SECONDS = 5 * 60

#: How often to compare our records against Goodreads and repair drift.
#:
#: Every Goodreads-side failure this project has had came from trusting our own
#: bookkeeping — a shelf add that "succeeded" without applying, a membership
#: check that read page one of a shelf that had outgrown it. Checking is cheap
#: (four shelf reads) and catching drift early is what stops 42 books quietly
#: sitting on two shelves at once.
RECONCILE_INTERVAL_SECONDS = 6 * 3600


#: How long one sweep may spend working before it returns.
#:
#: A sweep used to walk every book before finishing. That is fine for twenty
#: books and disastrous for three hundred and fifty: with each acquisition
#: costing a multi-second upstream search, a single sweep ran for hours, the
#: summary was never published, and the UI showed nothing happening while the
#: pipeline was in fact grinding through — invisibly. A *time* budget rather
#: than a count adapts on its own: when upstream is slow each sweep simply does
#: less, and progress stays steady and observable either way.
SWEEP_BUDGET_SECONDS = 120

#: A sweep that overruns this is doing something pathological; log it so the
#: cause is findable rather than mysterious.
SWEEP_WARN_SECONDS = 300

#: How long a service may be transiently broken before its failures become a
#: book's problem. A full day covers an upstream outage or a quota that resets
#: daily, which is the realistic worst case here.
TRANSIENT_GRACE_HOURS = 24


#: How many books may be advanced at once.
#:
#: The sweep was serial, and one book's `_advance` runs all of its stages — so
#: a single book whose acquisition search takes a minute consumed the entire
#: time budget. Measured: one book per 120s, which is twelve hours for three
#: hundred and fifty. The work is almost entirely network waiting, so running
#: several books at once costs nothing and divides the wall-clock accordingly.
ADVANCE_WORKERS = 6


class Scheduler:
    def __init__(self, interval: int = 15):
        self.interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._sweep_lock = threading.Lock()
        self._last_discover = 0.0
        self._last_discover_error = ""
        self._last_health = 0.0
        self._last_reconcile = 0.0
        self.last_result: dict = {}

    # -- lifecycle -------------------------------------------------------
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="goodreads-sweep", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.sweep()
            except Exception as exc:  # noqa: BLE001 - the loop must survive anything
                db().log(f"sweep crashed: {exc}", level="error")
            self._stop.wait(self.interval)
    # -- the work --------------------------------------------------------
    def sweep(self, force_discover: bool = False) -> dict:
        """One pass over every book. Returns a summary."""
        if not self._sweep_lock.acquire(blocking=False):
            return {"skipped": "a sweep is already running"}
        try:
            return self._sweep(force_discover)
        finally:
            self._sweep_lock.release()

    def _sweep(self, force_discover: bool) -> dict:
        summary = {"discover": None, "advanced": 0, "parked": 0, "books": 0, "health": None}

        now = time.time()

        # Credential checks run independently of book work, so a service that
        # starts rejecting auth is visible within minutes rather than whenever
        # a book next happens to touch that stage.
        if force_discover or (now - self._last_health) > HEALTH_INTERVAL_SECONDS:
            self._last_health = now
            try:
                from .health import check_all

                summary["health"] = check_all()
            except Exception as exc:  # noqa: BLE001
                db().log(f"health check failed: {exc}", level="error")

        if force_discover or (now - self._last_discover) > DISCOVER_INTERVAL_SECONDS:
            self._last_discover = now
            try:
                summary["discover"] = discover.run()
                self._last_discover_error = ""
            except Exception as exc:  # noqa: BLE001
                message = str(exc)
                # Discover runs every few minutes; logging the same failure
                # each time would bury everything else in the activity list.
                if message != self._last_discover_error:
                    db().log(f"discover failed: {message}", level="error")
                    self._last_discover_error = message
                summary["discover"] = {"error": message}

        # Compare our shelf records against Goodreads and queue a retry for
        # anything that has drifted. Runs on its own so drift is caught without
        # anyone noticing it first.
        if force_discover or (now - self._last_reconcile) > RECONCILE_INTERVAL_SECONDS:
            self._last_reconcile = now
            try:
                from .reconcile import reconcile

                summary["reconcile"] = reconcile(apply_changes=True)
            except Exception as exc:  # noqa: BLE001
                db().log(f"reconcile failed: {exc}", level="error")
                summary["reconcile"] = {"error": str(exc)}

        # Work the books that have gone longest without attention first, so a
        # bounded sweep still gives every book a turn instead of forever
        # re-trying the same head of the list.
        books = db().list_books()
        books.sort(key=_last_touched)
        started = time.time()
        spent = 0

        # Advance several books concurrently: the stages are I/O-bound, and a
        # serial sweep spent its whole budget on one slow search.
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=ADVANCE_WORKERS, thread_name_prefix="goodreads-adv"
        ) as pool:
            pending = {}
            queue = iter(books)
            while True:
                over_budget = (time.time() - started) >= SWEEP_BUDGET_SECONDS
                while not over_budget and len(pending) < ADVANCE_WORKERS:
                    try:
                        book = next(queue)
                    except StopIteration:
                        break
                    pending[pool.submit(self._advance, book)] = book
                    over_budget = (time.time() - started) >= SWEEP_BUDGET_SECONDS

                if not pending:
                    break

                done, _ = concurrent.futures.wait(
                    pending, return_when=concurrent.futures.FIRST_COMPLETED
                )
                for future in done:
                    book = pending.pop(future)
                    summary["books"] += 1
                    try:
                        outcome, used = future.result()
                    except Exception as exc:  # noqa: BLE001 - one book must not end the sweep
                        db().log(f"advance crashed for {book.get('title', '?')[:40]}: {exc}",
                                 level="error", book_id=book.get("id"))
                        continue
                    spent += used
                    if outcome == "advanced":
                        summary["advanced"] += 1
                    elif outcome == "parked":
                        summary["parked"] += 1

                if (time.time() - started) >= SWEEP_BUDGET_SECONDS and not pending:
                    break
                if (time.time() - started) >= SWEEP_BUDGET_SECONDS * 2:
                    break

        elapsed = time.time() - started
        summary["stages_run"] = spent
        summary["deferred"] = max(0, len(books) - summary["books"])
        summary["seconds"] = round(elapsed, 1)
        if elapsed > SWEEP_WARN_SECONDS:
            db().log(
                f"sweep took {elapsed:.0f}s for {summary['books']} book(s) — "
                f"something upstream is slow",
                level="warning",
            )
        self.last_result = summary
        return summary

    def _advance(self, book: dict) -> tuple[str, int]:
        """Run every stage of one book that is ready to run.

        Returns (outcome, stages_executed) so the caller can budget its pass.
        """
        book_id = book["id"]
        stages: dict = book.get("stages") or {}
        advanced = False
        ran = 0

        for stage in models.STAGES:
            row = stages.get(stage) or {}
            status = row.get("status", models.PENDING)
            attempts = int(row.get("attempts") or 0)

            # A download that finished *after* placement gave up has to wake it.
            #
            # `place` returns "skipped" when nothing is available, and skipped
            # counts as satisfied — so when the download completed a moment
            # later, placement never ran again and the file sat in staging
            # forever. 55 books were in exactly that state with a perfectly
            # good ebook on disk that no service had ever seen.
            if stage == "place" and status in SATISFIED and _format_waiting(book_id):
                db().reset_stage(book_id, "place")
                row = db().stage(book_id, "place") or {}
                stages["place"] = row
                status = row.get("status", models.PENDING)
                db().log(
                    "a download completed after placement gave up — re-running it",
                    book_id=book_id, stage="place",
                )

            if status in SATISFIED:
                continue

            if status == models.FAILED and attempts >= models.MAX_ATTEMPTS.get(stage, 3):
                # Parked for a human. Nothing downstream should run either.
                return "parked", ran

            if not self._prereqs_met(stages, stage):
                continue

            runner = REGISTRY.get(stage)
            if runner is None:
                continue

            ran += 1
            db().mark_running(book_id, stage)
            try:
                result = runner(book)
            except Exception as exc:  # noqa: BLE001 - a stage bug must not kill the sweep
                # A `ClientError` reaching here means a stage let one escape
                # rather than converting it. It still knows which service it
                # came from, so keep that rather than flattening it into a
                # string and losing the one fact that makes it actionable.
                result = models.StageResult.failed(
                    f"unhandled error: {exc}",
                    kind=exc.kind if isinstance(exc, ClientError) else "",
                    service=exc.service if isinstance(exc, ClientError) else "",
                )

            db().mark_done(book_id, stage, result)

            # A service being briefly unreachable or briefly broken is not a
            # book-level failure. Restarting Shelfmark produced 53 books marked
            # failed with "could not reach"; Open Notebook returned transient
            # 500s under load from the pipeline itself. Both were fine a minute
            # later. Anything transient is forgiven on a *clock* — see
            # `_forgive_transient` — so a service that is genuinely broken
            # still surfaces, but a busy one does not write off books.
            if result.status == models.FAILED and result.kind in ("network", "server"):
                forgiven = self._forgive_transient(book_id, stage, result)
                if forgiven is not None:
                    result = forgiven
                    stages[stage] = db().stage(book_id, stage) or {}

            if result.status == models.FAILED:
                db().log(
                    f"{stage} failed: {result.detail}",
                    level="error",
                    book_id=book_id,
                    stage=stage,
                )
            elif result.status == models.OK:
                db().log(
                    f"{stage}: {result.detail}" if result.detail else f"{stage} done",
                    book_id=book_id,
                    stage=stage,
                )
                advanced = True

                # `place` now succeeds as soon as one format is in place, so a
                # format that arrives later has to wake it back up — otherwise
                # the audiobook would land and never be indexed.
                #
                # Only when that format is genuinely not placed yet. Resetting
                # unconditionally re-ran placement (and therefore index,
                # notebook and verify) for books that were already correctly
                # filed — index counts visibly went *down* as finished books
                # were pushed back to pending for no reason.
                if stage.startswith("acquire_"):
                    wanted = "ebook" if stage == "acquire_ebook" else "audiobook"
                    placed = _placed_formats(book_id)
                    place_run = db().stage(book_id, "place") or {}
                    if wanted not in placed and place_run.get("status") in SATISFIED:
                        db().reset_stage(book_id, "place")
                        stages["place"] = db().stage(book_id, "place") or {}
                        db().log(
                            f"{stage} completed — re-running placement to pick it up",
                            book_id=book_id, stage="place",
                        )
                    # A book shelved as `collected-pdf` because its audiobook was
                    # unavailable should move to the both-formats shelf once the
                    # audiobook actually arrives.
                    elif stage == "acquire_audiobook" and wanted not in placed:
                        shelve = db().stage(book_id, "shelve") or {}
                        if shelve.get("status") in SATISFIED:
                            db().reset_stage(book_id, "shelve")
                            stages["shelve"] = db().stage(book_id, "shelve") or {}
                            db().log(
                                "audiobook arrived — re-shelving to the both-formats shelf",
                                book_id=book_id, stage="shelve",
                            )

            # Refresh our view so the next stage sees this one's result.
            stages[stage] = db().stage(book_id, stage) or {}

        if advanced:
            return "advanced", ran
        return ("ran" if ran else "idle"), ran

    def _forgive_transient(
        self, book_id: int, stage: str, result: models.StageResult
    ) -> models.StageResult | None:
        """Turn a transient failure into a retry, for up to a day.

        Returns the replacement result, or None once the service has been
        misbehaving long enough that it is a real problem worth a human's
        attention. Time is the right measure: how long something has been
        broken separates "busy for a moment" from "actually down", whereas a
        retry *count* is exhausted in minutes by an outage lasting hours.
        """
        count, since = db().bump_transient(book_id, stage)
        down_for = db().transient_age_hours(since)
        if down_for < TRANSIENT_GRACE_HOURS:
            # `failure_kind` is cleared because a blocked stage has not failed,
            # but `service` is deliberately kept: "blocked because Shelfmark is
            # unreachable" is exactly what the per-service view should be able
            # to show. A later successful run clears it through `mark_done`.
            db().execute(
                "UPDATE stage_runs SET status=?, failure_kind='' "
                "WHERE book_id=? AND stage=?",
                (models.BLOCKED, book_id, stage),
            )
            return models.StageResult.blocked(
                f"{result.detail[:150]}  [transient: {down_for:.1f}h of "
                f"{TRANSIENT_GRACE_HOURS}h, {count} attempts]",
                service=result.service,
            )
        return None

    def _prereqs_met(self, stages: dict, stage: str) -> bool:
        for prereq in PREREQUISITES.get(stage, ()):
            row = stages.get(prereq) or {}
            if row.get("status") not in SATISFIED:
                return False
        return True


def _format_waiting(book_id: int) -> bool:
    """A download succeeded but that format is not in the library yet."""
    placed = _placed_formats(book_id)
    for stage, wanted in (("acquire_ebook", "ebook"), ("acquire_audiobook", "audiobook")):
        run = db().stage(book_id, stage) or {}
        if run.get("status") == models.OK and wanted not in placed:
            return True
    return False


def _placed_formats(book_id: int) -> set[str]:
    """Which formats `place` has already filed for this book."""
    import json

    row = db().stage(book_id, "place") or {}
    raw = row.get("output_path") or ""
    if not raw:
        return set()
    try:
        data = json.loads(raw)
    except ValueError:
        return set()
    return set(data) if isinstance(data, dict) else set()


def _last_touched(book: dict) -> str:
    """Sort key: how long ago this book last had a stage run.

    Books are served least-recently-touched first so a bounded sweep rotates
    through the whole list instead of grinding the same head of it.

    This is the *most recent* stage timestamp, and getting that wrong is
    subtle: taking the oldest instead produces a key that never changes once
    set, so the same books lead every single sweep. That is exactly what
    happened — the sweep spent its whole budget re-trying books stuck on an
    upstream quota while 72 books that had never been attempted sat untouched
    behind them, and acquisitions appeared to plateau for no visible reason.
    """
    stamps = [
        (run or {}).get("finished_at") or (run or {}).get("started_at") or ""
        for run in (book.get("stages") or {}).values()
    ]
    live = [s for s in stamps if s]
    return max(live) if live else ""      # "" sorts first: never-attempted books lead


scheduler = Scheduler(interval=15)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
