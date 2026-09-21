"""A book held through an outage must not park the moment the outage ends.

The per-book grace clock ages on the wall, not on attempts. A book whose
`transient_since` was set during an outage would therefore be "25 hours into"
a clock the next time it failed — having been tried for one of those hours —
and `_forgive_transient` would park it on the spot. That is a false park the
breaker itself would have manufactured, so closing the breaker has to clear the
clock for everything the outage left it running on.
"""

from __future__ import annotations

from app import breaker, models, pipeline
from app.db import db

import helpers


def test_a_pretrip_forgiven_row_does_not_park_when_the_service_returns(
    monkeypatch, clock
):
    book_id = helpers.ebook_pending_book("A Little Life")

    # What the first books of an outage look like: `acquire._run` forgave them
    # on its own 24h branch *before* the breaker tripped, so they are blocked,
    # they name the service, and they carry a live clock — but no `held_by`,
    # because the trip's reclaim only looks at `failed` rows.
    db().execute(
        "UPDATE stage_runs SET status=?, service=?, failure_kind='', attempts=0, "
        "transient_count=7, transient_since=? WHERE book_id=? AND stage=?",
        (models.BLOCKED, "shelfmark", helpers.iso(-25 * 3600), book_id,
         "acquire_ebook"),
    )

    for _ in range(breaker.FAILURE_THRESHOLD):
        breaker.record_failure("shelfmark", "shelfmark: release search failed with "
                                           "HTTP 503: Unable to reach download source")
    row = db().stage(book_id, "acquire_ebook")
    assert row["held_by"] == "", "the trip should not have touched this row"
    assert row["transient_since"] is not None

    # The service answers.
    breaker.record_success("shelfmark")

    row = db().stage(book_id, "acquire_ebook")
    assert row["transient_since"] is None, "the outage's clock outlived the outage"
    assert row["transient_count"] == 0

    # And the consequence, end to end: the book fails again for a fresh reason
    # and gets a fresh grace rather than being written off on the stale clock.
    failing = {"on": True}
    monkeypatch.setattr(
        pipeline, "REGISTRY", helpers.stub_registry([], failing)
    )
    helpers.scheduler().sweep()

    row = db().stage(book_id, "acquire_ebook")
    assert row["status"] == models.BLOCKED, (
        "the book parked on a clock the outage spent, not the book"
    )
    assert row["transient_since"] is not None
    assert db().transient_age_hours(row["transient_since"]) < 1.0


def test_a_book_that_earned_its_own_clock_is_left_alone(clock):
    """The reset is scoped to the service that was holding, nothing wider."""
    book_id = helpers.ebook_pending_book("Earned It")
    db().execute(
        "UPDATE stage_runs SET status=?, service=?, transient_since=?, "
        "transient_count=3 WHERE book_id=? AND stage=?",
        (models.BLOCKED, "kavita", helpers.iso(-25 * 3600), book_id, "acquire_ebook"),
    )

    for _ in range(breaker.FAILURE_THRESHOLD):
        breaker.record_failure("shelfmark", "shelfmark: ... 503 ...")
    breaker.record_success("shelfmark")

    row = db().stage(book_id, "acquire_ebook")
    assert row["service"] == "kavita"
    assert row["transient_since"] is not None, "another service's clock was forgiven"
