"""discover — read the Goodreads to-read shelf and register anything new.

Not a per-book stage: this is the source of books rather than something done
to one. It is idempotent — books are keyed on the Goodreads id, so a re-run
refreshes metadata without creating duplicates or resetting progress.

Genres are only fetched for books we have not seen before, which keeps a
large shelf from turning into hundreds of GraphQL calls on every sweep.
"""

from __future__ import annotations

import json

from .. import genres as genres_lookup
from .. import models
from ..db import db
from ..goodreads import GoodreadsSession


def run(shelf: str = "to-read") -> dict:
    session = GoodreadsSession()
    books = session.fetch_shelf(shelf)

    created = 0
    refreshed = 0
    for entry in books:
        existing = db().query_one(
            "SELECT id FROM books WHERE goodreads_id = ?", (entry.book_id,)
        )
        is_new = existing is None

        # Only pay for genre lookups on books we have never classified, and
        # walk the whole provider chain rather than betting on one.
        genres = entry.genres
        source = ""
        if is_new and not genres:
            found = genres_lookup.lookup(
                {
                    "goodreads_id": entry.book_id,
                    "title": entry.title,
                    "author": entry.author,
                    "isbn": entry.isbn,
                    "isbn13": entry.isbn13,
                }
            )
            genres = found.genres
            source = found.source

        book = models.Book(
            goodreads_id=entry.book_id,
            title=entry.title or "(untitled)",
            author=entry.author,
            isbn=entry.isbn,
            isbn13=entry.isbn13,
            year=entry.year,
            cover_url=entry.cover_url,
            goodreads_url=entry.url,
            genres=genres,
            auto_shelve=db().get_setting("auto_shelve", "1") == "1",
        )
        book_id, was_created = db().upsert_book(book)
        if was_created and source:
            db().set_genres(book_id, json.dumps(genres), source)
        created += int(was_created)
        refreshed += int(not was_created)

    if created:
        db().log(f"discover: {created} new book(s) on '{shelf}'")
    return {"shelf": shelf, "seen": len(books), "created": created, "refreshed": refreshed}
