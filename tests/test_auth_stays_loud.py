"""A real misconfiguration must stay loud.

This is the regression test for the guard rail the whole design turns on: a
fact about the world ("no audiobook exists") is quiet, and a wrong API key is
not. Twenty books failing on a missing credential must still produce twenty
books the operator can see — the breaker must not adopt them, reclaim them, or
hold them, and `auth` must never be a transient kind.
"""

from __future__ import annotations

from app import breaker, main, models
from app.db import db

import helpers

AUTH_DETAIL = "settings: Shelfmark API key is not set. Add it on the Settings page."


def _keyless(count: int) -> list[int]:
    return [
        helpers.seed_book(f"Keyless {i}", {"acquire_ebook": {
            "status": models.FAILED, "kind": "auth", "service": "settings",
            "detail": AUTH_DETAIL, "attempts": 3}})
        for i in range(count)
    ]


def test_a_wrong_key_on_twenty_books_is_still_twenty_books(clock):
    ids = _keyless(20)

    # Nothing about the breaker can touch it.
    assert breaker.adopt_backlog() == []
    assert "auth" not in breaker.TRANSIENT_KINDS
    for book_id in ids:
        row = db().stage(book_id, "acquire_ebook")
        assert row["status"] == models.FAILED
        assert row["held_by"] == ""

    payload = main.issues()
    group = next(g for g in payload["groups"] if g["service"] == "settings")
    assert group["actionable"] is True
    assert len(group["books"]) == 20
    # A missing credential is a different screen from a rejected one, and the
    # fix line says which.
    assert "Settings" in group["fix"] and "set it" in group["fix"]
    assert payload["counts"]["failed_books"] == 20
    assert not [g for g in payload["groups"] if g["stage"] == "held"]


def test_a_missing_credential_does_not_trip_the_breaker(monkeypatch, clock):
    """A stage returning an `auth` failure must not open anything."""
    from app import pipeline

    book_id = helpers.ebook_pending_book("No Key")

    def missing_key(book: dict) -> models.StageResult:
        return models.StageResult.failed(AUTH_DETAIL, kind="auth",
                                         service="settings")

    monkeypatch.setattr(
        pipeline, "REGISTRY", {stage: missing_key for stage in models.STAGES}
    )
    helpers.scheduler().sweep()

    assert breaker.states() == {}
    assert db().stage(book_id, "acquire_ebook")["status"] == models.FAILED
    assert main.issues()["counts"]["failed_books"] == 1
