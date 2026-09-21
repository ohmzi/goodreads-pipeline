"""A book the breaker is holding has to read as held — every one of them.

`mark_held`'s UPDATE was guarded on `status <> 'blocked'`, and the pipeline
calls it for exactly the blocked rows: `_advance` marks a stage the breaker
refused when `status != BLOCKED or held_by != service`, which for a book
forgiven on its own 24h clock (`blocked`, `held_by=''`) is true — so the Python
guard said "write it" and the SQL guard said "no rows". The call was made, the
row was not written, and it was made again on the next sweep and lost again.

The operator-visible consequence, and the reason this is a defect rather than
untidiness: those books appear in *no* group on the panel — `BLOCKED` is not
`FAILED`, so `issues()` skips them, and the breaker's own row counts `held_by` —
while `book_state` reports them `partial`, the "the audiobook does not exist
anywhere" signal that the book_state change existed to prevent.
"""

from __future__ import annotations

from collections import Counter

from app import breaker, main, models, pipeline
from app.clients.base import ClientError
from app.db import db
from app.stages import acquire

import helpers

FAILURE = "shelfmark: release search failed with HTTP 503: Unable to reach download source"


class DeadShelfmark:
    def __init__(self, base_url: str | None = None) -> None:
        self.last_rejections: list[str] = []

    def search(self, title: str, author: str = "", content_type: str = "ebook"):
        raise ClientError("shelfmark", FAILURE.split(": ", 1)[1], status=503)

    def close(self) -> None:
        pass


def _forgiven_row(title: str) -> int:
    """A book exactly as `acquire`'s grace branch leaves it before a trip.

    `blocked`, naming the service, with a live per-book clock and no `held_by`
    — the shape the trip's reclaim does not touch, because it only ever looks
    at `failed` rows.
    """
    book_id = helpers.audiobook_pending_book(title)
    db().execute(
        "UPDATE stage_runs SET status=?, service=?, failure_kind='', attempts=0, "
        "transient_count=2, transient_since=? WHERE book_id=? AND stage=?",
        (models.BLOCKED, "shelfmark", helpers.iso(-60), book_id, "acquire_audiobook"),
    )
    return book_id


def test_mark_held_writes_the_row_it_is_called_for(clock):
    book_id = _forgiven_row("Blip")
    before = db().stage(book_id, "acquire_audiobook")
    assert before["held_by"] == ""

    db().mark_held(book_id, "acquire_audiobook", "shelfmark",
                   breaker.hold_detail("shelfmark"))

    after = db().stage(book_id, "acquire_audiobook")
    assert after["held_by"] == "shelfmark", "the UPDATE matched no rows"
    assert after["status"] == models.BLOCKED
    # The breaker is the clock now, so the book's own has to be cleared —
    # otherwise the hold walks out of the outage carrying an almost-spent 24h
    # budget and parks the moment the service returns.
    assert after["transient_count"] == 0
    assert after["transient_since"] is None
    assert after["failure_kind"] == ""


def test_mark_held_leaves_a_hold_it_already_wrote_alone(clock):
    """One UPDATE per book per outage, not one per book per 15-second sweep."""
    book_id = _forgiven_row("Already Held")
    db().mark_held(book_id, "acquire_audiobook", "shelfmark",
                   breaker.hold_detail("shelfmark"))
    stamped = db().stage(book_id, "acquire_audiobook")["finished_at"]

    db().mark_held(book_id, "acquire_audiobook", "shelfmark", "a second detail")

    row = db().stage(book_id, "acquire_audiobook")
    assert row["finished_at"] == stamped
    assert row["detail"] != "a second detail"


def test_a_hold_for_another_service_does_not_block_the_write(clock):
    """`held_by` is compared against the *inbound* service, not against ''."""
    book_id = _forgiven_row("Stale Hold")
    db().execute(
        "UPDATE stage_runs SET status=?, held_by=? WHERE book_id=? AND stage=?",
        (models.BLOCKED, "kavita", book_id, "acquire_audiobook"),
    )

    db().mark_held(book_id, "acquire_audiobook", "shelfmark",
                   breaker.hold_detail("shelfmark"))

    row = db().stage(book_id, "acquire_audiobook")
    assert row["held_by"] == "shelfmark"
    assert row["service"] == "shelfmark"


def test_the_panel_counts_every_stuck_book(monkeypatch, clock):
    """The acceptance scenario, with the count the operator reads.

    Six books, one worker, every search a 503. The first `FAILURE_THRESHOLD - 1`
    are forgiven on their own clocks before the trip happens, so on the sweep
    that trips they are `blocked` with no `held_by` — and the sweep after that
    is where `mark_held` is called for them. Before the fix that call wrote
    nothing, on that sweep or any of the thousands after it.
    """
    monkeypatch.setattr(acquire, "ShelfmarkClient", DeadShelfmark)
    monkeypatch.setattr(acquire, "free_space_gb", lambda path: 999.0)
    monkeypatch.setattr(pipeline, "ADVANCE_WORKERS", 1)

    book_ids = [helpers.audiobook_pending_book(f"Outage {i}") for i in range(6)]
    sched = helpers.scheduler()

    sched.sweep()
    assert breaker.states()["shelfmark"]["state"] == "open"

    # The sweep after the trip is the one that marks the forgiven rows.
    sched.sweep()

    rows = [db().stage(b, "acquire_audiobook") for b in book_ids]
    stuck = [r for r in rows if r["held_by"] != "shelfmark"]
    assert stuck == [], f"{len(stuck)} stuck books are in no group on the panel"

    group = next(g for g in main.issues()["groups"] if g["stage"] == "held")
    assert len(group["books"]) == 6, (
        f"the panel says {len(group['books'])} books are held while 6 are stuck"
    )
    assert "holding 6 books, none failed" in group["detail"]

    # And the book-level state, which is what the operator reads per book. A
    # held book is being waited on, not settled: `partial` means "the audiobook
    # does not exist anywhere", which is a different and false claim.
    states = Counter(main.book_state(helpers.load_book(b)) for b in book_ids)
    assert states == Counter({"working": 6}), states
    assert main.issues()["counts"]["partial_books"] == 0
    assert main.issues()["counts"]["failed_books"] == 0


def test_marking_does_not_spend_the_stages_budget(monkeypatch, clock):
    """`attempts` stays 0 for every held row, on every sweep."""
    monkeypatch.setattr(acquire, "ShelfmarkClient", DeadShelfmark)
    monkeypatch.setattr(acquire, "free_space_gb", lambda path: 999.0)
    monkeypatch.setattr(pipeline, "ADVANCE_WORKERS", 2)

    book_ids = [helpers.audiobook_pending_book(f"Outage {i}") for i in range(8)]
    sched = helpers.scheduler()
    for _ in range(5):
        clock.advance(breaker.COOLDOWN_BASE_SECONDS + 1)
        sched.sweep()

    rows = [db().stage(b, "acquire_audiobook") for b in book_ids]
    assert all(r["held_by"] == "shelfmark" for r in rows)
    assert all(r["attempts"] == 0 for r in rows)
    assert db().query_one(
        "SELECT COUNT(*) AS n FROM stage_runs WHERE status = 'failed'"
    )["n"] == 0
