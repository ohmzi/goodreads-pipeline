"""Seeding and stubbing helpers shared by the tests."""

from __future__ import annotations

import time
from datetime import datetime, timezone

from app import models
from app.clients.base import ClientError
from app.db import db


def iso(offset_seconds: float = 0.0) -> str:
    """A real-clock ISO stamp, optionally offset into the past."""
    return datetime.fromtimestamp(
        time.time() + offset_seconds, timezone.utc
    ).isoformat(timespec="seconds")


def seed_book(title: str, stages: dict | None = None) -> int:
    """Insert a book and set the given stages. Returns the book id.

    A stage's value is either a bare status string or a dict of columns, so a
    test states only what it cares about and every other stage stays `pending`
    exactly as `upsert_book` leaves it.
    """
    book_id, _ = db().upsert_book(models.Book(goodreads_id=f"gr-{title}", title=title))
    for stage, spec in (stages or {}).items():
        if isinstance(spec, str):
            spec = {"status": spec}
        db().execute(
            "UPDATE stage_runs SET status=?, detail=?, failure_kind=?, service=?, "
            "held_by=?, output_path=?, attempts=?, finished_at=?, queued_at=? "
            "WHERE book_id=? AND stage=?",
            (
                spec.get("status", models.PENDING),
                spec.get("detail", ""),
                spec.get("kind", ""),
                spec.get("service", ""),
                spec.get("held_by", ""),
                spec.get("output_path"),
                spec.get("attempts", 0),
                spec.get("finished_at") or iso(),
                spec.get("queued_at"),
                book_id,
                stage,
            ),
        )
    return book_id


#: Every stage satisfied, with the ebook actually placed.
_EBOOK_DONE = {
    "classify": models.OK,
    "place": {"status": models.OK, "output_path": '{"ebook": "/books/a.epub"}'},
    "index": models.OK,
    "notebook": models.OK,
    "verify": models.OK,
    "shelve": models.OK,
}


def ebook_pending_book(title: str) -> int:
    """A book whose only remaining work is the ebook acquisition.

    `acquire_ebook` is deliberately the pending stage rather than
    `acquire_audiobook`: the audiobook path checks free disk space before it
    searches, and a test should not depend on how full the machine running it
    happens to be.
    """
    return seed_book(title, dict(_EBOOK_DONE))


def audiobook_pending_book(title: str) -> int:
    """A book whose only remaining work is the audiobook acquisition.

    This is the shape of the observed outage: everything else on the book has
    settled, so a breaker holding the audiobook is the only thing the sweep can
    report, and `_advance` returns "held" rather than "ran".
    """
    done = dict(_EBOOK_DONE)
    done["acquire_ebook"] = models.OK
    return seed_book(title, done)


def load_book(book_id: int) -> dict:
    """The book dict `_advance` expects, straight from the database."""
    return next(b for b in db().list_books() if b["id"] == book_id)


def scheduler():
    """A Scheduler that will not probe, discover or reconcile.

    The timers are pushed to "just ran" so `_sweep` skips all three: they are
    the only parts of a sweep that reach the network, and none of them is what
    these tests are about.
    """
    from app import pipeline

    sched = pipeline.Scheduler(interval=15)
    stamp = time.time()
    sched._last_health = sched._last_discover = sched._last_reconcile = stamp
    return sched


def stub_registry(calls: list, failing: dict, client_error=None):
    """A REGISTRY whose runners touch nothing.

    `failing["on"]` decides whether the acquire stages raise. `client_error`
    builds the error so a test can choose between a 503 and a 429.
    """
    error = client_error or (
        lambda: ClientError(
            "shelfmark",
            "release search failed with HTTP 503: Unable to reach download "
            "source. Could not reach Anna's Archive after 10 attempt(s): "
            "ReadTimeout",
            status=503,
        )
    )

    def runner_for(stage: str):
        def run(book: dict) -> models.StageResult:
            calls.append(stage)
            if failing["on"] and stage in ("acquire_ebook", "acquire_audiobook"):
                raise error()
            return models.StageResult.ok(f"{stage} ok")

        return run

    return {stage: runner_for(stage) for stage in models.STAGES}
