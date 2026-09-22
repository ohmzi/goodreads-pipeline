"""A book the services searched for and did not find is not a server error.

`verify` ends a book that is still missing after `MAX_RESCANS` with a failure,
and that failure used to carry `kind="server"`. Nothing about it was a server
error: each service ran the search and answered, and the answer was "no match".

Two things followed from the wrong word. The breaker counts `server` among its
`TRANSIENT_KINDS`, so a service that was answering every request looked like
one that was falling over. And the panel renders `kind` into the operator's
next step, so 105 books arrived under "The service returned an error — usually
transient; retry, and check Services if it persists" — a retry that reruns a
search which correctly returns nothing, and returns nothing again.

The right word is already in the vocabulary: `notfound`, "the service says the
thing does not exist", which `breaker.ANSWER_KINDS` treats as evidence the
service is up.
"""

from __future__ import annotations

from app import breaker, main, models

import helpers


DETAIL = (
    "confirmed in Kavita, BookLore, Grimmory — MISSING from Open Notebook | "
    "Open Notebook: no source for this file — still missing after 3 rescans, "
    "so this needs a look at the service itself"
)
DETAIL_TWO = (
    "confirmed in Kavita, BookLore, Grimmory — MISSING from Audiobookshelf, "
    "Open Notebook | Audiobookshelf: searched, no match; Open Notebook: no "
    "source for this file — still missing after 3 rescans, so this needs a "
    "look at the service itself"
)


def test_notfound_is_not_evidence_of_an_outage():
    """The breaker must not read a working service as a failing one."""
    assert "notfound" not in breaker.TRANSIENT_KINDS
    assert "notfound" in breaker.ANSWER_KINDS


def test_the_fix_does_not_say_retry_an_empty_search():
    fix = main._fix_for("verify", "notfound", DETAIL, "opennotebook")

    assert "The file is in the library" in fix
    assert "Open Notebook" in fix
    assert "usually transient" not in fix


def test_books_missing_from_the_same_services_are_one_group():
    """Grouped by which services are missing it, not by the whole sentence.

    The rest of `detail` names the book's own search terms, so bucketing on
    the raw text gives one group per book — which is how these arrived as a
    wall of near-identical rows.
    """
    for i in range(3):
        helpers.seed_book(f"Unindexed {i}", {
            "classify": models.OK,
            "place": {"status": models.OK, "output_path": '{"ebook": "/books/a.epub"}'},
            "verify": {
                "status": models.FAILED, "kind": "notfound",
                "service": "Open Notebook",
                # Each book names its own search term, as the real ones do.
                "detail": DETAIL.replace("no source", f"no source {i}"),
            },
        })
    helpers.seed_book("Two missing", {
        "classify": models.OK,
        "place": {"status": models.OK, "output_path": '{"ebook": "/books/b.epub"}'},
        "verify": {"status": models.FAILED, "kind": "notfound", "detail": DETAIL_TWO},
    })

    groups = [g for g in main.issues()["groups"] if g["stage"] == "verify"]

    assert len(groups) == 2
    by_size = sorted(groups, key=lambda g: -len(g["books"]))
    assert len(by_size[0]["books"]) == 3
    assert by_size[0]["detail"] == "In the library, but not indexed by Open Notebook"
    assert len(by_size[1]["books"]) == 1
    assert by_size[1]["detail"] == (
        "In the library, but not indexed by Audiobookshelf, Open Notebook"
    )


def test_the_single_missing_service_is_named_on_the_group():
    """So the row can carry a service label and an impact line."""
    helpers.seed_book("One service", {
        "classify": models.OK,
        "place": {"status": models.OK, "output_path": '{"ebook": "/books/a.epub"}'},
        "verify": {"status": models.FAILED, "kind": "notfound",
                   "service": "Open Notebook", "detail": DETAIL},
    })

    group = next(g for g in main.issues()["groups"] if g["stage"] == "verify")

    assert group["service"] == "opennotebook"
    assert group["service_label"] == "Open Notebook"
    assert group["impact"] == "books are not added to notebooks"
    assert group["actionable"] is True


def test_the_book_still_counts_as_needing_a_human():
    """Softening the kind must not quietly drop the book off the panel.

    `verify` is not an acquire stage, so a failure there is always someone's to
    look at — the book is supposed to be in the services and is not.
    """
    helpers.seed_book("Still failed", {
        "classify": models.OK,
        "place": {"status": models.OK, "output_path": '{"ebook": "/books/a.epub"}'},
        "verify": {"status": models.FAILED, "kind": "notfound", "detail": DETAIL},
    })

    assert main.issues()["counts"]["failed_books"] == 1
