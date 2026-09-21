"""F1's false negative: a service that answered read as a service that did not.

The verdict `probe_verdict` was narrowed to a whitelist of failure *kinds*
(`auth`, `notfound`) because everything that was not a transient failure used to
close the breaker, including results that never went near the service. That
fixed the direction the verifier measured and opened the other one, because
`acquire._queue` returns `kind='data'` for two opposite situations:

  * `only 0.5 GB free where audiobooks land` -- returned BEFORE the search, so
    the service was never asked. Reading it as silence is correct, and is the
    whole of the first fix.
  * `no audiobook releases found for '<title>'` -- returned AFTER a search that
    succeeded. The service answered, and the answer was "nothing".

Measured by the verifier with the whitelist in place: 16.25 hours, 20
cooldowns, 25 *successful* dials, state still open, 5 books held, and no reset
path from the UI or a restart. Silent -- nothing failed, nothing logged as an
error, the library simply stopped acquiring -- and not exotic, because "no
audiobook exists in any configured source" is the dominant outstanding category
in a real library and `_last_touched` puts those books at the head of the
rotation, so the probe lands on one of them first every time.

The fix is the discriminator the stage already had and the verdict threw away:
`models.StageResult.answered`, set by the branch that dialed and returned, read
by `probe_verdict` and nothing else. These tests drive the real stage and the
real sweep, so what they assert is the branch's own behaviour rather than a
flag a test set.

Four directions, and all four matter:

* dialed, nothing found  -> closes the breaker, releases the books   (was broken)
* refused before dialing -> re-opens, and never counts as an answer  (must stay)
* a stage that raised    -> re-opens, it is not a service answer     (must stay)
* a service failing      -> re-opens on a doubled cooldown           (the point)
"""

from __future__ import annotations

import json

from app import breaker, models, pipeline
from app.clients.base import ClientError
from app.db import db
from app.stages import acquire

import helpers

FAILURE = (
    "shelfmark: release search failed with HTTP 503: Unable to reach download "
    "source. Could not reach Anna's Archive after 10 attempt(s): ReadTimeout"
)


class QuietShelfmark:
    """Shelfmark answering normally, with nothing to hand over.

    The service of the measured case: every call succeeds, and the search comes
    back empty because no audiobook of this book exists in any source.
    """

    name = "shelfmark"
    dials = 0

    def __init__(self, base_url: str | None = None) -> None:
        self.last_rejections: list[str] = []

    def search(self, title: str, author: str = "", content_type: str = "ebook"):
        QuietShelfmark.dials += 1
        return []

    def find_task(self, title: str, author: str = "", source_ids=()):
        QuietShelfmark.dials += 1
        return None

    def close(self) -> None:
        pass


class DeadReleases:
    """Shelfmark answering about a queued download whose releases all failed.

    The second false negative of the same class, one branch over: `_watch`
    returns `failed(kind='data')` after `find_task` *returned* -- the service
    plainly answered -- and a probe read as silence there wedges the breaker
    for every book whose download died, exactly as the empty search does.
    """

    name = "shelfmark"
    dials = 0

    def __init__(self, base_url: str | None = None) -> None:
        self.last_rejections: list[str] = []

    def find_task(self, title: str, author: str = "", source_ids=()):
        DeadReleases.dials += 1
        return ("task-1", "failed", {"error": "no peers for this release"})

    def close(self) -> None:
        pass


class DeadShelfmark:
    """Shelfmark with the sources behind it unreachable: a 503 on every call."""

    name = "shelfmark"
    dials = 0

    def __init__(self, base_url: str | None = None) -> None:
        self.last_rejections: list[str] = []

    def search(self, title: str, author: str = "", content_type: str = "ebook"):
        DeadShelfmark.dials += 1
        raise ClientError("shelfmark", FAILURE.split(": ", 1)[1], status=503)

    def find_task(self, title: str, author: str = "", source_ids=()):
        DeadShelfmark.dials += 1
        raise ClientError("shelfmark", FAILURE.split(": ", 1)[1], status=503)

    def close(self) -> None:
        pass


def _trip(clock) -> dict:
    """Open the breaker the way a real outage does, then walk into half-open."""
    for _ in range(breaker.FAILURE_THRESHOLD):
        breaker.record_failure("shelfmark", FAILURE)
    row = breaker.states()["shelfmark"]
    assert row["state"] == "open"
    clock.advance(breaker.COOLDOWN_BASE_SECONDS + 1)
    assert breaker.states()["shelfmark"]["state"] == "half_open"
    return row


def _queued_book(title: str, tried: int = acquire.MAX_RELEASE_ATTEMPTS) -> int:
    """A book whose download is queued with Shelfmark and whose task is dead."""
    return helpers.seed_book(title, {
        "classify": models.OK,
        "acquire_ebook": models.OK,
        "acquire_audiobook": {
            "status": models.BLOCKED,
            "service": "shelfmark",
            "detail": "queued from MyAnonaMouse (a good release)",
            "queued_at": helpers.iso(-300),
            "output_path": json.dumps([f"src-{i}" for i in range(tried)]),
        },
        "place": {"status": models.OK, "output_path": '{"ebook": "/books/a.epub"}'},
        "index": models.OK,
        "notebook": models.OK,
        "verify": models.OK,
        "shelve": models.OK,
    })


def _no_recovery_line() -> bool:
    return not [e for e in db().recent_events(200)
                if "answering again" in e["message"]]


# =========================================================================
# the direction the verifier measured: the service answers, and has nothing
# =========================================================================
def test_a_healthy_service_with_nothing_to_find_closes_the_breaker(monkeypatch, clock):
    """The reproduction, end to end: healthy Shelfmark, empty search, wedge.

    Six books, `ADVANCE_WORKERS` 6, one cooldown -- the shape the verifier
    measured over 20 of them. Before the fix the breaker re-opened on the
    successful dial and did so again on every cooldown after it, holding the
    whole library for as long as nothing changed; nothing else could clear it.
    """
    monkeypatch.setattr(acquire, "ShelfmarkClient", QuietShelfmark)
    monkeypatch.setattr(acquire, "free_space_gb", lambda path: 999.0)
    monkeypatch.setattr(pipeline, "ADVANCE_WORKERS", 6)

    book_ids = [helpers.audiobook_pending_book(f"Nothing Found {i}") for i in range(6)]
    trip = _trip(clock)

    QuietShelfmark.dials = 0
    helpers.scheduler().sweep()

    assert QuietShelfmark.dials >= 1, "the probe never reached the service"
    row = breaker.states()["shelfmark"]
    assert row["state"] == "closed", (
        "a service that answered the probe was read as silence: the breaker "
        "re-opens on every cooldown and the library stops acquiring"
    )
    assert row["trips"] == 0
    assert row["opened_at"] is None, "the outage's clock outlived the outage"
    assert row["open_until"] is None
    assert trip["opened_at"], "the trip did not record when the outage began"
    # The one line the operator reads when this happens by itself.
    assert [e for e in db().recent_events(200) if "answering again" in e["message"]]

    # The books are released by the close, and the ones the stale snapshot held
    # during that sweep run on the next one. Either way, nothing is left held
    # behind a breaker that is not holding anything.
    helpers.scheduler().sweep()
    for book_id in book_ids:
        stage_row = db().stage(book_id, "acquire_audiobook")
        assert stage_row["held_by"] == "", stage_row["detail"]
        assert stage_row["status"] == models.FAILED
        assert stage_row["failure_kind"] == "data"
        assert "no audiobook releases found" in stage_row["detail"]


def test_a_probe_the_service_answered_closes_it_even_from_watch(monkeypatch, clock):
    """The same false negative, one branch over: a release that died.

    `_watch` returns `failed(kind='data')` for a queued task the service
    reported as failed after every candidate had been tried. `find_task`
    returned, so the service answered; read as silence, this wedges the breaker
    for any library whose stuck books are stuck on dead releases.
    """
    monkeypatch.setattr(acquire, "ShelfmarkClient", DeadReleases)
    monkeypatch.setattr(acquire, "free_space_gb", lambda path: 999.0)
    monkeypatch.setattr(pipeline, "ADVANCE_WORKERS", 1)

    book_id = _queued_book("Dead Release")
    _trip(clock)

    DeadReleases.dials = 0
    helpers.scheduler().sweep()

    assert DeadReleases.dials == 1, "the probe never reached the service"
    row = breaker.states()["shelfmark"]
    assert row["state"] == "closed", (
        "Shelfmark reported a failed task and the breaker read it as silence"
    )
    assert row["trips"] == 0
    # The book still fails: a release that will not download is its own
    # problem, and the hold is what must not survive.
    stage_row = db().stage(book_id, "acquire_audiobook")
    assert stage_row["status"] == models.FAILED
    assert stage_row["failure_kind"] == "data"
    assert stage_row["held_by"] == ""


def test_the_same_discriminator_covers_the_notebook_stage(monkeypatch):
    """The second stage that returns `data` after a dial, and the same read.

    `notebook` is gated by the breaker like the acquire stages are
    (`SERVICE_STAGES` puts it under Open Notebook), and both of its `data`
    branches are reachable only once Open Notebook has answered: "there is no
    notebook by that name" is the reply to a listing, and "the source failed to
    process" is a status read back from the service. Unflagged, they are F1's
    false negative again, one service over — a working Open Notebook held
    forever by a book whose category points at a notebook nobody created.

    Asserted at the result rather than through a sweep because that is where
    the fact is set: `probe_verdict` is the only reader, and it is driven
    directly in the test below.
    """
    from app.stages import notebook

    class NoNotebook:
        name = "open notebook"

        def __init__(self, *args, **kwargs):
            pass

        def notebook_named(self, name):
            return None

        def close(self):
            pass

    monkeypatch.setattr(notebook, "OpenNotebookClient", NoNotebook)
    # The category map is not what this is about; every other branch of the
    # stage is behind a real file and a real classify.
    monkeypatch.setattr(notebook.classify, "destination_for",
                        lambda category: ("folder", "MyNotes"))

    # Under the library root, because `to_opennotebook_path` refuses anything
    # else — and a `.txt`, so `unreadable_reason` does not want a real epub.
    from app.config import settings

    book_id = helpers.seed_book("Notebook Miss", {
        "classify": models.OK,
        "place": {"status": models.OK,
                  "output_path": json.dumps(
                      {"ebook": str(settings.books_root / "book.txt")})},
    })

    result = notebook.run(helpers.load_book(book_id))

    assert result.status == models.FAILED
    assert result.kind == "data"
    assert result.answered is True, (
        "a `data` failure that came back from the service is unflagged, so a "
        "probe reads a working Open Notebook as silence"
    )
    assert breaker.probe_verdict(result) is True


# =========================================================================
# the direction the first fix established, which must not come back
# =========================================================================
def test_a_probe_refused_by_our_own_disk_is_still_not_an_answer(monkeypatch, clock):
    """F1 itself, on the real stage: the free-space check runs before the dial.

    Same `kind`, same stage, same `data` failure as the test above, and the
    opposite conclusion: nothing was asked, so nothing was answered. If the
    discriminator ever collapses back into an inference from the kind, this is
    the test that says so -- zero searches, and the breaker re-opens on the
    doubled cooldown.
    """
    monkeypatch.setattr(acquire, "ShelfmarkClient", QuietShelfmark)
    monkeypatch.setattr(acquire, "free_space_gb", lambda path: 0.5)
    monkeypatch.setattr(pipeline, "ADVANCE_WORKERS", 6)

    for i in range(6):
        helpers.audiobook_pending_book(f"Full Disk {i}")
    trip = _trip(clock)

    QuietShelfmark.dials = 0
    helpers.scheduler().sweep()

    assert QuietShelfmark.dials == 0, (
        "the free-space check no longer precedes the search"
    )
    row = breaker.states()["shelfmark"]
    assert row["state"] == "open", (
        "a probe that never reached the service closed the breaker"
    )
    assert row["trips"] == 2, "the no-evidence verdict was not a re-open"
    assert row["open_until"] > trip["open_until"], "the cooldown did not double"
    assert _no_recovery_line(), "a recovery was reported that nobody proved"


def test_a_stage_that_raised_is_still_not_an_answer(monkeypatch, clock):
    """The other measured shape of the first fix: `_advance`'s error synthesis.

    A stage that raises something that is not a `ClientError` becomes
    `failed(kind='', service='')`. It never dialed, it cannot have answered,
    and it must not carry a `data` kind into the verdict by some later
    refactor.
    """
    def boom(book):
        raise ValueError("stage bug")

    monkeypatch.setattr(pipeline, "REGISTRY",
                        {stage: boom for stage in models.STAGES})
    monkeypatch.setattr(pipeline, "ADVANCE_WORKERS", 1)

    helpers.audiobook_pending_book("Boom")
    trip = _trip(clock)

    helpers.scheduler().sweep()

    row = breaker.states()["shelfmark"]
    assert row["state"] == "open", "a stage bug was read as the service answering"
    assert row["trips"] == 2
    assert row["open_until"] > trip["open_until"]
    assert _no_recovery_line()


# =========================================================================
# the point of the whole thing: a service that is down still re-opens
# =========================================================================
def test_a_service_that_is_still_down_reopens_on_the_probe(monkeypatch, clock):
    """A fix that cannot re-open is worse than the wedge it removes.

    The same shape as the answered case -- one probe, one cooldown -- with the
    service failing the dial instead of answering it. One search, and the
    breaker goes back to open on a longer cooldown with the outage's own clock
    untouched.
    """
    monkeypatch.setattr(acquire, "ShelfmarkClient", DeadShelfmark)
    monkeypatch.setattr(acquire, "free_space_gb", lambda path: 999.0)
    monkeypatch.setattr(pipeline, "ADVANCE_WORKERS", 6)

    for i in range(6):
        helpers.audiobook_pending_book(f"Still Down {i}")
    trip = _trip(clock)

    DeadShelfmark.dials = 0
    helpers.scheduler().sweep()

    assert DeadShelfmark.dials == 1, (
        f"{DeadShelfmark.dials} searches against a service known to be down"
    )
    row = breaker.states()["shelfmark"]
    assert row["state"] == "open", "the breaker closed on a failing service"
    assert row["trips"] == 2
    assert row["open_until"] > trip["open_until"], "the cooldown did not double"
    assert row["opened_at"] == trip["opened_at"], "the outage restarted its clock"
    assert _no_recovery_line()
