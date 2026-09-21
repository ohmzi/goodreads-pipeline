"""Who tells the breaker about a failed probe, and how that was being guessed.

`_advance` records the failure of a refused probe as "the belt to the stage's
braces", for holds a stage did not take on the breaker's behalf — but it
decided whether the stage had already recorded by looking at `kind` being
empty, on the grounds that `breaker.hold()` clears `kind`. That is an inference
about the wrapper rather than about the fact, and it is wrong in the direction
that loses evidence: a stage that holds *without* recording, which `hold()`
makes just as empty-`kind`ed as one that recorded, was read as having recorded
and its failure never reached the breaker at all.

No shipped stage does that today, so nothing was broken by it — the acquire
stage records and then holds, in that order, on both of its hold paths. It is
latent, and what makes it worth fixing rather than documenting is that the
failure would be invisible: `resolve_probe` re-opens either way, so the state
machine stays right while `last_failure` — the sentence the operator's one row
prints — quietly goes stale.

The holder now says so (`StageResult.recorded`), the stage sets it where it
records, and `_advance` records when it is not set. Both halves are asserted
here, because a belt that never fires is not a belt.
"""

from __future__ import annotations

from app import breaker, models, pipeline
from app.clients.base import ClientError
from app.stages import acquire

import helpers

FAILURE = (
    "shelfmark: release search failed with HTTP 503: Unable to reach download "
    "source"
)


class DownShelfmark:
    name = "shelfmark"

    def __init__(self, base_url: str | None = None) -> None:
        self.last_rejections: list[str] = []

    def search(self, title: str, author: str = "", content_type: str = "ebook"):
        raise ClientError("shelfmark", FAILURE.split(": ", 1)[1], status=503)

    def find_task(self, title: str, author: str = "", source_ids=()):
        raise ClientError("shelfmark", FAILURE.split(": ", 1)[1], status=503)

    def close(self) -> None:
        pass


def _trip_into_half_open(clock) -> None:
    for _ in range(breaker.FAILURE_THRESHOLD):
        breaker.record_failure("shelfmark", FAILURE)
    clock.advance(breaker.COOLDOWN_BASE_SECONDS + 1)
    assert breaker.states()["shelfmark"]["state"] == "half_open"


def test_a_hold_that_does_not_say_it_recorded_is_recorded_by_the_pipeline(
    monkeypatch, clock
):
    """The latent hole, made real: a stage that holds and says nothing.

    The hold is on the probed service, so the probe is refused and re-opens the
    breaker either way — but the failure behind it has to reach the breaker, or
    the operator's row keeps whatever sentence was there before this outage.
    """
    calls: list[str] = []
    real = breaker.record_failure

    def spy(service, detail, retry_after=None):
        calls.append(str(detail))
        return real(service, detail, retry_after=retry_after)

    monkeypatch.setattr(breaker, "record_failure", spy)

    def runner(book: dict) -> models.StageResult:
        # What a stage that holds without recording looks like: a hold on the
        # probed service, an empty `kind` (hold() clears it, and `held()`
        # leaves it empty), and no `recorded`.
        return models.StageResult.held("held: shelfmark would not serve it",
                                       "shelfmark")

    monkeypatch.setattr(pipeline, "REGISTRY",
                        {stage: runner for stage in models.STAGES})
    monkeypatch.setattr(pipeline, "ADVANCE_WORKERS", 1)
    helpers.audiobook_pending_book("Held Without Recording")

    _trip_into_half_open(clock)
    calls.clear()

    helpers.scheduler().sweep()

    assert calls == ["held: shelfmark would not serve it"], (
        f"a hold that never recorded was left unrecorded: {calls}"
    )
    row = breaker.states()["shelfmark"]
    assert row["state"] == "open", "the refused probe did not re-open the breaker"
    assert row["trips"] == 2
    assert row["last_failure"] == "held: shelfmark would not serve it"


def test_a_hold_that_records_says_so_and_is_not_sent_twice(monkeypatch, clock):
    """The measured shape: `acquire._run` records, then hands back the hold.

    One call with the upstream's own words — the same invariant F2 protects
    through the pipeline, asserted here at the level of the field that carries
    it, so a stage that stops setting it fails here rather than in the operator
    panel.
    """
    calls: list[str] = []
    real = breaker.record_failure

    def spy(service, detail, retry_after=None):
        calls.append(str(detail))
        return real(service, detail, retry_after=retry_after)

    monkeypatch.setattr(acquire, "ShelfmarkClient", DownShelfmark)
    monkeypatch.setattr(acquire, "free_space_gb", lambda path: 999.0)
    monkeypatch.setattr(breaker, "record_failure", spy)
    book_id = helpers.audiobook_pending_book("Records First")

    _trip_into_half_open(clock)

    # What the stage hands back while the breaker is half-open: a hold, with
    # the upstream's words already recorded.
    result = acquire.run_audiobook(helpers.load_book(book_id))
    assert result.status == models.BLOCKED
    assert result.held_by == "shelfmark"
    assert result.recorded is True, (
        "the stage recorded the failure and did not say so; the pipeline would "
        "send it again and bury the upstream's words"
    )
    assert calls and all(c == FAILURE for c in calls), calls

    # And the default is the other way, because a hold built without saying so
    # is the case the pipeline has to act on.
    assert models.StageResult.held("held: x", "shelfmark").recorded is False
    assert breaker.hold(models.StageResult.failed("x", kind="network",
                                                  service="shelfmark"),
                        "shelfmark").recorded is False
    assert breaker.hold(models.StageResult.failed("x", kind="network",
                                                  service="shelfmark"),
                        "shelfmark", recorded=True).recorded is True
