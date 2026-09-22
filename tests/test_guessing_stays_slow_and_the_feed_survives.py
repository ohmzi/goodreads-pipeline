"""Sign-in throttling, and the two things an anonymous caller could do to it.

The throttle is a delay, never a lockout, and the delay is held *inside* the
concurrency slot on purpose — that is what bounds how fast anyone can guess.
Three consequences are pinned here: the slot is always given back, a flooded
server waits rather than refusing instantly, and neither the username nor the
volume of attempts can reach the operator's activity feed.
"""

from __future__ import annotations

import asyncio
import time

from tests.conftest import OPERATOR, OPERATOR_PASSWORD


def _wrong(client, username: str = OPERATOR, password: str = "not-the-password"):
    return client.post("/api/auth/login", json={"username": username, "password": password})


def test_a_refused_sign_in_always_gives_its_slot_back(signed_in, fast_throttle):
    """The slot is released by hand now, so releasing it twice — or not at all —
    is the obvious way to get this wrong.

    A leak shows up as a 429 on the ninth attempt: all eight slots would be
    gone and the ninth would wait out `_LOGIN_SLOT_WAIT_SECONDS` and be refused.
    A double release shows up as `ValueError` from `Semaphore.release`.
    """
    for attempt in range(20):
        assert _wrong(signed_in).status_code == 401, f"attempt {attempt + 1}"


def test_guessing_does_not_get_faster_when_the_slots_are_free(signed_in, fast_throttle):
    """The delay holds its slot, which is the whole bound on guess rate.

    Move the sleep outside the slot and this passes while the server is suddenly
    willing to run scrypt as fast as the slot turns over — which is the version
    of "fix" this deliberately did not take.
    """
    import inspect

    from app import main

    body = inspect.getsource(main.auth_login)
    sleep_at = body.index("await asyncio.sleep(delay)")
    release_at = body.index("_login_slots.release()")
    assert sleep_at < release_at, "the throttle delay no longer holds its slot"


def test_a_flooded_server_waits_before_it_refuses():
    """The other half: a busy semaphore is not an instant 429.

    That instant refusal was reachable without a session and landed on the
    owner's own correct password. Waiting a second first is what turns "your
    sign-in is refused" into "your sign-in is slow" for the flood's duration.

    No account is needed: the 429 is decided before the database is read.
    """
    import httpx

    from app import main

    async def flood_then_sign_in():
        held = 0
        try:
            for _ in range(8):
                await main._login_slots.acquire()
                held += 1
            transport = httpx.ASGITransport(app=main.app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as client:
                started = time.monotonic()
                resp = await client.post(
                    "/api/auth/login",
                    json={"username": OPERATOR, "password": OPERATOR_PASSWORD},
                )
                return resp, time.monotonic() - started
        finally:
            for _ in range(held):
                main._login_slots.release()

    resp, elapsed = asyncio.run(flood_then_sign_in())

    assert resp.status_code == 429
    assert elapsed >= main._LOGIN_SLOT_WAIT_SECONDS * 0.9, (
        "refused without waiting for a slot — the instant refusal is back"
    )


def test_a_free_semaphore_never_makes_a_sign_in_wait(signed_in):
    """And the wait is not paid when there is a slot, or every login would."""
    started = time.monotonic()
    resp = signed_in.post(
        "/api/auth/login", json={"username": OPERATOR, "password": OPERATOR_PASSWORD}
    )
    elapsed = time.monotonic() - started

    assert resp.status_code == 200
    assert elapsed < 1.0, f"a sign-in with slots free took {elapsed:.2f}s"


def test_a_huge_username_cannot_write_a_huge_row(signed_in, fast_throttle):
    """SEC-06 and GAP-01 together.

    The username is interpolated into a `warning` event, and `/api/state`
    re-sends the newest 60 events to every open browser — so an unbounded one
    was a way to fill the feed from an endpoint that needs no session.
    """
    from app.db import db

    _wrong(signed_in, username="u" * 200_000)

    rows = db().recent_events(10, level="warning")
    assert rows, "the failed sign-in was not recorded at all"
    message = rows[0]["message"]
    assert "u" * 64 in message
    assert "u" * 65 not in message, "more than MAX_USERNAME_CHARS was kept"
    assert len(message) <= 500


def test_a_long_event_message_is_clipped_and_says_so():
    """`Database.log` is the only writer of `events`, so it carries the bound."""
    from app.db import _MAX_EVENT_CHARS, db

    db().log("x" * 5000)
    message = db().recent_events(1)[0]["message"]

    assert len(message) == _MAX_EVENT_CHARS
    # Visible, not silent: a clipped line that reads as a whole one hides the
    # message that mattered.
    assert message.endswith("…")


def test_an_event_message_loses_its_newlines():
    """No renderer has `white-space: pre-wrap`, so a newline bought nothing —
    the point of collapsing is that the stored row and the rendered row agree."""
    from app.db import db

    db().log("first\nsecond\n\n  third")
    assert db().recent_events(1)[0]["message"] == "first second third"


def test_a_flood_of_failed_sign_ins_does_not_evict_the_feed(signed_in, fast_throttle):
    """The eviction, closed at the call site rather than by shortening the feed.

    Every attempt is still counted — the count is what the throttle runs on —
    but only the first and then every tenth reaches the log, so a caller in a
    loop cannot push the operator's own lines out of the newest 60.
    """
    from app.db import db

    db().log("the operator's own line")
    for _ in range(30):
        _wrong(signed_in)

    messages = [row["message"] for row in db().recent_events(200)]
    assert any("the operator's own line" in m for m in messages), "the feed was evicted"
    logged = [m for m in messages if "failed login" in m]
    assert len(logged) == 4, f"expected the 1st, 10th, 20th and 30th: {logged}"
    assert "30 in the last 15 min" in logged[0]
