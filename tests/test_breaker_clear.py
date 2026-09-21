"""The operator's way out: clearing a held service by hand.

Every other exit from a hold is the service proving it is answering — a probe
closes it, a success closes it, the cooldown only decides when to *ask* again —
and that is the right default right up until nothing can prove it. A breaker
whose probes keep coming back with no evidence holds a working service, and the
failure is silent: nothing fails, nothing logs an error, the library simply
stops acquiring. Measured, before F1's false negative was fixed: 16.25 hours,
20 cooldowns, 25 successful dials, 5 books held, and no way out — not from the
UI, and not from a restart, because the state is a row in SQLite rather than
something in a process's memory.

So there is a hatch, and what is tested here is that it is honest about what it
did rather than merely that it works:

* it releases the books and clears the outage's clock exactly as a recovery
  does, so a book held through the outage does not park on a clock nobody
  spent;
* it does **not** claim the service answered — no `answering again` line, and
  no fresh `last_ok_at`, because nothing was seen working;
* it cannot happen by accident: the route takes POST only, a session is
  required, the UI confirms before calling, and clearing something that is not
  held is a no-op that says so rather than an error or a second release. A GET
  never reaches the handler at all — it is answered by the SPA fallback's 404,
  which is what a link or a prefetch would get.
"""

from __future__ import annotations

import pytest

from app import auth, breaker, models
from app.db import db
from app.stages import acquire

import helpers

FAILURE = (
    "shelfmark: release search failed with HTTP 503: Unable to reach download "
    "source"
)


class BackUp:
    """Shelfmark answering again, so a released book has somewhere to go."""

    name = "shelfmark"
    dials = 0

    def __init__(self, base_url: str | None = None) -> None:
        self.last_rejections: list[str] = []

    def search(self, title: str, author: str = "", content_type: str = "ebook"):
        BackUp.dials += 1
        return []

    def close(self) -> None:
        pass


def _trip_and_hold(clock, books: int = 4) -> list[int]:
    """An open breaker with real books held behind it. Returns their ids."""
    ids = [helpers.audiobook_pending_book(f"Held {i}") for i in range(books)]
    for _ in range(breaker.FAILURE_THRESHOLD):
        breaker.record_failure("shelfmark", FAILURE)
    assert breaker.states()["shelfmark"]["state"] == "open"
    # The hold itself: no stage may run against a held service, so the sweep
    # writes `mark_held` rows and reports them held.
    helpers.scheduler().sweep()
    held = db().query(
        "SELECT COUNT(*) AS n FROM stage_runs WHERE held_by = 'shelfmark'"
    )[0]["n"]
    assert held >= 1, "nothing was held, so there is nothing for the hatch to free"
    return ids


def test_clearing_by_hand_releases_the_books_and_closes_the_breaker(clock):
    """The hatch does what a recovery does, minus the claim of recovery."""
    ids = _trip_and_hold(clock)

    result = breaker.clear("shelfmark", reason="test")

    assert result["service"] == "shelfmark"
    assert result["was"] == "open"
    assert result["cleared"] is True
    assert result["released"] >= 1
    assert "cleared by hand" in result["message"]
    assert "not been proven up" in result["message"], (
        "the response claims more than it knows"
    )

    row = breaker.states()["shelfmark"]
    assert row["state"] == "closed"
    assert row["trips"] == 0, "the next trip would escalate on a dead outage's count"
    assert row["opened_at"] is None
    assert row["open_until"] is None
    assert not row["last_ok_at"], (
        "a hand-clear stamped `last_ok_at`, which reads as 'we saw it answer'"
    )
    assert breaker.holding() == {}

    for book_id in ids:
        stage_row = db().stage(book_id, "acquire_audiobook")
        assert stage_row["transient_since"] is None, (
            "a per-book clock survived the release and will park the book"
        )
        # `held_by` is deliberately left where it is, exactly as a recovery
        # leaves it: the hold is not a field, it is the breaker refusing to run
        # the stage, and `mark_done` clears the field when the book next runs.
        # Writing it here would be a third writer racing the sweep's own two.
        assert stage_row["held_by"] == "shelfmark"


def test_it_says_it_was_cleared_by_hand_and_never_that_the_service_answered(clock):
    """The log is the record, and the difference is the whole point.

    An operator reading the activity log a week later must be able to tell a
    decision they made from a service that came back on its own — otherwise the
    one line that explains why a week's books ran against a dead service looks
    exactly like a recovery.
    """
    _trip_and_hold(clock)
    before = len(db().recent_events(200))

    breaker.clear("shelfmark", reason="freed disk on the indexer")

    events = db().recent_events(200)
    assert len(events) == before + 1, "the clear was not recorded exactly once"
    line = events[0]
    assert line["level"] == "warning"
    assert line["stage"] == "breaker"
    assert line["service"] == "shelfmark"
    assert "cleared by hand" in line["message"]
    assert "freed disk on the indexer" in line["message"], "the reason was dropped"
    assert not [e for e in events if "answering again" in e["message"]], (
        "a hand-clear was logged as the service answering"
    )


def test_a_cleared_breaker_lets_the_books_run_again(monkeypatch, clock):
    """Released is not the same as resumed; this is the half that matters.

    The hold only exists while the breaker refuses to run the stage, so once it
    is cleared the next sweep picks the books up with no further action — which
    is what makes the hatch an escape rather than another kind of stuck.
    """
    monkeypatch.setattr(acquire, "ShelfmarkClient", BackUp)
    monkeypatch.setattr(acquire, "free_space_gb", lambda path: 999.0)

    ids = _trip_and_hold(clock)
    breaker.clear("shelfmark", reason="test")

    BackUp.dials = 0
    helpers.scheduler().sweep()

    assert BackUp.dials == len(ids), (
        f"{BackUp.dials} of {len(ids)} released books were tried"
    )
    for book_id in ids:
        stage_row = db().stage(book_id, "acquire_audiobook")
        assert stage_row["held_by"] == ""
        assert stage_row["status"] == models.FAILED  # its own "nothing found"
        assert "no audiobook releases found" in stage_row["detail"]


def test_clearing_something_that_is_not_held_changes_nothing(clock):
    """A no-op that says so, because the honest answer is 'there was nothing'.

    This is also the double-click path and the stale-page path: the button is
    on a page that polls every six seconds, so the second press must not
    release a second time, invent an incident, or fail with an error the
    operator then has to interpret.
    """
    _trip_and_hold(clock)

    first = breaker.clear("shelfmark", reason="test")
    assert first["cleared"] is True
    events = len(db().recent_events(200))

    second = breaker.clear("shelfmark", reason="test")
    assert second["cleared"] is False
    assert second["released"] == 0
    assert second["was"] == "closed"
    assert "not holding anything" in second["message"]
    assert len(db().recent_events(200)) == events, (
        "clearing a breaker that is not holding anything wrote a log line"
    )

    empty = breaker.clear("")
    assert empty["cleared"] is False and empty["released"] == 0


def test_clearing_half_open_also_releases_the_probe_claim(clock):
    """Half-open is holding, so it is clearable, and the claim comes with it.

    A breaker sitting half-open with a probe nothing ever answers is the same
    wedge with an extra field: `holding()` reports it, so no stage runs, and the
    claim would otherwise sit there until `PROBE_STALE_SECONDS` released it.
    """
    for _ in range(breaker.FAILURE_THRESHOLD):
        breaker.record_failure("shelfmark", FAILURE)
    clock.advance(breaker.COOLDOWN_BASE_SECONDS + 1)
    assert breaker.states()["shelfmark"]["state"] == "half_open"
    assert breaker.claim_probe("shelfmark", 7, "acquire_audiobook") is True

    result = breaker.clear("shelfmark", reason="test")

    assert result["cleared"] is True
    row = breaker.states()["shelfmark"]
    assert row["state"] == "closed"
    assert not row.get("probe_book_id"), "the probe claim outlived the clear"
    assert not row.get("probe_at")
    # And the book that was probing is free to run: a closed breaker admits
    # everyone, including the claim's own holder.
    assert breaker.claim_probe("shelfmark", 8, "acquire_audiobook") is True


# =========================================================================
# the reach: the endpoint the panel calls
# =========================================================================
def _route(path: str):
    from app import main

    return next(r for r in main.app.routes if getattr(r, "path", "") == path)


def test_the_endpoint_is_post_only(clock):
    """The route's own method set, which is what keeps a link from clearing.

    A GET, a prefetch or a crawler cannot reach the handler at all: the only
    way in is a request that says it is a state change, and (below) a session.
    """
    from app import main

    route = _route("/api/services/{name}/breaker/clear")
    assert route.methods == {"POST"}, route.methods
    assert "/api/services/{name}/breaker/clear" not in main.PUBLIC_PATHS, (
        "the clear endpoint is reachable without a session"
    )


@pytest.fixture
def client():
    """A signed-in test client, without the app's startup hooks.

    Deliberately not used as a context manager: entering one would run the
    startup event, which starts the real sweep thread and the VNC browser.
    """
    from fastapi.testclient import TestClient

    from app import main

    db().set_user("operator", auth.hash_password("a-test-password"),
                  auth.new_epoch())
    c = TestClient(main.app)
    resp = c.post("/api/auth/login",
                  json={"username": "operator", "password": "a-test-password"})
    assert resp.status_code == 200, resp.text
    return c


def test_the_endpoint_clears_and_reports_what_it_did(client, clock):
    _trip_and_hold(clock)

    resp = client.post("/api/services/shelfmark/breaker/clear")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["cleared"] is True
    assert body["released"] >= 1
    assert body["was"] == "open"
    assert "cleared by hand" in body["message"]
    assert breaker.states()["shelfmark"]["state"] == "closed"
    # The panel's own wording, so a bug in the message is a failing test rather
    # than something only an operator would notice.
    assert client.post("/api/services/shelfmark/breaker/clear").json()["cleared"] is False


def test_the_endpoint_refuses_a_get_and_an_unknown_service(client, clock):
    _trip_and_hold(clock)

    # 404 rather than 405, and that is the SPA fallback's doing: it is
    # registered last and answers every unmatched GET under `/api/` with 404 on
    # purpose. The `methods == {"POST"}` assertion above is what pins the route
    # itself; this pins that whatever a GET gets, it is not the clear.
    assert client.get("/api/services/shelfmark/breaker/clear").status_code == 404
    assert client.post("/api/services/nonsense/breaker/clear").status_code == 404
    # Neither of those touches the hold.
    assert breaker.states()["shelfmark"]["state"] == "open"
    assert db().query(
        "SELECT COUNT(*) AS n FROM stage_runs WHERE held_by = 'shelfmark'"
    )[0]["n"] >= 1


def test_the_panel_really_offers_it_and_asks_first():
    """The UI half, checked against itself.

    There is no browser in this suite, and an `onclick` naming a function that
    does not exist fails silently in the one place nobody is looking — so the
    handler is checked to exist exactly once, to be wired to both rows that
    show a hold (the dashboard's, and the service page's, which is the only one
    left when a held service has nothing visible to list), and to be gated on a
    confirmation rather than firing on a stray click.
    """
    from pathlib import Path

    src = (Path(__file__).resolve().parent.parent
           / "app" / "static" / "app.js").read_text()

    assert src.count("function clearBreaker(") == 1, "the handler or its name"
    assert 'onclick="clearBreaker(' in src
    assert "g.stage === 'held'" in src, "the dashboard's held row lost the hatch"
    assert "d.breaker && d.breaker.state !== 'closed'" in src, (
        "the service page only offers it when a book happens to be listed"
    )
    # The confirmation, which is what stops a mis-click from releasing a
    # library: the call is behind it, and it says what it does.
    body = src.split("function clearBreaker(", 1)[1].split("\n}", 1)[0]
    assert "window.confirm(" in body
    assert "if (!ok) return;" in body
    assert "/breaker/clear`" in body, "the handler calls the wrong endpoint"


def test_the_endpoint_needs_a_session(clock):
    from fastapi.testclient import TestClient

    from app import main

    _trip_and_hold(clock)
    anonymous = TestClient(main.app)

    assert anonymous.post("/api/services/shelfmark/breaker/clear").status_code == 401
    assert breaker.states()["shelfmark"]["state"] == "open"
