"""The breaker's state machine, on its own — no pipeline involved."""

from __future__ import annotations

import threading

from app import breaker


FAIL = "shelfmark: release search failed with HTTP 503: Unable to reach download source"


def _trip(clock) -> None:
    for _ in range(breaker.FAILURE_THRESHOLD):
        breaker.record_failure("shelfmark", FAIL)


def test_below_threshold_stays_closed(clock):
    breaker.record_failure("shelfmark", FAIL)
    breaker.record_failure("shelfmark", FAIL)
    row = breaker.states()["shelfmark"]
    assert row["state"] == "closed"
    assert row["failures"] == 2
    assert breaker.holding() == {}


def test_the_third_consecutive_failure_trips(clock):
    _trip(clock)
    row = breaker.states()["shelfmark"]
    assert row["state"] == "open"
    assert row["trips"] == 1
    assert row["failures"] == 0          # the counter's job is done; trips owns it now
    assert row["opened_at"] and row["open_until"]
    assert breaker.holding() == {"shelfmark": True}


def test_a_success_resets_the_counter(clock):
    breaker.record_failure("shelfmark", FAIL)
    breaker.record_failure("shelfmark", FAIL)
    breaker.record_success("shelfmark")
    assert breaker.states()["shelfmark"]["failures"] == 0
    breaker.record_failure("shelfmark", FAIL)
    assert breaker.states()["shelfmark"]["state"] == "closed"


def test_an_unclassified_or_auth_failure_never_trips(clock):
    # `record_failure` is only ever called for transient kinds by the pipeline,
    # but the guard has to exist here too: an empty or auth service name is not
    # a service outage.
    assert breaker.record_failure("", FAIL) is False
    assert breaker.states() == {}


def test_effective_state_flips_open_to_half_open_on_the_clock(clock):
    _trip(clock)
    assert breaker.states()["shelfmark"]["state"] == "open"
    clock.advance(breaker.COOLDOWN_BASE_SECONDS - 1)
    assert breaker.states()["shelfmark"]["state"] == "open"
    clock.advance(2)
    assert breaker.states()["shelfmark"]["state"] == "half_open"


def test_only_one_of_six_threads_claims_the_probe(clock):
    _trip(clock)
    clock.advance(breaker.COOLDOWN_BASE_SECONDS + 1)

    winners: list[int] = []
    lock = threading.Lock()
    start = threading.Barrier(6)

    def compete(book_id: int) -> None:
        start.wait()
        if breaker.claim_probe("shelfmark", book_id, "acquire_audiobook"):
            with lock:
                winners.append(book_id)

    threads = [threading.Thread(target=compete, args=(i,)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(winners) == 1, f"{len(winners)} books got the probe: {winners}"


def test_a_closed_breaker_admits_everyone(clock):
    # The stale-snapshot path: the pipeline's `holding` snapshot said the
    # service was held, but by the time a worker asked, a probe had closed it.
    assert breaker.claim_probe("shelfmark", 1, "acquire_ebook") is True
    assert breaker.claim_probe("shelfmark", 2, "acquire_ebook") is True


def test_a_transient_probe_failure_reopens_with_a_doubled_cooldown(clock):
    _trip(clock)
    first = breaker.states()["shelfmark"]
    clock.advance(breaker.COOLDOWN_BASE_SECONDS + 1)
    assert breaker.claim_probe("shelfmark", 7, "acquire_audiobook") is True
    breaker.resolve_probe("shelfmark", answered=False)

    row = breaker.states()["shelfmark"]
    assert row["state"] == "open"
    assert row["trips"] == 2
    assert row["open_until"] > first["open_until"]
    # `opened_at` is the outage's age and must not restart on a re-probe.
    assert row["opened_at"] == first["opened_at"]


def test_the_cooldown_walks_and_caps(clock):
    assert [breaker.cooldown(n) for n in (1, 2, 3, 4, 5, 6)] == [
        300, 600, 1200, 2400, 3600, 3600
    ]
    assert breaker.cooldown(99) == breaker.COOLDOWN_MAX_SECONDS


def test_a_stale_claim_is_released(clock):
    _trip(clock)
    clock.advance(breaker.COOLDOWN_BASE_SECONDS + 1)
    assert breaker.claim_probe("shelfmark", 7, "acquire_audiobook") is True
    # The claiming worker died and never answered.
    clock.advance(breaker.PROBE_STALE_SECONDS + 1)
    assert breaker.claim_probe("shelfmark", 8, "acquire_audiobook") is True


def test_a_non_transient_probe_verdict_closes_the_breaker(clock):
    _trip(clock)
    clock.advance(breaker.COOLDOWN_BASE_SECONDS + 1)
    assert breaker.claim_probe("shelfmark", 7, "acquire_audiobook") is True
    # A search that answers "no release exists" proves the service is
    # reachable: a fact about one book, not about service health.
    breaker.resolve_probe("shelfmark", answered=True)
    row = breaker.states()["shelfmark"]
    assert row["state"] == "closed"
    assert row["trips"] == 0


def test_a_retry_after_extends_the_cooldown(clock):
    breaker.record_failure("shelfmark", FAIL)
    breaker.record_failure("shelfmark", FAIL)
    breaker.record_failure("shelfmark", FAIL, retry_after=1800.0)
    row = breaker.states()["shelfmark"]
    from datetime import datetime

    wait = datetime.fromisoformat(row["open_until"]).timestamp() - clock.now
    # 1800s from the header, not the 300s the backoff alone would pick.
    assert 1799 <= wait <= 1801
