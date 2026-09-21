"""A reclaim must not eat the one line that says what a book is downloading.

`mark_held` states the invariant at length: `queued_at`, `output_path`,
`artifact` and the `detail` of a row that has a `queued_at` are left alone,
because "queued from MyAnonaMouse (a good release)" is the only place the name
of the release being fetched exists. Losing it sends the operator looking for a
problem on a book that is downloading fine.

`hold_rows` — the breaker's reclaim half, which converts the `failed` rows a
trip explains into holds — had the `queued_at` guard on everything except the
`detail`, so the one writer the invariant was not applied to was the one that
runs over hundreds of rows at a time during an outage. Message loss only; the
row stays accounted for and the download is unaffected. It is still loss: the
line is never rewritten, because the row comes back as `blocked` with
`attempts=0` and nothing else ever writes that sentence.
"""

from __future__ import annotations

from app import breaker, models
from app.db import db

import helpers

FAILURE = (
    "shelfmark: release search failed with HTTP 503: Unable to reach download "
    "source"
)
RELEASE = "queued from MyAnonaMouse (a good release)"
HOLD = "held: Shelfmark cannot reach its sources — retries by itself when it recovers"


def _queued_failed_book(title: str) -> int:
    """A book whose download is queued *and* whose stage row is `failed`.

    The shape `reclaim` selects: a transient failure kind, so the trip owns it,
    on a row that also carries a live `queued_at` and the release it is
    fetching. A download can outlive the failure that was recorded next to it —
    the queued marker is what `_watch` picks the task back up from.
    """
    return helpers.seed_book(title, {
        "classify": models.OK,
        "acquire_ebook": models.OK,
        "acquire_audiobook": {
            "status": models.FAILED,
            "kind": "network",
            "service": "shelfmark",
            "detail": RELEASE,
            "queued_at": helpers.iso(-600),
            "output_path": '["src-1"]',
            "attempts": 2,
        },
        "place": {"status": models.OK, "output_path": '{"ebook": "/books/a.epub"}'},
        "index": models.OK,
        "notebook": models.OK,
        "verify": models.OK,
        "shelve": models.OK,
    })


def test_hold_rows_keeps_the_release_a_queued_row_is_fetching():
    """The guard, at the row level, next to the writer that lacked it."""
    book_id = _queued_failed_book("Downloading")

    stage_id = db().query_one(
        "SELECT id FROM stage_runs WHERE book_id=? AND stage='acquire_audiobook'",
        (book_id,),
    )["id"]

    changed = db().hold_rows([stage_id], "shelfmark", HOLD)

    assert changed == 1
    row = db().stage(book_id, "acquire_audiobook")
    assert row["detail"] == RELEASE, (
        f"a reclaim wrote the outage sentence over the release name: {row['detail']!r}"
    )
    # The rest of the hold is still written: the row is the breaker's now, and
    # the download it describes is untouched.
    assert row["status"] == models.BLOCKED
    assert row["held_by"] == "shelfmark"
    assert row["attempts"] == 0, "the breaker is the clock now"
    assert row["queued_at"]
    assert row["output_path"] == '["src-1"]'


def test_hold_rows_still_writes_the_hold_on_a_row_that_is_not_downloading():
    """The other half of the guard, which is what keeps it from being a no-op.

    A failed row with no `queued_at` has no sentence of its own to protect, and
    "waiting on a downed service" is exactly why nothing is happening to it.
    """
    book_id = _queued_failed_book("Failed")
    stage_id = db().query_one(
        "SELECT id FROM stage_runs WHERE book_id=? AND stage='acquire_audiobook'",
        (book_id,),
    )["id"]
    db().execute("UPDATE stage_runs SET queued_at=NULL WHERE id=?", (stage_id,))

    db().hold_rows([stage_id], "shelfmark", HOLD)

    row = db().stage(book_id, "acquire_audiobook")
    assert row["detail"] == HOLD
    assert row["held_by"] == "shelfmark"


def test_a_trip_through_the_breaker_keeps_the_release(clock):
    """The same thing through the caller that matters: `breaker.reclaim`.

    This is the path an outage actually takes — three transient failures, a
    trip, and the reclaim sweeping up every `failed` row the outage explains,
    hundreds at a time, in one statement per 500 rows. The row-level test above
    is the same code; this one is the reason anyone would run it.
    """
    book_id = _queued_failed_book("Downloading Through The Outage")

    for _ in range(breaker.FAILURE_THRESHOLD):
        breaker.record_failure("shelfmark", FAILURE)

    assert breaker.states()["shelfmark"]["state"] == "open"
    row = db().stage(book_id, "acquire_audiobook")
    assert row["detail"] == RELEASE, (
        "the trip's reclaim destroyed the release name of a book whose "
        f"download is in flight: {row['detail']!r}"
    )
    assert row["status"] == models.BLOCKED
    assert row["held_by"] == "shelfmark"
    assert row["queued_at"], "the queued marker went with the detail"
    assert row["output_path"] == '["src-1"]'
