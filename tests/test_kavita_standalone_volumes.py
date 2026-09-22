"""A standalone novel with no series must still be found by `find_series`.

Kavita's own search response is not, despite the name, only about series.
`series: []` is the ordinary answer for a book with no series in its embedded
metadata — most standalone novels — and for those the real, searchable title
sits in `chapters[].titleName` instead, read off the file's own metadata
rather than guessed from the folder name.

Reading only `series` made this method blind to that whole class of book.
Confirmed live against this operator's own Fiction library: three of L. Frank
Baum's own Oz novels came back from `/api/Search/search` with an empty
`series` and the real title only in `chapters`, and `verify` reported all
three "missing" from a service that had just found them a moment before, in
the same response, one field over.
"""

from __future__ import annotations

from app.clients.kavita import KavitaClient
from app.pathing import titles_match


class FakeKavita(KavitaClient):
    def __init__(self, response: dict):
        self._response = response
        self.calls: list[dict] = []

    def close(self) -> None:
        pass

    def _headers(self):
        return {}

    def json(self, method, path, what, **kwargs):
        self.calls.append({"method": method, "path": path, **kwargs})
        return self._response


#: A trimmed, real shape of what `/api/Search/search?queryString=Scarecrow`
#: actually returned for this operator's own library: an empty `series`, and
#: the match sitting in `chapters[].titleName`.
STANDALONE_RESPONSE = {
    "series": [],
    "chapters": [
        {
            "id": 1775,
            "title": "-100000",
            "titleName": "Oz 9 - The Scarecrow of Oz",
            "files": [{"filePath": "/books/Fiction/Baum, L. Frank - The Scarecrow "
                                    "of Oz (Oz, #9) (1915)/....epub"}],
        },
    ],
}

#: The ordinary case, for comparison: a book Kavita *did* assign a series to.
SERIES_RESPONSE = {
    "series": [{"name": "Bartol, Vladimir - Alamut", "libraryId": 2}],
    "chapters": [],
}

#: A chapter that carries no title at all (a placeholder/default entry) must
#: not become an empty-string candidate that trivially "matches" everything.
EMPTY_TITLE_RESPONSE = {
    "series": [],
    "chapters": [{"id": 1, "title": "-100000", "titleName": ""}],
}


def test_a_standalone_volumes_title_is_returned():
    client = FakeKavita(STANDALONE_RESPONSE)
    hits = client.find_series("Scarecrow")

    assert [h["name"] for h in hits] == ["Oz 9 - The Scarecrow of Oz"]


def test_the_standalone_title_matches_the_goodreads_title():
    """The actual fix, end to end: search result -> real match."""
    client = FakeKavita(STANDALONE_RESPONSE)
    hits = client.find_series("Scarecrow")

    assert any(titles_match(h["name"], "The Scarecrow of Oz (Oz, #9)") for h in hits)


def test_a_real_series_result_still_works_unchanged():
    client = FakeKavita(SERIES_RESPONSE)
    hits = client.find_series("Alamut")

    assert [h["name"] for h in hits] == ["Bartol, Vladimir - Alamut"]
    assert hits[0]["library_id"] == 2


def test_series_and_chapter_hits_combine_in_one_list():
    """A query that happens to hit both must not silently drop one half."""
    combined = {
        "series": SERIES_RESPONSE["series"],
        "chapters": STANDALONE_RESPONSE["chapters"],
    }
    client = FakeKavita(combined)
    hits = client.find_series("anything")

    names = {h["name"] for h in hits}
    assert names == {"Bartol, Vladimir - Alamut", "Oz 9 - The Scarecrow of Oz"}


def test_an_empty_chapter_title_is_not_a_candidate():
    """An empty string would satisfy `\\b\\b` against anything — must be dropped."""
    client = FakeKavita(EMPTY_TITLE_RESPONSE)

    assert client.find_series("Scarecrow") == []


def test_a_chapter_hit_has_no_library_id():
    """Honest about what is not known, rather than inventing one.

    No caller currently reads it — `verify` only ever extracts `["name"]` —
    so this is a statement of intent, not a behavior anything depends on yet.
    """
    client = FakeKavita(STANDALONE_RESPONSE)
    hits = client.find_series("Scarecrow")

    assert hits[0]["library_id"] is None


def test_neither_series_nor_chapters_present_is_an_empty_result():
    client = FakeKavita({})
    assert client.find_series("anything") == []


def test_an_empty_query_never_calls_the_service():
    client = FakeKavita(STANDALONE_RESPONSE)
    assert client.find_series("   ") == []
    assert client.calls == []
