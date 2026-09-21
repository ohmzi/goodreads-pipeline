"""The half-open probe's verdict: what a *held* result must not be able to say.

The defect this file exists for was in the field the verdict was read from.
`breaker.hold()` clears `kind` on purpose — a held stage has not failed — and
`pipeline._advance` decided whether the probe had answered by reading exactly
that field:

    was_transient = result.kind in breaker.TRANSIENT_KINDS   # always False
    breaker.resolve_probe(probe, answered=not was_transient) # always True

Half-open counts as holding (`breaker.holding`), so the probing book took
`acquire._run`'s already-held branch, which hands back a hold with no `kind` on
it. `answered` was therefore `True` on the 503 that proves the service is still
down, and the breaker *closed*: it released every held book, reset `trips` and
`opened_at`, and did it again on the next cooldown, forever.

Two things are asserted here, deliberately at two different levels:

* the mechanism, end to end, against the real acquire stage — a probe that
  fails must leave the breaker open on a longer cooldown;
* the invariant, whatever `kind` the result happens to carry — so the test
  cannot be satisfied by special-casing one value of a field that `hold()` is
  documented to clear.
"""

from __future__ import annotations

import pytest

from app import breaker, models, pipeline
from app.clients.base import ClientError
from app.db import db
from app.stages import acquire

import helpers

FAILURE = (
    "shelfmark: release search failed with HTTP 503: Unable to reach download "
    "source. Could not reach Anna's Archive after 10 attempt(s): ReadTimeout"
)


class DeadShelfmark:
    """Shelfmark with the sources behind it unreachable: a 503 on every search."""

    healthy = False
    searches = 0

    def __init__(self, base_url: str | None = None) -> None:
        self.last_rejections: list[str] = []

    def search(self, title: str, author: str = "", content_type: str = "ebook"):
        DeadShelfmark.searches += 1
        if not DeadShelfmark.healthy:
            raise ClientError("shelfmark", FAILURE.split(": ", 1)[1], status=503)
        raise AssertionError("the stub is only ever used while the source is down")

    def close(self) -> None:
        pass


def _trip(clock) -> dict:
    for _ in range(breaker.FAILURE_THRESHOLD):
        breaker.record_failure("shelfmark", FAILURE)
    row = breaker.states()["shelfmark"]
    assert row["state"] == "open"
    return row


def test_a_failing_probe_reopens_the_breaker_instead_of_closing_it(monkeypatch, clock):
    """The reproduction, end to end: 503 on every search, one book probing."""
    monkeypatch.setattr(acquire, "ShelfmarkClient", DeadShelfmark)
    monkeypatch.setattr(acquire, "free_space_gb", lambda path: 999.0)
    monkeypatch.setattr(pipeline, "ADVANCE_WORKERS", 1)

    book_ids = [helpers.audiobook_pending_book(f"Probe {i}") for i in range(3)]
    tripped = _trip(clock)

    # Walk into half-open, which `is_holding` reports as holding like any other.
    clock.advance(breaker.COOLDOWN_BASE_SECONDS + 1)
    assert breaker.states()["shelfmark"]["state"] == "half_open"
    assert breaker.is_holding("shelfmark")

    summary = helpers.scheduler().sweep()

    row = breaker.states()["shelfmark"]
    assert row["state"] == "open", (
        "a probe that failed with 503 closed the breaker: the very failure that "
        "proves the service is down was read as the service answering"
    )
    assert row["trips"] == 2, "the re-probe was not recorded as a new trip"
    # The outage is older than either probe, so its age must keep counting up
    # from the trip — the 24h escalation reads this field and nothing else.
    assert row["opened_at"] == tripped["opened_at"]
    assert row["open_until"] > tripped["open_until"], "the cooldown did not double"

    # The probing book is held, not written off, and the probe is released for
    # the next book rather than left claimed by a worker that has finished.
    probe = db().stage(book_ids[0], "acquire_audiobook")
    assert probe["status"] == models.BLOCKED
    assert probe["held_by"] == "shelfmark"
    assert probe["attempts"] == 0
    assert not row.get("probe_book_id")

    # And nothing downstream of the sweep believed a recovery happened.
    assert not [e for e in db().recent_events(200) if "answering again" in e["message"]]
    assert summary["parked"] == 0
    assert db().query_one(
        "SELECT COUNT(*) AS n FROM stage_runs WHERE status = 'failed'"
    )["n"] == 0


@pytest.mark.parametrize("kind", ["", "server", "network", "busy", "data", "auth"])
def test_a_probe_that_comes_back_held_never_closes_the_breaker(monkeypatch, clock, kind):
    """The invariant, for every value the field could be carrying.

    The probe's verdict was read from `result.kind` — the one field `hold()`
    exists to clear — so this asserts the outcome for every value it could be
    found in, including `''`, which is what the writer leaves behind. A stage
    that came back *held on the probed service* did not answer the question the
    probe asked, and no value of that field may change that.
    """
    _trip(clock)
    clock.advance(breaker.COOLDOWN_BASE_SECONDS + 1)

    def runner(book: dict) -> models.StageResult:
        # Exactly what `acquire._run` hands back when the breaker is holding,
        # with the field forced to each value in turn rather than left as
        # `hold()` writes it: `''` is the real one, and the rest are what a
        # verdict read from that field would be relying on.
        held = breaker.hold(
            models.StageResult.failed("503 from the source",
                                      service="shelfmark"),
            "shelfmark",
        )
        held.kind = kind
        return held

    monkeypatch.setattr(pipeline, "REGISTRY", {s: runner for s in models.STAGES})
    helpers.audiobook_pending_book("Probe Me")

    helpers.scheduler().sweep()

    row = breaker.states()["shelfmark"]
    assert row["state"] == "open", f"a held probe with kind={kind!r} closed the breaker"
    assert row["trips"] == 2


def test_a_sustained_outage_does_not_flap(monkeypatch, clock):
    """Ten cooldowns of a dead service: one incident, escalating, one probe each.

    This is the shape the defect produced in production: one trip line and one
    false recovery line per cooldown, `trips` stuck at 1 so the cooldown never
    grew past 300s, and `opened_at` rewritten every cycle so the outage always
    looked seconds old — which is what made the 24h escalation unreachable and
    left a week-long outage reading "Nothing to fix: these resume by
    themselves".
    """
    monkeypatch.setattr(acquire, "ShelfmarkClient", DeadShelfmark)
    monkeypatch.setattr(acquire, "free_space_gb", lambda path: 999.0)
    monkeypatch.setattr(pipeline, "ADVANCE_WORKERS", 4)

    for i in range(24):
        helpers.audiobook_pending_book(f"Outage {i}")
    sched = helpers.scheduler()

    DeadShelfmark.searches = 0
    sched.sweep()
    first = breaker.states()["shelfmark"]
    assert first["state"] == "open"
    assert first["trips"] == 1

    for _ in range(10):
        clock.advance(breaker.COOLDOWN_BASE_SECONDS + 1)
        DeadShelfmark.searches = 0
        sched.sweep()
        row = breaker.states()["shelfmark"]
        assert row["state"] == "open", "the breaker closed on a service that is down"
        assert row["opened_at"] == first["opened_at"], "the outage restarted its clock"
        # One probe per cooldown at most; the rest of the books are held
        # without reaching the source at all.
        assert DeadShelfmark.searches <= 1, (
            f"{DeadShelfmark.searches} searches against a service known to be down"
        )

    # `trips` grows, so the cooldown doubles instead of sitting at the base.
    final = breaker.states()["shelfmark"]
    assert final["trips"] > 1
    assert breaker.cooldown(final["trips"]) > breaker.cooldown(1)
    assert breaker.cooldown(final["trips"]) <= breaker.COOLDOWN_MAX_SECONDS

    # One incident, in the operator's log: one trip line, no phantom recoveries.
    trips = [e for e in db().recent_events(500)
             if e["stage"] == "breaker" and e["level"] == "error"]
    resumes = [e for e in db().recent_events(500) if "answering again" in e["message"]]
    assert len(trips) == 1, f"{len(trips)} trip lines for one outage"
    assert resumes == [], f"{len(resumes)} false 'answering again' lines"
    assert db().query_one(
        "SELECT COUNT(*) AS n FROM stage_runs WHERE stage = 'failed'"
    )["n"] == 0

    # Every book is still held by the service...
    from app import main

    rows = db().query("SELECT * FROM stage_runs WHERE stage = 'acquire_audiobook'")
    assert all(r["held_by"] == "shelfmark" for r in rows)

    # ...and because `opened_at` survived every re-probe, the outage's *age* is
    # real. This is the half the flap made unreachable: with `opened_at`
    # rewritten every cycle `age_hours` never left 0, so a week-long outage
    # still told the operator "Nothing to fix: these resume by themselves".
    assert "Nothing to fix" in main._fix_for_breaker("shelfmark", "open", 0.5)
    clock.advance(25 * 3600)
    hours = breaker.age_hours(breaker.states()["shelfmark"]["opened_at"])
    assert hours >= 24, f"the outage's own clock only reads {hours:.1f}h"
    assert "Nothing to fix" not in main._fix_for_breaker("shelfmark", "open", hours)
