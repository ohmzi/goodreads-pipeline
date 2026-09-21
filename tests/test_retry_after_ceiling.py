"""`Retry-After` is honoured, but the hold it can buy is bounded.

The module's own table says the ceiling is "cooldown, capped at 1h", and the
cap was applied to the backoff half only:

    wait = max(cooldown(trips), float(retry_after or 0.0))

so `max()` let the header win outright. That is not a cooldown at all when the
header is wrong or hostile — `86400` bought a day with no probe, and a
mis-serialised `1e9` put `open_until` in the year 2058. An open breaker runs no
stages, so nothing can report the success that would close it: the hold becomes
a wedge, and every book behind that service waits on it forever.

The information is not thrown away, though: a rate limiter's window is real
evidence about when to look again, so what the upstream asked for is logged
even when it is not obeyed.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from app import breaker
from app.db import db

FAILURE = "shelfmark: rate limited"


def _hold_seconds(clock, row: dict) -> float:
    return datetime.fromisoformat(row["open_until"]).timestamp() - clock.now


@pytest.mark.parametrize("asked", [86400.0, 1e9])
def test_a_trip_never_opens_for_longer_than_the_ceiling(clock, asked):
    for _ in range(breaker.FAILURE_THRESHOLD - 1):
        breaker.record_failure("shelfmark", FAILURE)
    breaker.record_failure("shelfmark", FAILURE, retry_after=asked)

    row = breaker.states()["shelfmark"]
    wait = _hold_seconds(clock, row)
    assert wait <= breaker.COOLDOWN_MAX_SECONDS, (
        f"a {asked}s Retry-After held the breaker for {wait / 3600:.1f}h"
    )
    # Exactly the cap, up to the whole-second resolution of our own stamps.
    assert abs(wait - breaker.COOLDOWN_MAX_SECONDS) <= 1


def test_a_re_probe_never_reopens_for_longer_than_the_ceiling(clock):
    """The other half of the defect: `_reopen` computed the same `max()`."""
    for _ in range(breaker.FAILURE_THRESHOLD):
        breaker.record_failure("shelfmark", FAILURE)
    clock.advance(breaker.COOLDOWN_BASE_SECONDS + 1)
    assert breaker.claim_probe("shelfmark", 7, "acquire_audiobook") is True

    breaker.record_failure("shelfmark", FAILURE, retry_after=1e9)

    row = breaker.states()["shelfmark"]
    assert row["state"] == "open"
    assert row["trips"] == 2
    assert _hold_seconds(clock, row) <= breaker.COOLDOWN_MAX_SECONDS


def test_an_absurd_header_still_releases_the_books(clock):
    """The consequence of the wedge, asserted directly: a probe must happen.

    With `open_until` in the year 2058, `effective_state` never flips to
    half-open and `claim_probe` refuses every book — and since no stage runs
    while the breaker is open, no success can be recorded either, so the only
    exit from the state is the one call the pipeline cannot make.
    """
    for _ in range(breaker.FAILURE_THRESHOLD):
        breaker.record_failure("shelfmark", FAILURE, retry_after=1e9)

    clock.advance(breaker.COOLDOWN_MAX_SECONDS + 1)

    assert breaker.states()["shelfmark"]["state"] == "half_open"
    assert breaker.claim_probe("shelfmark", 1, "acquire_audiobook") is True, (
        "no book may run against the service, so the breaker can never be "
        "discovered to have recovered"
    )
    breaker.resolve_probe("shelfmark", answered=True)
    assert breaker.states()["shelfmark"]["state"] == "closed"


def test_a_plausible_header_is_still_honoured(clock):
    """The clamp is a ceiling, not a floor: under it, the upstream wins."""
    for _ in range(breaker.FAILURE_THRESHOLD - 1):
        breaker.record_failure("shelfmark", FAILURE)
    breaker.record_failure("shelfmark", FAILURE, retry_after=1800.0)

    wait = _hold_seconds(clock, breaker.states()["shelfmark"])
    assert 1799 <= wait <= 1801, "the rate limiter's own window was discarded"


def test_the_clamped_request_is_not_lost(clock):
    """What was asked for survives in the log, even though we capped it."""
    for _ in range(breaker.FAILURE_THRESHOLD - 1):
        breaker.record_failure("shelfmark", FAILURE)
    breaker.record_failure("shelfmark", FAILURE, retry_after=86400.0)

    trips = [e for e in db().recent_events(50)
             if e["stage"] == "breaker" and e["level"] == "error"]
    assert len(trips) == 1
    assert "24.0h" in trips[0]["message"], (
        f"the operator cannot see that the service asked for a day: {trips[0]['message']!r}"
    )
    # And the line still says what we actually did.
    assert "retrying in 60 min" in trips[0]["message"]


def test_the_ceiling_is_the_documented_one(clock):
    """`wait_seconds` is the single place both paths go through."""
    assert breaker.wait_seconds(1, None) == breaker.COOLDOWN_BASE_SECONDS
    assert breaker.wait_seconds(99, None) == breaker.COOLDOWN_MAX_SECONDS
    assert breaker.wait_seconds(1, 600.0) == 600.0
    assert breaker.wait_seconds(1, 1e12) == breaker.COOLDOWN_MAX_SECONDS
    assert breaker.wait_seconds(1, -5.0) == breaker.COOLDOWN_BASE_SECONDS
