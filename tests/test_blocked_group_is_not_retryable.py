"""A failure group whose service is breaker-held must not offer "Retry all".

The panel used to let an operator click "Retry all" on the 18 audiobooks that
failed with `Shelfmark failed to download these` while Shelfmark's own breaker
was open — spending a click to re-queue books into the exact wait they were
already in, and reading as though the failure were the operator's to fix when
the service is simply down.

`actionable` cannot be the flag that expresses this: `renderIssuePreview`
filters the whole panel on it before a row is ever built, so turning it off
here would make the group disappear from "What needs you" rather than show it
differently — the group is real work still parked and belongs on the panel.
`blocked_by` is the separate signal the frontend uses to swap the button for a
link to the service actually in the way.
"""

from __future__ import annotations

from app import breaker, main, models

import helpers


FAILURE = "shelfmark: release search failed with HTTP 503: Unable to reach download source"


def _trip_shelfmark(clock) -> None:
    for _ in range(breaker.FAILURE_THRESHOLD):
        breaker.record_failure("shelfmark", FAILURE)


def _acquire_data_failure_group(count: int) -> None:
    """`count` books failing acquire_audiobook the way a dead release does.

    Same shape as the real "Shelfmark failed to download these" group: `kind`
    is `data` (a fact about this book's releases, not the service), and no
    `service` is recorded on the stage row — acquire's own failure results
    never set one for this branch, which is exactly why `service_for_stage`
    has to fill it in rather than the group's own `service` field.
    """
    for i in range(count):
        helpers.seed_book(f"Dead release {i}", {"acquire_audiobook": {
            "status": models.FAILED, "kind": "data", "service": "",
            "detail": "Shelfmark reported error: Download not found — after 4 different releases",
        }})


def test_retry_all_is_withheld_while_the_service_is_held(clock):
    _acquire_data_failure_group(18)
    _trip_shelfmark(clock)

    groups = main.issues()["groups"]
    target = next(g for g in groups if g["stage"] == "acquire_audiobook")

    assert target["blocked_by"] == "shelfmark"
    assert "Shelfmark is held right now" in target["fix"]


def test_the_group_stays_on_the_panel(clock):
    """The whole point: parked books are not the same as nothing to look at."""
    _acquire_data_failure_group(18)
    _trip_shelfmark(clock)

    groups = main.issues()["groups"]
    target = next(g for g in groups if g["stage"] == "acquire_audiobook")

    assert target["actionable"] is True
    assert len(target["books"]) == 18


def test_without_a_held_service_the_button_is_offered_as_before(clock):
    _acquire_data_failure_group(18)

    groups = main.issues()["groups"]
    target = next(g for g in groups if g["stage"] == "acquire_audiobook")

    assert "blocked_by" not in target
    assert target["fix"] == main._fix_for(
        "acquire_audiobook", "data",
        "Shelfmark reported error: Download not found — after 4 different releases",
    )


def test_the_original_cause_is_kept_not_replaced(clock):
    """The operator should not lose *why* it failed when told to wait."""
    _acquire_data_failure_group(1)
    _trip_shelfmark(clock)

    target = next(g for g in main.issues()["groups"] if g["stage"] == "acquire_audiobook")

    assert "Retry from the book's detail view" in target["fix"]


def test_a_service_specific_group_is_blocked_by_its_own_service(clock):
    """A group that already names a service (e.g. verify) uses that name
    directly, rather than falling through to the stage-based lookup — the two
    must agree when both are available."""
    helpers.seed_book("Unindexed", {
        "classify": models.OK,
        "place": {"status": models.OK, "output_path": '{"ebook": "/books/a.epub"}'},
        "verify": {
            "status": models.FAILED, "kind": "notfound", "service": "opennotebook",
            "detail": "confirmed in Kavita — MISSING from Open Notebook | "
                      "Open Notebook: no source for this file",
        },
    })
    for _ in range(breaker.FAILURE_THRESHOLD):
        breaker.record_failure("opennotebook", "open notebook: could not reach")

    target = next(g for g in main.issues()["groups"] if g["stage"] == "verify")
    assert target["blocked_by"] == "opennotebook"


def test_the_held_row_itself_is_never_marked_blocked(clock):
    """The synthetic breaker row is the one place `Clear hold` lives; it must
    not also grow a `blocked_by` pointing at itself."""
    for _ in range(breaker.FAILURE_THRESHOLD):
        breaker.record_failure("shelfmark", FAILURE)
    book_id = helpers.audiobook_pending_book("Held")
    db_row_service = "shelfmark"
    from app.db import db
    db().mark_held(book_id, "acquire_audiobook", db_row_service,
                   breaker.hold_detail("shelfmark"))

    held = next(g for g in main.issues()["groups"] if g["stage"] == "held")
    assert "blocked_by" not in held
