"""`GET /api/sources` paginates, and says nothing about it.

Its `limit` defaults to 50 and is capped at 100, sorted newest-first, and the
body is a bare JSON array — there is no `total`, no `next`, nothing that
distinguishes "these are all of them" from "these are the first 50 of 4,665".
`source_exists_for_path` read that one page as the whole list.

The consequence was not a wrong answer in a corner. That function is the
"have we added this book already?" guard, so a miss makes `notebook` add the
book again — and the new source is then the newest, which pushes another book
out of the 50-row window. The operator's Open Notebook went 78 sources in a
day, 331, then 4,169: 4,665 sources for 394 real files, one book added 174
times, each one re-embedded. `verify` read the same function, so 104 books
reported "no source for this file", each sat through three forced rescans, and
each parked as a failure.

These tests assert the paging itself and the guard that makes paging almost
never necessary: the id `notebook` recorded when it created the source.
"""

from __future__ import annotations

from app.clients.base import ClientError
from app.clients.opennotebook import OpenNotebookClient


PATH = "/app/data/uploads/library/Fiction/Bartol, Vladimir - Alamut/Alamut.epub"


class FakeOpenNotebook(OpenNotebookClient):
    """Serves a source list the way the real one does: a page at a time."""

    def __init__(self, sources):
        # Deliberately not calling super().__init__: no socket is wanted here.
        self._sources = list(sources)
        self.calls: list[dict] = []
        self.deleted: list[str] = []

    def close(self) -> None:
        pass

    def json(self, method, path, what, **kwargs):
        self.calls.append({"method": method, "path": path, **kwargs})
        if path == "/api/sources":
            params = kwargs.get("params") or {}
            limit = int(params.get("limit", 50))
            offset = int(params.get("offset", 0))
            assert limit <= 100, "the endpoint rejects a limit above 100"
            return self._sources[offset:offset + limit]
        if path.startswith("/api/sources/"):
            wanted = path.rsplit("/", 1)[1]
            for item in self._sources:
                if item["id"] == wanted:
                    return item
            raise ClientError("open notebook", "read source failed with HTTP 404: {}",
                              status=404)
        raise AssertionError(f"unexpected path {path}")

    def request(self, method, path, what, **kwargs):
        if method == "DELETE" and path.startswith("/api/sources/"):
            wanted = path.rsplit("/", 1)[1]
            self.deleted.append(wanted)
            self._sources = [s for s in self._sources if s["id"] != wanted]
            return None
        raise AssertionError(f"unexpected {method} {path}")


def _sources(count: int, *, target_at: int | None = None) -> list[dict]:
    """`count` sources, newest first, with the one we want buried at `target_at`."""
    out = []
    for i in range(count):
        path = PATH if i == target_at else f"/app/data/uploads/library/other/{i}.epub"
        out.append({
            "id": f"source:{i:05d}",
            "created": f"2026-09-{1 + i % 28:02d}T00:00:00Z",
            "asset": {"file_path": path, "url": None},
        })
    return out


def test_a_source_past_the_first_page_is_found():
    """The bug, stated directly: 400 sources, the one we want at 312."""
    client = FakeOpenNotebook(_sources(400, target_at=312))

    assert client.source_exists_for_path(PATH) == "source:00312"


def test_the_whole_list_is_walked_not_just_the_default_page():
    client = FakeOpenNotebook(_sources(400, target_at=312))
    client.source_exists_for_path(PATH)

    pages = [c for c in client.calls if c["path"] == "/api/sources"]
    # Five, not four: 400 divides exactly into full pages, and a full page is
    # indistinguishable from "there is more". One empty read is what ends it.
    assert [c["params"]["offset"] for c in pages] == [0, 100, 200, 300, 400]
    # Every page asks for the largest the endpoint will serve, or the walk
    # costs four times as many round-trips as it needs.
    assert {c["params"]["limit"] for c in pages} == {100}


def test_a_short_page_ends_the_walk():
    """No `total` comes back, so a part-full page is the only end marker."""
    client = FakeOpenNotebook(_sources(150, target_at=140))
    assert client.source_exists_for_path(PATH) == "source:00140"

    pages = [c for c in client.calls if c["path"] == "/api/sources"]
    assert len(pages) == 2, "a 50-row second page means there is no third"


def test_the_walk_is_bounded():
    """A pathological library must not turn one lookup into a five-minute scan."""
    client = FakeOpenNotebook(_sources(20_000))
    assert client.source_exists_for_path(PATH) is None

    pages = [c for c in client.calls if c["path"] == "/api/sources"]
    assert len(pages) == OpenNotebookClient.MAX_SOURCE_PAGES


def test_an_absent_file_is_absent():
    """The guard has to be able to say no, or nothing is ever added."""
    client = FakeOpenNotebook(_sources(120))
    assert client.source_exists_for_path(PATH) is None


def test_a_recorded_id_is_one_request():
    """The path `notebook` and `verify` actually take, and why it is preferred.

    Asking about a known id does not page at all, so it cannot be defeated by
    the size of the library — which is the property that stopped the duplicate
    loop, rather than merely making the scan correct.
    """
    client = FakeOpenNotebook(_sources(400, target_at=312))

    assert client.source_exists("source:00312") is True
    assert [c["path"] for c in client.calls] == ["/api/sources/source:00312"]


def test_a_deleted_id_reads_as_gone_not_as_an_error():
    client = FakeOpenNotebook(_sources(10))
    assert client.source_exists("source:99999") is False


def test_every_copy_of_a_file_is_listed_oldest_first():
    """What the repair command collapses."""
    sources = _sources(300)
    for index, day in ((10, "01"), (150, "05"), (299, "03")):
        sources[index]["asset"] = {"file_path": PATH, "url": None}
        sources[index]["created"] = f"2026-09-{day}T00:00:00Z"
    client = FakeOpenNotebook(sources)

    assert client.sources_for_path(PATH) == [
        "source:00010", "source:00299", "source:00150",
    ]


def test_delete_removes_one_source():
    sources = _sources(120, target_at=99)
    client = FakeOpenNotebook(sources)

    client.delete_source("source:00099")

    assert client.deleted == ["source:00099"]
    assert client.source_exists_for_path(PATH) is None
