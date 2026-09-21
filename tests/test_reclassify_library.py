"""Re-classifying a library must never leave a book half-moved.

`app.cli reclassify` renames books inside a real library, so these tests are
about the failure cases as much as the happy one: the dry run has to touch
nothing at all, a second apply has to have nothing left to do, and a move that
fails has to leave the row saying exactly what the disk says. The state that
matters is the pair — where the file is, and what the record claims — so the
assertions here compare both, not one.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

import helpers
from app import models
from app.cli import main as cli_main
from app.config import settings
from app.db import db

DIRNAME = "Someone - Once Upon a River (2019)"


@pytest.fixture
def library():
    """A book tree of its own, emptied before and after every test."""
    for root in (settings.books_root, settings.audiobooks_root):
        shutil.rmtree(root, ignore_errors=True)
        root.mkdir(parents=True, exist_ok=True)
    yield settings.books_root
    for root in (settings.books_root, settings.audiobooks_root):
        shutil.rmtree(root, ignore_errors=True)


def seed(
    title: str = "Once Upon a River",
    genres: tuple[str, ...] = ("Historical Fiction", "Fiction", "Literary"),
    category: str = "History",
    *,
    folder: str = "History",
    dirname: str = DIRNAME,
    filename: str = "book.epub",
    audio: bool = False,
) -> int:
    """A book as the pipeline leaves one: genres, a category, a real file.

    `folder` is where the file actually is, which is not always where the
    category says — that is the state a re-classify exists to fix.
    """
    book_id = helpers.seed_book(title, {stage: models.OK for stage in models.STAGES})
    db().execute("UPDATE books SET author=? WHERE id=?", ("Someone", book_id))
    db().set_genres(book_id, json.dumps(list(genres)), "goodreads")
    db().set_category(book_id, category)

    if folder is not None:
        target = settings.books_root / folder / dirname
        target.mkdir(parents=True, exist_ok=True)
        ebook = target / filename
        ebook.write_bytes(b"PK\x03\x04 pretend this is an epub")
        placed = {"ebook": str(ebook)}
        if audio:
            adir = settings.audiobooks_root / "Someone" / title
            adir.mkdir(parents=True, exist_ok=True)
            (adir / "track01.mp3").write_bytes(b"\xff\xfb pretend audio")
            placed["audiobook"] = str(adir)
        db().set_output(book_id, "place", json.dumps(placed))
    return book_id


def snapshot() -> str:
    """Every row and every file a run could touch, in one comparable string."""
    rows = db().query(
        "SELECT b.id, b.title, b.category, b.needs_review, sr.stage, sr.status, "
        "sr.output_path, sr.attempts FROM books b JOIN stage_runs sr ON sr.book_id=b.id "
        "ORDER BY b.id, sr.stage"
    )
    files = sorted(str(p) for p in settings.books_root.rglob("*"))
    return json.dumps(
        [[r["id"], r["title"], r["category"], r["needs_review"], r["stage"],
          r["status"], r["output_path"], r["attempts"]] for r in rows] + files
    )


def placed_ebook(book_id: int) -> str:
    return json.loads(db().stage(book_id, "place")["output_path"])["ebook"]


def statuses(book_id: int) -> dict[str, str]:
    return {stage: db().stage(book_id, stage)["status"] for stage in models.STAGES}


# --------------------------------------------------------------------------
# the dry run
# --------------------------------------------------------------------------
def test_the_dry_run_touches_nothing(library, capsys):
    book_id = seed()
    before = snapshot()

    assert cli_main(["reclassify"]) == 0
    out = capsys.readouterr().out

    assert "Dry run" in out
    assert "History" in out and "Fiction" in out
    assert "Nothing was changed" in out
    # Not one row, and not one file.
    assert snapshot() == before
    assert db().get_book(book_id)["category"] == "History"


def test_the_dry_run_names_the_path_it_would_move(library, capsys):
    seed()
    cli_main(["reclassify"])
    out = capsys.readouterr().out
    assert str(settings.books_root / "History" / DIRNAME / "book.epub") in out
    assert str(settings.books_root / "Fiction" / DIRNAME / "book.epub") in out


def test_an_empty_library_says_so(library, capsys):
    assert cli_main(["reclassify"]) == 0
    assert "no books in the library" in capsys.readouterr().out


# --------------------------------------------------------------------------
# applying
# --------------------------------------------------------------------------
def test_apply_moves_the_file_and_the_record_together(library, capsys):
    book_id = seed()
    source = settings.books_root / "History" / DIRNAME / "book.epub"
    destination = settings.books_root / "Fiction" / DIRNAME / "book.epub"

    assert cli_main(["reclassify", "--apply"]) == 0
    out = capsys.readouterr().out

    assert destination.exists()
    assert not source.exists()
    assert not source.parent.exists(), "the folder it left behind should not linger"

    assert db().get_book(book_id)["category"] == "Fiction"
    assert placed_ebook(book_id) == str(destination)

    # Loud, and in both directions.
    assert str(source) in out and str(destination) in out

    status = statuses(book_id)
    # `place` must never be reset: that is what parks an already-placed book
    # as "nothing matching it is in newDownloads" forever.
    assert status["place"] == models.OK
    # The services that hold the old path are told to look again.
    assert status["index"] == models.PENDING
    assert status["notebook"] == models.PENDING
    assert status["verify"] == models.PENDING


def test_a_second_apply_has_nothing_left_to_do(library, capsys):
    seed()
    cli_main(["reclassify", "--apply"])
    capsys.readouterr()
    before = snapshot()

    assert cli_main(["reclassify", "--apply"]) == 0
    assert "nothing to do" in capsys.readouterr().out
    assert snapshot() == before


def test_the_nonfiction_books_come_out_of_fiction(library, capsys):
    """The other half of the same bug: "nonfiction" contains "fiction"."""
    book_id = seed("Sapiens", ("Nonfiction", "History", "Anthropology"),
                   "Fiction", folder="Fiction")

    cli_main(["reclassify", "--apply"])

    assert db().get_book(book_id)["category"] == "History"
    assert (settings.books_root / "History" / DIRNAME / "book.epub").exists()
    assert not (settings.books_root / "Fiction" / DIRNAME / "book.epub").exists()


def test_a_category_that_shares_a_folder_moves_nothing(library, capsys):
    """Fantasy and Fiction are the same folder and the same notebook.

    Driven by a genre list that reads Fiction under a record that says Fantasy,
    which is also what a category set by hand in the UI looks like — the
    command realigns it, the same way `backfill-genres` already does.

    The file itself must not be touched: its destination is the path it is
    already at, and a mover that renames a file onto itself deletes it.
    """
    book_id = seed("The Night Circus", ("Fiction",), "Fantasy", folder="Fiction")
    book = settings.books_root / "Fiction" / DIRNAME / "book.epub"
    inode = book.stat().st_ino

    cli_main(["reclassify", "--apply"])

    assert db().get_book(book_id)["category"] == "Fiction"
    assert book.exists() and book.stat().st_ino == inode
    # Nothing in any service referred to a path that changed.
    assert statuses(book_id)["index"] == models.OK


def test_a_book_with_nothing_placed_only_changes_its_record(library, capsys):
    book_id = seed(folder=None)
    assert db().stage(book_id, "place")["output_path"] is None

    cli_main(["reclassify", "--apply"])

    assert db().get_book(book_id)["category"] == "Fiction"
    assert db().stage(book_id, "place")["output_path"] is None
    assert statuses(book_id)["index"] == models.OK


def test_a_title_is_classified_the_way_the_stage_would(library, capsys):
    """No genres at all — the command mirrors `classify.run`'s title fallback."""
    book_id = seed("A Brief History of Fiction", (), "History", folder="History")

    cli_main(["reclassify", "--apply"])

    assert db().get_book(book_id)["category"] == "Fiction"


def test_an_audiobook_path_is_never_touched(library, capsys):
    book_id = seed(audio=True)
    audiobook = Path(json.loads(db().stage(book_id, "place")["output_path"])["audiobook"])

    cli_main(["reclassify", "--apply"])

    assert audiobook.is_dir()
    placed = json.loads(db().stage(book_id, "place")["output_path"])
    # Rewritten for the ebook, and only for the ebook.
    assert placed["audiobook"] == str(audiobook)
    assert placed["ebook"].endswith(str(Path("Fiction") / DIRNAME / "book.epub"))


def test_the_review_flag_is_recomputed_even_when_the_category_stands_still(library, capsys):
    """A book whose only genre is "Nonfiction" now matches no rule at all."""
    book_id = seed("Quiet", ("Nonfiction",), "Fiction", folder="Fiction")
    assert db().get_book(book_id)["needs_review"] == 0

    cli_main(["reclassify", "--apply"])

    assert db().get_book(book_id)["category"] == "Fiction"
    assert db().get_book(book_id)["needs_review"] == 1


# --------------------------------------------------------------------------
# when the move cannot happen
# --------------------------------------------------------------------------
def test_a_record_left_behind_by_a_crash_is_finished(library, capsys):
    """The rename happened, the writes did not.

    The file is in the new folder and the record still points at the old one,
    where nothing is. The next run has to recognise that as "already moved"
    rather than "gone" — and must not delete the file on the way past.
    """
    book_id = seed()
    source = settings.books_root / "History" / DIRNAME / "book.epub"
    destination = settings.books_root / "Fiction" / DIRNAME
    destination.mkdir(parents=True)
    shutil.move(str(source), str(destination / "book.epub"))
    assert not source.exists()

    cli_main(["reclassify", "--apply"])

    assert (destination / "book.epub").exists()
    assert placed_ebook(book_id) == str(destination / "book.epub")
    assert db().get_book(book_id)["category"] == "Fiction"


@pytest.mark.skipif(os.geteuid() == 0, reason="root pays no attention to the mode bits")
def test_a_move_the_filesystem_refuses_leaves_everything_alone(library, capsys):
    book_id = seed()
    source = settings.books_root / "History" / DIRNAME / "book.epub"
    unwritable = settings.books_root / "Fiction"
    unwritable.mkdir()
    os.chmod(unwritable, 0o555)
    try:
        before = snapshot()
        assert cli_main(["reclassify", "--apply"]) == 0
    finally:
        os.chmod(unwritable, 0o755)
    out = capsys.readouterr().out

    assert "!!" in out
    # The promise: never a file in one folder and a row claiming another.
    assert snapshot() == before
    assert source.exists()
    assert not (unwritable / DIRNAME).exists()
    assert db().get_book(book_id)["category"] == "History"
    assert placed_ebook(book_id) == str(source)
    assert statuses(book_id)["place"] == models.OK
    assert statuses(book_id)["index"] == models.OK


def test_a_file_that_vanished_is_reported_and_left_alone(library, capsys):
    book_id = seed()
    source = settings.books_root / "History" / DIRNAME / "book.epub"
    recorded = placed_ebook(book_id)
    source.unlink()

    assert cli_main(["reclassify", "--apply"]) == 0
    out = capsys.readouterr().out

    assert "missing" in out
    assert "Once Upon a River" in out
    assert recorded in out
    # The category is not moved to a folder the file is not in.
    assert db().get_book(book_id)["category"] == "History"
    assert placed_ebook(book_id) == recorded
    assert statuses(book_id)["index"] == models.OK


def test_a_book_whose_record_points_outside_the_library_is_left_alone(library, capsys):
    """Nothing outside the books tree is ours to rename — audiobooks least of all."""
    book_id = seed(audio=True)
    outside = settings.audiobooks_root / "Someone" / "Once Upon a River" / "track01.mp3"
    db().set_output(book_id, "place", json.dumps({"ebook": str(outside)}))

    cli_main(["reclassify", "--apply"])
    out = capsys.readouterr().out

    assert "outside" in out
    assert outside.exists()
    assert db().get_book(book_id)["category"] == "History"
