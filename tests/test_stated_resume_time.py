"""When an upstream says when it will be back, say so.

The held-service row answers "what should I do?" with "nothing, these resume by
themselves" — which is true, and leaves out the only thing the operator
actually wants at that point, which is *when*. The answer was already in the
failure text and was being thrown away: Shelfmark relays Prowlarr's message
verbatim, and it names the hour the block lifts.

Read for display only. `wait_seconds` deliberately caps how long a breaker will
hold on an upstream's say-so, because an upstream-controlled duration is not a
promise — this is the same untrusted number arriving by an even less
trustworthy route, so it changes what the operator is told and not what the
pipeline does.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app import breaker, main


def _in(hours: float) -> str:
    when = datetime.now(timezone.utc) + timedelta(hours=hours)
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


#: The real message, from the operator's breaker row.
PROWLARR = (
    'shelfmark: release search failed with HTTP 503: {"error":"every indexer '
    'is disabled by Prowlarr after recent failures (until %s)"}'
)


def test_the_stated_hour_is_read_out_of_the_upstream_message():
    when = breaker.stated_resume_at(PROWLARR % "2026-09-22T12:17:04Z")

    assert when == datetime(2026, 9, 22, 12, 17, 4, tzinfo=timezone.utc)


def test_the_note_says_how_long_is_left():
    note = breaker.resume_note(PROWLARR % _in(11.6))

    assert "12h" in note


def test_a_block_lifting_within_the_hour_is_given_in_minutes():
    note = breaker.resume_note(PROWLARR % _in(0.5))

    assert "min" in note
    assert "0h" not in note


def test_a_time_that_has_passed_says_nothing():
    """Repeating it would read as though the outage were expected to continue."""
    assert breaker.resume_note(PROWLARR % "2020-01-01T00:00:00Z") == ""


def test_a_message_with_no_time_in_it_is_unchanged():
    assert breaker.resume_note("shelfmark: could not reach download source") == ""
    assert breaker.resume_note("") == ""
    assert breaker.resume_note(None) == ""


def test_a_malformed_time_is_not_a_crash():
    assert breaker.resume_note("disabled (until 2026-13-45T99:99:99Z)") == ""


def test_the_headline_and_the_fix_both_carry_it():
    detail = PROWLARR % _in(11.6)

    headline = breaker.headline("shelfmark", 211, 0.5, detail)
    fix = main._fix_for_breaker("shelfmark", "open", 0.5, detail)

    assert "holding 211 books, none failed" in headline
    assert "12h" in headline
    assert fix.startswith("Nothing to fix")
    assert "Shelfmark says the block lifts in 12h" in fix


def test_without_a_stated_time_the_old_line_is_unchanged():
    fix = main._fix_for_breaker("shelfmark", "open", 0.5, "plain 503")

    assert fix.startswith("Nothing to fix: these resume by themselves")
    assert "Shelfmark is up — the fault is upstream." in fix


def test_the_hold_itself_is_not_extended_by_what_an_upstream_claims():
    """The cap in `wait_seconds` is a safety property, not an oversight."""
    assert breaker.wait_seconds(1, 86_400) == breaker.COOLDOWN_MAX_SECONDS
