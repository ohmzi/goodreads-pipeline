"""`cli repair` must not let one book's vanished source end the whole batch.

The link-hygiene loop walks every book and, for each one, asks Open Notebook
about the source it found. Nothing wrapped that ask: a source gone missing
between the listing walk at the top of the command and this book's turn in the
loop — which the fake below reproduces directly, and which `notebook`'s own
404 handling exists to shrug off for exactly the same reason — raised straight
out of the function. `main()` has no handler either, so one book's bad luck
took the traceback all the way up and discarded every result already printed
for the books ahead of it in the list.
"""

from __future__ import annotations

import json

import pytest

from app import models
from app.cli import main as cli_main
from app.clients import opennotebook as opennotebook_module
from app.clients.base import ClientError
from app.config import settings
from app.db import db

import helpers

NOTEBOOK_ID = "notebook:fictional"


class FakeOpenNotebook:
    name = "open notebook"

    def __init__(self, *, gone: set[str] = frozenset()):
        self._gone = gone
        self.sources: dict[str, str] = {}   # source_id -> file_path

    def close(self):
        pass

    def add(self, source_id: str, path: str) -> None:
        self.sources[source_id] = path

    def iter_sources(self):
        for sid, path in self.sources.items():
            yield {"id": sid, "created": "2026-09-21T00:00:00Z",
                   "asset": {"file_path": path, "url": None}}

    def notebook_named(self, name):
        return {"id": NOTEBOOK_ID, "name": name} if name == "Fictional" else None

    def source_notebooks(self, source_id):
        if source_id in self._gone:
            raise ClientError(self.name, "read source failed with HTTP 404: {}",
                              status=404)
        return [NOTEBOOK_ID]   # already correctly attached, once each

    # Unused by this test's path, present so the loop does not AttributeError
    # if it ever reaches them.
    def attach(self, notebook_id, source_id):
        raise AssertionError("not expected: every book here is either fine or gone")

    def detach(self, notebook_id, source_id):
        raise AssertionError("not expected: every book here is either fine or gone")

    def delete_source(self, source_id):
        raise AssertionError("no duplicates in this test")


def _book(title: str, source_id: str) -> tuple[dict, str]:
    ebook = str(settings.books_root / "Fiction" / f"{title}.epub")
    book_id = helpers.seed_book(title, {
        "classify": models.OK,
        "place": {"status": models.OK, "output_path": json.dumps({"ebook": ebook})},
    })
    db().set_category(book_id, "Fiction", needs_review=False)
    on_path = f"{settings.opennotebook_library_root}/Fiction/{title}.epub"
    return {"id": book_id, "title": title}, on_path


@pytest.fixture
def library(tmp_path):
    yield


def test_a_vanished_source_is_reported_not_fatal(monkeypatch, capsys):
    fake = FakeOpenNotebook(gone={"source:gone"})
    _, gone_path = _book("Gone", "source:gone")
    fake.add("source:gone", gone_path)
    _, fine_path = _book("Fine", "source:fine")
    fake.add("source:fine", fine_path)

    monkeypatch.setattr(opennotebook_module, "OpenNotebookClient", lambda: fake)

    exit_code = cli_main(["repair"])

    assert exit_code == 0, "one bad book must not fail the whole command"
    out = capsys.readouterr().out
    assert "Gone: ClientError" in out
    assert "1 correct" in out, "the book that was fine still got counted"


def test_books_after_the_vanished_one_are_still_processed(monkeypatch, capsys):
    """Order matters: a dict preserves insertion order, so `Gone` is visited
    before `Fine` — proving the loop *continues* rather than merely not
    crashing on the last book by coincidence."""
    fake = FakeOpenNotebook(gone={"source:gone"})
    _, gone_path = _book("Gone", "source:gone")
    fake.add("source:gone", gone_path)
    _, fine_path = _book("Fine", "source:fine")
    fake.add("source:fine", fine_path)

    monkeypatch.setattr(opennotebook_module, "OpenNotebookClient", lambda: fake)
    cli_main(["repair"])

    out = capsys.readouterr().out
    gone_at = out.index("Gone")
    fine_summary_at = out.index("1 correct")
    assert gone_at < fine_summary_at, "the summary must include the book after the failure"
