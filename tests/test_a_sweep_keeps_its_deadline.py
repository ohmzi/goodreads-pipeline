"""The sweep's time budget, which used to be advisory.

Two separate holes. The three periodic phases ran before the clock started, so
they were entirely outside the budget and the duration the sweep reported was
never the time it took. And the worker pool was entered as a context manager,
whose `__exit__` calls `shutdown(wait=True)` — so a sweep that broke out of its
loop still waited for every in-flight book. Measured on the live deployment:
sweeps against a 120s budget were ending at 310-432s.
"""

from __future__ import annotations

import time

import helpers
import pytest

from app import pipeline


def test_the_reported_duration_covers_the_periodic_phases(monkeypatch):
    from app import health

    monkeypatch.setattr(
        health, "check_all", lambda quiet=False: time.sleep(0.4) or {"results": {}}
    )
    sched = helpers.scheduler()
    sched._last_health = 0.0  # due
    try:
        summary = sched.sweep()
    finally:
        sched.stop()

    assert summary["seconds"] >= 0.4, (
        "a phase ran outside the clock the sweep reports"
    )


def test_a_phase_with_no_budget_left_is_left_for_next_time(monkeypatch):
    from app import health

    called = []
    monkeypatch.setattr(
        health, "check_all", lambda quiet=False: called.append(1) or {}
    )
    # Anything left to do is less than this.
    monkeypatch.setattr(pipeline, "_PHASE_RESERVE_SECONDS", 10_000.0)

    sched = helpers.scheduler()
    sched._last_health = 0.0  # due
    try:
        summary = sched.sweep()
    finally:
        sched.stop()

    assert called == [], "a phase was started with no budget to finish it"
    assert "health" in summary["skipped"]
    # And nothing was lost: the timer did not move, so it is still due.
    assert sched._last_health == 0.0


def test_a_sweep_gives_up_instead_of_waiting_for_its_workers(monkeypatch):
    """The budget, actually enforced.

    The stub takes far longer than the budget. Under the old shape the sweep
    still waited for it, because leaving the `with` block joins.
    """
    monkeypatch.setattr(pipeline, "SWEEP_BUDGET_SECONDS", 1)
    helpers.seed_book("A slow book")
    sched = helpers.scheduler()
    sched._advance = lambda book, holding=None: (time.sleep(5), ("advanced", 0))[1]

    began = time.monotonic()
    try:
        summary = sched.sweep()
    finally:
        took = time.monotonic() - began
        sched.stop()

    assert took < 3.5, f"the sweep waited for work it had stopped asking for ({took:.1f}s)"
    assert summary["abandoned"] >= 1


def test_an_abandoned_book_is_not_started_again(monkeypatch):
    """A worker left running cannot be stopped, so the next sweep has to know
    about it — otherwise two stage runs write the same row."""
    monkeypatch.setattr(pipeline, "SWEEP_BUDGET_SECONDS", 1)
    helpers.seed_book("A slow book")
    sched = helpers.scheduler()
    submitted: list[int] = []

    def slow(book, holding=None):
        submitted.append(book["id"])
        time.sleep(5)
        return ("advanced", 0)

    sched._advance = slow
    try:
        sched.sweep()
        assert len(submitted) == 1
        assert sched._in_flight, "the abandoned worker was not tracked"

        second = sched.sweep()

        assert len(submitted) == 1, "the same book was submitted twice"
        assert second["in_flight"] == 1
    finally:
        sched.stop()


def test_a_failed_submit_gives_the_book_back(monkeypatch):
    """The claim must not outlive a submission that never happened.

    `submit` refuses once the pool is shut down and can also fail to start a
    thread. The done-callback that normally releases a book belongs to a future
    that was never created, so without an explicit release the id stays in
    `_in_flight` for the life of the process and every later sweep silently
    skips that book — its stages still `pending`, nothing logged, the UI
    showing it as merely not-yet-done.
    """
    monkeypatch.setattr(pipeline, "SWEEP_BUDGET_SECONDS", 30)
    helpers.seed_book("A book that cannot be submitted")
    sched = helpers.scheduler()

    def refuse(*_args, **_kwargs):
        raise RuntimeError("cannot schedule new futures after shutdown")

    sched._advance = lambda book, holding=None: ("advanced", 0)
    monkeypatch.setattr(sched._executor, "submit", refuse)
    try:
        with pytest.raises(RuntimeError):
            sched.sweep()
    finally:
        sched.stop()

    assert not sched._in_flight, "the book was claimed and never released"


def test_a_book_is_released_once_its_worker_finishes(monkeypatch):
    """The guard must not leak, or every book would be skipped forever."""
    monkeypatch.setattr(pipeline, "SWEEP_BUDGET_SECONDS", 30)
    helpers.seed_book("A quick book")
    sched = helpers.scheduler()
    sched._advance = lambda book, holding=None: ("advanced", 1)
    try:
        sched.sweep()
        # The callback fires on the worker thread; give it a moment to land.
        deadline = time.monotonic() + 2
        while sched._in_flight and time.monotonic() < deadline:
            time.sleep(0.01)

        assert not sched._in_flight
    finally:
        sched.stop()
