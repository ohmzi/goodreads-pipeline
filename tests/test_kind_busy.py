"""A rate limit is not a refusal: 429 is `busy`, not unclassified.

Before this, a 429 fell through `ClientError.kind` to `""` — unclassified — so
it was neither forgiven by `_forgive_transient` (which gates on the kind) nor
grouped with its service, and every affected book was written `failed` on its
first attempt. That is the operator's group 2.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx

from app import breaker, main, models, pipeline
from app.clients.base import ClientError, _retry_after
from app.db import db

import helpers


def test_408_425_429_are_busy_and_everything_else_is_not():
    assert ClientError("s", "x", status=429).kind == "busy"
    assert ClientError("s", "x", status=425).kind == "busy"
    assert ClientError("s", "x", status=408).kind == "busy"
    assert ClientError("s", "x", status=500).kind == "server"
    assert ClientError("s", "x", status=401).kind == "auth"
    assert ClientError("s", "x", status=404).kind == "notfound"
    assert ClientError("s", "x").kind == "network"


def test_busy_is_transient_and_actionable():
    """Transient, so one 429 does not park a book; actionable, so a *persistent*
    rate limit is still something the operator is told about."""
    assert "busy" in breaker.TRANSIENT_KINDS
    assert "busy" in main.ACTIONABLE_KINDS
    bucket, headline = main._cause_bucket("acquire_ebook", "busy", "429 too many")
    assert bucket == "busy"
    assert "rate-limiting" in headline
    assert main._actionable("busy", bucket) is True
    fix = main._fix_for("acquire_ebook", "busy", "429 too many", "shelfmark")
    assert "rate-limiting" in fix and "Shelfmark" in fix


def test_retry_after_is_read_from_either_form():
    assert _retry_after(httpx.Response(429, headers={"Retry-After": "90"})) == 90.0
    assert _retry_after(httpx.Response(429)) is None
    assert _retry_after(httpx.Response(429, headers={"Retry-After": "nonsense"})) is None

    when = datetime.now(timezone.utc) + timedelta(seconds=120)
    header = when.strftime("%a, %d %b %Y %H:%M:%S GMT")
    parsed = _retry_after(httpx.Response(429, headers={"Retry-After": header}))
    assert 100 <= parsed <= 121


def test_a_retry_after_lands_on_the_breaker_clock(clock):
    for _ in range(breaker.FAILURE_THRESHOLD - 1):
        breaker.record_failure("shelfmark", "429 too many requests")
    breaker.record_failure("shelfmark", "429 too many requests", retry_after=1200.0)

    row = breaker.states()["shelfmark"]
    wait = datetime.fromisoformat(row["open_until"]).timestamp() - clock.now
    # The header's 20 minutes, not the 5 the backoff alone would have chosen.
    assert 1199 <= wait <= 1201


def test_one_429_slows_the_book_down_instead_of_failing_it(monkeypatch, clock):
    """One book, one rate limit, no outage: forgiven on the grace clock."""
    helpers.ebook_pending_book("Rate Limited")

    def busy():
        return ClientError("shelfmark", "release search failed with HTTP 429: "
                                       "too many requests", status=429)

    monkeypatch.setattr(
        pipeline, "REGISTRY", helpers.stub_registry([], {"on": True}, busy)
    )
    summary = helpers.scheduler().sweep()

    row = db().stage(1, "acquire_ebook")
    assert row["status"] == models.BLOCKED
    assert row["transient_count"] == 1
    assert summary["parked"] == 0
    assert db().query_one(
        "SELECT COUNT(*) AS n FROM stage_runs WHERE status = 'failed'"
    )["n"] == 0
    # Below the threshold, so the service itself is not held.
    assert breaker.holding() == {}
