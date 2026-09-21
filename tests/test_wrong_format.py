"""An audiobook search that only finds an epub is not a fault.

Group 5 of the observed failure: "found 1 release(s) for 'Love in Lowercase'
but every one was the wrong format — not a book", reported as an alarming,
actionable sentence with the rejection reason appended. It is really the same
thing as "no audiobook exists", which this codebase already treats as a quiet
fact about the world; the rejection detail belongs on the book's own page, not
in the headline.

These drive the real acquire stage with a stubbed Shelfmark, so what is tested
is the sentence the pipeline actually produces.
"""

from __future__ import annotations

from app import breaker, main, models
from app.db import db
from app.stages import acquire

import helpers

REJECTION = ("Francesc Miralles - Love in Lowercase (epub) — categorised as a "
             "book, not an audiobook")


class StubShelfmark:
    """A Shelfmark that searched successfully and found nothing usable."""

    content_type = "audiobook"
    rejections: list[str] = []

    def __init__(self, base_url: str | None = None) -> None:
        self.last_rejections: list[str] = []

    def search(self, title: str, author: str = "", content_type: str = "ebook"):
        self.last_rejections = list(StubShelfmark.rejections)
        return []

    def close(self) -> None:
        pass


def _sweep(monkeypatch, title: str, rejections: list[str],
           stage: str = "acquire_audiobook") -> int:
    """Run the real stage for one book and return its id."""
    StubShelfmark.rejections = rejections
    monkeypatch.setattr(acquire, "ShelfmarkClient", StubShelfmark)
    # The audiobook path checks the disk before it searches.
    monkeypatch.setattr(acquire, "free_space_gb", lambda path: 999.0)
    seed = (helpers.audiobook_pending_book if stage == "acquire_audiobook"
            else helpers.ebook_pending_book)
    book_id = seed(title)
    helpers.scheduler().sweep()
    return book_id


def test_a_wrong_format_audiobook_search_reads_as_no_audiobook(monkeypatch):
    book_id = _sweep(monkeypatch, "Love in Lowercase", [REJECTION])
    row = db().stage(book_id, "acquire_audiobook")

    assert row["status"] == models.FAILED
    assert row["failure_kind"] == "data", "a fact about availability, not a fault"
    assert row["detail"].startswith("no audiobook releases found for")
    # The detail a human wants on *this* book's page is still there.
    assert REJECTION in row["detail"]
    assert "1 release(s)" in row["detail"]

    bucket, headline = main._cause_bucket("acquire_audiobook", row["failure_kind"],
                                          row["detail"])
    assert bucket == "no-audiobook"
    assert headline == "No audiobook exists in any configured source"
    assert main._actionable(row["failure_kind"], bucket) is False


def test_a_wrong_format_ebook_search_reads_as_no_ebook(monkeypatch):
    book_id = _sweep(monkeypatch, "Some Book",
                     ["Film.2019.1080p — release name looks like video (1080p)"],
                     stage="acquire_ebook")
    row = db().stage(book_id, "acquire_ebook")
    assert row["failure_kind"] == "data"
    assert row["detail"].startswith("no ebook releases found for")
    bucket, headline = main._cause_bucket("acquire_ebook", row["failure_kind"],
                                          row["detail"])
    assert bucket == "no-ebook"
    assert main._actionable(row["failure_kind"], bucket) is False


def test_the_group_carries_the_reason_and_asks_nothing_of_a_human(monkeypatch):
    _sweep(monkeypatch, "Love in Lowercase", [REJECTION])
    payload = main.issues()

    group = next(g for g in payload["groups"]
                 if g["detail"] == "No audiobook exists in any configured source")
    assert group["actionable"] is False
    assert len(group["books"]) == 1
    # The rejection reason is surfaced for the book, without leading.
    assert {"id", "title"} <= set(group["books"][0])
    assert "categorised as a book" in group["sample"]
    assert "Nothing to fix" in group["fix"]

    # And a plain "nothing found at all" search is still non-actionable, so the
    # two forms of the same fact group together as one row.
    _sweep(monkeypatch, "Nothing At All", [])
    payload = main.issues()
    group = next(g for g in payload["groups"]
                 if g["detail"] == "No audiobook exists in any configured source")
    assert len(group["books"]) == 2


def test_a_wrong_format_failure_never_trips_or_is_reclaimed(monkeypatch):
    book_id = _sweep(monkeypatch, "Love in Lowercase", [REJECTION])
    # No service is named and the kind is not transient, so it is not outage
    # evidence and nothing may absorb it.
    assert breaker.adopt_backlog() == []
    assert breaker.states() == {}
    row = db().stage(book_id, "acquire_audiobook")
    assert row["status"] == models.FAILED
    assert row["failure_kind"] == "data"
    assert row["held_by"] == ""
