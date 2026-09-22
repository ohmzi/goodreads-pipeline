"""Running `notebook` twice must not put the book into Open Notebook twice.

This is the acceptance test for the duplicate loop. The stage's guard against
double-adding was `source_exists_for_path`, which read one silently-paginated
page of `/api/sources` — so on any library past 50 sources the guard missed,
the stage created the book again, and the fresh source took a slot in that same
50-row window and pushed another book out of it. Each run made the next run
worse: 78 sources in a day, then 331, then 4,169.

The fix is the order of the questions. The id the stage recorded last time is
asked about first, by id, which no amount of library growth can hide. The path
scan is the fallback, and it now walks every page.
"""

from __future__ import annotations

import json

from app import models
from app.clients.base import ClientError
from app.config import settings
from app.db import db
from app.stages import notebook

import helpers


NOTEBOOK_ID = "notebook:fictional"

#: Built from the test settings, because `to_opennotebook_path` insists the
#: file really is under the configured library root before it will translate it.
EBOOK = str(settings.books_root / "Fiction" / "Author - Title.pdf")
BOOK_PATH = f"{settings.opennotebook_library_root}/Fiction/Author - Title.pdf"


class FakeOpenNotebook:
    """Open Notebook, including the part that made this hard: the 50-row page."""

    name = "open notebook"
    PAGE = 50

    def __init__(self, filler: int = 0):
        # `filler` unrelated sources, newest first, exactly as the service
        # orders them. Anything created later is inserted at the front.
        self.sources: list[dict] = [
            {"id": f"other:{i}", "created": "2026-09-01T00:00:00Z",
             "asset": {"file_path": f"/app/data/uploads/library/x/{i}.pdf"}}
            for i in range(filler)
        ]
        self.created: list[str] = []
        self.links: dict[str, list[str]] = {}

    # -- the two lookups the stage may use -------------------------------
    def iter_sources(self):
        """Paged, and *only* the first page unless the caller asks for more."""
        yield from self.sources[:self.PAGE]

    def sources_for_path(self, container_path):
        found = [(s["created"], s["id"]) for s in self.iter_sources()
                 if (s.get("asset") or {}).get("file_path") == container_path]
        found.sort()
        return [sid for _c, sid in found]

    def source_exists_for_path(self, container_path):
        ids = self.sources_for_path(container_path)
        return ids[0] if ids else None

    def source_exists(self, source_id):
        return any(s["id"] == source_id for s in self.sources)

    # -- the rest of the surface the stage touches -----------------------
    def notebooks(self):
        return [{"id": NOTEBOOK_ID, "name": "Fictional"}]

    def notebook_named(self, name):
        return self.notebooks()[0] if name == "Fictional" else None

    def create_source_from_path(self, container_path, title):
        sid = f"source:{len(self.created)}"
        self.created.append(sid)
        self.sources.insert(0, {
            "id": sid, "created": "2026-09-21T00:00:00Z",
            "asset": {"file_path": container_path},
        })
        return sid

    def source_notebooks(self, source_id):
        if not self.source_exists(source_id):
            raise ClientError(self.name, "read source failed with HTTP 404: {}",
                              status=404)
        return list(self.links.get(source_id, []))

    def attach(self, notebook_id, source_id):
        self.links.setdefault(source_id, []).append(notebook_id)

    def detach(self, notebook_id, source_id):
        self.links[source_id] = [n for n in self.links.get(source_id, [])
                                 if n != notebook_id]

    def ensure_attached(self, notebook_id, source_id):
        existing = self.source_notebooks(source_id)
        if existing.count(notebook_id) == 1:
            return True
        if existing.count(notebook_id) > 1:
            self.detach(notebook_id, source_id)
        self.attach(notebook_id, source_id)
        return notebook_id in self.source_notebooks(source_id)

    def source_state(self, source_id):
        if not self.source_exists(source_id):
            raise ClientError(self.name, "read source failed with HTTP 404: {}",
                              status=404)
        return {"status": "completed", "embedded": True, "chunks": 12,
                "has_text": True, "notebooks": list(self.links.get(source_id, []))}

    def close(self):
        pass


def _book(title: str = "Title") -> dict:
    book_id = helpers.seed_book(title, {
        "classify": models.OK,
        "place": {"status": models.OK,
                  "output_path": json.dumps({"ebook": EBOOK})},
        "index": models.OK,
    })
    db().set_category(book_id, "Fiction", needs_review=False)
    return {"id": book_id, "title": title, "category": "Fiction"}


def _run(book: dict, fake: FakeOpenNotebook, monkeypatch) -> models.StageResult:
    """One pass of the stage, recording its artifact as the pipeline would."""
    monkeypatch.setattr(notebook, "OpenNotebookClient", lambda: fake)
    result = notebook.run(book)
    if result.status == models.OK:
        db().execute(
            "UPDATE stage_runs SET status=?, artifact=? WHERE book_id=? AND stage='notebook'",
            (models.OK, result.artifact, book["id"]),
        )
    return result


def test_the_second_run_reuses_the_source(monkeypatch):
    fake = FakeOpenNotebook()
    book = _book()

    first = _run(book, fake, monkeypatch)
    second = _run(book, fake, monkeypatch)

    assert first.status == models.OK
    assert second.status == models.OK
    assert fake.created == ["source:0"], "the book was added a second time"
    assert "reused existing source" in second.detail


def test_a_source_pushed_off_the_first_page_is_still_found(monkeypatch):
    """The actual failure, in the order it actually happened.

    The book is added, and then other books are added — which is all it takes,
    because the list endpoint shows the newest 50 and this book's source is no
    longer among them. Every later run of the stage searched a window that
    could not contain the answer, added the book again, and pushed somebody
    else's source out of the window in turn.

    The recorded id is what survives this: it is asked about by id, so it does
    not matter how many sources stand in front of it.
    """
    fake = FakeOpenNotebook()
    book = _book()

    assert _run(book, fake, monkeypatch).status == models.OK
    assert fake.created == ["source:0"]

    # 60 other books get added, as they were.
    for i in range(60):
        fake.sources.insert(0, {
            "id": f"later:{i}", "created": "2026-09-22T00:00:00Z",
            "asset": {"file_path": f"/app/data/uploads/library/y/{i}.pdf"},
        })
    assert fake.source_exists_for_path(BOOK_PATH) is None, \
        "the fake must reproduce the blind spot, or this test proves nothing"

    for _ in range(3):
        assert _run(book, fake, monkeypatch).status == models.OK

    assert fake.created == ["source:0"], "the book was added again"
    assert fake.links["source:0"] == [NOTEBOOK_ID], "attached exactly once"


def test_a_source_deleted_behind_our_back_is_recreated(monkeypatch):
    """A recorded id is preferred, not trusted blindly."""
    fake = FakeOpenNotebook()
    book = _book()
    _run(book, fake, monkeypatch)

    fake.sources = [s for s in fake.sources if s["id"] != "source:0"]
    result = _run(book, fake, monkeypatch)

    assert result.status == models.OK
    assert fake.created == ["source:0", "source:1"]


def test_leftover_duplicates_are_reported_not_deleted(monkeypatch):
    """A per-book stage must not delete rows in another service as a side effect."""
    fake = FakeOpenNotebook()
    book = _book()
    for _ in range(3):
        fake.create_source_from_path(BOOK_PATH, "Title")

    result = _run(book, fake, monkeypatch)

    assert result.status == models.OK
    assert len(fake.sources) == 3, "nothing was removed"
    assert "2 duplicate source(s)" in result.detail
    assert "cleanup" in result.detail
