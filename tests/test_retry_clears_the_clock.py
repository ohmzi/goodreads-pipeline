"""A retried stage starts with a fresh forgiveness clock, not a spent one.

Listed by the verifier as a consequence rather than a defect, and fixed here
because it is the same false park in the other direction: the per-book clock
ages on the wall, so a >24h outage leaves a book "25 hours into" a clock no
human retry ever spent. `reset_stage` (what Retry runs) cleared the stage and
not the clock, and `bump_transient` reuses an existing `transient_since` — so
the next 503 escalated to `failed` on the first attempt, and every retry after
that did it again. The panel never cleared, and the fix line was the same one
the outage would have produced.
"""

from __future__ import annotations

from app import breaker, models, pipeline
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


def _spent_clock(book_id: int, hours: float = 25.0) -> None:
    """The row a long outage leaves behind: blocked, naming its service, with a
    per-book clock that has been running far past the grace."""
    db().execute(
        "UPDATE stage_runs SET status=?, service=?, failure_kind='', attempts=0, "
        "transient_count=9, transient_since=? WHERE book_id=? AND stage=?",
        (models.BLOCKED, "shelfmark", helpers.iso(-hours * 3600), book_id,
         "acquire_audiobook"),
    )


def test_retry_clears_the_per_book_clock(clock):
    book_id = helpers.audiobook_pending_book("Retried")
    _spent_clock(book_id)

    db().reset_stage(book_id, "acquire_audiobook")

    row = db().stage(book_id, "acquire_audiobook")
    assert row["status"] == models.PENDING
    assert row["transient_since"] is None, "the retry inherited a spent clock"
    assert row["transient_count"] == 0


def test_a_retried_book_gets_a_fresh_grace_instead_of_parking(monkeypatch, clock):
    """The consequence, end to end: Retry, one more 503, and the book is
    forgiven like any first failure rather than written off."""
    monkeypatch.setattr(acquire, "ShelfmarkClient", DeadShelfmark)
    monkeypatch.setattr(acquire, "free_space_gb", lambda path: 999.0)
    monkeypatch.setattr(pipeline, "ADVANCE_WORKERS", 1)

    book_id = helpers.audiobook_pending_book("Retried After An Outage")
    _spent_clock(book_id)

    db().reset_stage(book_id, "acquire_audiobook")      # the operator's Retry
    helpers.scheduler().sweep()                          # one more 503

    row = db().stage(book_id, "acquire_audiobook")
    assert row["status"] == models.BLOCKED, (
        "the retried book was parked on a clock the outage spent, not the book"
    )
    assert row["held_by"] in ("", "shelfmark")
    assert row["attempts"] == 0
    age = db().transient_age_hours(row["transient_since"])
    assert age < 1.0, f"the book's clock still reads {age:.1f}h"


def test_retry_is_repeatable(clock):
    """Twice in a row must not be worse than once — the panel has to clear."""
    book_id = helpers.audiobook_pending_book("Retried Twice")
    _spent_clock(book_id)
    for _ in range(2):
        db().reset_stage(book_id, "acquire_audiobook")
        row = db().stage(book_id, "acquire_audiobook")
        assert row["transient_since"] is None
        assert row["attempts"] == 0
        assert row["held_by"] == ""


def test_reset_stage_clears_the_breaker_hold_too(clock):
    """A held row comes back as pending: `held_by` is part of the stage's state."""
    book_id = helpers.audiobook_pending_book("Held Then Retried")
    for _ in range(breaker.FAILURE_THRESHOLD):
        breaker.record_failure("shelfmark", FAILURE)
    db().mark_held(book_id, "acquire_audiobook", "shelfmark",
                   breaker.hold_detail("shelfmark"))
    assert db().stage(book_id, "acquire_audiobook")["held_by"] == "shelfmark"

    db().reset_stage(book_id, "acquire_audiobook")

    row = db().stage(book_id, "acquire_audiobook")
    assert row["held_by"] == ""
    assert row["status"] == models.PENDING
