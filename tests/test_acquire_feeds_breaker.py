"""Drive the real acquire stage against a stubbed Shelfmark: 503, 503, success.

This is the acceptance test for the whole class of problem. The stub is a
stand-in for Shelfmark refusing to reach Anna's Archive, and what is asserted
is the operator-visible outcome:

* no book is written `failed` while the source is down;
* exactly ONE incident is recorded, not one per book;
* the books proceed by themselves, with no Retry, once the source answers.

`pipeline.ADVANCE_WORKERS` is pinned to 1 here so the ordering is
deterministic: the first `FAILURE_THRESHOLD - 1` books are forgiven by the
per-book grace branch, the trip happens on the next one, and every book after
that is held.
"""

from __future__ import annotations

from app import breaker, models, pipeline
from app.clients.base import ClientError
from app.clients.shelfmark import Release
from app.db import db
from app.stages import acquire

import helpers

FAILURE = (
    "shelfmark: release search failed with HTTP 503: Unable to reach download "
    "source. Could not reach Anna's Archive after 10 attempt(s): ReadTimeout"
)


class StubShelfmark:
    """A Shelfmark that is down, until `healthy` is flipped."""

    healthy = False

    def __init__(self, base_url: str | None = None) -> None:
        self.last_rejections: list[str] = []

    def search(self, title: str, author: str = "", content_type: str = "ebook"):
        if not StubShelfmark.healthy:
            raise ClientError("shelfmark", FAILURE.split(": ", 1)[1], status=503)
        self.last_rejections = []
        return [
            Release(source="libgen", source_id=f"id-{title}", title=title,
                    format="epub")
        ]

    def rank(self, releases, title: str, author: str = ""):
        return releases

    def download(self, release, title: str, content_type: str = "ebook"):
        return {"task_id": f"task-{release.source_id}"}

    def iter_tasks(self):
        return iter(())

    def find_task(self, title: str, author: str = "", source_ids=()):
        # The download is queued and not yet started, which is a `blocked`
        # stage — the point of the last assertion below.
        return None

    def close(self) -> None:
        pass


def _sweep() -> dict:
    return helpers.scheduler().sweep()


def test_one_failure_is_one_piece_of_trip_evidence(monkeypatch, clock):
    """Counted once, not twice.

    `acquire._run` feeds the breaker for the failures it forgives itself, and
    `_advance` feeds it for the `failed` results it sees. A failure that
    crosses the acquire grace must be counted by exactly one of them, or
    "three consecutive failures" is a looser claim than it reads as.
    """
    StubShelfmark.healthy = False
    monkeypatch.setattr(acquire, "ShelfmarkClient", StubShelfmark)
    monkeypatch.setattr(acquire, "free_space_gb", lambda path: 999.0)
    monkeypatch.setattr(pipeline, "ADVANCE_WORKERS", 1)

    book_id = helpers.audiobook_pending_book("Past The Grace")
    # This book's own clock expired hours ago, so the stage returns a real
    # `failed` result and `_advance` is the one that records it.
    db().execute(
        "UPDATE stage_runs SET transient_since=?, transient_count=9 "
        "WHERE book_id=? AND stage=?",
        (helpers.iso(-25 * 3600), book_id, "acquire_audiobook"),
    )

    helpers.scheduler().sweep()

    row = db().stage(book_id, "acquire_audiobook")
    assert row["status"] == models.FAILED, "the grace did not expire as it should"
    assert row["failure_kind"] == "server"
    assert row["held_by"] == ""
    assert breaker.states()["shelfmark"]["failures"] == 1


def test_503_then_503_then_success(monkeypatch, clock):
    StubShelfmark.healthy = False
    monkeypatch.setattr(acquire, "ShelfmarkClient", StubShelfmark)
    monkeypatch.setattr(pipeline, "ADVANCE_WORKERS", 1)
    # The audiobook path checks the disk before it searches; the test is not
    # about how full this machine is.
    monkeypatch.setattr(acquire, "free_space_gb", lambda path: 999.0)

    # The audiobook is the one outstanding stage, so each book makes exactly
    # one search per sweep and the failure count is easy to reason about.
    book_ids = [helpers.audiobook_pending_book(f"Outage Book {i}") for i in range(6)]

    # -- the source is down -------------------------------------------------
    first = _sweep()

    rows = [db().stage(b, "acquire_audiobook") for b in book_ids]
    assert first["parked"] == 0
    assert db().query_one(
        "SELECT COUNT(*) AS n FROM stage_runs WHERE status = 'failed'"
    )["n"] == 0, "a book was written off for an upstream outage"
    assert all(r["status"] == models.BLOCKED for r in rows)

    # The trip takes FAILURE_THRESHOLD books to happen, so the first two are
    # forgiven on the per-book clock before it does (`held_by=''`, a live
    # `transient_since`) and every book after it is held. At most two books per
    # outage can be in that state, and the close-time reset below is what stops
    # their clock turning into a park.
    held = [r for r in rows if r["held_by"] == "shelfmark"]
    forgiven = [r for r in rows if r["held_by"] == ""]
    assert len(held) == 6 - (breaker.FAILURE_THRESHOLD - 1)
    assert len(forgiven) == breaker.FAILURE_THRESHOLD - 1

    # The breaker acted inside the FIRST sweep, seconds in — not at the 24h
    # book-grace boundary, which is what let an outage park 169 books.
    assert breaker.states()["shelfmark"]["trips"] == 1
    oldest = max(db().transient_age_hours(r["transient_since"]) for r in rows)
    assert oldest < 0.05, f"the per-book clock reached {oldest:.2f}h"
    # attempts is zeroed by the hold, so nothing is "parked" after the outage —
    # which is what makes the recovery automatic rather than a 169-click job.
    assert all(r["attempts"] == 0 for r in rows)

    # ONE incident, one log line. No per-book failure events at all.
    trips = [e for e in db().recent_events(200)
             if e["stage"] == "breaker" and e["level"] == "error"]
    assert len(trips) == 1
    assert trips[0]["service"] == "shelfmark"
    per_book = [e for e in db().recent_events(200)
                if e["level"] == "error" and e["stage"] == "acquire_audiobook"]
    assert per_book == []

    # -- still down, next sweep: nothing new ---------------------------------
    second = _sweep()
    rows = [db().stage(b, "acquire_audiobook") for b in book_ids]
    assert second["held"] == 6
    assert all(r["status"] == models.BLOCKED for r in rows)
    assert db().query_one(
        "SELECT COUNT(*) AS n FROM stage_runs WHERE status = 'failed'"
    )["n"] == 0
    assert len([e for e in db().recent_events(200)
                if e["stage"] == "breaker" and e["level"] == "error"]) == 1, \
        "the outage was re-reported instead of staying one incident"

    # -- the source answers --------------------------------------------------
    StubShelfmark.healthy = True
    for _ in range(3):
        clock.advance(breaker.COOLDOWN_BASE_SECONDS + 1)
        _sweep()

    assert breaker.states().get("shelfmark", {}).get("state", "closed") == "closed"
    rows = [db().stage(b, "acquire_audiobook") for b in book_ids]
    assert all(r["held_by"] == "" for r in rows), "a book stayed held after recovery"
    assert all(r["status"] == models.BLOCKED for r in rows)   # queued, not failed
    assert all(r["queued_at"] for r in rows), "the downloads did not start"
    assert db().query_one(
        "SELECT COUNT(*) AS n FROM stage_runs WHERE status = 'failed'"
    )["n"] == 0
