"""A held service costs no book, and everything resumes on its own.

The observed failure: 155 audiobooks sitting `failed` because Anna's Archive
was unreachable, reported as 155 things for a human to look at. Here the
breaker is open before the sweep starts, so the sweep must attempt *nothing*
against that service, write no failure, and log one line instead of 155.
"""

from __future__ import annotations

import pytest

from app import breaker, models, pipeline
from app.db import db

import helpers

TRIP_DETAIL = (
    "shelfmark: release search failed with HTTP 503: Unable to reach download "
    "source. Could not reach Anna's Archive after 10 attempt(s): ReadTimeout"
)


@pytest.fixture
def books() -> list[int]:
    return [helpers.audiobook_pending_book(f"Held Book {i}") for i in range(10)]


def test_a_held_service_is_not_attempted_at_all(monkeypatch, clock, books):
    for _ in range(breaker.FAILURE_THRESHOLD):
        breaker.record_failure("shelfmark", TRIP_DETAIL)
    assert breaker.is_holding("shelfmark")

    calls: list[str] = []
    monkeypatch.setattr(pipeline, "REGISTRY", helpers.stub_registry(calls, {"on": True}))

    summary = helpers.scheduler().sweep()

    # The strongest possible statement: no stage was run, so there was nothing
    # to mark failed.
    assert calls == []
    assert summary["held"] == 10
    assert summary["parked"] == 0
    assert summary["advanced"] == 0

    rows = [db().stage(b, "acquire_audiobook") for b in books]
    assert all(r["status"] == models.BLOCKED for r in rows)
    assert all(r["held_by"] == "shelfmark" for r in rows)
    # The breaker is the clock now: the stage's own budget must not be spent on
    # an outage, and the per-book grace clock must not tick for it either.
    assert all(r["attempts"] == 0 for r in rows)
    assert all(r["transient_count"] == 0 for r in rows)
    assert all(r["transient_since"] is None for r in rows)
    assert all(r["failure_kind"] == "" for r in rows)

    # One incident, not ten rows.
    assert db().query_one(
        "SELECT COUNT(*) AS n FROM stage_runs WHERE status = 'failed'"
    )["n"] == 0
    trips = [e for e in db().recent_events(200)
             if e["stage"] == "breaker" and e["level"] == "error"]
    assert len(trips) == 1
    assert trips[0]["service"] == "shelfmark"

    # The writer/reader invariant: the held row carries the service's own words
    # and its name, so the "Open" link lands on something that explains itself.
    assert "HTTP 503" in rows[0]["detail"]
    assert "Shelfmark" in rows[0]["detail"]


def test_a_held_book_reads_as_working_not_partial(clock, books):
    from app import main

    for _ in range(breaker.FAILURE_THRESHOLD):
        breaker.record_failure("shelfmark", TRIP_DETAIL)
    for book_id in books:
        db().mark_held(book_id, "acquire_audiobook", "shelfmark",
                       breaker.hold_detail("shelfmark"))

    states = [main.book_state(helpers.load_book(b)) for b in books]
    # Not `partial` ("the audiobook does not exist anywhere"), which would leave
    # a book looking settled while the pipeline was still waiting for it.
    assert set(states) == {"working"}
    assert main._totals([helpers.load_book(b) for b in books])["failed"] == 0


def test_everything_resumes_when_the_service_answers(monkeypatch, clock, books):
    for _ in range(breaker.FAILURE_THRESHOLD):
        breaker.record_failure("shelfmark", TRIP_DETAIL)

    failing = {"on": True}
    calls: list[str] = []
    monkeypatch.setattr(
        pipeline, "REGISTRY", helpers.stub_registry(calls, failing)
    )
    sched = helpers.scheduler()

    sched.sweep()
    assert all(
        db().stage(b, "acquire_audiobook")["held_by"] == "shelfmark" for b in books
    )

    # The service comes back. The clock walks past the cooldown, one book takes
    # the probe, and from there the sweep does the rest by itself — nobody
    # clicks Retry, and nothing is reset by hand.
    failing["on"] = False
    for _ in range(4):
        clock.advance(breaker.COOLDOWN_BASE_SECONDS + 1)
        sched.sweep()

    assert breaker.states()["shelfmark"]["state"] == "closed"
    rows = [db().stage(b, "acquire_audiobook") for b in books]
    assert all(r["status"] == models.OK for r in rows)
    assert all(r["held_by"] == "" for r in rows)
    assert all(r["transient_since"] is None for r in rows)
    assert db().query_one(
        "SELECT COUNT(*) AS n FROM stage_runs WHERE status = 'failed'"
    )["n"] == 0

    # The close wrote one line, not ten.
    closes = [e for e in db().recent_events(200)
              if e["stage"] == "breaker" and "answering again" in e["message"]]
    assert len(closes) == 1
