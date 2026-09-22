"""What the Settings page writes, and what its Test button actually proves.

Both are about the same failure mode: a panel that says something confident and
untrue. An arbitrary key used to land in the `credentials` table because the
write loop iterated the request body; Test used to answer "ok" to a wrong API
key because it called a route that needs no key.
"""

from __future__ import annotations

import pytest

from app.clients.base import ClientError
from app.db import db


def test_an_unknown_credential_key_is_refused(signed_in):
    resp = signed_in.put("/api/settings", json={"values": {"not_a_real_key": "x"}})

    assert resp.status_code == 400
    assert "not_a_real_key" in resp.json()["detail"]
    assert db().get_credential("not_a_real_key") is None


def test_the_whole_body_is_checked_before_anything_is_written(signed_in):
    """An empty value is how a credential is deleted, so a body that is going
    to be refused must not be allowed to delete one on its way there."""
    db().set_credential("kavita_api_key", b"still-here")

    resp = signed_in.put(
        "/api/settings",
        json={"values": {"kavita_api_key": "", "not_a_real_key": "x"}},
    )

    assert resp.status_code == 400
    assert db().get_credential("kavita_api_key") == b"still-here"


def test_a_real_key_is_still_stored_and_deleted(signed_in):
    """The guard must not have broken the actual write path."""
    from app.crypto import cipher

    assert signed_in.put(
        "/api/settings", json={"values": {"kavita_api_key": "k-123"}}
    ).json()["saved"] == ["kavita_api_key"]
    assert cipher().decrypt(db().get_credential("kavita_api_key")) == "k-123"

    assert signed_in.put(
        "/api/settings", json={"values": {"kavita_api_key": ""}}
    ).json()["saved"] == []
    assert db().get_credential("kavita_api_key") is None


def test_every_key_the_ui_renders_is_a_key_this_accepts(signed_in):
    """The two lists must not drift: a field the form offers and the endpoint
    rejects is a Save button that always fails."""
    from app import main

    fields = {f["key"] for f in signed_in.get("/api/settings").json()["fields"]}

    assert fields <= main._CREDENTIAL_KEYS


def test_the_test_button_runs_something_that_needs_the_credential(signed_in, monkeypatch):
    """The regression this closes.

    Every `health()` route these clients have is unauthenticated, so Test used
    to report success for a wrong API key — a green tick beside a red banner
    naming the same service. The stub's `health()` raises rather than returning,
    so a regression fails here loudly instead of passing quietly.
    """
    from app import main

    class WrongKey:
        def health(self):
            raise AssertionError("the unauthenticated health route was used")

        def libraries(self):
            raise ClientError(
                "kavita", "list libraries failed with HTTP 401: unauthorized",
                status=401,
            )

        def close(self):
            pass

    monkeypatch.setitem(main.SERVICE_FACTORIES, "kavita", WrongKey)

    body = signed_in.post("/api/settings/test/kavita").json()

    assert body["ok"] is False
    assert "401" in body["detail"]


def test_the_test_button_still_reports_a_working_service(signed_in, monkeypatch):
    from app import main

    class Working:
        def health(self):
            raise AssertionError("the unauthenticated health route was used")

        def libraries(self):
            return ["Books", "Audiobooks"]

        def close(self):
            pass

    monkeypatch.setitem(main.SERVICE_FACTORIES, "kavita", Working)

    assert signed_in.post("/api/settings/test/kavita").json() == {
        "ok": True, "detail": "2 libraries",
    }


def test_an_unknown_service_is_still_a_404(signed_in):
    assert signed_in.post("/api/settings/test/nonsense").status_code == 404


def test_the_test_button_and_the_health_check_ask_the_same_question(signed_in):
    """Not just "does it call probe" — that they agree on every service.

    Test exists to answer what the banner answers. If the two ever picked
    different calls, the page would be able to contradict itself again.
    """
    from app import health, main

    assert set(main.SERVICE_FACTORIES) == {name for name, _, _ in health.SERVICES}
