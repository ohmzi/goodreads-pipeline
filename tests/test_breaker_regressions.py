"""Regressions for the four defects the breaker shipped with, and for the
ground the four fixes moved.

Each defect was found by an adversarial verifier against a build whose own 43
tests were passing, so every test here is written to fail on that build rather
than to restate a behaviour that already worked:

  D1  a probe that failed against a DOWN service closed the breaker, because
      the verdict was read from `result.kind` -- the one field `breaker.hold()`
      exists to clear. `test_a_recovered_service_still_closes_the_breaker`
      guards the other direction (a fix that makes the breaker unclosable is
      worse than the flap), and `test_a_failed_probe_is_counted_once` guards
      the fix itself, which counts the failure from two places.
  D2  `record_success` was unreachable -- `StageResult.ok()` took no service --
      so a service's failure counter had no reset and no time window. The
      verifier's instrument reported "0 calls across 32 stage runs", but it
      drove the sweep with a stub registry whose `ok` named no service, so it
      could not have reported anything else. The test here drives a stage that
      really does name its service.
  D3  `max(cooldown, retry_after)` let an upstream header of 86400 through the
      documented 1h ceiling, and 1e9 wedged the breaker for 31 years.
  D4  `mark_held`'s `WHERE status <> 'blocked'` skipped exactly the rows
      `_advance` calls it for, so books the outage was holding were in no group
      at all and the panel undercounted them.

Three tests at the end of this file used to pin behaviour that was still
wrong, one per defect left open by that round, as `xfail(strict=True)` so they
could not be quietly deleted and a fix would make them XPASS. All three are
fixed now and the markers are gone:

* `test_a_failed_probe_keeps_the_upstreams_words` -- `_advance` recorded a
  failed probe twice, and the second call overwrote the upstream's message
  with our own wrapper around it;
* `test_a_queued_download_keeps_its_release_through_a_hold` -- a book whose
  download is in flight lost the `queued from <release>` line to the hold;
* `test_a_probe_that_never_reached_the_service_cannot_close_the_breaker` -- a
  probe refused by our own free-space check was read as the service answering.
  Its pin also asserted that the probe would *ask* the service, which the
  documented ordering of that check forbids; that assertion is gone, and why
  is written where it was.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from app import breaker, main, models, pipeline
from app.clients.base import ClientError
from app.db import db
from app.stages import acquire, shelve as shelve_stage

import helpers

FAILURE = (
    "shelfmark: release search failed with HTTP 503: Unable to reach download "
    "source. Could not reach Anna's Archive after 10 attempt(s): ReadTimeout"
)


def _now() -> str:
    return helpers.iso()


def _seed(title: str, **columns) -> int:
    """A book whose only outstanding stage is the audiobook acquisition."""
    book_id = helpers.audiobook_pending_book(title)
    if columns:
        assignments = ", ".join(f"{k}=?" for k in columns)
        db().execute(
            f"UPDATE stage_runs SET {assignments} WHERE book_id=? "
            f"AND stage='acquire_audiobook'",
            (*columns.values(), book_id),
        )
    return book_id


# =========================================================================
# D1 -- the probe's verdict
# =========================================================================
class DownShelfmark:
    """Shelfmark with the sources behind it unreachable."""

    name = "shelfmark"
    searches = 0
    reached: list[str] = []

    def __init__(self, base_url: str | None = None) -> None:
        self.last_rejections: list[str] = []

    def find_task(self, title, author="", source_ids=()):
        DownShelfmark.searches += 1
        DownShelfmark.reached.append(title)
        raise ClientError("shelfmark", FAILURE.split(": ", 1)[1], status=503)

    def search(self, title, author="", content_type="ebook"):
        DownShelfmark.searches += 1
        DownShelfmark.reached.append(title)
        raise ClientError("shelfmark", FAILURE.split(": ", 1)[1], status=503)

    def close(self) -> None:
        pass


class UpShelfmark:
    """The same service, answering."""

    name = "shelfmark"
    searches = 0

    def __init__(self, base_url: str | None = None) -> None:
        self.last_rejections: list[str] = []

    def find_task(self, title, author="", source_ids=()):
        UpShelfmark.searches += 1
        return ("task-9", "complete", {})

    def close(self) -> None:
        pass


def _queued_book(title: str) -> int:
    """A book already queued with Shelfmark, so the stage takes `_watch`."""
    return _seed(title, status=models.BLOCKED, service="shelfmark", held_by="",
                 queued_at=_now(), detail="queued from a source")


def _trip(clock) -> dict:
    """Open the breaker directly, without the pipeline, and return its row."""
    for _ in range(breaker.FAILURE_THRESHOLD):
        breaker.record_failure("shelfmark", FAILURE)
    return breaker.states()["shelfmark"]


def test_a_recovered_service_still_closes_the_breaker(monkeypatch, clock):
    """The fix must not make the breaker unclosable.

    Attacked through `acquire._watch` rather than `_queue`: a book already
    queued whose `find_task` 503s takes the same early-hold branch from a
    different entry point, and a fix that re-opens on every probe would leave
    a healthy service held forever with every book stuck behind it.
    """
    monkeypatch.setattr(acquire, "ShelfmarkClient", DownShelfmark)
    monkeypatch.setattr(acquire, "free_space_gb", lambda path: 999.0)
    monkeypatch.setattr(pipeline, "ADVANCE_WORKERS", 1)

    DownShelfmark.searches = 0
    DownShelfmark.reached = []
    books = [_queued_book(f"Watched {i}") for i in range(6)]

    sched = helpers.scheduler()
    sched.sweep()
    assert breaker.states()["shelfmark"]["state"] == "open"

    for _ in range(3):
        clock.advance(breaker.COOLDOWN_BASE_SECONDS + 1)
        sched.sweep()
        assert breaker.states()["shelfmark"]["state"] == "open"
        # A state assertion alone cannot see the flap: the breaker closed on
        # the probe and re-tripped on the next book within the same sweep.
        # The phantom recovery line is what the operator actually saw.
        assert not [e for e in db().recent_events(200)
                    if "answering again" in e["message"]], (
            "the breaker reported a recovery in the middle of an outage"
        )

    # The service comes back.
    monkeypatch.setattr(acquire, "ShelfmarkClient", UpShelfmark)
    clock.advance(breaker.cooldown(breaker.states()["shelfmark"]["trips"]) + 1)
    sched.sweep()

    row = breaker.states()["shelfmark"]
    assert row["state"] == "closed", "a recovered service left the breaker open"
    assert row["trips"] == 0
    assert row["opened_at"] is None

    assert db().query_one(
        "SELECT COUNT(*) AS n FROM stage_runs WHERE stage='acquire_audiobook' "
        "AND status = ?", (models.OK,)
    )["n"] == len(books), "held books did not resume after recovery"
    assert db().query_one(
        "SELECT COUNT(*) AS n FROM stage_runs WHERE stage='acquire_audiobook' "
        "AND held_by <> ''")["n"] == 0


def test_a_failed_probe_is_counted_once(monkeypatch, clock):
    """The fix calls `record_failure` from acquire; `_advance` calls it again.

    Both fire for the probing book in the same sweep -- `_advance`'s
    `refused_probe` block exists as the belt to that braces. They must not
    both count: one failed probe is one re-open, not two.
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

    _queued_book("Probe Me")
    _trip(clock)
    clock.advance(breaker.COOLDOWN_BASE_SECONDS + 1)
    assert breaker.states()["shelfmark"]["state"] == "half_open"

    helpers.scheduler().sweep()
    row = breaker.states()["shelfmark"]
    assert row["trips"] == 2, f"one failed probe counted as {row['trips']} trips"
    assert row["failures"] == 0
    assert len([c for c in calls if c]) >= 1


def test_one_probe_per_cooldown_however_many_books_are_waiting(monkeypatch, clock):
    """A dead service costs one search per cooldown, not one per book."""
    monkeypatch.setattr(acquire, "ShelfmarkClient", DownShelfmark)
    monkeypatch.setattr(acquire, "free_space_gb", lambda path: 999.0)
    monkeypatch.setattr(pipeline, "ADVANCE_WORKERS", 6)

    for i in range(24):
        _queued_book(f"Outage {i}")

    sched = helpers.scheduler()
    DownShelfmark.searches = 0
    sched.sweep()
    assert DownShelfmark.searches == 24, "the first sweep must discover the outage"

    seen = []
    for _ in range(6):
        clock.advance(breaker.COOLDOWN_BASE_SECONDS + 1)
        DownShelfmark.searches = 0
        DownShelfmark.reached = []
        sched.sweep()
        assert DownShelfmark.searches <= 1, (
            f"{DownShelfmark.searches} searches against a service known to be down"
        )
        assert breaker.states()["shelfmark"]["state"] == "open"
        seen += DownShelfmark.reached

    # The probe is not pinned to one book: once a probe re-opens the breaker it
    # is released, so the next cooldown may try a different book.
    assert len(seen) >= 2, "the same book probed every cooldown"


# =========================================================================
# D2 -- the failure counter's reset and its window
# =========================================================================
def test_record_success_is_reachable_from_a_non_acquire_stage(monkeypatch, clock):
    """Where the reset comes from, for a stage that is not `acquire_*`.

    The verifier instrumented `record_success` and saw zero calls -- but it
    drove the sweep with a stub registry whose `ok` carried no service, so it
    reported zero whether the fix were present or not. This drives the real
    `shelve` stage, whose `ok` names Goodreads.
    """
    seen: list[str] = []
    real = breaker.record_success
    monkeypatch.setattr(breaker, "record_success",
                        lambda service: (seen.append(service), real(service))[1])

    monkeypatch.setattr(shelve_stage, "auto_shelve_enabled", lambda: True)
    from app import goodreads

    monkeypatch.setattr(goodreads, "move_to_shelf",
                        lambda session, book_id, shelf: None)
    monkeypatch.setattr(goodreads, "GoodreadsSession", lambda: object())

    book_id = helpers.seed_book("Answered", {
        "classify": models.OK,
        "acquire_ebook": models.OK,
        "acquire_audiobook": models.SKIPPED,
        "place": {"status": models.OK, "output_path": '{"ebook": "/b/a.epub"}'},
        "index": models.OK, "notebook": models.OK, "verify": models.OK,
        "shelve": models.PENDING,
    })

    breaker.record_failure("goodreads", FAILURE)
    breaker.record_failure("goodreads", FAILURE)
    assert breaker.states()["goodreads"]["failures"] == 2

    helpers.scheduler().sweep()

    assert seen == ["goodreads"], (
        "a real stage's `ok` did not reach record_success; the counter has no "
        "reset on healthy traffic"
    )
    row = breaker.states()["goodreads"]
    assert row["failures"] == 0, "a success left the failure counter standing"
    assert row["trips"] == 0
    assert row["last_ok_at"]
    assert db().stage(book_id, "shelve")["status"] == models.OK


def test_failures_inside_the_window_still_trip(clock):
    """The window must bound the gap, not switch the breaker off.

    One a minute, one an hour, a real outage: all of them are a streak.
    """
    for _ in range(breaker.FAILURE_THRESHOLD):
        clock.advance(50 * 60)
        breaker.record_failure("shelfmark", FAILURE)

    row = breaker.states()["shelfmark"]
    assert row["state"] == "open", "a steady failure rate did not trip"
    assert row["trips"] == 1


def test_failures_a_day_apart_still_trip_and_two_days_apart_do_not(clock):
    """Where the window sits now, in both directions.

    It bounds the gap BETWEEN failures, and `failures_since` is re-stamped on
    every count, so a streak is "N failures, each within a day of the one
    before it". An hour, the original number, was measured too tight in the one
    direction that costs: a service failing once every 61 minutes -- forever,
    and never once succeeding -- restarted its own streak on every count and
    was *never held*, so the per-book grace parked those books as `failed` one
    at a time. That is this module's own failure mode, arrived at the slow way.

    The other direction is not an oversight: gaps wider than the window still
    age the streak out, because three failures a couple of days apart say
    nothing about whether the service is usable *now* -- and that false
    positive is what the bound exists to prevent. A service failing more slowly
    than once a day, forever, is therefore still never held; what that costs is
    one book at a time on its own 24h grace, never the 169-book wall, which
    needs a rate of failures rather than a total.
    """
    assert breaker.FAILURE_WINDOW_SECONDS == 24 * 3600
    assert breaker.FAILURE_WINDOW_SECONDS >= breaker.COOLDOWN_MAX_SECONDS

    # 61 minutes apart, the rate the verifier measured. Under the hour window
    # this walked 1 -> 1 -> 1 forever; it must now walk 1 -> 2 -> trip.
    tripped_at = None
    for count in range(1, 21):
        clock.advance(61 * 60)
        if breaker.record_failure("shelfmark", FAILURE):
            tripped_at = count
            break

    row = breaker.states()["shelfmark"]
    assert tripped_at == breaker.FAILURE_THRESHOLD, tripped_at
    assert row["state"] == "open"
    assert row["trips"] == 1

    # And the bound still bites, on its own service: gaps wider than the window
    # are not consecutive failures, however many of them arrive.
    for _ in range(20):
        clock.advance(breaker.FAILURE_WINDOW_SECONDS + 60)
        breaker.record_failure("kavita", "kavita: release search failed with HTTP 503")

    aged = breaker.states()["kavita"]
    assert aged["state"] == "closed", "three unrelated failures days apart tripped it"
    assert aged["failures"] == 1, "the streak was not restarted by the gap"


# =========================================================================
# D3 -- the cooldown ceiling
# =========================================================================
def _wait(clock, service="shelfmark") -> float:
    row = breaker.states()[service]
    return datetime.fromisoformat(row["open_until"]).timestamp() - clock.now


@pytest.mark.parametrize("retry_after", [7200.0, 86400.0, 1e9])
def test_the_clamp_holds_on_the_probe_path_too(clock, retry_after):
    """A wedge that needs a second hostile header is still a wedge.

    The trip path was measured by the verifier; `_reopen` -- reached from
    `record_failure` in half-open and from `resolve_probe(answered=False)` --
    is the same `wait_seconds` and must be capped the same way.
    """
    for _ in range(breaker.FAILURE_THRESHOLD - 1):
        breaker.record_failure("shelfmark", "429")
    breaker.record_failure("shelfmark", "429", retry_after=retry_after)
    assert _wait(clock) <= breaker.COOLDOWN_MAX_SECONDS

    clock.advance(breaker.COOLDOWN_MAX_SECONDS + 1)
    assert breaker.states()["shelfmark"]["state"] == "half_open"
    breaker.record_failure("shelfmark", "429 still", retry_after=retry_after)

    wait = _wait(clock)
    assert wait <= breaker.COOLDOWN_MAX_SECONDS, (
        f"a Retry-After of {retry_after:g} re-opened the breaker for {wait / 3600:.1f}h"
    )


def test_a_clamped_retry_after_is_still_reported(clock):
    """A discarded 86400 is its own bug: say what the upstream asked for."""
    for _ in range(breaker.FAILURE_THRESHOLD - 1):
        breaker.record_failure("shelfmark", "429")
    breaker.record_failure("shelfmark", "429 too many requests",
                           retry_after=86400.0)

    trips = [e for e in db().recent_events(20)
             if e["stage"] == "breaker" and e["level"] == "error"]
    assert len(trips) == 1
    assert "capped" in trips[0]["message"], trips[0]["message"]
    assert "24.0h" in trips[0]["message"], (
        "the upstream's own number is not in the log"
    )

    # And the same on the re-probe line, which goes through `_reopen`.
    clock.advance(breaker.COOLDOWN_MAX_SECONDS + 1)
    breaker.record_failure("shelfmark", "429 still", retry_after=1e9)
    reprobes = [e for e in db().recent_events(20)
                if "still not answering" in e["message"]]
    assert len(reprobes) == 1
    assert "capped" in reprobes[0]["message"]


def test_a_clamped_hold_is_distinguishable_from_a_cooldown_derived_one(clock):
    """The two log lines must not read the same: one discarded real evidence."""
    for _ in range(breaker.FAILURE_THRESHOLD):
        breaker.record_failure("shelfmark", "503")
    plain = [e["message"] for e in db().recent_events(20)
             if e["stage"] == "breaker" and e["level"] == "error"]
    assert plain and "capped" not in plain[0]

    # Close it and trip again, this time on a header the cap threw away.
    breaker.record_success("shelfmark")
    assert breaker.states()["shelfmark"]["state"] == "closed"
    db().execute("DELETE FROM events")

    for _ in range(breaker.FAILURE_THRESHOLD - 1):
        breaker.record_failure("shelfmark", "429")
    breaker.record_failure("shelfmark", "429", retry_after=86400.0)
    capped = [e["message"] for e in db().recent_events(20)
              if e["stage"] == "breaker" and e["level"] == "error"]
    assert capped and "capped" in capped[0], capped
    assert capped[0] != plain[0]


def test_a_retry_after_inside_the_cap_still_wins(clock):
    """The clamp must be a clamp, not a silent return to the backoff."""
    assert breaker.cooldown(1) == breaker.COOLDOWN_BASE_SECONDS
    for _ in range(breaker.FAILURE_THRESHOLD - 1):
        breaker.record_failure("shelfmark", "429")
    breaker.record_failure("shelfmark", "429", retry_after=900.0)
    assert abs(_wait(clock) - 900.0) < 2, "the header did not win inside the cap"


def test_one_hostile_header_cannot_wedge_the_breaker(clock):
    """After the cap the service is probed on the ordinary schedule."""
    for _ in range(breaker.FAILURE_THRESHOLD):
        breaker.record_failure("shelfmark", "429", retry_after=1e9)
    assert _wait(clock) <= breaker.COOLDOWN_MAX_SECONDS

    clock.advance(breaker.COOLDOWN_MAX_SECONDS + 1)
    assert breaker.states()["shelfmark"]["state"] == "half_open"
    assert breaker.claim_probe("shelfmark", 1, "acquire_audiobook")

    clock.advance(400 * 24 * 3600)
    assert breaker.states()["shelfmark"]["state"] != "open", "still wedged"


# =========================================================================
# D4 -- mark_held
# =========================================================================
def test_the_panel_count_matches_the_stuck_books_once_the_outage_settles(
    monkeypatch, clock
):
    """Every book the outage is holding is in the group, and the count is right.

    The verifier measured the author's own scenario one sweep in -- 4 held and
    2 orphans -- which is a mid-run snapshot of a state that converges. What
    must hold, and did not before the fix, is that it converges at all: the
    orphan rows are `blocked` with `held_by=''`, so they appear in no group
    (BLOCKED is not FAILED) and `book_state` reports them `partial` -- "the
    audiobook does not exist anywhere", the exact false signal that change
    existed to prevent.
    """
    monkeypatch.setattr(acquire, "ShelfmarkClient", DownShelfmark)
    monkeypatch.setattr(acquire, "free_space_gb", lambda path: 999.0)
    monkeypatch.setattr(pipeline, "ADVANCE_WORKERS", 1)

    books = [_seed(f"Outage {i}") for i in range(6)]
    sched = helpers.scheduler()

    # The first sweep is the one that *starts* the outage: its `holding`
    # snapshot is taken before the trip, so up to FAILURE_THRESHOLD-1 books
    # that failed on the way to the trip are still `blocked` rather than held.
    # That lag is one sweep wide and must not survive the next one.
    for sweep in range(4):
        sched.sweep()
        if sweep == 0:
            clock.advance(breaker.COOLDOWN_BASE_SECONDS + 1)
            continue

        rows = db().query("SELECT * FROM stage_runs WHERE stage='acquire_audiobook'")
        stuck = len(rows)
        group = next(g for g in main.issues()["groups"] if g["stage"] == "held")
        assert len(group["books"]) == stuck, (
            f"sweep {sweep + 1}: the panel says {len(group['books'])} of {stuck} "
            f"stuck books are held"
        )
        assert not [r for r in rows if r["status"] == models.BLOCKED
                    and not r["held_by"]], (
            "a book held by the outage is in no group at all"
        )
        for book in db().list_books():
            assert main.book_state(book) != "partial", (
                "a held book reads 'the audiobook does not exist anywhere'"
            )
        clock.advance(breaker.COOLDOWN_BASE_SECONDS + 1)

    assert stuck == len(books)


def test_mark_held_leaves_a_row_already_held_by_that_service_alone():
    """One UPDATE per book per outage, not one per book per 15s sweep."""
    book_id = _seed("Held", status=models.BLOCKED, service="shelfmark",
                    held_by="shelfmark", detail="the hold from last sweep",
                    attempts=3)
    db().mark_held(book_id, "acquire_audiobook", "shelfmark", "a new hold")
    row = db().stage(book_id, "acquire_audiobook")
    assert row["detail"] == "the hold from last sweep"
    assert row["attempts"] == 3


def test_mark_held_reaches_a_blocked_row_held_by_nobody():
    """The orphan case the guard used to skip, stated at the row level."""
    book_id = _seed("Orphan", status=models.BLOCKED, service="shelfmark",
                    held_by="", detail="the sources are unavailable", attempts=7)
    db().mark_held(book_id, "acquire_audiobook", "shelfmark", "held: down")
    row = db().stage(book_id, "acquire_audiobook")
    assert row["status"] == models.BLOCKED
    assert row["held_by"] == "shelfmark"
    assert row["detail"] == "held: down"
    assert row["attempts"] == 0, "the breaker is the clock now"


def test_mark_held_writes_the_inbound_service():
    """Not `''`, and not whichever service held it before."""
    book_id = _seed("Rebadged", status=models.BLOCKED, service="kavita",
                    held_by="kavita", detail="old")
    db().mark_held(book_id, "acquire_audiobook", "shelfmark", "held: shelfmark")
    row = db().stage(book_id, "acquire_audiobook")
    assert row["held_by"] == "shelfmark"
    assert row["service"] == "shelfmark"


# =========================================================================
# fixed after the fact: the pins these three used to carry are gone
# =========================================================================
def test_a_failed_probe_keeps_the_upstreams_words(monkeypatch, clock):
    """The message the operator's panel shows is the upstream's, un-nested.

    `_advance`'s `refused_probe` block called `record_failure` a second time
    with the *held* result, and `last_failure` -- which the panel renders as
    the held group's `sample` -- came back as our own wrapper around the
    upstream's message, nested inside another copy of it on every book marked
    held after the probe in the same sweep. The stage had already recorded the
    upstream's own words; the second call must not replace them.
    """
    monkeypatch.setattr(acquire, "ShelfmarkClient", DownShelfmark)
    monkeypatch.setattr(acquire, "free_space_gb", lambda path: 999.0)
    monkeypatch.setattr(pipeline, "ADVANCE_WORKERS", 1)

    for i in range(6):
        _queued_book(f"Words {i}")
    _trip(clock)
    clock.advance(breaker.COOLDOWN_BASE_SECONDS + 1)

    helpers.scheduler().sweep()

    row = breaker.states()["shelfmark"]
    assert not row["last_failure"].startswith("held:"), row["last_failure"]
    sample = next(g for g in main.issues()["groups"]
                  if g["stage"] == "held")["sample"]
    assert "retries by itself when it recovers" not in sample, sample


def test_a_queued_download_keeps_its_release_through_a_hold():
    """Stated at the row, because a sweep cannot state it.

    A book whose download is in flight is `blocked` with `held_by=''` and a
    `queued from <release>` detail, so the widened D4 guard reaches it and used
    to write the outage sentence over the only line naming the release it is
    fetching. `queued_at` and `output_path` survive and it does resume, so the
    loss was the detail line only -- which is the line the operator reads to
    find out what the book is doing. The hold is still written: `book_state`
    reads `held_by` to tell a book waiting on a source from one whose audiobook
    "does not exist anywhere".
    """
    book_id = _seed("Queued", status=models.BLOCKED, service="shelfmark",
                    held_by="", queued_at=_now(),
                    detail="queued from MyAnonaMouse (a good release)")

    db().mark_held(book_id, "acquire_audiobook", "shelfmark", "held: down")

    row = db().stage(book_id, "acquire_audiobook")
    assert "MyAnonaMouse" in row["detail"], (
        "a download that is still in flight lost the release it is fetching: "
        f"{row['detail']!r}"
    )


def test_a_probe_that_never_reached_the_service_cannot_close_the_breaker(
    monkeypatch, clock
):
    """A probe refused by our own disk is not Shelfmark answering.

    `acquire._queue` measures free space before it searches -- deliberately, so
    a full disk does not waste an upstream query (`docs/PIPELINE.md`) -- so the
    result is `failed(kind='data')` and it proves nothing at all about the
    service. The verdict this used to be read by was "not a transient
    failure", which closed the breaker with zero searches and sent the next
    sweep at every book.

    The pin this replaces asserted `searches >= 1`, i.e. that the probe would
    ask. It cannot and should not: the disk is measured before the search on
    purpose, and what the verdict owes is the *absence of an answer*, which is
    a re-open on the doubled cooldown.
    """
    monkeypatch.setattr(acquire, "ShelfmarkClient", DownShelfmark)
    monkeypatch.setattr(acquire, "free_space_gb", lambda path: 0.5)
    monkeypatch.setattr(pipeline, "ADVANCE_WORKERS", 6)

    for i in range(6):
        _seed(f"Full {i}")
    trip = _trip(clock)
    clock.advance(breaker.COOLDOWN_BASE_SECONDS + 1)
    assert breaker.states()["shelfmark"]["state"] == "half_open"

    DownShelfmark.searches = 0
    helpers.scheduler().sweep()

    assert DownShelfmark.searches == 0, "the free-space check no longer precedes the search"
    row = breaker.states()["shelfmark"]
    assert row["state"] == "open", (
        "a probe that never reached the service closed the breaker: the next "
        "sweep runs every book against a service nobody asked"
    )
    assert row["trips"] == 2, "the no-evidence verdict was not read as a re-open"
    assert row["open_until"] > trip["open_until"], "the cooldown did not double"
    # One book was attempted and refused by the disk; the other five never had
    # a stage run at all.
    assert not [e for e in db().recent_events(200) if "answering again" in e["message"]]
