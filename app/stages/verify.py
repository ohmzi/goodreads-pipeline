"""verify — prove the book is actually present in each service.

Every earlier stage reports what it *asked* a service to do. None of them
confirms the service actually ended up holding the book, and that gap is
exactly how you get a book marked complete that you cannot find anywhere.

This stage searches each service for the title and records what it finds. It
runs after `notebook` and before `shelve`, so a book is never moved to a
`collected-*` shelf on the strength of a claim that was never checked.

Three things make this less trivial than it looks, each learned the hard way:

  * **Search endpoints are not uniform.** Kavita only accepts `?queryString=`
    (`?query=` returns a 400 that reads like a broken endpoint). BookLore's
    `/search` answers 500 for every parameter combination and `/api/v1/books`
    ignores filters entirely, so the only way to check is to fetch the library
    and match locally.
  * **Rescans are asynchronous.** BookLore gained a title between two calls
    seconds apart, so a check run immediately after `index` reports books as
    missing that are merely unscanned. A miss within a grace window is treated
    as "not yet" rather than as a failure.
  * **Titles are punctuated differently everywhere.** BookLore stores
    "The MUQADDIMAH : An Introduction" where Goodreads has "The Muqaddimah: An
    Introduction". Matching is on a normalised, word-boundary form — loose
    enough to survive punctuation, tight enough that "less" does not match
    "21 Lessons for the 21st Century".
"""

from __future__ import annotations


import re
from datetime import datetime, timezone
from pathlib import PurePosixPath

from .. import models
from ..clients.abs_client import AudiobookshelfClient
from ..clients.base import ClientError
from ..clients.booklore import BookLoreClient, GrimmoryClient
from ..clients.kavita import KavitaClient
from ..clients.opennotebook import OpenNotebookClient, to_opennotebook_path
from ..db import db
from ..pathing import titles_match
from . import classify
from .place import placed_paths


#: Rescans are asynchronous — BookLore grew from 1756 to 1757 titles between
#: two calls seconds apart. Verifying immediately after `index` therefore
#: reports books as missing that are simply not scanned yet. A miss inside this
#: window is treated as "not yet", not as a failure.
SCAN_GRACE_SECONDS = 10 * 60

#: How many times a missing book will trigger a rescan before it is handed to a
#: human. Services do skip files on a scan and pick them up on the next one, so
#: one or two retries are worth it; more than that is a real problem.
MAX_RESCANS = 3


def _index_age_seconds(book_id: int) -> float | None:
    row = db().stage(book_id, "index") or {}
    stamp = row.get("finished_at")
    if not stamp:
        return None
    try:
        finished = datetime.fromisoformat(stamp)
    except ValueError:
        return None
    if finished.tzinfo is None:
        finished = finished.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - finished).total_seconds()


INDEXABLE_BY_SERVICES = {".epub", ".pdf"}


def _main_title(title: str) -> str:
    """The title without a series parenthetical or subtitle.

    "A Decline in Prophets (Rowland Sinclair #2)" -> "A Decline in Prophets".
    Used as a search query, because the services search literally.
    """
    text = re.sub(r"\(.*?\)|\[.*?\]", " ", title or "")
    text = re.split(r"[:;]", text, maxsplit=1)[0]
    return re.sub(r"\s+", " ", text).strip()


def _search_term(title: str) -> str:
    """A single distinctive word to query a literal search engine with.

    Kavita's search is strict enough that a leading article defeats it
    outright: "A Decline in Prophets" returns nothing while
    "Decline in Prophets" returns the series. Multi-word queries are fragile
    for the same reason, and so is an author name, which Kavita does not index
    against series.

    One long, ordinary word is enough to pull the right candidates back, and
    `titles_match` decides between them — which is how this should have worked
    from the start, rather than trusting the search engine to be precise.
    """
    words = [w for w in _main_title(title).split() if len(w) > 3]
    if not words:
        words = _main_title(title).split()
    if not words:
        return title
    return max(words, key=len)


def run(book: dict) -> models.StageResult:
    book_id = book["id"]
    title = book.get("title") or ""
    paths = placed_paths(book_id)
    ebook = paths.get("ebook", "")
    audiobook = paths.get("audiobook", "")

    if not ebook and not audiobook:
        return models.StageResult.skipped("nothing placed, so nothing to verify")

    # The library apps read epub and pdf. A `.mobi` or `.azw3` sits in the
    # library correctly but will never appear in their search results, so
    # failing verification for one is a permanent, unfixable failure — the book
    # would be retried forever for a format neither app supports.
    indexable = True
    if ebook and PurePosixPath(ebook).suffix.lower() not in INDEXABLE_BY_SERVICES:
        indexable = False

    found: list[str] = []
    missing: list[str] = []
    notes: list[str] = []

    if not indexable and ebook:
        notes.append(
            f"{PurePosixPath(ebook).suffix.lower()} is not a format the library apps "
            f"index — skipped, the book is in the library regardless"
        )

    # --- the three book-tree indexers -------------------------------------
    if ebook and indexable:
        # Kavita's search is a literal match against its own series names, and
        # its names come from embedded metadata. Searching for the full
        # Goodreads title — "A Decline in Prophets (Rowland Sinclair #2)" —
        # matches nothing, while the series it actually holds is called
        # "Decline in Prophets". A single distinctive word is enough to bring
        # the right candidates back; `titles_match` picks between them.
        kavita_query = _search_term(title)

        for label, factory in (
            ("Kavita", KavitaClient),
            ("BookLore", BookLoreClient),
            ("Grimmory", GrimmoryClient),
        ):
            client = factory()
            try:
                if label == "Kavita":
                    hits = [h["name"] for h in client.find_series(kavita_query)]
                else:
                    hits = [h["title"] for h in client.find_books(title)]
                if any(titles_match(h, title) for h in hits):
                    found.append(label)
                else:
                    missing.append(label)
                    notes.append(f"{label}: searched, no match for '{kavita_query[:40]}'")
            except ClientError as exc:
                missing.append(label)
                notes.append(f"{label}: {exc}")
            except Exception as exc:  # noqa: BLE001 - one service must not stop the rest
                missing.append(label)
                notes.append(f"{label}: {type(exc).__name__}: {exc}")
            finally:
                client.close()

    # --- audiobookshelf ----------------------------------------------------
    if audiobook:
        client = AudiobookshelfClient()
        try:
            hits = [h["title"] for h in client.find_books(title)]
            if any(titles_match(h, title) for h in hits):
                found.append("Audiobookshelf")
            else:
                missing.append("Audiobookshelf")
                notes.append("Audiobookshelf: searched, no match")
        except ClientError as exc:
            missing.append("Audiobookshelf")
            notes.append(f"Audiobookshelf: {exc}")
        except Exception as exc:  # noqa: BLE001
            missing.append("Audiobookshelf")
            notes.append(f"Audiobookshelf: {type(exc).__name__}: {exc}")
        finally:
            client.close()

    # --- open notebook -----------------------------------------------------
    # Only expected when the format is one Open Notebook can actually read.
    _folder, notebook_name = classify.destination_for(book.get("category") or "")
    if ebook and notebook_name:
        suffix = ebook.lower().rsplit(".", 1)[-1]
        if f".{suffix}" in {".epub", ".pdf", ".txt", ".md", ".docx", ".html", ".htm"}:
            client = OpenNotebookClient()
            try:
                # Ask about the id `notebook` recorded before hunting for the
                # path. The path scan has to page through every source in the
                # service; the id is one request and cannot be missed by a
                # short page. Reading the list first is what reported 104
                # present books as absent.
                source_id = str((db().stage(book_id, "notebook") or {}).get("artifact") or "")
                if source_id and not client.source_exists(source_id):
                    source_id = ""
                if not source_id:
                    source_id = client.source_exists_for_path(to_opennotebook_path(ebook))
                if not source_id:
                    missing.append("Open Notebook")
                    notes.append("Open Notebook: no source for this file")
                else:
                    notebook = client.notebook_named(notebook_name)
                    nid = str((notebook or {}).get("id") or "")
                    links = client.source_notebooks(source_id)
                    if nid and links.count(nid) == 1:
                        found.append("Open Notebook")
                    elif nid and links.count(nid) > 1:
                        found.append("Open Notebook")
                        notes.append(f"Open Notebook: attached {links.count(nid)}x (duplicate)")
                    else:
                        missing.append("Open Notebook")
                        notes.append(f"Open Notebook: source not in '{notebook_name}'")
            except ClientError as exc:
                missing.append("Open Notebook")
                notes.append(f"Open Notebook: {exc}")
            except Exception as exc:  # noqa: BLE001
                missing.append("Open Notebook")
                notes.append(f"Open Notebook: {type(exc).__name__}: {exc}")
            finally:
                client.close()

    summary = f"confirmed in {', '.join(found)}" if found else "not found anywhere"
    if missing:
        summary += f" — MISSING from {', '.join(missing)}"
    if notes:
        summary += " | " + "; ".join(notes)

    if not missing:
        return models.StageResult.ok(summary)

    # The grace window exists because rescans are asynchronous. It must only
    # apply to the *first* check after a scan though — index re-runs reset it,
    # so applying it every time left books oscillating inside the window
    # forever and nothing ever resolved. One pass of patience, then judgment.
    row = db().stage(book_id, "verify") or {}
    attempts = int(row.get("attempts") or 0)
    age = _index_age_seconds(book_id)
    if attempts <= 1 and age is not None and age < SCAN_GRACE_SECONDS:
        return models.StageResult.blocked(
            f"{summary} — first check after a rescan, giving it "
            f"{SCAN_GRACE_SECONDS // 60} min"
        )

    # Past the grace period and still missing. Some services quietly skip files
    # on a scan — Kavita reported zero series for three books whose files were
    # sitting in the library folder, and a rescans later they appear. So rather
    # than just failing, ask for another scan and check again. Bounded, so a
    # book that is genuinely broken still ends up in front of a human.
    row = db().stage(book_id, "verify") or {}
    rescans = int(row.get("output_path") or 0) if str(row.get("output_path") or "").isdigit() else 0
    if rescans < MAX_RESCANS:
        db().execute(
            "UPDATE stage_runs SET status=?, attempts=0, detail='' "
            "WHERE book_id=? AND stage='index'",
            (models.PENDING, book_id),
        )
        db().set_output(book_id, "verify", str(rescans + 1))
        return models.StageResult.blocked(
            f"{summary} — forcing a rescan (attempt {rescans + 1} of {MAX_RESCANS})"
        )

    # `notfound`, not `server`. Every service here answered — it ran the search
    # and returned no match — so calling this a server error was wrong twice
    # over. It told the breaker nothing was reachable when everything was, and
    # it put "The service returned an error — usually transient; retry" on the
    # operator's panel for 105 books, which is not what happened and not a fix:
    # retrying a search that correctly returns nothing returns nothing again.
    # `notfound` is already the vocabulary's word for "the service says the
    # thing does not exist", and `answered=True` keeps it out of the breaker's
    # outage arithmetic.
    #
    # The service is named when exactly one is at fault, which is the common
    # case and the difference between "something is missing somewhere" and
    # "Kavita has not indexed this". With several, the names stay in `detail`.
    return models.StageResult.failed(
        f"{summary} — still missing after {MAX_RESCANS} rescans, so this needs "
        f"a look at the service itself",
        kind="notfound", answered=True,
        service=(missing[0] if len(missing) == 1 else ""),
    )
