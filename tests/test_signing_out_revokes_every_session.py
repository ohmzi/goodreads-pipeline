"""Signing out has to revoke the token, not just drop the cookie.

Session tokens are stateless, so "delete the cookie" only stops the browser
that made the request. Any copy taken earlier — from the cookie jar, from a
proxy log, from a shared machine — stayed valid for the rest of its seven days.
Rotating the user's `session_epoch` is what actually ends those.
"""

from __future__ import annotations

from app import auth


def test_the_token_under_the_cookie_stops_being_accepted(signed_in):
    """The mechanism, stated directly.

    `/api/auth/status` is public and answers only whether a cookie is *valid*,
    so it cannot tell a revoked token from a deleted one. This asks the signer
    itself, which is the same check every request makes.
    """
    token = signed_in.cookies[auth.COOKIE_NAME]
    assert auth.session_username(token) == "operator"

    assert signed_in.post("/api/auth/logout").status_code == 200

    assert auth.session_username(token) is None


def test_replaying_the_old_cookie_gets_a_401(signed_in):
    """The same thing over HTTP, at a path that actually needs a session."""
    token = signed_in.cookies[auth.COOKIE_NAME]
    assert signed_in.get("/api/state").status_code == 200

    signed_in.post("/api/auth/logout")

    replayed = signed_in.get(
        "/api/state", headers={"Cookie": f"{auth.COOKIE_NAME}={token}"}
    )
    assert replayed.status_code == 401


def test_the_password_is_not_touched_by_signing_out(signed_in):
    """`set_user` upserts the whole row, so this is a real way to break login.

    Rotating the epoch reads the row back and rewrites the same hash; a version
    that passed an empty string would sign everyone out and lock them out in
    one move, and it would look like a successful logout.
    """
    from app.db import db

    before = db().get_user("operator")["password_hash"]

    signed_in.post("/api/auth/logout")

    assert db().get_user("operator")["password_hash"] == before
    assert auth.verify_password("a-test-password", before)


def test_signing_in_again_works_after_signing_out(signed_in):
    """The revocation must revoke the old token, not the account."""
    from tests.conftest import OPERATOR, OPERATOR_PASSWORD

    signed_in.post("/api/auth/logout")

    resp = signed_in.post(
        "/api/auth/login",
        json={"username": OPERATOR, "password": OPERATOR_PASSWORD},
    )
    assert resp.status_code == 200, resp.text
    assert signed_in.get("/api/state").status_code == 200


def test_signing_out_without_a_session_is_refused_by_the_gate(anonymous):
    """Logout is gated like every other route, and stays that way.

    Worth pinning because it is the shape of the handler's `if username:`
    guard: the gate has already proved there is a session by the time the
    handler runs, so a caller reaching it with no session would be a bug in the
    gate rather than a case to handle cheerfully.
    """
    assert anonymous.post("/api/auth/logout").status_code == 401
