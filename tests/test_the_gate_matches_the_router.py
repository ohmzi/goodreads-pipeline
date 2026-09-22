"""The session gate, the headers it adds, and the bytes it will not serve.

`require_session` is both the access check and the place two request-shaped
attacks are now answered: a path the router and the gate disagree about, and a
`Range` header against a file anyone can fetch.
"""

from __future__ import annotations

import asyncio

from starlette.requests import Request
from starlette.responses import PlainTextResponse


def _request(path: str, method: str = "GET", headers: list | None = None) -> Request:
    """A Request whose scope path is exactly `path`.

    Built by hand on purpose. A URL client normalises dot segments and
    re-encodes `%3F` before the string leaves it, which is precisely the
    transformation under test — httpx would hand the app a tidied path and the
    test would pass whether or not the gate had been fixed.
    """
    return Request({
        "type": "http",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": headers or [],
        "server": ("goodreads", 8090),
        "client": ("10.0.0.9", 1234),
        "root_path": "",
    })


def _run(middleware, request):
    """Drive one middleware call, recording whether it reached the app."""
    reached = []

    async def call_next(req):
        reached.append(req)
        return PlainTextResponse("reached")

    return asyncio.run(middleware(request, call_next)), bool(reached)


def test_a_path_the_router_would_not_match_is_not_treated_as_public():
    """`/static/app.css%3F/../app.js`.

    `request.url.path` reparses the URL, so it answered `/static/app.css` for
    this scope path — a public asset — and the request was waved through to a
    static mount and an SPA fallback the router had never matched it to. The
    gate now compares the same string the router routes on.
    """
    from app import main

    request = _request("/static/app.css?/../app.js")

    # The old expression, pinned deliberately: this is what the fix is a fix
    # for, so a future starlette that stops behaving this way is reported here
    # rather than silently changing what the security assertion below means.
    assert request.url.path == "/static/app.css"

    response, reached = _run(main.require_session, request)
    assert not reached, "a path that is not public was granted without a session"
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_a_public_asset_still_needs_no_session():
    """The other half: the fix must not have closed the login page's door."""
    from app import main

    response, reached = _run(main.require_session, _request("/static/app.css"))

    assert reached, "the sign-in page's stylesheet was refused"
    assert response.status_code == 200


def test_a_range_header_never_reaches_the_static_mount():
    """SEC-10a.

    Starlette below 1.6.0 merges overlapping byte ranges with a nested loop, so
    a `Range:` header carrying thousands of them is a cheap way to burn CPU —
    on a request that needs no session at all. The header is dropped rather
    than the response range-checked, and only the header.
    """
    from app import main

    request = _request(
        "/static/app.css",
        headers=[(b"range", b"bytes=0-0,0-0,0-0"), (b"accept", b"text/css")],
    )
    _run(main.require_session, request)

    names = [name.lower() for name, _ in request.scope["headers"]]
    assert b"range" not in names
    assert b"accept" in names, "the strip took more than the Range header"


def test_a_range_request_gets_the_whole_file(anonymous):
    """The observable half of SEC-10: no 206, and the same bytes."""
    full = anonymous.get("/static/app.css")
    ranged = anonymous.get("/static/app.css", headers={"Range": "bytes=0-9"})

    assert full.status_code == 200
    assert ranged.status_code == 200, "a Range still earned a 206"
    assert ranged.content == full.content


def test_every_response_carries_the_hardening_headers(anonymous):
    """Including the refusal the gate writes itself.

    That is the response an unauthenticated caller actually gets, and applying
    the headers only to routes would have missed exactly the traffic an
    attacker generates most of.
    """
    resp = anonymous.get("/api/state")

    assert resp.status_code == 401
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert resp.headers["Referrer-Policy"] == "no-referrer"
    assert resp.headers["X-Frame-Options"] == "SAMEORIGIN"
    # No CSP, deliberately — see `_SECURITY_HEADERS`. A regression that added
    # one would blank every book cover.
    assert "Content-Security-Policy" not in resp.headers


def test_sameorigin_not_deny_because_the_login_page_is_framed(anonymous):
    """`/goodreads` frames `/vnc/vnc.html` same-origin."""
    from pathlib import Path

    src = (Path(__file__).resolve().parent.parent
           / "app" / "static" / "goodreads.html").read_text(encoding="utf-8")

    assert "/vnc/vnc.html" in src, "the frame this header value exists for"
    assert anonymous.get("/login").headers["X-Frame-Options"] == "SAMEORIGIN"


def test_api_answers_are_not_stored_and_pages_keep_their_own_policy(anonymous):
    """`setdefault`, so a route that decided its own caching is not overruled."""
    assert anonymous.get("/api/health").headers["Cache-Control"] == "no-store"
    assert (
        anonymous.get("/login").headers["Cache-Control"]
        == "no-cache, must-revalidate"
    )
