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

from . import breaker, models
from .clients.base import ClientError
from .db import db
from .health import canonical, service_for_stage
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

#: Budget a periodic phase must have left to be worth starting. The three
#: together take about half a minute against live services — measured on the
#: real deployment: reconcile 25s over 255 shelf records, discover 6s over a
#: 92-book shelf, check_all 0.5s — so this leaves room for the slowest of them
#: and skips all three only when a sweep is already nearly out of time.
#:
#: A phase already running cannot be interrupted. The only bound available is
#: on starting one, which is why this is a *reserve* rather than a timeout.
_PHASE_RESERVE_SECONDS = 40.0

#: How long a service may be transiently broken before its failures become a
#: book's problem. A full day covers an upstream outage or a quota that resets
#: daily, which is the realistic worst case here.
#:
#: This answers "has this book been failing long enough to be a real problem",
#: which is a different question from the breaker's "is this service usable
#: right now" — see `breaker`'s module docstring. They never both run for one
#: failure: while the breaker holds a service, `_forgive_transient` is not
#: reached at all.
TRANSIENT_GRACE_HOURS = 24


#: How many books may be advanced at once.
#:
#: The sweep was serial, and one book's `_advance` runs all of its stages — so
#: a single book whose acquisition search takes a minute consumed the entire
#: time budget. Measured: one book per 120s, which is twelve hours for three
#: hundred and fifty. The work is almost entirely network waiting, so running
#: several books at once costs nothing and divides the wall-clock accordingly.
#:
#: Six concurrent searches is also what makes a 429 from a struggling source
#: likely, and that is deliberately *not* fixed by lowering this number:
#: throttling here would slow every healthy sweep to pay for an outage. The
#: breaker is the fix — once a service is held it runs zero books at a time,
#: which is a sharper version of the same idea and reverts by itself.
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
        #: Books currently inside `_advance`, wherever that is happening. A
        #: sweep that runs out of budget stops *waiting* but cannot stop the
        #: worker it left behind, so the next sweep has to know which books are
        #: still in someone's hands. Without this it would submit the same book
        #: again and two stage runs would write the same row.
        self._in_flight: set[int] = set()
        self._in_flight_lock = threading.Lock()
        #: Owned by the scheduler rather than by a sweep, so a sweep can return
        #: without joining work it has stopped waiting for. See `_sweep`.
        self._pool: concurrent.futures.ThreadPoolExecutor | None = None

    @property
    def _executor(self) -> concurrent.futures.ThreadPoolExecutor:
        """The worker pool, created on first use.

        Not created in `__init__` because a `ThreadPoolExecutor` starts no
        threads until something is submitted, and not created per sweep because
        `ThreadPoolExecutor.__exit__` calls `shutdown(wait=True)` — which is
        what made this budget advisory rather than real.
        """
        if self._pool is None:
            self._pool = concurrent.futures.ThreadPoolExecutor(
                max_workers=ADVANCE_WORKERS, thread_name_prefix="goodreads-adv"
            )
        return self._pool

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
        if self._pool is not None:
            # Not waited on: a sweep is allowed to leave a book mid-advance,
            # and shutting down is the same situation. Queued work is dropped
            # and picked up by the next sweep, which finds it still pending.
            self._pool.shutdown(wait=False, cancel_futures=True)
            self._pool = None

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
        summary = {"discover": None, "advanced": 0, "parked": 0, "held": 0,
                   "books": 0, "health": None, "breaker": {}, "in_flight": 0,
                   "abandoned": 0, "skipped": []}

        # The clock starts here, not at the book loop below. The three periodic
        # phases are plain synchronous calls that together take about half a
        # minute against live services, and they used to run entirely outside
        # the budget — so `seconds` below was never the time this sweep took,
        # and a phase was free to overrun by however much it liked.
        started = time.time()
        now = started

        def remaining() -> float:
            return SWEEP_BUDGET_SECONDS - (time.time() - started)

        def take_phase(name: str) -> bool:
            """Claim the right to start a periodic phase, or record the skip.

            An already-running phase cannot be interrupted, so the only bound
            available is on starting one. No timer is updated when a phase is
            skipped, so nothing is lost: it is still due, and the next sweep
            tries again a few minutes later.
            """
            if remaining() > _PHASE_RESERVE_SECONDS:
                return True
            summary["skipped"].append(name)
            return False

        # Credential checks run independently of book work, so a service that
        # starts rejecting auth is visible within minutes rather than whenever
        # a book next happens to touch that stage.
        health_due = force_discover or (now - self._last_health) > HEALTH_INTERVAL_SECONDS
        if health_due and take_phase("health"):
            self._last_health = now
            try:
                from .health import check_all

                summary["health"] = check_all()
            except Exception as exc:  # noqa: BLE001
                db().log(f"health check failed: {exc}", level="error")

        # An outage that parked books before the breaker existed — or before
        # this process started — is invisible to it: a parked book is never
        # attempted again, so it can never feed a failure in. Adopting the
        # recent backlog is what lets the breaker open on an outage it did not
        # witness, and the trip reclaims those rows, so it cannot fire twice.
        try:
            summary["breaker_adopted"] = breaker.adopt_backlog()
        except Exception as exc:  # noqa: BLE001
            db().log(f"breaker backlog adoption failed: {exc}", level="error")

        discover_due = force_discover or (now - self._last_discover) > DISCOVER_INTERVAL_SECONDS
        if discover_due and take_phase("discover"):
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
        reconcile_due = force_discover or (now - self._last_reconcile) > RECONCILE_INTERVAL_SECONDS
        if reconcile_due and take_phase("reconcile"):
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
        spent = 0

        # One snapshot of "which services are held" for the whole sweep, passed
        # to every worker. See `breaker.holding` for why a snapshot is safe.
        holding = breaker.holding()

        # Advance several books concurrently: the stages are I/O-bound, and a
        # serial sweep spent its whole budget on one slow search.
        #
        # The pool is the scheduler's and is deliberately *not* entered as a
        # context manager. `ThreadPoolExecutor.__exit__` calls
        # `shutdown(wait=True)`, so every break out of the loop below was
        # followed by a wait for six in-flight books anyway — measured on the
        # live deployment, sweeps that broke out at the 2x ceiling still ended
        # at 310-432s against a 120s budget. Outliving the sweep is what makes
        # the budget real, and `_in_flight` is what makes it safe.
        pool = self._executor
        pending = {}
        queue = iter(books)
        while True:
            over_budget = (time.time() - started) >= SWEEP_BUDGET_SECONDS
            while not over_budget and len(pending) < ADVANCE_WORKERS:
                try:
                    book = next(queue)
                except StopIteration:
                    break
                if not self._claim(book["id"]):
                    # Still being advanced by the sweep that gave up waiting
                    # for it. Submitting it again would put two stage runs on
                    # one book, writing the same row twice.
                    summary["in_flight"] += 1
                    continue
                try:
                    future = pool.submit(self._advance, book, holding)
                except RuntimeError:
                    # `submit` refuses once the pool is shut down, and can also
                    # fail to start a thread. The claim above must be given back
                    # by hand here, because the done-callback that normally
                    # releases it belongs to a future that was never created —
                    # without this the book stays in `_in_flight` for the life
                    # of the process and every later sweep silently skips it.
                    self._release(book["id"])
                    raise
                future.add_done_callback(
                    lambda _future, book_id=book["id"]: self._release(book_id)
                )
                pending[future] = book
                over_budget = (time.time() - started) >= SWEEP_BUDGET_SECONDS

            if not pending:
                break

            # Bounded, so one wedged book cannot hold the sweep open. With no
            # timeout this waited on the slowest worker indefinitely, and the
            # budget checks below were never reached at all.
            done, _ = concurrent.futures.wait(
                pending,
                timeout=max(1.0, remaining()),
                return_when=concurrent.futures.FIRST_COMPLETED,
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
                elif outcome == "held":
                    summary["held"] += 1

            if (time.time() - started) >= SWEEP_BUDGET_SECONDS and not pending:
                break
            if (time.time() - started) >= SWEEP_BUDGET_SECONDS * 2:
                break

        # Whatever is still running belongs to no one now. These stay in
        # `_in_flight` until their own callback fires, so the next sweep skips
        # them rather than starting a second run of the same book.
        summary["abandoned"] = len(pending)

        elapsed = time.time() - started
        summary["stages_run"] = spent
        summary["deferred"] = max(0, len(books) - summary["books"])
        summary["seconds"] = round(elapsed, 1)
        # So a quiet sweep explains itself: "held" counts books whose only
        # runnable stage was refused by the breaker, and the state map says
        # which services are holding.
        summary["breaker"] = {
            name: row["state"] for name, row in breaker.states().items()
        }
        if elapsed > SWEEP_WARN_SECONDS:
            db().log(
                f"sweep took {elapsed:.0f}s for {summary['books']} book(s) — "
                f"something upstream is slow",
                level="warning",
            )
        self.last_result = summary
        return summary

    def _claim(self, book_id: int) -> bool:
        """Take a book for advancing, unless someone already has it.

        Returns False when the book is already being advanced — which happens
        exactly when an earlier sweep ran out of budget and left the worker
        running. See `_in_flight`.
        """
        with self._in_flight_lock:
            if book_id in self._in_flight:
                return False
            self._in_flight.add(book_id)
            return True

    def _release(self, book_id: int) -> None:
        """Give a book back once its worker has actually finished.

        Wired up as a future callback rather than called from the sweep's own
        loop, because a sweep that stopped waiting is precisely the case this
        has to keep working for: the callback fires when the worker ends,
        whether or not anybody is still watching.
        """
        with self._in_flight_lock:
            self._in_flight.discard(book_id)

    def _advance(self, book: dict, holding: dict | None = None) -> tuple[str, int]:
        """Run every stage of one book that is ready to run.

        Returns (outcome, stages_executed) so the caller can budget its pass.
        `holding` is this sweep's snapshot of the breaker (`breaker.holding`).
        """
        book_id = book["id"]
        stages: dict = book.get("stages") or {}
        holding = holding or {}
        advanced = False
        held = 0
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

            # A service the breaker holds is not attempted at all, so no book
            # can be written off for it and the sweep stops grinding. This is
            # checked before the stage runs, which is the whole point: `busy`
            # and `server` failures cost an upstream search each, and during a
            # rate limit every extra search digs the hole deeper.
            svc = service_for_stage(stage)
            probe = ""
            if svc and holding.get(svc):
                # `claim_probe` answers True when the breaker is actually
                # closed (this snapshot was stale) or when this book has just
                # taken the half-open probe — and only one book per service
                # ever gets that.
                if not breaker.claim_probe(svc, book_id, stage):
                    row = stages.get(stage) or {}
                    if row.get("status") != models.BLOCKED or row.get("held_by") != svc:
                        db().mark_held(book_id, stage, svc, breaker.hold_detail(svc))
                        stages[stage] = db().stage(book_id, stage) or {}
                    held += 1
                    continue
                probe = svc

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
                    retry_after=exc.retry_after if isinstance(exc, ClientError) else None,
                )

            # Captured before any downgrade below: the probe's verdict is about
            # the *raw* result. `hold()` clears `kind`, so reading it afterwards
            # would report every probe as answered and close the breaker on the
            # very failure that proves the service is still down.
            #
            # That is not enough on its own: a stage that holds a book by itself
            # (`acquire._run` does, the moment it sees the breaker holding)
            # never produces a result with a transient kind at all, so there is
            # no raw kind left to read. `refused_probe` below is the rest of the
            # verdict — a hold *on the probed service* is the stage saying the
            # service would not serve it, which is not an answer.
            #
            # `answered_probe` is `breaker.probe_verdict` rather than "not
            # transient" for the same reason: the verdict it replaces treated
            # every non-transient failure as the service answering, including
            # the ones that never reached it. A probe refused by our own disk
            # check (`acquire._queue` measures free space before it searches)
            # closed the breaker having asked nothing, and the next sweep ran
            # every book against a dead service.
            #
            # The verdict is not read off `kind` alone any more, because `data`
            # is both "the service answered and has nothing" and "we never
            # asked"; the stage carries that answer on the result now
            # (`StageResult.answered`), and `probe_verdict` is the one reader.
            was_transient = result.kind in breaker.TRANSIENT_KINDS
            refused_probe = bool(
                probe and result.status == models.BLOCKED
                and result.held_by == probe
            )
            answered_probe = breaker.probe_verdict(result, refused_probe)

            # Ordering here is load-bearing: `record_failure` runs *before*
            # `mark_done`, so the trip's reclaim cannot race the row this worker
            # is about to write. The breaker trips, reclaims the rows that are
            # already `failed`, and this result is downgraded and written as
            # held instead of failed. The other way round, every worker in
            # flight at the moment of the trip would add a fresh `failed` row
            # *after* the reclaim and the panel would keep a tail of them.
            if result.status == models.FAILED and was_transient and result.service:
                blamed = canonical(result.service)
                breaker.record_failure(blamed, result.detail,
                                       retry_after=result.retry_after)
                if breaker.is_holding(blamed):
                    # Not this book's fault, and not this book's failure: the
                    # service is down, so the row is written held rather than
                    # failed. `recorded=True` because the call three lines up
                    # was this failure's record, so the probe block below has
                    # nothing left to send.
                    result = breaker.hold(result, blamed, recorded=True)

            db().mark_done(book_id, stage, result)

            if result.status == models.BLOCKED and result.held_by:
                # A stage can hold a book by itself, before this sweep's
                # snapshot knew the breaker was open (see `acquire._run`). It
                # is still a book the breaker refused, and it has to be counted
                # like one, or the very sweep the outage started would report
                # `held=0` while ten books sat held — the sweep summary would
                # be the one place the outage was invisible.
                held += 1

            if probe:
                if refused_probe and not result.recorded:
                    # The belt to the stage's braces, and only for a hold the
                    # stage did not take on the breaker's behalf. That is read
                    # off `result.recorded`, which the holder sets when it has
                    # already fed this failure to `record_failure` — not off
                    # `kind` being empty, which is how it used to be inferred
                    # and which answers a different question: `acquire._run`
                    # holds with an empty `kind` *because* it recorded, but a
                    # stage that holds without recording also produces an empty
                    # `kind` through `hold()`'s clearing, and was silently read
                    # as having recorded.
                    #
                    # Recording a probe twice is not a state bug — the verdict
                    # below does not depend on this call — it is a *message*
                    # bug, and the message is what the operator reads.
                    # `result.detail` here is the *held* sentence, and
                    # `hold_detail()` embeds `last_failure` verbatim, so the
                    # second call replaces the upstream's own words with our
                    # wrapper around them, nested inside another copy on every
                    # book marked held after the probe in the same sweep. With
                    # `recorded` the belt is a real one: an unrecorded hold now
                    # gets recorded (which is the point of the belt), and the
                    # one stage shape that has been measured does not.
                    breaker.record_failure(probe, result.detail,
                                           retry_after=result.retry_after)
                breaker.resolve_probe(
                    probe, answered=answered_probe,
                    retry_after=result.retry_after,
                )
            elif result.status == models.OK and result.service:
                # Only ever an `ok`: see `breaker.record_success` for why a
                # blocked result must never be fed back here.
                breaker.record_success(canonical(result.service))

            # A service being briefly unreachable or briefly broken is not a
            # book-level failure. Restarting Shelfmark produced 53 books marked
            # failed with "could not reach"; Open Notebook returned transient
            # 500s under load from the pipeline itself. Both were fine a minute
            # later. Anything transient is forgiven on a *clock* — see
            # `_forgive_transient` — so a service that is genuinely broken
            # still surfaces, but a busy one does not write off books.
            #
            # Reachable only when the breaker is *not* holding: a held failure
            # was downgraded above, so it is no longer FAILED.
            if result.status == models.FAILED and result.kind in breaker.TRANSIENT_KINDS:
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
                    # Recorded rather than left to be guessed: the service page
                    # used to recover this by substring-matching the service's
                    # name in the message, which is why the operator's activity
                    # page could not show this outage as Shelfmark's.
                    service=canonical(result.service),
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
        if held:
            # Every stage this book could have run was refused by the breaker.
            # Counted by the caller like `parked`, so a sweep of held books
            # reads as "held", not as "nothing happened".
            return "held", ran
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

        This measures one book's patience; `breaker` measures whether the
        service is usable at all. They are deliberately separate and never both
        run for one failure — while the breaker holds a service this is not
        reached, because that failure was already downgraded to a hold, and the
        breaker's clock has to reset this one's `transient_since` when it
        closes or a book held through a long outage would park the moment it
        ended. See `breaker`'s module docstring.
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
