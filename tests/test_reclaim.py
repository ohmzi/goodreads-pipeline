"""The trip reclaims what it explains — and only that.

`failure_kind IN ('network','server','busy')` is the whole predicate. A `data`
failure is a fact about a release, not a fault, and folding an 18-book
"Shelfmark failed to download these" group into an outage hold would be the
"a fact about the world is not a fault" mistake in the other direction.
"""

from __future__ import annotations

from app import breaker, main, models
from app.db import db

import helpers

FAILURE = "shelfmark: release search failed with HTTP 503: Unable to reach download source"


def _trip() -> None:
    for _ in range(breaker.FAILURE_THRESHOLD):
        breaker.record_failure("shelfmark", FAILURE)


def test_server_and_busy_are_reclaimed_and_data_is_not(clock):
    server_book = helpers.seed_book("A Little Life", {"acquire_audiobook": {
        "status": models.FAILED, "kind": "server", "service": "shelfmark",
        "detail": FAILURE, "attempts": 1}})
    busy_book = helpers.seed_book("Rate Limited", {"acquire_audiobook": {
        "status": models.FAILED, "kind": "busy", "service": "shelfmark",
        "detail": "shelfmark: too many requests", "attempts": 1}})
    data_book = helpers.seed_book("Love in Lowercase", {"acquire_audiobook": {
        "status": models.FAILED, "kind": "data", "service": "",
        "detail": "no audiobook releases found for 'Love in Lowercase'",
        "attempts": 1}})
    claimed_book = helpers.seed_book("Shelfmark Said No", {"acquire_audiobook": {
        "status": models.FAILED, "kind": "data", "service": "shelfmark",
        "detail": "Shelfmark reported error: no sources — after 4 different releases",
        "attempts": 4}})

    _trip()

    for book_id in (server_book, busy_book):
        row = db().stage(book_id, "acquire_audiobook")
        assert row["status"] == models.BLOCKED
        assert row["held_by"] == "shelfmark"
        assert row["failure_kind"] == ""
        assert row["attempts"] == 0
        # The writer/reader invariant: the held row still carries the service's
        # own words and its name, which is what the "Open" link lands on.
        assert "500" in row["detail"] or "503" in row["detail"] or "too many" in row["detail"]
        assert "Shelfmark" in row["detail"]

    for book_id in (data_book, claimed_book):
        row = db().stage(book_id, "acquire_audiobook")
        assert row["status"] == models.FAILED, "a fact about a release was absorbed"
        assert row["held_by"] == ""
        assert row["failure_kind"] == "data"


def test_a_data_failure_keeps_its_own_group_and_stays_non_actionable(clock):
    helpers.seed_book("Love in Lowercase", {"acquire_audiobook": {
        "status": models.FAILED, "kind": "data", "service": "",
        "detail": "no audiobook releases found for 'Love in Lowercase'"}})
    _trip()

    payload = main.issues()
    group = next(g for g in payload["groups"]
                 if g["detail"] == "No audiobook exists in any configured source")
    assert group["actionable"] is False
    assert len(group["books"]) == 1
    # The breaker row and the fact row coexist, which is the point: one is a
    # fault, the other is the world.
    assert any(g["stage"] == "held" for g in payload["groups"])


def test_nothing_is_reclaimed_when_the_breaker_never_trips(clock):
    book_id = helpers.seed_book("Fine", {"acquire_audiobook": {
        "status": models.FAILED, "kind": "server", "service": "shelfmark",
        "detail": FAILURE, "attempts": 1}})
    breaker.record_failure("shelfmark", FAILURE)      # one, below the threshold
    row = db().stage(book_id, "acquire_audiobook")
    assert row["status"] == models.FAILED
