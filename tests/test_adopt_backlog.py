"""The 169 books the breaker never saw.

On a deploy, those books are already `failed` and are never attempted again —
so they can never feed a failure in, and a breaker that only counts live
failures sits closed while the panel shows 169 rows. Adopting the recent
backlog is what lets it open on an outage it did not witness, and the trip
reclaims the rows, which is what makes the operator's 169 clicks unnecessary.
"""

from __future__ import annotations

from app import breaker, models
from app.db import db

import helpers

FAILURE = "shelfmark: release search failed with HTTP 503: Unable to reach download source"


def _parked(title: str, age_hours: float, kind: str = "server",
            stage: str = "acquire_audiobook", service: str = "shelfmark") -> int:
    return helpers.seed_book(title, {stage: {
        "status": models.FAILED, "kind": kind, "service": service,
        "detail": FAILURE, "attempts": 5,
        "finished_at": helpers.iso(-int(age_hours * 3600)),
    }})


def test_a_recent_backlog_trips_and_is_reclaimed(clock):
    ids = [_parked(f"Parked {i}", 1.0) for i in range(3)]

    assert breaker.adopt_backlog() == ["shelfmark"]

    for book_id in ids:
        row = db().stage(book_id, "acquire_audiobook")
        assert row["status"] == models.BLOCKED
        assert row["held_by"] == "shelfmark"
        # `attempts` is zeroed, so the rows are not parked any more and the
        # next sweep picks them up rather than returning "parked" for them.
        assert row["attempts"] == 0

    # Idempotent: the trip reclaimed them, so there is nothing left to adopt.
    assert breaker.adopt_backlog() == []
    assert breaker.states()["shelfmark"]["trips"] == 1


def test_a_backlog_older_than_the_window_is_left_alone(clock):
    # Older than the grace, the existing rule already says this is not a
    # passing outage; reviving it would resurrect failures already written off.
    ids = [_parked(f"Ancient {i}", breaker.ADOPT_WINDOW_HOURS + 6) for i in range(3)]
    assert breaker.adopt_backlog() == []
    for book_id in ids:
        assert db().stage(book_id, "acquire_audiobook")["status"] == models.FAILED


def test_two_rows_are_not_an_outage(clock):
    _parked("One", 1.0)
    _parked("Two", 1.0)
    assert breaker.adopt_backlog() == []
    assert breaker.states() == {}


def test_only_transient_kinds_on_the_service_s_own_stages_count(clock):
    # A fact about a release is not outage evidence, however many there are.
    for i in range(4):
        _parked(f"Data {i}", 1.0, kind="data")
    # Nor is a transient failure on a stage this service does not own — `index`
    # fans out over four apps and blaming Shelfmark for it would be a guess.
    for i in range(4):
        _parked(f"Elsewhere {i}", 1.0, stage="index")
    assert breaker.adopt_backlog() == []
    assert breaker.states() == {}


def test_adoption_happens_once_per_sweep(clock):
    """The sweep wires it in, so an operator does not have to run anything."""
    ids = [_parked(f"Parked {i}", 0.5) for i in range(4)]
    summary = helpers.scheduler().sweep()
    assert summary["breaker_adopted"] == ["shelfmark"]
    assert all(
        db().stage(b, "acquire_audiobook")["held_by"] == "shelfmark" for b in ids
    )
    # A second sweep adopts nothing and re-reports nothing.
    second = helpers.scheduler().sweep()
    assert second["breaker_adopted"] == []
    assert len([e for e in db().recent_events(200)
                if e["stage"] == "breaker" and e["level"] == "error"]) == 1
