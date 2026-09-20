"""Compare goodreads's shelf records against what Goodreads actually shows.

Every failure this project had on the Goodreads side came from the same root:
trusting our own bookkeeping. A shelf add that reported success without
applying; a membership check that read page one of a shelf that had outgrown
one page. Both were found by hand, hours late, after books had been left on
`to-read` or — worse — sitting on `to-read` and their collected shelf at once.

This makes checking a standing operation rather than something a person does
when something looks wrong. It reads the account's shelves once (four requests,
not one per book), compares them against what each book's `shelve` stage claims,
and resets anything that disagrees so the pipeline re-does it.

It is deliberately read-mostly: the only thing it changes is queueing a retry.
It never edits Goodreads itself, because the repair path already exists in the
`shelve` stage and duplicating it here would mean two places to get wrong.
"""

from __future__ import annotations

from . import goodreads
from .db import db

#: Shelf names goodreads writes to. Reading these is how we learn the truth.
COLLECTED_SHELVES = (
    "to-read",
    "collected-pdf",
    "collected-audiobook",
    "collected-pdf-audiobook",
)


def current_shelves() -> dict[str, set[str]]:
    """book_id -> the shelves it is actually on, straight from Goodreads."""
    session = goodreads.GoodreadsSession()
    result: dict[str, set[str]] = {}
    for shelf in COLLECTED_SHELVES:
        # A few pages: to-read is the largest shelf and the one that caused the
        # original bug, so reading only its first page is precisely the mistake
        # this module exists to catch.
        for entry in session.fetch_shelf(shelf, max_pages=8):
            result.setdefault(entry.book_id, set()).add(shelf)
    return result


def reconcile(apply_changes: bool = False) -> dict:
    """Find books whose recorded shelf disagrees with reality.

    Returns a summary. With `apply_changes`, drifted books are queued for
    another attempt by resetting their `shelve` stage.
    """
    rows = db().query("""
        SELECT b.id, b.title, b.goodreads_id, sr.artifact AS shelf
        FROM stage_runs sr JOIN books b ON b.id = sr.book_id
        WHERE sr.stage='shelve' AND sr.status='ok'
    """)
    if not rows:
        return {"checked": 0, "drifted": 0, "details": []}

    actual = current_shelves()
    drifted = []
    for row in rows:
        on = actual.get(str(row["goodreads_id"]), set())
        expected = row["shelf"] or ""
        problems = []
        if expected and expected not in on:
            problems.append(f"not on '{expected}'")
        if "to-read" in on:
            problems.append("still on to-read")
        if problems:
            drifted.append(
                {"id": row["id"], "title": row["title"], "expected": expected,
                 "actual": sorted(on), "why": "; ".join(problems)}
            )

    if drifted and apply_changes:
        for item in drifted:
            db().reset_stage(item["id"], "shelve")
        db().log(
            f"reconcile: {len(drifted)} shelf record(s) disagreed with Goodreads "
            f"— queued for re-shelving",
            level="warning",
            stage="shelve",
        )

    return {"checked": len(rows), "drifted": len(drifted), "details": drifted[:20]}
