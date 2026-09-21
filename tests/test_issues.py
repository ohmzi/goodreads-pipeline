"""The JS contract for the breaker's one row, asserted field by field.

`renderIssuePreview` in `app.js` reads exactly `actionable`, `stage` (through
`STAGE_LABEL[g.stage] || g.stage`), `books.length`, `service`, `detail` and
`fix`. Nothing under `app/static/` is touched by this change, so every field it
needs has to already be right here.

The stage name is the load-bearing one: it must never equal a real stage, or
the book detail view's per-stage `fix` lookup would match this synthetic group
and replace a book's own explanation with the outage's.
"""

from __future__ import annotations

from app import breaker, main, models
from app.db import db

import helpers

FAILURE = "shelfmark: release search failed with HTTP 503: Unable to reach download source"


def _held_books(count: int, stage: str = "acquire_audiobook") -> list[int]:
    ids = [helpers.audiobook_pending_book(f"Held {i}") for i in range(count)]
    for _ in range(breaker.FAILURE_THRESHOLD):
        breaker.record_failure("shelfmark", FAILURE)
    for book_id in ids:
        db().mark_held(book_id, stage, "shelfmark", breaker.hold_detail("shelfmark"))
    return ids


def test_one_group_for_the_held_service(clock):
    ids = _held_books(10)
    payload = main.issues()

    held = [g for g in payload["groups"] if g["stage"] == "held"]
    assert len(held) == 1
    group = held[0]

    assert group["service"] == "shelfmark"
    assert group["service_label"] == "Shelfmark"
    assert group["impact"] == "no downloads can start"
    assert group["actionable"] is True
    assert len(group["books"]) == 10
    assert {b["id"] for b in group["books"]} == set(ids)
    assert "holding 10 books, none failed" in group["detail"]
    assert group["fix"].startswith("Nothing to fix")
    assert "Shelfmark's service page" in group["fix"]
    assert group["kind"] == "network"
    assert group["sample"]

    # The pseudo-stage must not collide with a real one.
    assert group["stage"] not in models.STAGES

    # The tile and the panel read the same rows, which is the other half of the
    # fix: 169 held books are not 169 failed books.
    assert payload["counts"]["failed_books"] == 0
    assert payload["counts"]["issue_groups"] == 1


def test_a_book_held_on_two_stages_is_one_book(clock):
    for _ in range(breaker.FAILURE_THRESHOLD):
        breaker.record_failure("shelfmark", FAILURE)
    book_id = helpers.audiobook_pending_book("Both")
    db().mark_held(book_id, "acquire_ebook", "shelfmark",
                   breaker.hold_detail("shelfmark"))
    db().mark_held(book_id, "acquire_audiobook", "shelfmark",
                   breaker.hold_detail("shelfmark"))

    group = next(g for g in main.issues()["groups"] if g["stage"] == "held")
    assert len(group["books"]) == 1


def test_the_other_groups_are_untouched(clock):
    helpers.seed_book("Needs A Rule", {"classify": {
        "status": models.FAILED, "kind": "", "service": "",
        "detail": "No genre rule matched", "attempts": 3}})
    _held_books(3)
    payload = main.issues()

    stages = {g["stage"] for g in payload["groups"]}
    assert "classify" in stages, "a genuine manual task went missing"
    assert "held" in stages


def test_past_the_grace_the_row_escalates_and_stays_one_row(clock):
    _held_books(4)
    clock.advance(26 * 3600)
    payload = main.issues()

    held = [g for g in payload["groups"] if g["stage"] == "held"]
    assert len(held) == 1
    group = held[0]
    assert "for 26 hours" in group["detail"]
    assert "past the 24h grace" in group["fix"]
    assert group["actionable"] is True


def test_the_breaker_state_is_visible_without_parsing_prose(clock):
    _held_books(2)
    payload = main._with_breakers({"services": [
        {"service": "shelfmark"}, {"service": "kavita"},
    ]})
    shelfmark = payload["services"][0]["breaker"]
    assert shelfmark["state"] in ("open", "half_open")
    assert shelfmark["trips"] == 1
    assert "503" in shelfmark["last_failure"]
    assert payload["services"][1]["breaker"]["state"] == "closed"
