"""FastAPI app: the JSON API and the single-page UI.

Everything is behind a session cookie except the login page, the login
endpoint and the health probe. That includes the noVNC bridge — the raw RFB
socket is loopback-only inside the container and reached through an
authenticated WebSocket proxy here, so the browser that is logged into
Goodreads is never exposed on a port of its own.
"""

from __future__ import annotations

import asyncio
import hashlib
import subprocess
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from . import __version__, auth, breaker, models, version_data
from .clients.abs_client import AudiobookshelfClient
from .clients.booklore import BookLoreClient, GrimmoryClient
from .clients.kavita import KavitaClient
from .clients.opennotebook import OpenNotebookClient
from .clients.shelfmark import ShelfmarkClient
from .config import APP_DIR, settings
from .crypto import cipher
from .db import db
from .goodreads import GoodreadsSession, resolve_user_id, set_user_id
from .login import login_session
from .pathing import free_space_gb
from .permissions import harden_data_dir
from .pipeline import Scheduler
from .stages import shelve as shelve_stage

app = FastAPI(title="Goodreads", version=__version__)

STATIC_DIR = APP_DIR / "static"
NOVNC_ROOT = Path("/usr/share/novnc")
VNC_TCP_HOST = "127.0.0.1"
VNC_TCP_PORT = 5900

#: Reachable without a session. Everything else is gated by the middleware.
#: `/api/auth/status` is here because the sign-in page asks it whether to skip
#: the form; it reveals nothing but whether the caller already holds a session.
#:
#: The two asset files are here for the same reason, and were missing them:
#: the sign-in page loads its stylesheet from `/static/`, the gate answered
#: with a 303 to `/login`, the browser followed it and parsed the sign-in page
#: as CSS — so the page rendered with every `var(--token)` unresolved and every
#: colour gone. Both files are build artefacts with no user data in them, and
#: nothing they contain is reachable without a session.
PUBLIC_PATHS = frozenset({
    "/login",
    "/api/auth/login",
    "/api/auth/status",
    "/api/health",
    "/favicon.ico",
    "/static/app.css",
    "/static/app.js",
})

scheduler = Scheduler(interval=settings.poll_interval)

# Credential keys the UI knows how to render, with human labels. Anything
# stored but not listed still round-trips; this only drives the form.
CREDENTIAL_FIELDS: list[tuple[str, str, str]] = [
    ("kavita_api_key", "Kavita API key", "Kavita -> Settings -> API Key"),
    ("booklore_username", "BookLore username", ""),
    ("booklore_password", "BookLore password", ""),
    ("grimmory_username", "Grimmory username", ""),
    ("grimmory_password", "Grimmory password", ""),
    ("abs_username", "Audiobookshelf username", ""),
    ("abs_password", "Audiobookshelf password", ""),
    ("opennotebook_password", "Open Notebook password", "leave blank if auth is disabled"),
    ("sabnzbd_api_key", "SABnzbd API key", "diagnostics only"),
    ("prowlarr_api_key", "Prowlarr API key", "diagnostics only"),
    ("goodreads_appsync_key", "Goodreads AppSync key", "rotates; genre lookups fall back to other sources"),
    ("googlebooks_api_key", "Google Books API key", "optional — without it Google Books is skipped (it rate-limits hard unkeyed)"),
    ("hardcover_api_key", "Hardcover API key", "optional, not yet used"),
]

#: Just the keys, for validating a settings write before it reaches the
#: database. Derived from `CREDENTIAL_FIELDS` so the two cannot drift apart.
_CREDENTIAL_KEYS = frozenset(key for key, _, _ in CREDENTIAL_FIELDS)

SERVICE_FACTORIES = {
    "shelfmark": ShelfmarkClient,
    "kavita": KavitaClient,
    "booklore": BookLoreClient,
    "grimmory": GrimmoryClient,
    "audiobookshelf": AudiobookshelfClient,
    "opennotebook": OpenNotebookClient,
}

# Verified against when the username does not exist, so that a bad username
# and a bad password cost the same work and take the same time.
_DUMMY_HASH = auth.hash_password(auth.generate_password(24))


# --------------------------------------------------------------------------
# authentication
# --------------------------------------------------------------------------
def _host_of(value: str) -> str:
    """The `host:port` part of an `Origin` header, a `Host` header or a URL.

    Both spellings arrive here: an `Origin` is a URL (`https://host:8443`) and
    a `Host` header is a bare authority (`host:8443`). Parsing them the same
    way is what makes the comparison a comparison rather than a guess.
    """
    value = (value or "").strip()
    if not value:
        return ""
    parsed = urlparse(value if "//" in value else f"//{value}")
    return (parsed.netloc or "").lower()


def _refused_origin(origin: str, host: str) -> bool:
    """Whether a request a browser sent came from another site.

    This is the CSRF question, and it is deliberately not left to
    `SameSite=Lax`: that is a browser-side control, so it does nothing at all
    for a caller that is not a browser, and it withholds cookies on a
    cross-site POST but not on a cross-site GET — which is exactly the shape of
    the requests worth attacking here (`/api/sweep` moves real files).

    A missing `Origin` is allowed, and has to be: browsers omit it on
    same-origin navigations, and neither the container's own healthcheck nor
    the pipeline's service calls is a browser at all. So this answers "did
    another site send this?", never "is this caller signed in?" — the session
    cookie is the second question and is asked separately.

    `settings.public_origin` wins when set, because a proxy that rewrites
    `Host` makes the request's own header the wrong thing to compare against.
    """
    if not origin:
        return False
    expected = _host_of(settings.public_origin) or _host_of(host)
    return bool(expected) and _host_of(origin) != expected


@app.middleware("http")
async def require_session(request: Request, call_next):
    # `request.scope["path"]`, never `request.url.path`. The router matches on
    # the scope path, but `request.url.path` reparses the URL and so cuts it at
    # a decoded `?`: with a scope path of `/static/app.css%3F/../app.js` it
    # answered `/static/app.css`, which is a public asset, so the request was
    # waved through and then reached a mount and a fallback the router would
    # never have matched it to. Comparing the same string the router matches
    # closes that and the Host-header variant of it at once.
    path = request.scope["path"]

    origin = request.headers.get("origin", "")
    if _refused_origin(origin, request.headers.get("host", "")):
        # Logged only when the caller actually held a session. This check runs
        # before the session check, so an anonymous caller can reach it at
        # will, and a line per attempt would be a way to fill the activity feed
        # — the same eviction `_log_failed_login` exists to stop. A *signed-in*
        # browser being refused is the case worth a line, because the symptom
        # otherwise is a page that quietly stops working.
        if auth.session_username(request.cookies.get(auth.COOKIE_NAME)):
            db().log(
                f"refused a cross-origin request to {path} from {origin}",
                level="warning",
            )
        return JSONResponse(
            {"error": "cross-origin request refused"}, status_code=403
        )

    # `Range` is dropped for the whole static mount, before the public-path
    # check so it applies to gated files too. Starlette below 1.6.0 merges
    # overlapping byte ranges with a nested loop, so one request carrying
    # thousands of ranges is a cheap way to burn CPU — and `/static/app.css`
    # and `/static/app.js` are reachable with no session at all, which is what
    # made this worth doing rather than merely worth pinning. Nothing here
    # wants partial content — the mount holds a stylesheet, a script, a
    # favicon and the pages, and a browser fetching any of them wants the
    # whole file — so serving it in one piece is the same answer, and partial
    # content buys nothing worth an O(n^2) merge.
    if path.startswith("/static/"):
        request.scope["headers"] = [
            (name, value)
            for name, value in request.scope["headers"]
            if name.lower() != b"range"
        ]

    if path in PUBLIC_PATHS:
        return await call_next(request)

    if auth.session_username(request.cookies.get(auth.COOKIE_NAME)):
        return await call_next(request)

    # API callers get a status they can act on; a page load gets the form.
    if path.startswith("/api/") or path.startswith("/vnc/"):
        return JSONResponse({"error": "authentication required"}, status_code=401)
    return RedirectResponse("/login", status_code=303)


#: Applied to every response, via `setdefault` so a route that sets its own
#: value keeps it.
#:
#: There is deliberately no Content-Security-Policy here. The obvious one
#: blanks the UI: `img-src 'self' data:` kills every book cover, because
#: `cover_url` is scraped straight from Goodreads and never proxied locally,
#: along with the two `s.gr-assets.com` backgrounds (the header wordmark and
#: the nav magnifier). `script-src 'unsafe-inline'` would have to be granted to
#: keep the pages' inline handlers working, which is most of what a CSP is for.
#: Four headers that cannot break anything are worth more than five that can.
_SECURITY_HEADERS: tuple[tuple[str, str], ...] = (
    # Stops a response that is not the type it claims from being sniffed into
    # one that is.
    ("X-Content-Type-Options", "nosniff"),
    # Full URLs leak into the Referer of anything a page links out to. Nothing
    # here needs to tell another site where the operator came from.
    ("Referrer-Policy", "no-referrer"),
    # SAMEORIGIN, not DENY: `/goodreads` frames `/vnc/vnc.html`, which is
    # same-origin, so DENY would blank the Goodreads login panel.
    ("X-Frame-Options", "SAMEORIGIN"),
)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    for name, value in _SECURITY_HEADERS:
        response.headers.setdefault(name, value)
    # API answers are per-session state, never worth caching. Set last and
    # still via setdefault, so a route that already decided its own caching
    # policy is not overruled.
    if request.scope["path"].startswith("/api/"):
        response.headers.setdefault("Cache-Control", "no-store")
    return response


@app.get("/api/health")
def health() -> dict:
    """Unauthenticated liveness probe, for the container healthcheck."""
    return {"status": "ok"}


class LoginBody(BaseModel):
    username: str
    password: str
    # Defaults to the long session, so any caller that does not send the flag
    # behaves exactly as it did before it existed. The sign-in page's "Keep me
    # signed in." checkbox is what turns it off.
    keep_signed_in: bool = True


def _client_key(request: Request) -> str:
    """Identify the client for rate limiting.

    `X-Forwarded-For` is set by the caller and trivially rotated, so trusting
    it would hand an attacker a free bypass of the per-client limit. It is
    honoured only when TRUST_PROXY is on, meaning a proxy you control rewrites
    it; otherwise the real socket peer is used.
    """
    if settings.trust_proxy:
        forwarded = request.headers.get("x-forwarded-for", "")
        address = forwarded.split(",")[0].strip() if forwarded else ""
        if address:
            return address
    return (request.client.host if request.client else "") or "unknown"


#: Bounds how many sign-ins are processed at once. scrypt is memory-hard by
#: design, so without a cap a flood of concurrent attempts would pin the CPU
#: and allocate far more than this box should be asked for.
_login_slots = asyncio.Semaphore(8)

#: How long a sign-in waits for one of those slots before being refused. The
#: throttle's delay is deliberately held *inside* the slot — that is what bounds
#: how fast anyone can guess — so under a flood slots turn over slowly, and
#: refusing the moment all eight were busy turned any eight concurrent failures
#: into a 429 for the owner's own correct password. Waiting briefly costs a real
#: sign-in a second at worst and costs a flood eight more slots' worth of delay.
_LOGIN_SLOT_WAIT_SECONDS = 1.0

#: Longest username kept. Nothing bounds usernames today and the only account is
#: created from the CLI, so this truncates rather than rejecting: the 401 still
#: reads correctly and an existing credential cannot be made unusable.
MAX_USERNAME_CHARS = 64

#: Log the first failed sign-in from a client, then every Nth after that.
_FAILED_LOGIN_EVERY = 10

#: What a sign-in gets when "Keep me signed in." is unticked: the cookie stops
#: twelve hours later rather than at the full session lifetime, which is all a
#: shared machine should be trusted with.
_SHORT_TTL_SECONDS = 12 * 3600


def _log_failed_login(username: str, ip_key: str, request: Request) -> None:
    """Record a failed sign-in, but not once per attempt.

    `/api/state` re-sends the newest 60 events to every open browser every six
    seconds, and `/api/auth/login` is reachable without a session — so one
    anonymous caller in a loop was enough to push every real line out of the
    feed. The throttle already counts attempts per key, so this reads that
    count rather than keeping a second one: the first failure from a client is
    logged, and then every tenth, each line carrying the running count. The
    audit trail survives; the eviction does not.
    """
    failures = auth.throttle.recent_failures(ip_key)
    if failures == 1 or failures % _FAILED_LOGIN_EVERY == 0:
        db().log(
            f"failed login for '{username}' from {_client_key(request)} "
            f"({failures} in the last {auth.throttle.window // 60} min)",
            level="warning",
        )


@app.post("/api/auth/login")
async def auth_login(body: LoginBody, request: Request) -> JSONResponse:
    username = body.username.strip()
    # Bounded where it is *used* rather than by the model. `Field(max_length=)`
    # would reject the request instead, and the only account there is was
    # created from the CLI — so a cap the model enforced could lock the
    # operator out of their own UI over a username that already exists.
    safe_name = username[:MAX_USERNAME_CHARS]
    ip_key = f"ip:{_client_key(request)}"
    user_key = f"user:{safe_name.lower()}"

    # Wait for a slot rather than refuse when all eight are busy. See
    # `_LOGIN_SLOT_WAIT_SECONDS`: the delay below holds its slot on purpose, so
    # an instant refusal here was reachable by an attacker aiming a flood at a
    # known username, and it landed on the owner's correct password too.
    try:
        await asyncio.wait_for(_login_slots.acquire(), timeout=_LOGIN_SLOT_WAIT_SECONDS)
    except asyncio.TimeoutError:
        return JSONResponse(
            {"error": "too many sign-in attempts in progress — try again shortly"},
            status_code=429,
        )

    try:
        row = db().get_user(username)
        # A missing user is verified against a dummy hash so it costs the same
        # as a wrong password — otherwise response time reveals valid usernames.
        stored = row["password_hash"] if row else _DUMMY_HASH
        # scrypt is deliberately slow and CPU-bound, so it runs off the event
        # loop; otherwise a handful of concurrent attempts would stall every
        # other request in the process.
        password_ok = await run_in_threadpool(auth.verify_password, body.password, stored)
        ok = password_ok and row is not None

        if not ok:
            auth.throttle.record_failure(ip_key)
            auth.throttle.record_failure(user_key)
            # Progressive delay, never a lockout. A correct password returned
            # above, so this can only ever punish a wrong guess — which is what
            # stops this being a lockout an attacker can aim at a known
            # username to shut the owner out of their own UI.
            #
            # The sleep stays *inside* the slot on purpose. It is what bounds
            # how many guesses a flood can land per second; releasing the slot
            # first would let an attacker pipeline scrypt at pure slot speed.
            delay = max(auth.throttle.delay_for(ip_key), auth.throttle.delay_for(user_key))
            if delay:
                await asyncio.sleep(delay)
            _log_failed_login(safe_name, ip_key, request)
            return JSONResponse({"error": "invalid username or password"}, status_code=401)

        epoch = row["session_epoch"] or ""
    finally:
        # Released here rather than by `async with`: `asyncio.Semaphore` tracks
        # no ownership, so leaving the block via the 401 return above would
        # release a second time on `__aexit__` and grant a slot that does not
        # exist. `acquire` is the only thing that can still be in flight at the
        # 429 above, and a cancelled acquire leaks nothing.
        _login_slots.release()

    auth.throttle.record_success(ip_key)
    auth.throttle.record_success(user_key)
    db().log(f"login: {username} from {_client_key(request)}")

    ttl = auth.SESSION_TTL_SECONDS if body.keep_signed_in else _SHORT_TTL_SECONDS

    response = JSONResponse({"ok": True, "username": username})
    response.set_cookie(
        auth.COOKIE_NAME,
        auth.issue_session(username, epoch, ttl),
        # The cookie and the token must expire together: a longer max_age would
        # leave the browser sending a cookie the server has already rejected.
        max_age=ttl,
        httponly=True,
        samesite="lax",
        # Plain HTTP on the LAN by default. Turn on via COOKIE_SECURE=1 once
        # this is only ever reached over HTTPS, or the cookie stops being sent.
        secure=settings.cookie_secure,
        path="/",
    )
    return response


@app.post("/api/auth/logout")
def auth_logout(request: Request) -> JSONResponse:
    """Sign out — everywhere, not just here.

    Session tokens are stateless, so deleting the cookie only stops *this*
    browser sending its copy: any other copy taken earlier stayed valid for the
    rest of its seven days, which made "Sign out" a gesture rather than a
    revocation. Rotating the user's `session_epoch` is what actually revokes,
    because every request compares the token's epoch against the stored one.

    That is a deliberate trade with the sign-out itself: signing out here signs
    out every device. The alternative — naming and revoking one session — needs
    a server-side session table, which this design does not have.

    The existing password hash is read back and rewritten unchanged.
    `set_user` upserts the whole row, so passing anything else would silently
    rewrite the password.
    """
    # The gate has already proved this cookie carries a session, so both checks
    # below are unreachable while that holds. They are not removed because the
    # cost of being wrong is a 500 on the sign-out button: if this path is ever
    # made public, or the gate's notion of "signed in" ever widens, the handler
    # still has to cope with there being nothing to revoke.
    username = auth.session_username(request.cookies.get(auth.COOKIE_NAME))
    if username:
        row = db().get_user(username)
        if row is not None:
            # Before the cookie is cleared: if this raises, the request fails
            # loudly and the cookie stays, rather than answering "ok" to a
            # sign-out that revoked nothing.
            db().set_user(username, row["password_hash"], auth.new_epoch())
            db().log(f"logout: {username} (all sessions revoked)")

    response = JSONResponse({"ok": True})
    response.delete_cookie(auth.COOKIE_NAME, path="/")
    return response


@app.get("/api/auth/status")
def auth_status(request: Request) -> dict:
    username = auth.session_username(request.cookies.get(auth.COOKIE_NAME))
    return {"authenticated": username is not None, "username": username}


# --------------------------------------------------------------------------
# noVNC bridge
# --------------------------------------------------------------------------
@app.websocket("/vnc/ws")
async def vnc_websocket(websocket: WebSocket) -> None:
    """Authenticated bridge to the loopback-only VNC socket.

    x11vnc listens on 127.0.0.1 inside this container and nothing else can
    reach it. The browser speaks RFB over this WebSocket instead, so the
    desktop that is logged into Goodreads has no exposed port of its own and
    inherits this app's session check.
    """
    # Before the session check and before any `accept`, so a caller refused for
    # its origin sees exactly what a caller refused for its session sees —
    # `close(1008)` — and the handshake cannot be used to tell the two apart.
    origin = websocket.headers.get("origin", "")
    if _refused_origin(origin, websocket.headers.get("host", "")):
        if auth.session_username(websocket.cookies.get(auth.COOKIE_NAME)):
            db().log(
                f"refused a cross-origin VNC handshake from {origin}",
                level="warning",
            )
        await websocket.close(code=1008)  # policy violation
        return

    if not auth.session_username(websocket.cookies.get(auth.COOKIE_NAME)):
        await websocket.close(code=1008)  # policy violation
        return

    offered = websocket.scope.get("subprotocols") or []
    await websocket.accept(subprotocol="binary" if "binary" in offered else None)

    try:
        reader, writer = await asyncio.open_connection(VNC_TCP_HOST, VNC_TCP_PORT)
    except OSError as exc:
        db().log(f"VNC bridge could not reach x11vnc: {exc}", level="error")
        await websocket.close(code=1011)
        return

    async def browser_to_vnc() -> None:
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                return
            chunk = message.get("bytes")
            if chunk:
                writer.write(chunk)
                await writer.drain()

    async def vnc_to_browser() -> None:
        while True:
            chunk = await reader.read(65536)
            if not chunk:
                return
            await websocket.send_bytes(chunk)

    pumps = [
        asyncio.create_task(browser_to_vnc()),
        asyncio.create_task(vnc_to_browser()),
    ]
    try:
        await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
    except WebSocketDisconnect:
        pass
    finally:
        for task in pumps:
            task.cancel()
        writer.close()
        try:
            await writer.wait_closed()
        except (OSError, RuntimeError):
            pass
        try:
            await websocket.close()
        except RuntimeError:
            pass


@app.get("/vnc/{asset:path}")
def vnc_asset(asset: str) -> FileResponse:
    """Serve the noVNC client from the image, without escaping its directory."""
    root = NOVNC_ROOT.resolve()
    if not root.is_dir():
        raise HTTPException(503, "noVNC is not installed in this image")

    candidate = (root / (asset or "vnc.html")).resolve()
    if candidate != root and not candidate.is_relative_to(root):
        raise HTTPException(404, "not found")
    if not candidate.is_file():
        raise HTTPException(404, "not found")
    return FileResponse(candidate)


# --------------------------------------------------------------------------
# views
# --------------------------------------------------------------------------
def _render_page(name: str) -> HTMLResponse:
    """Serve a static page with the asset version stamped in.

    The version is a query string on app.js/app.css, so a deploy can never be
    masked by a cached stylesheet or script.
    """
    body = (STATIC_DIR / name).read_text(encoding="utf-8")
    return HTMLResponse(
        body.replace("__ASSET_VERSION__", _asset_version()),
        headers={"Cache-Control": "no-cache, must-revalidate"},
    )


def _asset_version() -> str:
    """A short stamp that changes whenever a static file changes.

    Without this the browser happily serves a cached `app.js` after a deploy —
    the server was correct and the UI looked unchanged, which is a confusing
    way to spend an afternoon. The stamp is derived from the files themselves,
    so it needs no build step and no one has to remember to bump it.
    """
    digest = hashlib.sha256()
    for name in ("app.js", "app.css", "favicon.ico"):
        path = STATIC_DIR / name
        try:
            digest.update(path.read_bytes())
        except OSError:
            digest.update(name.encode())
    return digest.hexdigest()[:10]


@app.get("/")
def index() -> HTMLResponse:
    return _render_page("index.html")


@app.get("/login")
def login_page() -> HTMLResponse:
    return _render_page("login.html")


@app.get("/favicon.ico", include_in_schema=False)
def favicon() -> FileResponse:
    """The tab icon: goodreads.com's own.

    It already had a slot in PUBLIC_PATHS, which is where it belongs — a
    browser asks for this before anyone has a session, so a gated one would
    come back as a redirect to the sign-in page and the tab would show
    nothing. Long-lived because it never changes.
    """
    return FileResponse(
        STATIC_DIR / "favicon.ico",
        media_type="image/x-icon",
        headers={"Cache-Control": "public, max-age=604800"},
    )


@app.get("/goodreads")
def goodreads_page() -> HTMLResponse:
    return _render_page("goodreads.html")


@app.get("/api/version")
def version() -> dict:
    """What the running instance is actually serving."""
    return {"asset_version": _asset_version(), "app": "goodreads", "version": app.version}


@app.get("/api/version/history")
def version_history() -> dict:
    """Per-release breakdown for the version page."""
    return {"current": app.version, "releases": version_data.VERSION_HISTORY}


# --------------------------------------------------------------------------
# lifecycle
# --------------------------------------------------------------------------
@app.on_event("startup")
def _startup() -> None:
    db()
    # The umask set at package import covers everything created from here on,
    # but a deployment that has been running already has a database, its
    # sidecars and a session file at the mode they were made with. Logged only
    # when something actually changed, so this is one line on the first start
    # after an upgrade rather than one on every start.
    tightened = harden_data_dir(settings.data_dir)
    if tightened:
        db().log(
            f"tightened permissions on {', '.join(tightened)} — these were "
            f"readable by other accounts on the host"
        )
    # A stage added after a book was created has no row for it; without this
    # that stage would silently never record a result.
    db().ensure_stages()
    if db().user_count() == 0:
        db().log(
            "no login exists yet — create one with "
            "`python -m app.cli set-password <username>`, or nobody can sign in",
            level="error",
        )
    _start_vnc()
    scheduler.start()


@app.on_event("shutdown")
def _shutdown() -> None:
    scheduler.stop()


def _start_vnc() -> None:
    """Bring up the virtual display for the Goodreads login.

    Best-effort: if the packages or the script are missing, everything else
    still works and the login page reports why the view is blank.
    """
    script = APP_DIR.parent / "docker" / "start-vnc.sh"
    if script.exists():
        try:
            subprocess.Popen(["/bin/bash", str(script)])
        except OSError as exc:
            db().log(f"could not start VNC: {exc}", level="error")


# --------------------------------------------------------------------------
# views
# --------------------------------------------------------------------------
#: book id -> (sampled_at, progress). Used to measure the *observed* download
#: rate rather than inferring one from the task's added_time, which includes
#: however long the task sat in the queue — a book that waited 45 minutes and
#: then downloaded for two reads as "7 hours remaining".
_PROGRESS_SAMPLES: dict[str, tuple[float, float]] = {}


def _eta_from_samples(book_id: str, progress: float) -> int | None:
    """Project a finish from how fast progress has actually been moving."""
    import time as _t

    now = _t.time()
    previous = _PROGRESS_SAMPLES.get(book_id)
    _PROGRESS_SAMPLES[book_id] = (now, progress)

    if not previous or progress <= 0 or progress >= 100:
        return None
    last_at, last_progress = previous
    elapsed = now - last_at
    gained = progress - last_progress
    # Needs a real interval and real movement; a stalled task must report no
    # estimate rather than an optimistic one.
    if elapsed < 5 or gained <= 0.1:
        return None
    rate = gained / elapsed          # percent per second
    if rate <= 0:
        return None
    return int((100 - progress) / rate)


def _live_progress(books: list[dict]) -> dict[str, dict]:
    """Live download progress, keyed by book id as a string.

    Only in-flight books are looked up, so this stays one extra HTTP call per
    poll rather than one per book. Showing a percentage, elapsed time and a
    projected finish is the difference between "working" and "wedged" — an
    opaque "downloading" detail reads identically in both cases.
    """
    watching = []
    for book in books:
        stages = book.get("stages") or {}
        for stage in ("acquire_ebook", "acquire_audiobook"):
            run = stages.get(stage) or {}
            if run.get("status") == models.BLOCKED and run.get("artifact") is not None:
                watching.append(book)
                break
    if not watching:
        return {}

    from .clients.shelfmark import ShelfmarkClient
    from .pathing import match_score

    client = ShelfmarkClient()
    try:
        tasks = list(client.iter_tasks())
    except Exception:  # noqa: BLE001 - progress is a nicety, never break the page
        return {}
    finally:
        client.close()

    out: dict[str, dict] = {}
    for book in watching[:60]:
        title = book.get("title") or ""
        author = book.get("author") or ""
        best = None
        for _task_id, state, entry in tasks:
            haystack = " ".join(
                str(entry.get(f) or "") for f in ("title", "book_title", "name", "author")
            )
            score = match_score(haystack, title, author)
            if best is None or score > best[0]:
                best = (score, state, entry)
        if best is None or best[0] < 0.5:
            continue

        _score, state, entry = best
        try:
            progress = float(entry.get("progress") or 0)
        except (TypeError, ValueError):
            progress = 0.0
        info: dict = {
            "state": state,
            "progress": round(progress, 1),
            "message": str(entry.get("status_message") or ""),
            "format": str(entry.get("format") or ""),
            "source": str(entry.get("source_display_name") or entry.get("source") or ""),
            "retry_available": bool(entry.get("retry_available")),
        }
        try:
            added = float(entry.get("added_time") or 0)
        except (TypeError, ValueError):
            added = 0.0
        if added:
            import time as _t

            info["elapsed_seconds"] = int(max(0.0, _t.time() - added))
        # Measured rate, not inferred from queue time.
        eta = _eta_from_samples(str(book["id"]), progress)
        if eta is not None:
            info["eta_seconds"] = eta
        out[str(book["id"])] = info
    # Forget books that are no longer in flight so the dict cannot grow forever.
    for stale in set(_PROGRESS_SAMPLES) - set(out):
        _PROGRESS_SAMPLES.pop(stale, None)
    return out


@app.get("/api/state")
def state() -> dict:
    from .health import summary as health_summary

    books = db().list_books()
    return {
        "books": books,
        "events": db().recent_events(60),
        "stages": list(models.STAGES),
        "categories": sorted((settings.load_categories().get("categories") or {}).keys()),
        "auto_shelve": shelve_stage.auto_shelve_enabled(),
        "version": app.version,
        "disk_free_gb": round(free_space_gb(settings.books_root), 1),
        "health": health_summary(),
        "totals": _totals(books),
        "progress": _live_progress(books),
        "_debug_samples": len(_PROGRESS_SAMPLES),
        "goodreads": {
            # Detected from the stored session; overridable from Settings.
            "user_id": resolve_user_id(),
            "session_age": _session_age_text(),
            "has_session": login_session.status()["saved"],
        },
        "sweep": scheduler.last_result,
        "paths": {
            "books_root": str(settings.books_root),
            "audiobooks_root": str(settings.audiobooks_root),
            "staging": str(settings.staging_dir),
        },
    }


def _with_breakers(payload: dict) -> dict:
    """Fold breaker state into a health summary.

    Done here rather than in `health` so `health` stays import-free of
    `breaker` and the dependency graph stays acyclic (`breaker` reads `health`).
    The JS ignores unknown keys, so this is inert until the UI renders it — it
    exists so a script or a test can see the state without parsing prose.
    """
    states = breaker.states()
    for entry in payload.get("services", []):
        row = states.get(entry["service"]) or {}
        entry["breaker"] = {
            "state": row.get("state", "closed"),
            "open_until": row.get("open_until"),
            "opened_at": row.get("opened_at"),
            "trips": int(row.get("trips") or 0),
            "failures": int(row.get("failures") or 0),
            "last_failure": row.get("last_failure") or "",
        }
    return payload


@app.get("/api/health/services")
def services_health() -> dict:
    """Credential and reachability status for every service we depend on."""
    from .health import summary

    return _with_breakers(summary())


@app.post("/api/health/services/check")
def services_health_check() -> dict:
    """Re-probe everything now, rather than waiting for the timer."""
    from .health import check_all, summary

    check_all()
    return _with_breakers(summary())


@app.post("/api/reconcile")
def run_reconcile() -> dict:
    """Check our shelf records against Goodreads and queue retries for drift."""
    from .reconcile import reconcile

    result = reconcile(apply_changes=True)
    db().log(
        f"reconcile: {result['drifted']} of {result['checked']} shelf record(s) drifted"
    )
    return result


@app.get("/api/issues")
def issues() -> dict:
    """Everything that needs a human, grouped so it can be acted on.

    The point is that a failure should arrive with its cause and its fix
    attached. "no audiobook releases found" is not a bug and should not sit in
    the same list as "Kavita rejected your API key", which is.
    """
    from .health import IMPACT, canonical, label_for, service_for_stage

    books = db().list_books()
    groups: dict[tuple[str, str, str, str], dict] = {}
    # Books the breaker is holding, gathered per service so they can be
    # rendered as one row instead of never (a hold is not a failure, so the
    # loop below would skip every one of them) or as 169.
    held: dict[str, list[dict]] = {}
    held_seen: dict[str, set] = {}

    for book in books:
        for stage, run in (book.get("stages") or {}).items():
            if run.get("status") == models.BLOCKED and run.get("held_by"):
                svc = canonical(run["held_by"])
                seen = held_seen.setdefault(svc, set())
                # Deduplicated by book: one book can be held on two stages at
                # once (ebook and audiobook) and it is one book.
                if book["id"] not in seen:
                    seen.add(book["id"])
                    held.setdefault(svc, []).append(
                        {"id": book["id"], "title": book["title"]}
                    )
                continue
            if run.get("status") != models.FAILED:
                continue
            kind = run.get("failure_kind") or ""
            detail = (run.get("detail") or "").strip()
            # Recorded at the point of failure, not parsed back out of the
            # message. Empty is a real answer — `index` and `verify` fan out
            # over several services and name them in `detail` instead.
            service = canonical(run.get("service") or "")
            # Group by *cause*, not by message. Twenty books with no audiobook
            # release available is one fact about the world, not twenty
            # problems — grouping on the raw string produces one group per book
            # and buries the two things that actually need a human.
            bucket, headline = _cause_bucket(stage, kind, detail)
            # Service is part of the key: the same failure text against two
            # different services is two different things to go and fix.
            key = (stage, kind, service, bucket)
            group = groups.setdefault(
                key,
                {
                    "stage": stage,
                    "kind": kind,
                    "service": service,
                    "service_label": label_for(service) if service else "",
                    "impact": IMPACT.get(service, ""),
                    "detail": headline,
                    "sample": detail,
                    "books": [],
                    "fix": _fix_for(stage, kind, detail, service),
                    "actionable": _actionable(kind, bucket),
                },
            )
            group["books"].append({"id": book["id"], "title": book["title"]})

    # One synthetic row per service the breaker is holding. This is the whole
    # point of the breaker: an outage that would have been 169 book rows is one
    # line naming the service, with the books listed under it.
    for svc, row in breaker.open_breakers().items():
        books_held = held.get(svc, [])
        hours = breaker.age_hours(row.get("opened_at"))
        # A pseudo-stage. The row is about a service, not a stage, and the JS
        # renders an unknown stage as itself; using a real stage name would be
        # a lie about which of the two acquire stages is held, and would also
        # let the book page's per-stage `fix` lookup match this group and
        # replace a book's own explanation with the outage's.
        key = ("", "network", svc, f"breaker:{svc}")
        groups[key] = {
            "stage": "held",
            "kind": "network",
            "service": svc,
            "service_label": label_for(svc),
            "impact": IMPACT.get(svc, ""),
            "detail": breaker.headline(svc, len(books_held), hours,
                                       row.get("last_failure") or ""),
            "sample": row.get("last_failure") or "",
            "books": books_held,
            "fix": _fix_for_breaker(svc, row["state"], hours,
                                    row.get("last_failure") or ""),
            # Required for the row to render at all (`renderIssuePreview`
            # filters on it), and not a claim that the operator can *do*
            # something — the `fix` line is what says otherwise. Dropping it
            # would let the panel read "Nothing needs attention" while 169
            # books sat untouched.
            "actionable": True,
        }

    # A group whose service is currently held cannot be retried into anything
    # but the hold it is already waiting on. The panel used to offer "Retry
    # all" on 18 parked audiobooks while Shelfmark's breaker was open, which
    # spends the operator's attention to re-queue them into the same wait — and
    # reads as though the failure were theirs to fix when the service is down.
    #
    # `actionable` is deliberately left alone: the preview filters on it before
    # it ever reaches this row, so flipping it to False would make the group
    # disappear from "What needs you" rather than show it differently — the
    # exact failure mode `IMPACT`/`actionable=True` on the breaker's own row
    # above already exists to avoid. `blocked_by` instead tells the UI to swap
    # the button for a link to the thing actually in the way.
    open_now = breaker.open_breakers()
    for group in groups.values():
        if group["stage"] == "held":
            continue
        blocker = canonical(group["service"]) or service_for_stage(group["stage"])
        if blocker and blocker in open_now:
            group["blocked_by"] = blocker
            group["fix"] = (
                f"{label_for(blocker)} is held right now, so retrying these "
                f"cannot get further than the hold. Wait for it to clear, then "
                f"retry. Original cause: {group['fix']}"
            )

    ordered = sorted(
        groups.values(),
        key=lambda g: (not g["actionable"], -len(g["books"])),
    )
    needs_review = [b for b in books if b.get("needs_review")]

    return {
        "groups": ordered,
        "counts": {
            "failed_books": sum(1 for b in books if book_state(b) == "failed"),
            "partial_books": sum(1 for b in books if book_state(b) == "partial"),
            "issue_groups": len(ordered),
            "actionable_groups": sum(1 for g in ordered if g["actionable"]),
            "needs_review": len(needs_review),
        },
        "needs_review": [
            {"id": b["id"], "title": b["title"], "category": b["category"]}
            for b in needs_review
        ],
    }


def _cause_bucket(stage: str, kind: str, detail: str) -> tuple[str, str]:
    """Collapse a failure message into (bucket, human headline).

    The bucket is the grouping key; the headline is what the UI shows for the
    whole group. Per-book specifics stay in `sample`.
    """
    lowered = detail.lower()

    if "no audiobook releases found" in lowered:
        return "no-audiobook", "No audiobook exists in any configured source"
    if "no ebook releases found" in lowered:
        return "no-ebook", "No ebook exists in any configured source"
    if "free space" in lowered or "gb free" in lowered:
        return "disk", "Not enough free disk space"
    if kind == "auth":
        return f"auth:{detail[:60].lower()}", detail[:160]
    if kind == "network":
        return "network", "Service could not be reached"
    if kind == "busy":
        # One bucket, not one per message: a rate limit is one condition
        # however many books it touched, and the detail below already carries
        # the service's own words.
        return "busy", "The service is rate-limiting requests"
    if kind == "server":
        return f"server:{detail[:60].lower()}", detail[:160]
    if stage == "verify" and "missing from" in lowered:
        # Grouped by *which services* are missing it, not by the whole
        # sentence. The rest of `detail` names the book's own search terms, so
        # bucketing on the raw text gives one group per book — which is how 105
        # books arrived as a wall of near-identical rows.
        who = detail.split("MISSING from", 1)[1].split("|", 1)[0].strip(" —")
        return f"unindexed:{who.lower()}", f"In the library, but not indexed by {who}"
    if "shelfmark reported" in lowered or (
        "tried all" in lowered and "every download failed" in lowered
    ):
        # Two messages, one fact: `_watch` reports the first as soon as a
        # queued release comes back failed; `_pick` reports the second on a
        # later sweep once every candidate is exhausted and none of them is
        # left to try. Both mean "Shelfmark accepted releases for this book
        # and every one died," so both get the one row this bucket exists for.
        return "shelfmark-error", "Shelfmark failed to download these"
    if "csrf" in lowered or "session" in lowered and "goodreads" in lowered:
        return "goodreads-session", "Goodreads session expired"
    if stage == "notebook" and "cannot read" in lowered:
        return "format", "File format Open Notebook cannot read"
    if stage in ("acquire_ebook", "acquire_audiobook") and kind == "data":
        # A safety net under the two checks above, not a replacement for them:
        # this only catches what neither named pattern did. Every `data`
        # failure on these two stages is this same class of fact — no usable
        # release could be gotten for this book — however it ends up worded,
        # and every wording embeds the book's own title. Bucketing on the raw
        # text before this check existed put the title in the key, and a
        # title never repeats: on this operator's own backlog it produced 69
        # one-book rows, including several from a message this code no longer
        # even writes — proof that matching exact phrasing here is not
        # something a future wording change can be trusted not to break again.
        which = "an audiobook" if stage == "acquire_audiobook" else "an ebook"
        return f"acquire-data:{stage}", f"Could not get {which} for these"
    return f"other:{detail[:60].lower()}", detail[:160] or "Failed"


def _actionable(kind: str, bucket: str) -> bool:
    """Whether a human can do anything about this class of failure.

    "No audiobook release exists" is a fact about the world, not a fault. It is
    reported, but it must not sit in the same list as "your API key is wrong",
    or the list stops being worth reading.
    """
    if bucket in ("no-audiobook", "no-ebook", "format"):
        return False
    return True


def _has_failure(book: dict) -> bool:
    return any(
        (run or {}).get("status") == models.FAILED
        for run in (book.get("stages") or {}).values()
    )


#: Stages whose failure is about a *format*, not about the book being broken.
FORMAT_STAGES = ("acquire_ebook", "acquire_audiobook")

#: Failures a human can act on. Everything else is either a fact about the
#: world or a transient condition the pipeline handles itself.
#:
#: `busy` belongs here even though it is the most transient of the lot: when a
#: breaker holds the service, no book is attempted against it and this never
#: reaches the panel at all. What it catches is the case where the breaker is
#: *not* tripping — one book, one 429, no outage — and then a rate limit that
#: keeps happening is a real thing to be told about rather than something to
#: bury in `partial`.
ACTIONABLE_KINDS = ("auth", "network", "server", "busy")


def book_state(book: dict) -> str:
    """One of: done | partial | unavailable | failed | working.

    These are deliberately four different things, because lumping them together
    made the attention list useless:

      * **failed** — something a human can act on: rejected credentials, a
        post-place stage that broke. This is the only state that belongs in
        "needs attention".
      * **partial** — the book is in the library, but one format could not be
        found. Nothing is wrong; there is nothing to fix. An audiobook that
        does not exist anywhere is the common case.
      * **unavailable** — no format was found in any source, so there is no
        book at all. Also not a fault, but a different one from `partial`.
      * **done / working** — self-explanatory.

    A book whose *only* outstanding stage is the audiobook counts as `partial`,
    not `working`. Without that, a book finished everywhere it could be sat in
    "in progress" indefinitely while its audiobook waited on a source that was
    never coming — 233 books looked busy when in truth nothing was happening to
    them and nothing was going to.
    """
    stages = book.get("stages") or {}
    failed = [(name, run or {}) for name, run in stages.items()
              if (run or {}).get("status") == models.FAILED]

    if failed:
        # A stage after placement breaking is always actionable — the book is
        # supposed to be in the services and is not.
        if any(name not in FORMAT_STAGES for name, _ in failed):
            return "failed"
        # Only acquisition failed. If it was for a reason a human can act on,
        # say so; otherwise it is a fact about availability.
        if any((run.get("failure_kind") or "") in ACTIONABLE_KINDS for _, run in failed):
            return "failed"
        placed = (stages.get("place") or {}).get("status") == models.OK
        return "partial" if placed else "unavailable"

    outstanding = {
        name for name, run in stages.items()
        if (run or {}).get("status") not in (models.OK, models.SKIPPED)
    }
    if not outstanding:
        return "done"

    # Only the optional audiobook left. While it is genuinely downloading, the
    # book is working; once it is merely stuck waiting on a source, the book is
    # as finished as it is going to get and belongs in "audiobook missing".
    if outstanding <= {"acquire_audiobook"}:
        audio_run = stages.get("acquire_audiobook") or {}
        audio = audio_run.get("status")
        placed = (stages.get("place") or {}).get("status") == models.OK
        # `housed()` counts BLOCKED as settled, which is right for "the
        # audiobook does not exist anywhere" and wrong for a breaker hold: that
        # book is not finished being looked for, it is waiting on a source to
        # come back. Calling it `partial` would leave it looking settled while
        # the pipeline was actively waiting, which is worse than the truthful
        # `failed` it reads as today.
        if housed(audio) and placed and not (audio_run.get("held_by") or ""):
            return "partial"

    return "working"


def housed(status: str | None) -> bool:
    """True when a stage is not going to make progress without outside help."""
    return status in (models.BLOCKED, models.FAILED, models.PENDING)


def _totals(books: list[dict]) -> dict:
    """At-a-glance counts for the dashboard."""
    counts = {"complete": 0, "partial": 0, "unavailable": 0,
              "in_flight": 0, "failed": 0}
    key_for = {"done": "complete", "partial": "partial",
               "unavailable": "unavailable", "failed": "failed",
               "working": "in_flight"}
    review = 0
    for book in books:
        counts[key_for[book_state(book)]] += 1
        if book.get("needs_review"):
            review += 1
    return {
        "books": len(books),
        "complete": counts["complete"],
        "partial": counts["partial"],
        "unavailable": counts["unavailable"],
        "in_flight": counts["in_flight"],
        "failed": counts["failed"],
        "needs_review": review,
        "shelved": sum(
            1 for b in books
            if ((b.get("stages") or {}).get("shelve") or {}).get("status") == models.OK
        ),
    }


def _fix_for(stage: str, kind: str, detail: str, service: str = "") -> str:
    """A concrete next step for a failure, in the operator's language.

    Names the service when one is known. "Credentials rejected" leaves you to
    work out *whose* credentials; "Kavita is rejecting its credentials" does
    not, and that is the whole point of recording the service.
    """
    from .health import label_for

    lowered = detail.lower()
    who = label_for(service) if service else ""
    where = f"{who}'s service page" if who else "Services"
    if kind == "auth" and service == "settings":
        # Not a rejected credential — one that was never set. The fix is a
        # different screen from a wrong key, so it says so.
        return "A credential is missing — open Settings and set it, then Test."
    if kind == "auth":
        return f"{who + ' is rejecting its credentials' if who else 'Credentials rejected'} — open {where} and use Test to fix this one."
    if kind == "network":
        return f"{who + ' could not be reached' if who else 'The service could not be reached'} — check it is running, then retry."
    if kind == "server":
        return f"{who + ' returned an error' if who else 'The service returned an error'} — usually transient; retry, and check {where} if it persists."
    if kind == "busy":
        return (f"{who + ' is rate-limiting requests' if who else 'The service is rate-limiting requests'}"
                f" — the pipeline slows down and retries. Nothing to fix unless it lasts; check {where}.")
    if stage == "verify" and kind == "notfound":
        # Deliberately not "retry". The searches ran and came back empty, so
        # running them again returns empty again; what is worth checking is
        # whether the service is scanning the folder the file is actually in.
        from .stages.verify import MAX_RESCANS

        target = detail.split("MISSING from", 1)[1].split("|", 1)[0].strip(" —") \
            if "MISSING from" in detail else (who or "the service")
        return (f"The file is in the library, but {target} still does not list it "
                f"after {MAX_RESCANS} rescans. Check that library's folders cover "
                f"where the book was placed, then retry.")
    if "no audiobook releases" in lowered:
        return "No audiobook exists in any configured source. Nothing to fix; retry later or shelve the ebook alone."
    if "no ebook releases" in lowered:
        return "No ebook exists in any configured source. Retry later, or add it by hand."
    if "free space" in lowered or "gb free" in lowered:
        return "Not enough disk space where audiobooks land. Free space, then retry."
    if stage == "classify":
        return "No genre matched. Add a rule in categories.yml, or set the category by hand on the book."
    if stage == "place":
        return "The download finished but no matching file was found. Check the staging folder."
    if stage == "shelve":
        return "The Goodreads shelf move failed. Re-login to Goodreads if the session expired."
    return "Retry from the book's detail view; if it persists, check the service's logs."


def _fix_for_breaker(service: str, state: str, hours: float = 0.0,
                     last_failure: str = "") -> str:
    """What to do about a held service — usually "nothing, yet".

    The escalation is the part that matters. A young outage really is not the
    operator's to fix, and saying so is the honest answer rather than a shrug;
    but the same line would keep saying it forever, so once the outage has
    outlived `TRANSIENT_GRACE_HOURS` the sentence changes and points at the
    service itself. That is what keeps the breaker from being the thing that
    hides a permanently dead upstream.
    """
    from .health import label_for
    from .pipeline import TRANSIENT_GRACE_HOURS

    who = label_for(service)
    where = f"{who}'s service page"
    # An upstream that states its own resume time has answered the only
    # question this row raises. Said first, because "nothing to fix" is a much
    # easier thing to accept once you know when it ends.
    resume = breaker.resume_note(last_failure)
    if resume:
        return (f"Nothing to fix{resume.replace(' — it says', ' — {} says'.format(who), 1)}. "
                f"The held books resume on their own; open {where} to see the "
                f"full message.")
    if hours >= TRANSIENT_GRACE_HOURS:
        return (
            f"{who} has been unable to reach its sources for {hours:.0f} hours "
            f"— past the {TRANSIENT_GRACE_HOURS}h grace, so this is not a "
            f"passing outage. Check {who} itself; open {where} to see what it "
            f"is saying."
        )
    if state == "half_open":
        return (
            f"Retrying now, one book at a time. Nothing to do — these resume "
            f"on their own; open {where} to see what it is saying."
        )
    return (
        f"Nothing to fix: these resume by themselves once {who} answers again. "
        f"{who} is up — the fault is upstream. Open {where} to see what it is "
        f"saying."
    )


@app.get("/api/books/{book_id}")
def book_detail(book_id: int) -> dict:
    row = db().get_book(book_id)
    if row is None:
        raise HTTPException(404, "no such book")
    book = db().list_books()
    match = next((b for b in book if b["id"] == book_id), None)
    if match is None:
        raise HTTPException(404, "no such book")

    placed: dict = {}
    place_run = (match.get("stages") or {}).get("place") or {}
    if place_run.get("output_path"):
        try:
            import json as _json

            placed = _json.loads(place_run["output_path"])
        except ValueError:
            placed = {}

    return {
        "book": match,
        "placed": placed,
        "events": [
            e
            for e in db().recent_events(200)
            if e.get("book_id") == book_id
        ][:40],
    }


# --------------------------------------------------------------------------
# book actions
# --------------------------------------------------------------------------
class CategoryBody(BaseModel):
    category: str


class ToggleBody(BaseModel):
    enabled: bool


@app.post("/api/books/{book_id}/retry/{stage}")
def retry(book_id: int, stage: str) -> dict:
    if stage not in models.STAGES:
        raise HTTPException(404, f"unknown stage {stage}")
    if db().get_book(book_id) is None:
        raise HTTPException(404, "no such book")
    db().reset_stage(book_id, stage)
    db().log(f"manual retry of {stage}", book_id=book_id, stage=stage)
    return {"ok": True}


@app.post("/api/books/{book_id}/category")
def set_category(book_id: int, body: CategoryBody) -> dict:
    if db().get_book(book_id) is None:
        raise HTTPException(404, "no such book")
    categories = settings.load_categories().get("categories") or {}
    if body.category not in categories:
        raise HTTPException(400, f"{body.category} is not a category in categories.yml")
    db().set_category(book_id, body.category, needs_review=False)
    # A different category means a different folder, so redo the placement.
    db().reset_stage(book_id, "place")
    db().log(f"category set to {body.category} by hand", book_id=book_id)
    return {"ok": True}


@app.post("/api/books/{book_id}/auto_shelve")
def set_auto_shelve(book_id: int, body: ToggleBody) -> dict:
    if db().get_book(book_id) is None:
        raise HTTPException(404, "no such book")
    db().set_auto_shelve(book_id, body.enabled)
    return {"ok": True}


@app.post("/api/auto_shelve")
def set_global_auto_shelve(body: ToggleBody) -> dict:
    db().set_setting("auto_shelve", "1" if body.enabled else "0")
    db().log(f"global auto-shelve {'on' if body.enabled else 'off'}")
    return {"ok": True}


# --------------------------------------------------------------------------
# jobs
# --------------------------------------------------------------------------
@app.post("/api/sweep")
def sweep() -> dict:
    return scheduler.sweep(force_discover=True)


@app.get("/api/categories")
def categories() -> dict:
    """Categories with their folder and notebook, for the UI dropdown."""
    rules = settings.load_categories()
    return {"categories": rules.get("categories") or {}, "fallback": rules.get("fallback")}


# --------------------------------------------------------------------------
# credentials
# --------------------------------------------------------------------------
def _service_urls() -> dict[str, str]:
    """Where each service actually lives, for display next to its health."""
    return {
        "shelfmark": settings.shelfmark_url,
        "kavita": settings.kavita_url,
        "booklore": settings.booklore_url,
        "grimmory": settings.grimmory_url,
        "audiobookshelf": settings.abs_url,
        "opennotebook": settings.opennotebook_url,
        "sabnzbd": settings.sabnzbd_url,
    }


#: Which stored credentials belong to which service, so a service's own page
#: shows its own two fields rather than all thirteen interleaved.
CRED_PREFIXES: dict[str, tuple[str, ...]] = {
    "kavita": ("kavita_",),
    "booklore": ("booklore_",),
    "grimmory": ("grimmory_",),
    "audiobookshelf": ("abs_",),
    "opennotebook": ("opennotebook_",),
    "goodreads": ("goodreads_",),
}


def _fields_for(service: str) -> list[dict]:
    stored = set(db().credential_keys())
    prefixes = CRED_PREFIXES.get(service)
    if not prefixes:
        return []
    return [
        {"key": key, "label": label, "hint": hint, "is_set": key in stored}
        for key, label, hint in CREDENTIAL_FIELDS
        if key.startswith(prefixes)
    ]


@app.get("/api/services/{name}")
def service_detail(name: str) -> dict:
    """One service, end to end: is it healthy, what is stuck on it, its log.

    This is the page a failure should land on. The list view answers "is
    anything broken"; this answers "what exactly, since when, and what is it
    holding up" without sending you to a shell.
    """
    from .health import SERVICE_STAGES, canonical, label_for, summary

    key = canonical(name)
    health = summary()
    row = next((s for s in health["services"] if s["service"] == key), None)
    if row is None and key not in ("goodreads", "settings"):
        raise HTTPException(404, f"unknown service {name}")

    # Everything currently waiting on this service, with the stage that is
    # stuck and what it said. `blocked` counts too: a book parked on an
    # unreachable service is exactly what you want to see here.
    held: list[dict] = []
    for book in db().list_books():
        for stage, run in (book.get("stages") or {}).items():
            if canonical((run or {}).get("service") or "") != key:
                continue
            if (run or {}).get("status") not in (models.FAILED, models.BLOCKED):
                continue
            held.append({
                "id": book["id"],
                "title": book["title"],
                "stage": stage,
                "status": run["status"],
                "detail": run.get("detail") or "",
                "failure_kind": run.get("failure_kind") or "",
                "attempts": run.get("attempts") or 0,
            })

    label = label_for(key)
    events, inferred = _events_for(key, label)

    breaker_row = breaker.states().get(key) or {}
    return {
        "service": key,
        "label": label,
        "health": row,
        # Present for every service, `closed` for most: a script or a test can
        # ask "is the pipeline holding work on this service" without reading
        # the issue panel's prose.
        "breaker": {
            "state": breaker_row.get("state", "closed"),
            "open_until": breaker_row.get("open_until"),
            "opened_at": breaker_row.get("opened_at"),
            "trips": int(breaker_row.get("trips") or 0),
            "last_failure": breaker_row.get("last_failure") or "",
        },
        "url": _service_urls().get(key, ""),
        "fields": _fields_for(key),
        "stages": list(SERVICE_STAGES.get(key, ())),
        "held": sorted(held, key=lambda h: (h["status"] != "failed", h["title"])),
        "events": events,
        # True when these were recovered by matching the service's name in the
        # message rather than read from `events.service`. The UI says so, so an
        # inferred line is never mistaken for a recorded one.
        "events_inferred": inferred,
        # A service page is also how you get to Goodreads' own sign-in flow.
        "login_url": "/goodreads" if key == "goodreads" else "",
    }


@app.post("/api/services/{name}/breaker/clear")
def clear_breaker(name: str) -> dict:
    """Release a held service by hand: the way out of a wedged breaker.

    Every other exit from a hold is the service proving it is answering, which
    is the right default and no use at all when nothing can prove it — a
    breaker whose probes keep coming back with no evidence holds a working
    service, and there is no clock that closes it, no restart that clears it
    (the state is a row, not memory) and no button anywhere that did. The
    failure is silent, so the escape hatch has to be easy to reach and blunt
    about what it did: `breaker.clear` owns the honesty (it releases the books
    exactly as a recovery does, logs a `warning` naming the operator, and does
    not stamp `last_ok_at` or claim the service answered); this endpoint is the
    reach, and `app.js` confirms before calling it.

    POST and only POST. A GET on this path never reaches the handler — the SPA
    fallback is registered last, matches every GET, and answers `/api/...` with
    404 rather than the page — so a link, a prefetch or a reload cannot release
    anything. The body is the breaker's own account of what it did,
    `breaker.clear`'s return verbatim, and the panel prints its `message`.
    """
    from .health import LABELS, canonical

    key = canonical(name)
    if not key or key not in LABELS:
        raise HTTPException(404, f"unknown service {name}")
    return breaker.clear(key, reason="cleared by hand from the services panel")


def _events_for(service: str, label: str) -> tuple[list[dict], bool]:
    """This service's log, preferring records over inference.

    `events.service` only started being written when the column was added, so
    on an existing database every service's log is empty until the next probe
    runs. Rather than show "Nothing yet" for five minutes — which reads as
    broken rather than as new — fall back to matching the service's own name in
    the message. That is the same substring guess the UI used to make for
    failures; it is acceptable *here* because it is labelled as inferred and
    stops being used the moment real records exist.
    """
    recorded = db().recent_events(limit=80, service=service)
    if recorded:
        return recorded, False
    rows = db().query(
        "SELECT * FROM events WHERE message LIKE ? ORDER BY id DESC LIMIT 80",
        (f"%{label}%",),
    )
    return [dict(r) for r in rows], bool(rows)


@app.get("/api/settings")
def get_settings() -> dict:
    stored = set(db().credential_keys())
    return {
        "fields": [
            {
                "key": key,
                "label": label,
                "hint": hint,
                "is_set": key in stored,
            }
            for key, label, hint in CREDENTIAL_FIELDS
        ],
        "services": _service_urls(),
    }


class CredentialBody(BaseModel):
    values: dict[str, str]


@app.put("/api/settings")
def put_settings(body: CredentialBody) -> dict:
    """Store the credentials sent, deleting the ones sent empty.

    The whole body is checked before anything is written. `set_credential` is
    an unconditional upsert and the loop below iterates the request body
    directly, so without this any key a caller cared to invent became a row in
    the `credentials` table — and because the writes are sequential, a body
    that was going to be rejected partway through could still have deleted a
    real credential on its way to the failure.
    """
    unknown = sorted(set(body.values) - _CREDENTIAL_KEYS)
    if unknown:
        raise HTTPException(400, f"unknown credential key(s): {', '.join(unknown)}")

    saved = []
    for key, value in body.values.items():
        if value == "":
            db().delete_credential(key)
            continue
        db().set_credential(key, cipher().encrypt(value))
        saved.append(key)
    db().log(f"credentials updated: {', '.join(saved) or 'none'}")
    return {"ok": True, "saved": saved}


@app.post("/api/settings/test/{service}")
def test_service(service: str) -> dict:
    """Test one service's credentials.

    Runs the same authenticated probe the background health check runs, rather
    than the client's own `health()`. Every `health()` route these clients have
    is unauthenticated — Kavita `/api/health`, BookLore `/api/v1/healthcheck`,
    Audiobookshelf `/status`, Open Notebook `/api/config`, Shelfmark
    `/api/health` — so Test used to answer "ok" to a wrong API key: a green
    tick in the panels beside a red banner naming the same service, with
    nothing to say which one to believe. `probe` owns the per-service choice of
    call and the error handling, and closes the client itself.
    """
    from .health import probe

    factory = SERVICE_FACTORIES.get(service)
    if factory is None:
        raise HTTPException(404, f"unknown service {service}")
    ok, detail, _kind = probe(service, factory)
    return JSONResponse({"ok": ok, "detail": detail}, status_code=200)


# --------------------------------------------------------------------------
# Goodreads login
# --------------------------------------------------------------------------
@app.get("/api/goodreads/login")
def login_status() -> dict:
    status = login_session.status()
    status["session_age"] = _session_age_text()
    return status


@app.post("/api/goodreads/login")
def login_start() -> dict:
    return login_session.start()


@app.post("/api/goodreads/login/save")
def login_save() -> dict:
    return login_session.save()


@app.post("/api/goodreads/login/cancel")
def login_cancel() -> dict:
    return login_session.cancel()


class UserIdBody(BaseModel):
    user_id: str = ""


@app.post("/api/goodreads/user_id")
def set_goodreads_user_id(body: UserIdBody) -> dict:
    """Pin the Goodreads user id, or clear it to re-detect from the session."""
    set_user_id(body.user_id)
    resolved = resolve_user_id(refresh=True)
    return {"ok": True, "user_id": resolved}


def _session_age_text() -> str:
    age = GoodreadsSession().session_age_seconds()
    if age is None:
        return "no stored session"
    minutes = int(age // 60)
    if minutes < 60:
        return f"stored {minutes} min ago"
    hours = minutes // 60
    if hours < 48:
        return f"stored {hours} h ago"
    return f"stored {hours // 24} days ago"


# --------------------------------------------------------------------------
# static
# --------------------------------------------------------------------------
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# Registered last so it cannot shadow any real route. The UI routes with a
# hash (`#/books`), but a bare `/books` still has to land somewhere sensible
# rather than 404 — deep links get pasted around, and a 404 reads as "the app
# is broken" when the app is fine.
@app.get("/{unknown:path}")
def spa_fallback(unknown: str) -> HTMLResponse:
    if unknown.startswith(("api/", "static/", "vnc/")) or unknown in ("login", "goodreads"):
        raise HTTPException(404, "not found")
    # Through `_render_page`, not `FileResponse`: serving the file raw leaves
    # the literal `__ASSET_VERSION__` in it, so a deep link — exactly the URL
    # someone pastes or bookmarks and comes back to after a deploy — requests
    # `/static/app.js?v=__ASSET_VERSION__` and gets whatever the browser cached.
    return _render_page("index.html")
