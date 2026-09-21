"""What a service-level failure has to be for it to count.

`FAILURE_THRESHOLD` reads "three *consecutive* failures", and both halves of
that word were missing in practice:

* nothing ever reset the counter. `pipeline._advance` feeds
  `breaker.record_success` from an `ok` result that names a service, and no
  stage ever set one — `StageResult.ok()` did not even take the parameter — so
  the call was dead code. Instrumented across 32 successful stage runs: zero
  calls. The signature is the service name alone, so once two unrelated
  failures were banked, a third from any cause at any time tripped the breaker
  and held every book for that service;
* nothing bounded the streak in time. Even with the reset, a service whose
  never-succeeding stage keeps failing leaves a counter with no floor under it.

Both are asserted here, at the level each one lives at: the reset through the
real acquire stage, the window through the counter itself.
"""

from __future__ import annotations

import json

from app import breaker, models, pipeline
from app.db import db
from app.stages import acquire

import helpers

FAILURE = "shelfmark: release search failed with HTTP 503: Unable to reach download source"


class CompletedShelfmark:
    """Shelfmark answering normally: the task we queued has finished."""

    name = "shelfmark"

    def __init__(self, base_url: str | None = None) -> None:
        self.last_rejections: list[str] = []

    def find_task(self, title: str, author: str = "", source_ids=()):
        return ("task-1", "complete", {})

    def close(self) -> None:
        pass


#: A book whose audiobook is queued with Shelfmark, and whose other formats are
#: already placed — so the completed task is the very next thing to happen.
_QUEUED = {
    "classify": models.OK,
    "acquire_ebook": models.OK,
    "acquire_audiobook": {"status": models.BLOCKED, "queued_at": helpers.iso()},
    "place": {"status": models.OK,
              "output_path": json.dumps({"ebook": "/books/a.epub",
                                         "audiobook": "/audiobooks/a.mp3"})},
    "index": models.OK,
    "notebook": models.OK,
    "verify": models.OK,
    "shelve": models.OK,
}


def test_the_failures_counter_has_a_time_window(clock):
    """Three failures spread beyond the window are not three consecutive ones.

    This is the verifier's measurement, reduced to the counter: gaps of more
    than `FAILURE_WINDOW_SECONDS` with no other evidence in between used to walk
    1 -> 2 -> trip. They must not: a failure a day old says nothing about
    whether the service is usable now.
    """
    breaker.record_failure("shelfmark", FAILURE)
    assert breaker.states()["shelfmark"]["failures"] == 1

    for _ in range(2):
        clock.advance(breaker.FAILURE_WINDOW_SECONDS + 60)
        breaker.record_failure("shelfmark", FAILURE)

    row = breaker.states()["shelfmark"]
    assert row["state"] == "closed", "old failures tripped the breaker"
    assert row["failures"] == 1, "the streak did not restart"
    assert breaker.holding() == {}


def test_failures_inside_the_window_are_still_consecutive(clock):
    """The window must not turn the breaker off — only age its evidence out."""
    for i in range(breaker.FAILURE_THRESHOLD):
        clock.advance(breaker.FAILURE_WINDOW_SECONDS // (breaker.FAILURE_THRESHOLD + 1))
        breaker.record_failure("shelfmark", FAILURE)

    row = breaker.states()["shelfmark"]
    assert row["state"] == "open"
    assert row["trips"] == 1


def test_an_aged_out_streak_does_not_climb_the_escalation(clock):
    """A trip restarts the streak, and an aged-out streak cannot resume it."""
    for _ in range(breaker.FAILURE_THRESHOLD):
        breaker.record_failure("shelfmark", FAILURE)
    assert breaker.states()["shelfmark"]["trips"] == 1
    breaker.record_success("shelfmark")

    clock.advance(breaker.FAILURE_WINDOW_SECONDS + 60)
    breaker.record_failure("shelfmark", FAILURE)
    breaker.record_failure("shelfmark", FAILURE)
    assert breaker.states()["shelfmark"]["state"] == "closed"
    breaker.record_failure("shelfmark", FAILURE)
    row = breaker.states()["shelfmark"]
    assert row["state"] == "open"
    assert row["trips"] == 1, "a fresh outage inherited the old one's trip count"
    assert row["failures"] == 0


def test_a_successful_run_clears_a_part_built_streak(monkeypatch, clock):
    """The reset, end to end: a real stage success is a real `record_success`.

    Two failures are banked for Shelfmark — one short of a trip — and then the
    service answers. Before the fix this could not happen: `ok()` carried no
    service, `_advance`'s guarded call never fired, and the two failures stayed
    banked until some third one, days later, held every book in the pipeline.
    """
    monkeypatch.setattr(acquire, "ShelfmarkClient", CompletedShelfmark)
    book_id = helpers.seed_book("Answered", dict(_QUEUED))

    breaker.record_failure("shelfmark", FAILURE)
    breaker.record_failure("shelfmark", FAILURE)
    assert breaker.states()["shelfmark"]["failures"] == 2

    summary = helpers.scheduler().sweep()

    assert summary["advanced"] == 1
    row = breaker.states()["shelfmark"]
    assert row["failures"] == 0, "a success left the failure counter standing"
    assert row["trips"] == 0
    assert row["state"] == "closed"
    assert row["last_ok_at"], "the success was not recorded at all"

    # The `ok` that did it names the service it used: that is the whole
    # mechanism, and the row keeps it because `mark_done` writes it through.
    assert db().stage(book_id, "acquire_audiobook")["status"] == models.OK
    assert db().stage(book_id, "acquire_audiobook")["service"] == "shelfmark"


def test_a_stage_that_owns_a_service_names_it_on_success(monkeypatch, clock):
    """Where the name comes from. No `ok` names a service -> no reset, ever."""
    monkeypatch.setattr(acquire, "ShelfmarkClient", CompletedShelfmark)
    book_id = helpers.seed_book("Answered Directly", dict(_QUEUED))

    result = acquire.run_audiobook(helpers.load_book(book_id))

    assert result.status == models.OK
    assert result.service == "shelfmark", (
        "the stage's success does not say which service it used, so the "
        "breaker's failure counter has no reset on healthy traffic"
    )


def test_ok_carries_a_service_when_it_has_one(clock):
    """The parameter's contract: empty stays a real answer for fan-out stages."""
    assert models.StageResult.ok("done", service="kavita").service == "kavita"
    assert models.StageResult.ok("done").service == ""
    # And the pipeline only feeds *ok* results to the breaker, never a
    # `blocked` one that happens to name a service: `_forgive_transient` keeps
    # the service on the blocked result deliberately, and resetting on that
    # would mean the counter could never reach its threshold at all.
    assert models.StageResult.blocked("held", service="shelfmark").service == "shelfmark"
