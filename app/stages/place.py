"""place — put the book where it belongs, without copying it.

Ebooks land in `newDownloads/` (Shelfmark's staging destination) and are
renamed into `<Category>/<Author> - <Title> (<Year>)/`. Because staging and the
category folders are both under the book library, that rename stays inside one
filesystem — `move_into_place` refuses to fall back to a copy, so a duplicate
is impossible rather than merely unlikely.

Audiobooks are handled differently: Shelfmark is configured to organise them
itself into `Author/Title` (the layout Audiobookshelf matches on), so this
stage only *verifies* the result rather than moving it. Moving them again
would risk breaking the author/title pairing ABS relies on.
"""

from __future__ import annotations

import json
from pathlib import Path

from .. import models
from ..clients.base import ClientError
from ..config import settings
from ..db import db
from ..pathing import (
    AUDIO_EXTS,
    EBOOK_EXTS,
    book_folder_name,
    find_matching_entries,
    has_media,
    is_junk,
    is_media_file,
    match_score,
    move_into_place,
    sanitize_component,
)
from . import classify
from .acquire import acquired_formats


def placed_paths(book_id: int) -> dict[str, str]:
    """{'ebook': path, 'audiobook': path} for whatever landed."""
    row = db().stage(book_id, "place") or {}
    raw = row.get("output_path") or ""
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {}


def run(book: dict) -> models.StageResult:
    book_id = book["id"]
    has_ebook, has_audio = acquired_formats(book_id)
    if not has_ebook and not has_audio:
        # Distinguish "still downloading" from "gave up". Blocking forever on a
        # book whose downloads have both permanently failed would hide it from
        # the `shelve` stage, which is the one that reports the failure.
        rows = [
            db().stage(book_id, stage) or {}
            for stage in ("acquire_ebook", "acquire_audiobook")
        ]
        if all(row.get("status") in models.TERMINAL for row in rows):
            return models.StageResult.skipped("nothing downloaded — nothing to place")
        return models.StageResult.blocked("no completed download to place yet")

    category = book.get("category") or ""
    if not category:
        return models.StageResult.failed("no category resolved; run classify first")
    folder, _notebook = classify.destination_for(category)

    placed: dict[str, str] = {}
    notes: list[str] = []
    waiting: list[str] = []

    try:
        if has_ebook:
            path = _place_ebook(book, folder)
            if path is None:
                waiting.append(
                    f"ebook downloaded but nothing matching it is in {settings.staging_dir}"
                )
            else:
                placed["ebook"] = path
                notes.append(f"ebook -> {path}")

                # The embedded-metadata genre source needs the actual file, which
                # did not exist when `classify` ran. Now that it does, take a
                # second look — and if the category improves, move it again rather
                # than leaving it in the fallback folder.
                fresh = dict(db().get_book(book_id) or {})
                new_category, note = classify.reclassify_with_file(fresh, path)
                notes.append(note)
                if new_category and new_category != category:
                    new_folder, _ = classify.destination_for(new_category)
                    current = Path(placed["ebook"])
                    target_dir = settings.books_root / new_folder / current.parent.name
                    moved = move_into_place(current, target_dir / current.name)
                    placed["ebook"] = str(moved)
                    notes.append(f"re-filed into {new_folder}/")

        if has_audio:
            path = _locate_audiobook(book)
            if path is None:
                waiting.append(
                    f"audiobook downloaded but no matching folder is under {settings.audiobooks_root}"
                )
            else:
                placed["audiobook"] = path
                notes.append(f"audiobook -> {path}")
    except ClientError as exc:
        return models.StageResult.failed(
            str(exc), kind=exc.kind, service=exc.service
        )
    except RuntimeError as exc:
        # A filesystem problem — the rename refused to cross a filesystem, or
        # the source vanished. No service is involved.
        return models.StageResult.failed(str(exc))

    # Nothing at all landed: the downloads are either still in flight or both
    # failed, and there is genuinely nothing to place.
    if not placed:
        if waiting:
            return models.StageResult.blocked("; ".join(waiting))
        return models.StageResult.blocked("no completed download to place yet")

    db().set_output(book_id, "place", json.dumps(placed))

    # Succeed as soon as *one* format is in place.
    #
    # This used to require both, which stalled the entire chain behind an
    # audiobook: 137 books with their ebook sitting in the library never got
    # indexed or added to a notebook because `place` refused to finish while a
    # download that might never arrive was still outstanding. The other format
    # is picked up later — a newly completed acquisition resets this stage.
    if waiting:
        return models.StageResult.ok(
            "; ".join(notes) + "  ||  still waiting on: " + "; ".join(waiting),
            artifact=json.dumps(placed),
        )
    return models.StageResult.ok("; ".join(notes), artifact=json.dumps(placed))


def _place_ebook(book: dict, folder: str) -> str | None:
    title = book["title"]
    author = book.get("author") or ""
    year = book.get("year")

    # Already filed? Then placement is done. Checked first, because the file is
    # long gone from staging by the time anything re-runs this stage.
    existing = _already_placed_ebook(book, folder)
    if existing:
        return existing

    source = _locate_ebook_source(title, author)
    if source is None:
        return None

    destination_dir = settings.books_root / folder / book_folder_name(author, title, year)
    final = move_into_place(source, destination_dir / source.name)

    # Only the file moves. If it arrived inside a folder Shelfmark made, tidy
    # that folder away rather than leaving an empty husk in staging.
    parent = source.parent
    if parent != settings.staging_dir and parent.exists() and not any(parent.iterdir()):
        try:
            parent.rmdir()
        except OSError:
            pass

    return str(final)


def _already_placed_ebook(book: dict, folder: str) -> str | None:
    """The book's ebook, if it is already sitting in its category folder.

    Placement has to be idempotent. Resetting `place` — which re-classification
    and the repair command both do — used to re-run it against the staging
    folder only, where the file no longer is because it was moved the first
    time. The stage then reported "nothing matching it is in newDownloads" for
    a book that was already correctly filed, and 65 books were stuck that way.
    """
    title = book.get("title") or ""
    author = book.get("author") or ""
    target = settings.books_root / folder
    if not target.is_dir():
        return None

    for entry in target.iterdir():
        if not entry.is_dir() or match_score(entry.name, title, author) < 0.55:
            continue
        for child in entry.rglob("*"):
            if is_media_file(child, EBOOK_EXTS):
                return str(child)
    return None


def _locate_ebook_source(title: str, author: str):
    """The best ebook candidate sitting in staging, if any."""
    hits = find_matching_entries(settings.staging_dir, title, author)
    for _score, path in hits:
        if is_media_file(path, EBOOK_EXTS):
            return path
    # Nothing directly matching — look inside the best-matching folder.
    for _score, path in hits:
        if path.is_dir():
            for child in sorted(path.rglob("*")):
                if is_media_file(child, EBOOK_EXTS):
                    return child
    return None


def _locate_audiobook(book: dict) -> str | None:
    """Get the audiobook into `Author/Title`, wherever it landed.

    Unlike ebooks, audiobooks tend to arrive in the *right tree* but the wrong
    shape: SABnzbd's `audiobook` category completes straight into the
    Audiobooks root with raw usenet names. Shelfmark cannot rescue them either
    — it sees the files at `/audiobook` while SABnzbd reports their paths as
    `/data/<library>/...`, and that namespace mismatch defeats its move step.
    So goodreads does the placement.

    Only entries sitting *directly* in a staging root are moved. Anything
    already nested under an author folder is treated as organised, which keeps
    this idempotent and stops it from re-shuffling a good library.
    """
    title = book["title"]
    author = book.get("author") or ""
    root = settings.audiobooks_root

    target = root / sanitize_component(author) / sanitize_component(title)
    if has_media(target, AUDIO_EXTS):
        return str(target)

    # Same idempotency rule as the ebook path: already organised, nothing to do.
    for author_dir in root.iterdir():
        if not author_dir.is_dir() or is_junk(author_dir):
            continue
        if match_score(author_dir.name, title, author) < 0.3 and \
                sanitize_component(author).lower() != author_dir.name.lower():
            continue
        for title_dir in author_dir.iterdir():
            if title_dir.is_dir() and has_media(title_dir, AUDIO_EXTS) and \
                    match_score(title_dir.name, title, author) >= 0.55:
                return str(title_dir)

    best: tuple[float, Path] | None = None
    for staging in _audiobook_staging_dirs():
        for entry in sorted(staging.iterdir()):
            if is_junk(entry):
                continue
            if entry == target:
                continue
            if not has_media(entry, AUDIO_EXTS):
                continue
            score = match_score(entry.name, title, author)
            if score < 0.5:
                continue
            if best is None or score > best[0]:
                best = (score, entry)

    if best is None:
        return None

    source = best[1]
    destination = target if source.is_dir() else target / source.name
    return str(move_into_place(source, destination))


def _audiobook_staging_dirs() -> list[Path]:
    """Where an un-organised audiobook may be sitting.

    `.incoming` is the staging directory SABnzbd's audiobook category is
    pointed at; the root itself is still scanned so audiobooks that were
    already downloaded there before that change still get picked up.
    """
    root = settings.audiobooks_root
    dirs = []
    if root.is_dir():
        dirs.append(root)
    incoming = root / ".incoming"
    if incoming.is_dir():
        dirs.append(incoming)
    return dirs
