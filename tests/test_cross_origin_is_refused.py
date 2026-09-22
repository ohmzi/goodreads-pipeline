"""Cross-origin requests, refused at the door.

`SameSite=Lax` is a browser-side control. It does nothing at all for a caller
that is not a browser, and it withholds the cookie on a cross-site POST but not
on a cross-site GET — which is the shape of several endpoints worth attacking
here (`/api/sweep` moves real files on disk). So the origin is checked on the
server, before the session is even looked at.
"""

from __future__ import annotations

import pytest


@pytest.mark.parametrize(
    "origin,host,refused",
    [
        ("", "goodreads:8090", False),
        ("http://goodreads:8090", "goodreads:8090", False),
        ("https://goodreads:8090", "goodreads:8090", False),
        ("http://evil.example", "goodreads:8090", True),
        # Right host, wrong port — a different origin by the spec.
        ("http://goodreads:8091", "goodreads:8090", True),
        # From a sandboxed frame or a file:// page. Never anything useful.
        ("null", "goodreads:8090", True),
    ],
)
def test_the_origin_check(origin, host, refused):
    from app import main

    assert main._refused_origin(origin, host) is refused


def test_a_configured_public_origin_wins_over_the_requests_own_host(monkeypatch):
    """A proxy that rewrites `Host` otherwise makes every request look foreign."""
    from app import main

    monkeypatch.setattr(main.settings, "public_origin", "https://goodreads.example.com")

    assert main._refused_origin("https://goodreads.example.com", "goodreads:8090") is False
    assert main._refused_origin("http://goodreads:8090", "goodreads:8090") is True


def test_a_cross_origin_write_is_refused_and_does_not_happen(signed_in):
    from app.db import db

    resp = signed_in.post(
        "/api/auto_shelve",
        json={"enabled": True},
        headers={"Origin": "http://evil.example"},
    )

    assert resp.status_code == 403
    assert resp.json()["error"] == "cross-origin request refused"
    assert db().get_setting("auto_shelve", "") == "", "the write went through anyway"


def test_it_is_refused_before_the_session_is_checked(anonymous):
    """403, not 401: a cross-site caller learns nothing about whether it holds
    a session, and an unsigned caller is not told to go and get one."""
    resp = anonymous.get("/api/state", headers={"Origin": "http://evil.example"})

    assert resp.status_code == 403


def test_a_same_origin_request_is_untouched(signed_in):
    assert (
        signed_in.get("/api/state", headers={"Origin": "http://testserver"}).status_code
        == 200
    )


def test_a_request_with_no_origin_is_allowed(anonymous):
    """The container's healthcheck and the pipeline's own service calls are not
    browsers, and browsers omit `Origin` on same-origin navigations."""
    assert anonymous.get("/api/health").status_code == 200


def test_only_a_signed_in_refusal_is_logged(signed_in, anonymous):
    """The check runs before the session check, so a line per refusal would be
    a way to fill the activity feed from an address with no account."""
    from app.db import db

    anonymous.get("/api/state", headers={"Origin": "http://evil.example"})
    assert not [
        e for e in db().recent_events(20) if "cross-origin" in e["message"]
    ], "an anonymous refusal reached the feed"

    signed_in.get("/api/state", headers={"Origin": "http://evil.example"})
    assert any(
        "cross-origin" in e["message"] for e in db().recent_events(20)
    ), "a signed-in browser being refused left no trace"


def _handshake(ws_client, origin):
    """The close code a rejected handshake gets, or None if it was accepted."""
    from starlette.websockets import WebSocketDisconnect

    try:
        with ws_client.websocket_connect("/vnc/ws", headers={"origin": origin}) as ws:
            ws.close()
    except WebSocketDisconnect as exc:
        return exc.code
    return None


def test_the_vnc_handshake_refuses_another_sites_page(signed_in):
    """The panel would otherwise be reachable from any page the operator has
    open, against a socket that has no password of its own."""
    assert _handshake(signed_in, "http://evil.example") == 1008


def test_the_vnc_refusals_are_indistinguishable(signed_in, anonymous):
    """Wrong origin and no session both answer `close(1008)`, so the handshake
    cannot be used to work out which one the caller got wrong."""
    by_origin = _handshake(signed_in, "http://evil.example")
    by_session = _handshake(anonymous, "http://testserver")

    assert by_origin == by_session == 1008
