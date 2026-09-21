"""Regressions for the four problems the *verification* of the breaker found.

The breaker's own four defects (D1-D4) are covered in
`test_breaker_regressions.py`; these are the problems those fixes introduced or
left standing, each one written to fail on the build that shipped them:

  F1  `resolve_probe` read the verdict from `not was_transient and not
      refused_probe`, which is a statement about failure *kinds* being used as
      a statement about the *service*. Everything that was not a transient
      failure closed the breaker, including results that never went near it.
      `acquire._queue` measures free space before it searches, so an audiobook
      probe on a full disk came back `failed(kind='data')` and closed the
      breaker with zero searches; the same hole swallowed a stage that raised
      anything that was not a `ClientError` (`kind` and `service` are both
      `''` there). Reproduced by `/tmp/adv2/ind6_probe_noask.py`.
      The first fix narrowed the verdict to a whitelist of *kinds*, which
      fixed that direction and opened the other one, because `data` is what
      the same stage returns after a search that succeeded and found no
      release. The breakers then re-opened on a healthy service on every
      cooldown, silently -- 16.25h and 20 cooldowns measured, 5 books held
      with no way out. Fixed by carrying the distinction the stage knows
      (`models.StageResult.answered`) instead of inferring it from a kind two
      situations share; `test_a_probe_the_service_answered_about_is_not_silence`
      is that case, and the parametrized verdict table below pins the
      never-dialed half so it cannot come back.
  F2  The D1 fix records a failed probe from two places in one sweep, and the
      second one -- `_advance`'s `refused_probe` block, carrying the *held*
      sentence -- overwrote `last_failure`, which `hold_detail()` embeds
      verbatim and the panel renders as the group's `sample`. The operator read
      our wrapper around the upstream's message, nested inside a second copy of
      itself on every book marked held after the probe. Reproduced by
      `/tmp/adv2/ind1b_doublecall.py`.
  F3  D4's widened guard also reached a book whose download is legitimately in
      flight (`blocked`, `held_by=''`, `queued_at` set) and replaced the one
      line naming the release it was fetching. Reproduced by
      `/tmp/adv2/ind4_markheld.py` section C.
  F4  `FAILURE_WINDOW_SECONDS` was re-stamped on every count, so it bounded the
      gap between failures and not the streak: a service failing once every 61
      minutes, forever and never once succeeding, was never held. Deliberately
      re-decided rather than left as it was -- see the constant, and
      `test_breaker_regressions.py` for the numbers -- and pinned here at the
      level of the rates it does and does not catch.
"""

from __future__ import annotations

import pytest

from app import breaker, main, models, pipeline
from app.clients.base import ClientError
from app.db import db
from app.stages import acquire

import helpers

FAILURE = (
    "shelfmark: release search failed with HTTP 503: Unable to reach download "
    "source. Could not reach Anna's Archive after 10 attempt(s): ReadTimeout"
)


class DownShelfmark:
    """Shelfmark with the sources behind it unreachable: a 503 on every call."""

    name = "shelfmark"
    searches = 0

    def __init__(self, base_url: str | None = None) -> None:
        self.last_rejections: list[str] = []

    def search(self, title: str, author: str = "", content_type: str = "ebook"):
        DownShelfmark.searches += 1
        raise ClientError("shelfmark", FAILURE.split(": ", 1)[1], status=503)

    def find_task(self, title: str, author: str = "", source_ids=()):
        DownShelfmark.searches += 1
        raise ClientError("shelfmark", FAILURE.split(": ", 1)[1], status=503)

    def close(self) -> None:
        pass


def _queued_book(title: str, detail: str = "queued from MyAnonaMouse (a good release)") -> int:
    """A book whose download is in flight with Shelfmark.

    `blocked` with a live `queued_at`, naming nobody: this is the row the D4
    guard reaches and the one that must not lose its own words.
    """
    return helpers.seed_book(title, {
        "classify": models.OK,
        "acquire_ebook": models.OK,
        "acquire_audiobook": {
            "status": models.BLOCKED,
            "service": "shelfmark",
            "detail": detail,
            "queued_at": helpers.iso(-120),
            "output_path": '["src-1"]',
        },
        "place": {"status": models.OK, "output_path": '{"ebook": "/books/a.epub"}'},
        "index": models.OK,
        "notebook": models.OK,
        "verify": models.OK,
        # Seeded so the queued acquire stage is the book's only outstanding
        # work: a pending `shelve` would run for real during a sweep and its
        # own failure would be the thing under test instead.
        "shelve": models.OK,
    })


def _trip(clock) -> dict:
    for _ in range(breaker.FAILURE_THRESHOLD):
        breaker.record_failure("shelfmark", FAILURE)
    row = breaker.states()["shelfmark"]
    assert row["state"] == "open"
    return row


# =========================================================================
# F1 -- a probe that never reached the service
# =========================================================================
@pytest.mark.parametrize("status,kind,expected", [
    # The service's own answers. `ok` is the stage completing against it; a
    # rejected credential and a 404 are both the service replying.
    (models.OK, "", True),
    (models.OK, "server", True),
    (models.FAILED, "auth", True),
    (models.FAILED, "notfound", True),
    # The service failing is not the service answering.
    (models.FAILED, "network", False),
    (models.FAILED, "server", False),
    (models.FAILED, "busy", False),
    # Nothing about the service at all: the disk-space check (which runs before
    # the search), an unhandled stage error, a fact about a release.
    (models.FAILED, "data", False),
    (models.FAILED, "", False),
    # Ran, did not fail: a wait it decided on, or nothing to do.
    (models.BLOCKED, "", True),
    (models.SKIPPED, "", True),
])
def test_only_the_services_own_results_answer_a_probe(status, kind, expected):
    """The verdict, for every shape a result can have.

    `refused` is asserted separately because it is the one input the result
    cannot carry: the stage handed the book back without asking, and no value
    of any field may turn that into an answer (that is D1's invariant, kept).
    """
    result = models.StageResult(status=status, detail="x", kind=kind,
                                service="shelfmark")
    assert breaker.probe_verdict(result) is expected
    assert breaker.probe_verdict(result, refused=True) is False


def test_the_stages_own_word_is_the_only_thing_that_separates_the_two_data_cases():
    """`answered` decides, and nothing else can outvote a failing service.

    The two `data` failures in one function (`acquire._queue`) are opposite
    facts about the service and identical in every field a reader could use, so
    the stage states which it did. Three assertions, one per way the flag could
    be misread: set, unset, and set on a result that is the service *failing*.
    """
    found_nothing = models.StageResult.failed(
        "no audiobook releases found for 'X'", kind="data",
        service="shelfmark", answered=True,
    )
    assert breaker.probe_verdict(found_nothing) is True
    assert breaker.probe_verdict(
        models.StageResult.failed("only 0.5 GB free where audiobooks land",
                                  kind="data")
    ) is False
    assert breaker.probe_verdict(
        models.StageResult.failed("503 from the source", kind="server",
                                  service="shelfmark", answered=True)
    ) is False, "a flag closed the breaker on a service that is failing"


def test_a_probe_refused_by_our_own_disk_does_not_close_the_breaker(monkeypatch, clock):
    """F1, end to end, through the stage that has the free-space check.

    The disk is below the floor, so `acquire._queue` returns
    `failed(kind='data')` without searching. That is our own disk refusing the
    book, and the breaker must not read it as Shelfmark answering: it re-opens
    on the doubled cooldown and no further book is attempted.
    """
    monkeypatch.setattr(acquire, "ShelfmarkClient", DownShelfmark)
    monkeypatch.setattr(acquire, "free_space_gb", lambda path: 0.5)
    monkeypatch.setattr(pipeline, "ADVANCE_WORKERS", 6)

    for i in range(6):
        helpers.audiobook_pending_book(f"Full {i}")
    trip = _trip(clock)
    clock.advance(breaker.COOLDOWN_BASE_SECONDS + 1)
    assert breaker.states()["shelfmark"]["state"] == "half_open"

    DownShelfmark.searches = 0
    helpers.scheduler().sweep()

    row = breaker.states()["shelfmark"]
    assert row["state"] == "open", "a probe that never asked closed the breaker"
    assert row["trips"] == 2
    assert row["open_until"] > trip["open_until"]
    # The flap the finding describes is the *next* sweep running everything.
    assert DownShelfmark.searches == 0
    clock.advance(breaker.cooldown(row["trips"]) + 1)
    helpers.scheduler().sweep()
    assert DownShelfmark.searches == 0


def test_a_probe_that_raised_an_unrelated_error_does_not_close_the_breaker(
    monkeypatch, clock
):
    """F1, the other measured shape: a stage bug, not a service fault.

    `_advance` turns a stage that raises something other than a `ClientError`
    into `failed("unhandled error: ...", kind="", service="")`, and `''` is not
    a transient failure either -- so the old verdict closed the breaker on a
    traceback and the next sweep ran every book. The breaker knows nothing
    about a `ValueError`, and must say so.
    """
    def boom(book):
        raise ValueError("stage bug")

    monkeypatch.setattr(pipeline, "REGISTRY",
                        {stage: boom for stage in models.STAGES})
    monkeypatch.setattr(pipeline, "ADVANCE_WORKERS", 1)

    helpers.audiobook_pending_book("Boom")
    _trip(clock)
    clock.advance(breaker.COOLDOWN_BASE_SECONDS + 1)

    helpers.scheduler().sweep()

    row = breaker.states()["shelfmark"]
    assert row["state"] == "open", "a stage bug was read as the service answering"
    assert row["trips"] == 2
    assert not [e for e in db().recent_events(200) if "answering again" in e["message"]]


def test_a_probe_that_found_nothing_is_not_a_probe_that_was_answered(monkeypatch, clock):
    """The stub shape, kept for the reason it is still true.

    This test used to assert the *opposite* of what it says now, and the bug
    it enshrined was F1's false negative: a hand-built `failed(kind='data')`
    from a stage says nothing about whether the stage dialed, so reading it as
    silence is right for the free-space check and wrong for a search that
    answered. The distinction cannot be recovered from the result's kind or
    service -- this stub carries both, and it is neither -- which is why the
    stage states it (`StageResult.answered`) instead of a reader guessing.

    What the test below (`..._the_service_answered_about_...`) covers is the
    real branch, which sets it. This one covers everything that does not.
    """
    def empty(book):
        return models.StageResult.failed(
            "no audiobook releases found for 'X'", kind="data",
            service="shelfmark",
        )

    monkeypatch.setattr(pipeline, "REGISTRY",
                        {stage: empty for stage in models.STAGES})
    monkeypatch.setattr(pipeline, "ADVANCE_WORKERS", 1)

    book_id = helpers.audiobook_pending_book("Nothing Found")
    _trip(clock)
    clock.advance(breaker.COOLDOWN_BASE_SECONDS + 1)

    helpers.scheduler().sweep()

    assert breaker.states()["shelfmark"]["state"] == "open", (
        "a result that says nothing about the service closed the breaker"
    )
    # The book is not held for it: the failure is its own, and it stays visible
    # as a data failure rather than being folded into an outage.
    row = db().stage(book_id, "acquire_audiobook")
    assert row["status"] == models.FAILED
    assert row["failure_kind"] == "data"
    assert row["held_by"] == ""


class FindsNothing:
    """A Shelfmark that answers, and has nothing to hand over.

    The healthy service of F1's second half. It searches successfully and
    returns no releases, which is what a real library looks like on the books
    that lead the rotation: "no audiobook exists in any configured source".
    """

    name = "shelfmark"
    dials = 0

    def __init__(self, base_url: str | None = None):
        self.last_rejections: list[str] = []

    def search(self, title: str, author: str = "", content_type: str = "ebook"):
        FindsNothing.dials += 1
        return []

    def close(self):
        pass


def test_a_probe_the_service_answered_about_is_not_silence(monkeypatch, clock):
    """F1's second half, end to end through the branch that sets `answered`.

    The verdict F1 fixed reads `data` as "the service was never asked", which
    is true of the free-space check and false of the branch immediately below
    it: `acquire._queue` returns the same `data` failure after a search that
    *succeeded* and found no release. Narrowed to the kind, the verdict re-opens
    the breaker on a service that answered every time, forever -- measured at
    16.25h, 20 cooldowns, 25 successful dials, 5 books held and no reset path
    from the UI or a restart -- and the books that trigger it are quarter of a
    real library, so it is not a corner.

    Driving the real stage is the point: what is asserted is that the branch
    which knows the difference says so, not that some test set a flag.
    """
    monkeypatch.setattr(acquire, "ShelfmarkClient", FindsNothing)
    monkeypatch.setattr(acquire, "free_space_gb", lambda path: 999.0)
    monkeypatch.setattr(pipeline, "ADVANCE_WORKERS", 1)

    book_ids = [helpers.audiobook_pending_book(f"Nothing Found {i}") for i in range(3)]
    _trip(clock)
    clock.advance(breaker.COOLDOWN_BASE_SECONDS + 1)
    assert breaker.states()["shelfmark"]["state"] == "half_open"

    FindsNothing.dials = 0
    helpers.scheduler().sweep()

    assert FindsNothing.dials >= 1, "the probe never reached the service"
    row = breaker.states()["shelfmark"]
    assert row["state"] == "closed", (
        "the service answered the probe and the breaker read it as silence"
    )
    assert row["trips"] == 0
    assert row["opened_at"] is None
    assert [e for e in db().recent_events(200) if "answering again" in e["message"]], \
        "the recovery was not reported"

    # Every book keeps its own failure: "no audiobook exists" is a fact about
    # the release, not about the outage, so it must not be folded into a hold
    # -- and it must stop being held the moment the breaker lets go, or the
    # hold is what the operator is left reading.
    for book_id in book_ids:
        stage_row = db().stage(book_id, "acquire_audiobook")
        assert stage_row["held_by"] == "", (
            f"a book stayed held after the service answered: {stage_row['detail']}"
        )
        assert stage_row["status"] == models.FAILED
        assert stage_row["failure_kind"] == "data"
        assert "no audiobook releases found" in stage_row["detail"]


def test_a_probe_the_service_answered_still_closes_the_breaker(monkeypatch, clock):
    """The direction that must not break: a fix that cannot close is worse.

    Both halves the module documents as real answers, on the real stage: a
    search that succeeds and queues the download (the acquire stage returns
    `blocked` for that, and it is proof the service is up), and an `ok` when
    the queued task reports complete.
    """
    class Back:
        name = "shelfmark"

        def __init__(self, base_url: str | None = None):
            self.last_rejections: list[str] = []

        def find_task(self, title, author="", source_ids=()):
            return ("task-9", "complete", {})

        def close(self):
            pass

    monkeypatch.setattr(acquire, "ShelfmarkClient", Back)
    monkeypatch.setattr(acquire, "free_space_gb", lambda path: 999.0)
    monkeypatch.setattr(pipeline, "ADVANCE_WORKERS", 1)

    _queued_book("Comes Back")
    _trip(clock)
    clock.advance(breaker.COOLDOWN_BASE_SECONDS + 1)
    assert breaker.states()["shelfmark"]["state"] == "half_open"

    helpers.scheduler().sweep()

    row = breaker.states()["shelfmark"]
    assert row["state"] == "closed", "a recovered service left the breaker open"
    assert row["trips"] == 0
    assert [e for e in db().recent_events(200) if "answering again" in e["message"]]


# =========================================================================
# F2 -- our wrapper must not bury the upstream's words
# =========================================================================
def test_the_upstreams_words_survive_a_failed_probe_and_are_not_nested(
    monkeypatch, clock
):
    """The panel's sample is the service's own message, exactly once.

    One failed probe used to call `record_failure` twice in the same sweep, the
    second time with the *held* sentence, so `last_failure` -- the raw text the
    held group renders as `sample` and `hold_detail` embeds verbatim -- became
    our wrapper around the upstream's message, and every book marked held after
    the probe got it nested inside a second copy. The stage has already
    recorded the upstream's words; the second call must not replace them.
    """
    monkeypatch.setattr(acquire, "ShelfmarkClient", DownShelfmark)
    monkeypatch.setattr(acquire, "free_space_gb", lambda path: 999.0)
    monkeypatch.setattr(pipeline, "ADVANCE_WORKERS", 1)

    calls: list[str] = []
    real = breaker.record_failure

    def spy(service, detail, retry_after=None):
        calls.append(str(detail))
        return real(service, detail, retry_after=retry_after)

    monkeypatch.setattr(breaker, "record_failure", spy)

    for i in range(6):
        _queued_book(f"Words {i}")
    _trip(clock)
    clock.advance(breaker.COOLDOWN_BASE_SECONDS + 1)
    # Only the sweep's own calls are in question; the trip above is the spy's
    # first three.
    calls.clear()

    helpers.scheduler().sweep()

    row = breaker.states()["shelfmark"]
    assert row["trips"] == 2, "the probe was not counted as a re-open"
    assert [c for c in calls if c] == [FAILURE], (
        f"one failed probe recorded {len(calls)} failures: {calls}"
    )
    assert row["last_failure"] == FAILURE
    assert not row["last_failure"].startswith("held:")

    # What the operator's panel row shows, and what each held book's own row
    # says: the upstream's message, wrapped at most once.
    group = next(g for g in main.issues()["groups"] if g["stage"] == "held")
    assert group["sample"] == FAILURE
    for r in db().query("SELECT detail FROM stage_runs "
                        "WHERE stage='acquire_audiobook'"):
        assert r["detail"].count("held:") <= 1, r["detail"]
        assert r["detail"].count(FAILURE) <= 1, r["detail"]


# =========================================================================
# F3 -- a download in flight keeps its own words
# =========================================================================
def test_a_queued_download_keeps_its_release_when_the_breaker_refuses_it(
    monkeypatch, clock
):
    """F3 through the pipeline, which is where mark_held is called from.

    The breaker is open, so the queued book's stage is refused before it runs:
    `mark_held` writes the hold. `queued_at` and `output_path` were never the
    problem -- the detail line was, and it is the only line saying what the
    book is doing.
    """
    monkeypatch.setattr(pipeline, "ADVANCE_WORKERS", 1)
    book_id = _queued_book("Downloading")
    _trip(clock)

    summary = helpers.scheduler().sweep()

    assert summary["held"] == 1
    row = db().stage(book_id, "acquire_audiobook")
    assert "MyAnonaMouse" in row["detail"], row["detail"]
    # The hold is still written: `book_state` reads `held_by` to tell a book
    # waiting on a source from one whose audiobook "does not exist anywhere".
    assert row["held_by"] == "shelfmark"
    assert row["queued_at"]
    assert row["output_path"] == '["src-1"]'
    assert row["attempts"] == 0

    group = next(g for g in main.issues()["groups"] if g["stage"] == "held")
    assert [b["id"] for b in group["books"]] == [book_id]
    assert main.book_state(helpers.load_book(book_id)) == "working"


def test_a_queued_download_keeps_its_release_through_a_failed_probe(
    monkeypatch, clock
):
    """F3 for the other writer, which is the one a probe goes through.

    A queued download is a prime probe candidate -- its stage is the one the
    breaker gates -- and its poll fails, so the row is written by `mark_done`
    with the hold's result rather than by `mark_held`. The download is still
    queued and still running, so its own words still describe it, and
    `mark_done` has to leave them where the refusal path leaves them too.
    """
    monkeypatch.setattr(acquire, "ShelfmarkClient", DownShelfmark)
    monkeypatch.setattr(acquire, "free_space_gb", lambda path: 999.0)
    monkeypatch.setattr(pipeline, "ADVANCE_WORKERS", 1)

    book_id = _queued_book("Probed")
    _trip(clock)
    clock.advance(breaker.COOLDOWN_BASE_SECONDS + 1)

    helpers.scheduler().sweep()

    row = db().stage(book_id, "acquire_audiobook")
    assert "MyAnonaMouse" in row["detail"], row["detail"]
    assert row["held_by"] == "shelfmark"
    assert row["queued_at"]

    # And the words come back to life with it: the hold is what the outage
    # contributed, not the release the book is fetching.
    class Back:
        name = "shelfmark"

        def __init__(self, base_url: str | None = None):
            self.last_rejections: list[str] = []

        def find_task(self, title, author="", source_ids=()):
            return ("task-1", "complete", {})

        def close(self):
            pass

    monkeypatch.setattr(acquire, "ShelfmarkClient", Back)
    clock.advance(breaker.cooldown(breaker.states()["shelfmark"]["trips"]) + 1)
    helpers.scheduler().sweep()

    row = db().stage(book_id, "acquire_audiobook")
    assert row["status"] == models.OK
    assert row["held_by"] == ""


# =========================================================================
# F4 -- the streak window, at the rates it catches and the ones it does not
# =========================================================================
@pytest.mark.parametrize("gap_minutes", [30, 61, 120, 6 * 60, 23 * 60])
def test_a_service_failing_slower_than_a_sweep_is_still_held(clock, gap_minutes):
    """Any rate inside the window trips on the third failure.

    The measured case is 61 minutes -- the rate that, under the original
    one-hour window, restarted its own streak on every count and was never
    held, forever, so the per-book grace parked those books one at a time. The
    day-sized window is what makes the *rate* the thing that matters rather
    than the interval.
    """
    tripped_at = None
    for count in range(1, 21):
        clock.advance(gap_minutes * 60)
        if breaker.record_failure("shelfmark", FAILURE):
            tripped_at = count
            break

    assert tripped_at == breaker.FAILURE_THRESHOLD, (
        f"a service failing every {gap_minutes} minutes was never held"
    )
    assert breaker.states()["shelfmark"]["state"] == "open"


@pytest.mark.parametrize("gap_minutes", [24 * 60 + 1, 48 * 60])
def test_failures_wider_apart_than_the_window_are_not_a_streak(clock, gap_minutes):
    """And the bound still holds: unrelated failures do not accumulate.

    This is what the window is *for*, and it is deliberately not traded away --
    three failures days apart say nothing about whether the service is usable
    now. What that leaves uncovered is a service failing more slowly than once
    a day, forever; the cost of that is one book at a time on its own 24h
    grace, not the 169-book wall, which needs a rate rather than a total.
    """
    for _ in range(20):
        clock.advance(gap_minutes * 60)
        breaker.record_failure("shelfmark", FAILURE)

    row = breaker.states()["shelfmark"]
    assert row["state"] == "closed"
    assert row["failures"] == 1, "the streak was not restarted by the gap"
