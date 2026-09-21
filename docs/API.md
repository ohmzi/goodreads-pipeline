# API

The HTTP surface of the same process that serves the UI, enumerated from
`app/main.py`. There is no API token, no OAuth, no versioned public interface
and no per-route scoping: one session cookie opens everything, and the routes
are shaped for `app/static/app.js`, the only client that ships with the app.

That has one practical consequence for a script. The routes are stable enough to
drive, but they are not a contract — a response field exists because the UI
renders it. Where a shape is loose (a `SELECT *` row, a generated string) this
file says so rather than pretending otherwise.

Everything below is reachable at `http://<host>:8091`. The process reads two
sources of truth for its behaviour: the SQLite database on the data volume, and
`categories.yml`. See [CONFIGURATION.md](CONFIGURATION.md) for the environment,
[PIPELINE.md](PIPELINE.md) for what the stages do, and
[OPERATIONS.md](OPERATIONS.md) for the CLI.

## Conventions

| Property | Value |
|---|---|
| Request bodies | JSON (`Content-Type: application/json`). Only `PUT /api/settings`, the login route and a few small assignment and toggle bodies take one; every other route takes nothing |
| Responses | JSON, except the three HTML pages and the noVNC assets |
| Method | `GET`, `POST`, `PUT` and one WebSocket. No `DELETE`, no `PATCH`. The API routes do not answer `HEAD`, so `curl -I` returns 405 |
| Trailing slash | Significant. `/api/health/` is not `PUBLIC_PATHS`, so it is gated like everything else (`app/main.py:49-55`, an exact string match) |
| Pagination | None. `GET /api/state` returns every book on every call |
| CORS | Not enabled. No middleware is registered for it, so a browser page on another origin cannot read these responses. Clients are the app's own UI or a script |
| Rate limiting | Only on `POST /api/auth/login` |
| Generated schema | `GET /openapi.json` (needs a session) serves a FastAPI-generated schema. It documents the parameters, and describes every response body as an unconstrained object, because the handlers are annotated `-> dict` (`app/main.py:498`) |

### Error shapes

Three, and they are not interchangeable:

| Shape | Where | Example |
|---|---|---|
| `{"error": "..."}` | The session middleware, and the login route's own 401/429 | `{"error": "authentication required"}` |
| `{"detail": "..."}` | `HTTPException` raised inside a handler — every 404 and the category 400 | `{"detail": "no such book"}` |
| `{"detail": [ ... ]}` | FastAPI request validation, always `422` | `{"type": "missing", "loc": ["body", "category"], ...}` |

`POST /api/settings/test/{service}` is the exception to the rule that failures
use an error status: a service that fails its check returns `200` with
`{"ok": false, "detail": "<error>"}`, because a failed credential test is a
successful test.

## Authentication

There is no bearer token. A script authenticates exactly as a browser does: it
POSTs credentials to the login route, keeps the cookie, and sends it on every
subsequent request.

### The cookie

`goodreads_session` (`app/auth.py:34`), set by `POST /api/auth/login`
(`app/main.py:188-198`) and read on every request by the middleware
(`app/main.py:100`) and by the WebSocket handshake (`app/main.py:227`).

| Attribute | Value |
|---|---|
| Value | `base64url(payload).base64url(HMAC-SHA256)` — stateless, no session table |
| `Max-Age` | 604800 seconds (7 days), also carried as `exp` inside the signed payload (`app/auth.py:35`) |
| `HttpOnly` | yes — JavaScript cannot read it, so `document.cookie` in the app's own page shows nothing |
| `SameSite` | `Lax` — withholds the cookie on a cross-site POST, which is the CSRF case that matters here |
| `Secure` | only when `COOKIE_SECURE=1`. Left off by default because the app serves plain HTTP on the LAN, and a `Secure` cookie over HTTP is silently dropped (`app/main.py:194-196`) |
| `Path` | `/` |

The signature and the payload's password epoch are verified before anything
else; the mechanism, and what a leaked cookie is worth, are in
[SECURITY.md](SECURITY.md). What matters to a client: the cookie is a bearer
token, so it is as sensitive as the password, and a password change evicts it
immediately.

### Signing in from a script

```bash
# 1. sign in; the cookie lands in cookies.txt
curl -s -c cookies.txt http://<host>:8091/api/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"username": "you", "password": "..."}'
# {"ok":true,"username":"you"}

# 2. reuse it. Every later call is this same shape.
curl -s -b cookies.txt http://<host>:8091/api/state
curl -s -b cookies.txt -X POST http://<host>:8091/api/sweep
```

`cookies.txt` holds the signed session in the clear; treat it as a credential
file. A wrong password or an unknown username both return
`401 {"error": "invalid username or password"}` after a progressive delay — the
delay is deliberate, so a script that retries in a loop will get slower rather
than faster. Signing out is `POST /api/auth/logout`, which is itself gated: it
deletes the cookie (`app/main.py:205`) and an unauthenticated call gets the
usual `401` instead.

The WebSocket is authenticated with the same cookie, sent on the handshake. A
browser does that automatically; a script has to set the `Cookie` header itself.

### Failed sign-ins

| Property | Value | Where |
|---|---|---|
| Free attempts | 3 per key, then a delay | `app/auth.py:179-186` |
| Delay | 0.5 s doubling per failure, ceiling 8 s | `app/auth.py:229` |
| Window | 15 minutes | `app/auth.py:179-186` |
| Keys | username (lowercased) and client address, counted separately; the larger delay wins | `app/main.py:145-146`, `app/main.py:172` |
| Concurrent sign-ins | 8, throttled by a semaphore; a 9th gets `429` | `app/main.py:139`, `app/main.py:148` |
| Never | A lockout. A correct password is always answered, so an attacker cannot lock the owner out | `app/auth.py:165-177` |

The client address comes from the socket peer unless `TRUST_PROXY=1`, in which
case `X-Forwarded-For` is honoured — the header is caller-controlled, so
trusting it without a proxy that rewrites it is a free bypass of the per-client
limit (`app/main.py:120-133`).

### What is reachable without a session

`PUBLIC_PATHS` is the complete set (`app/main.py:49-55`). Everything else — the
pages, `/static`, every route under `/api/`, `/vnc/` and the WebSocket — is
gated by one middleware (`app/main.py:94-106`).

| Path | Why it is open |
|---|---|
| `/login` | The sign-in form |
| `/api/auth/login` | The route the form posts to |
| `/api/auth/status` | The sign-in page asks whether to render the form; the answer reveals only whether the caller already holds a session |
| `/api/health` | The container healthcheck (`docker-compose.yml:38-40`) |
| `/favicon.ico` | Allowlisted, but no route serves it — it falls through to the catch-all and returns the index page with status 200 |

The allowlist is an exact string match on the path, so a query string does not
change the answer (`/api/health?x=1` is public) and neither does case
(`/LOGIN` is not `/login`). A request that fails the check is answered by what
it asked for, not by who is asking:

| Path | Answer |
|---|---|
| Starts with `/api/` or `/vnc/` | `401 {"error": "authentication required"}` |
| Anything else, including `/`, `/docs`, `/openapi.json`, `/static/app.js` | `303` redirect to `/login` |

That split is the reason a script should read status codes rather than bodies:
`401` means "sign in and retry", `303` means "you asked for a page".

`GET /api/version` reports what the running instance is serving — the static
asset stamp and the app version (`app/main.py:344-347`). It is gated like every
other `/api/` route. `GET /api/health` is the unauthenticated liveness probe:
it returns `{"status": "ok"}` and touches nothing else — not the database, not a
service — so a green healthcheck proves uvicorn is answering and nothing more
(`app/main.py:109-112`). Credential and reachability state for the six
integrations is a different route with its own probe.

## Routes

32 routes and one static mount (`app/main.py`, counted from the decorated
functions). Grouped by area.

### Auth

| Method | Path | Session | Request | Returns |
|---|---|---|---|---|
| POST | `/api/auth/login` | no | `{"username": str, "password": str}` | `200 {"ok": true, "username": "<name>"}` and sets the cookie; `401 {"error": "invalid username or password"}`; `429 {"error": "too many sign-in attempts in progress — try again shortly"}` |
| POST | `/api/auth/logout` | yes | — | `200 {"ok": true}`, cookie deleted with `Path=/` |
| GET | `/api/auth/status` | no | — | `{"authenticated": bool, "username": str\|null}` |

The login handler strips the username, verifies the password against scrypt in a
worker thread, and compares a nonexistent username against a dummy hash so the
two answers cost the same (`app/main.py:88`, `app/main.py:158-163`). Both
outcomes are logged: failures at `warning`, successes at `info`.

### State

| Method | Path | Session | Request | Returns |
|---|---|---|---|---|
| GET | `/api/state` | yes | — | The whole dashboard payload — see below |
| GET | `/api/version` | yes | — | `{"asset_version": "<10 hex chars>", "app": "goodreads", "version": "1.0.0"}` |
| GET | `/api/issues` | yes | — | `{"groups": [...], "counts": {...}, "needs_review": [...]}` |
| GET | `/api/categories` | yes | — | `{"categories": {...}, "fallback": ...}` — `app/categories.yml` verbatim (`app/main.py:900-904`) |

`asset_version` is a SHA-256 over `app.js` and `app.css`, truncated to ten
characters (`app/main.py:311-326`). It changes when the UI files change, not
when the Python does, so it identifies a build's front end rather than the
process. The process's own version is the FastAPI app version
(`app/main.py:39`).

`GET /api/state` is what the UI polls, every 6 seconds (`app/static/app.js:1182`):

| Key | Type | Contents |
|---|---|---|
| `books` | array | Every row of `books`, `genres` parsed into a list, plus a `stages` map of stage name to `stage_runs` row |
| `events` | array | The newest 60 rows of `events` |
| `stages` | array | `models.STAGES` in pipeline order (`app/models.py:12-23`) |
| `categories` | array | Category names from `categories.yml`, sorted |
| `auto_shelve` | bool | The global switch, from the `settings` table |
| `disk_free_gb` | number | Free space on the books root, one decimal |
| `health` | object | `health.summary()` — the same object `GET /api/health/services` returns |
| `totals` | object | `books`, `complete`, `partial`, `unavailable`, `in_flight`, `failed`, `needs_review`, `shelved` |
| `progress` | object | Book id -> live download detail; empty when nothing is mid-download |
| `_debug_samples` | int | Size of the in-process ETA sampler. A diagnostic counter, not a queue depth |
| `goodreads` | object | `user_id`, `session_age`, `has_session` |
| `sweep` | object | The last sweep's summary; `{}` until one has run. Carries `held` (books whose only runnable stage the breaker refused) and `breaker` (service -> state), so a quiet sweep explains itself |
| `paths` | object | `books_root`, `audiobooks_root`, `staging` |

Two keys do work when you ask for them:

- `progress` looks each in-flight book up in Shelfmark's task list to report a
  percentage, elapsed time and a projected finish, because an opaque
  "downloading" reads the same whether it is moving or wedged (`app/main.py:421-495`).
  It costs one upstream call per poll, caps at the first 60 watched books, and
  reports no estimate rather than an optimistic one when a task has stalled.
- `goodreads.user_id` is detected from the stored browser session and written to
  the `settings` table, so it survives a restart and is returned without another
  detection attempt once a value exists (`app/goodreads.py:109-112`,
  `app/goodreads.py:128`). The 10-minute cooldown bounds repeated detection when
  none has succeeded, so a session that cannot be resolved does not hammer
  Goodreads on every poll (`app/goodreads.py:98-99`, `app/goodreads.py:118-120`).
  It is empty when detection has not succeeded; `POST /api/goodreads/user_id`
  pins it instead.

`GET /api/issues` is the attention list, grouped by cause rather than by book,
so twenty books with no audiobook release in any source read as one fact about
the world rather than twenty problems (`app/main.py:558-625`):

| Field | Contents |
|---|---|
| `groups[].stage`, `.kind`, `.service`, `.service_label` | The stage, the recorded failure kind, and the service that failed — recorded at the point of failure, not parsed back out of the message |
| `groups[].impact` | What breaks while that service is down, e.g. `no downloads can start` (`app/health.py:37-44`) |
| `groups[].detail`, `.sample` | The group headline, and one book's original message |
| `groups[].fix` | A generated next step in the operator's language, naming the service when one is known |
| `groups[].actionable` | Whether a human can do anything about this class of failure. `false` for "no release exists" and format problems |
| `groups[].books` | `[{id, title}]` |
| `counts` | `failed_books`, `partial_books`, `issue_groups`, `actionable_groups`, `needs_review` |
| `needs_review` | `[{id, title, category}]` |

One group is synthetic. When the service breaker is holding a service — three
consecutive transient failures, so nothing is attempted against it — `/api/issues`
emits **one** group for that service instead of one per affected book: `stage` is
the pseudo-stage `"held"` (never a real stage name, so the book detail view's
per-stage `fix` lookup cannot match it), `service` is set, `actionable` is `true`
so the row renders at all, and `detail` carries the count and, past the 24-hour
grace, the outage's age. `fix` says there is nothing to do until the grace is
outlived. The books themselves are `blocked` with `held_by` set, so they are not
counted in `counts.failed_books` and they resume by themselves
(`app/breaker.py`, `app/main.py:603-700`).

Groups are ordered actionable-first, then by how many books they hold. The
`failed`/`partial`/`unavailable`/`working` classification behind those counts is
computed per request from the stage rows, not stored (`app/main.py:684-738`);
the difference between them is elaborated in [PIPELINE.md](PIPELINE.md).

### Books and stages

| Method | Path | Session | Request | Returns |
|---|---|---|---|---|
| GET | `/api/books/{book_id}` | yes | — | `{"book": {...}, "placed": {...}, "events": [...]}`; `404 {"detail": "no such book"}` |
| POST | `/api/books/{book_id}/retry/{stage}` | yes | — | `{"ok": true}`; `404` for an unknown stage (`{"detail": "unknown stage <name>"}`) or an unknown book |
| POST | `/api/books/{book_id}/category` | yes | `{"category": str}` | `{"ok": true}`; `400 {"detail": "<name> is not a category in categories.yml"}`; `404` for an unknown book |
| POST | `/api/books/{book_id}/auto_shelve` | yes | `{"enabled": bool}` | `{"ok": true}`; `404` for an unknown book |
| POST | `/api/auto_shelve` | yes | `{"enabled": bool}` | `{"ok": true}` — the global switch |

`book` in the detail route is the same object as the entry in
`/api/state`'s `books`; `placed` is the `place` stage's `output_path` parsed as
JSON, i.e. `{"ebook": ..., "audiobook": ...}`, and `{}` when that is absent or
unparsable. `events` is the book's rows taken from the newest 200 events and
capped at 40, so a book that has not been mentioned in the last 200 lines
returns an empty list even though it has history (`app/main.py:830-838`).

Two of these routes are the API's own retry path, and they are not symmetrical:

- `retry` resets the named stage **and everything downstream of it** to
  pending, attempts zero (`app/db.py:437-450`). Retrying `place` therefore
  re-runs `place`, `index`, `notebook`, `verify` and `shelve`. It queues work;
  it does not run anything.
- Setting a category also resets `place`, because the category decides the
  destination folder (`app/main.py:871-872`). The file moves on the next sweep.

`auto_shelve` is two switches that are both consulted by the `shelve` stage: the
per-book flag (`app/stages/shelve.py:82`) and the global one
(`app/stages/shelve.py:84`). Either off parks the stage as `blocked` with a
message saying which. A newly discovered book inherits the global setting at
insert time (`app/stages/discover.py:60`).

### Jobs

| Method | Path | Session | Request | Returns |
|---|---|---|---|---|
| POST | `/api/sweep` | yes | — | The sweep summary, or `{"skipped": "a sweep is already running"}` |
| POST | `/api/reconcile` | yes | — | `{"checked": int, "drifted": int, "details": [...]}` — `details` holds at most 20 entries |

`POST /api/sweep` runs a whole sweep synchronously in a worker thread, with
`force_discover=True`: the credential probes, `discover`, `reconcile` and the
per-book stage machine all run, whether or not their intervals are due
(`app/main.py:895-897`, `app/pipeline.py:127-134`). The request returns when the
sweep does, which the time budget bounds rather than a book count
(`SWEEP_BUDGET_SECONDS`, `app/pipeline.py:72`). It is the only route that writes
to the library tree, and it reaches Goodreads indirectly — by running the stages
documented in [PIPELINE.md](PIPELINE.md). It is not the only route that reaches
Goodreads: `POST /api/reconcile` reads the account's shelves, and
`POST /api/goodreads/login` drives a real browser to goodreads.com. Concurrent
calls do not queue: the second one is refused with the `skipped` shape, which is
a different body from a real summary.

`POST /api/reconcile` compares each book's recorded shelf against what the
account actually shows and resets the `shelve` stage of anything that has
drifted. It always applies — there is no dry-run parameter on the route
(`app/main.py:551`) — and it reads the account's four shelves over the network,
so it needs a live Goodreads session and a few seconds (`app/reconcile.py:46-86`).

### Health

| Method | Path | Session | Request | Returns |
|---|---|---|---|---|
| GET | `/api/health` | no | — | `{"status": "ok"}` |
| GET | `/api/health/services` | yes | — | `health.summary()` |
| POST | `/api/health/services/check` | yes | — | The same summary, after probing all six services now |

The summary is `{"services": [...], "unhealthy": [...], "auth_failures": [...],
"all_ok": bool}`. Each entry in `services` carries `service`, `label`, `impact`,
`checked`, `ok`, `detail`, `failure_kind`, `checked_at`, `ok_since` and
`breaker` (`app/health.py:154-179`, `app/main.py:577-598`). `ok` is `null` for a
service that has never been probed. `breaker` is `{state, open_until,
opened_at, trips, failures, last_failure}` and is `closed` for every service that
has never failed, so a script can ask "is the pipeline holding work on this
service" without reading the issue panel's prose. `POST .../check` is the only way to force a probe without waiting for
the five-minute timer or triggering a sweep (`app/main.py:537-543`).

`all_ok` is computed from the last stored probe of each service, not from a
fresh check, so it can be up to five minutes stale. A probe is the cheapest
*authenticated* call each service offers — listing libraries, or notebooks —
precisely so that a wrong key cannot report green (`app/health.py:90-116`).
Shelfmark is the exception: that instance puts no auth in front of its health
route, so its probe calls that instead (`app/health.py:99-101`). The Test button
on a service page is a different, weaker check — the difference is in
[OPERATIONS.md](OPERATIONS.md).

### Services and credentials

| Method | Path | Session | Request | Returns |
|---|---|---|---|---|
| GET | `/api/services/{name}` | yes | — | One service end to end; `404 {"detail": "unknown service <name>"}` |
| POST | `/api/services/{name}/breaker/clear` | yes | — | Release a held service by hand; `404` for a name that is not a service |

`name` is canonicalised — lowercased with spaces removed — so `Open Notebook`
and `opennotebook` are the same request (`app/health.py:61-71`).
`goodreads` and `settings` are valid names even though neither is probed: one
has a browser session instead of a health route, and the other is where a
missing credential is reported from.

| Field | Contents |
|---|---|
| `service`, `label` | The canonical slug and its human name |
| `health` | This service's row from the summary above, or `null` |
| `url` | The endpoint this instance is configured to call (`app/main.py:910-920`) |
| `fields` | The stored credentials belonging to this service, as `{key, label, hint, is_set}` — never values. Empty for a service with no credential prefix mapped, which includes Shelfmark |
| `breaker` | This service's breaker state, `closed` for one that has never failed |
| `stages` | The per-book stages this service is on the hook for (`app/health.py:50-58`) |
| `held` | Every book currently `failed` or `blocked` on this service: `{id, title, stage, status, detail, failure_kind, attempts}` |
| `events` | Up to 80 log lines for this service |
| `events_inferred` | `true` when those lines were recovered by matching the service's name in the message rather than read from the event's `service` column |
| `login_url` | `/goodreads` for Goodreads, empty otherwise |

`held` counts `blocked` as well as `failed` on purpose: a book parked on an
unreachable service is exactly what the page exists to show. `events_inferred`
is set when the recorded column was empty, which on an older database is true
until the next probe writes a row (`app/main.py:1004-1022`).

`POST .../breaker/clear` is the only endpoint that changes a breaker without
evidence, and it exists because the alternative is a library that has stopped
acquiring with nothing able to restart it: a hold ends when the service proves
it is answering, and a breaker nothing can prove anything about would otherwise
hold forever. It releases the books and clears the per-book clock exactly as a
recovery does, and returns `{service, label, was, cleared, released,
was_open_for_hours, was_open_until, message}` — `cleared: false` with `released:
0` when nothing was held (a double click, a stale page), never an error.
Nothing in the response claims the service answered: `message` says it was
cleared by hand and that the next sweep will re-trip it if it is still down,
and `last_ok_at` on the row is left untouched, because a click is not a probe.
The UI calls this from **Clear hold**, behind a confirmation. POST only — a
`GET` on the path is not routed to it at all, so a link or a prefetch cannot
release a hold (`app/breaker.py:732-813`, `app/main.py:1183-1206`).

### Settings

| Method | Path | Session | Request | Returns |
|---|---|---|---|---|
| GET | `/api/settings` | yes | — | `{"fields": [...], "services": {...}}` |
| PUT | `/api/settings` | yes | `{"values": {"<key>": "<value>"}}` | `{"ok": true, "saved": ["<key>", ...]}` |
| POST | `/api/settings/test/{service}` | yes | — | `200 {"ok": bool, "detail": str}`; `404` for an unknown service |

`GET` returns the fields the UI knows how to render, each with `key`, `label`,
`hint` and `is_set`, plus the service URLs this instance calls
(`app/main.py:1025-1039`). It never returns a credential value; there is no
route that does, because stored credentials are Fernet-encrypted with the key
from `.env` and only ever decrypted inside the process
(`app/crypto.py:31-42`).

`PUT` is the only writer. An empty string deletes the key
(`app/main.py:1049-1052`); a non-empty one encrypts and stores it. Keys not in
`CREDENTIAL_FIELDS` are still stored and still round-trip — that list drives the
form, not the schema (`app/main.py:59-75`). `saved` lists only the keys given a
value, so a request that only clears fields returns an empty list. One request
writes all its values under one log line, and the whole payload is a dict: there
is no per-key route.

`POST .../test/{service}` constructs that service's client and calls its own
`health()` route, returning the result as a string. It writes nothing and
records nothing — it is not the authenticated probe
(`app/main.py:1059-1073`).

### Goodreads login

| Method | Path | Session | Request | Returns |
|---|---|---|---|---|
| GET | `/api/goodreads/login` | yes | — | `{"active": bool, "started_at": float\|null, "message": str, "saved": bool, "vnc_url": "/vnc/vnc.html?autoconnect=1&resize=scale", "session_age": str}` |
| POST | `/api/goodreads/login` | yes | — | `{"ok": true, "message": "browser starting"}` or `"already running"` |
| POST | `/api/goodreads/login/save` | yes | — | `{"ok": true, "message": "saving session"}`; `{"ok": false, "message": "no login in progress"}` |
| POST | `/api/goodreads/login/cancel` | yes | — | `{"ok": true}` |
| POST | `/api/goodreads/user_id` | yes | `{"user_id": str}` | `{"ok": true, "user_id": "<resolved>"}` |

There is no password flow to automate, so this route family drives a real
Chromium on the virtual display and captures its cookies when a human presses
Save. `start` launches the browser on its own thread and returns immediately —
the login outlives the request by design, because you might take two minutes
over a CAPTCHA (`app/login.py:54-61`). `save` writes the browser's storage state
to the data volume and closes the browser; `cancel` closes it and writes nothing
(`app/login.py:110-117`). `session_age` is a rendered string — `no stored
session`, `stored 12 min ago`, `stored 3 h ago` — not a number
(`app/main.py:1113-1123`).

`POST /api/goodreads/user_id` pins the account id explicitly, or clears the pin
when given an empty or omitted value and re-detects from the session; the
resolved value comes back in the response and may be empty if detection fails
(`app/main.py:1105-1110`).

### VNC

| Method | Path | Session | Request | Returns |
|---|---|---|---|---|
| WS | `/vnc/ws` | yes | RFB bytes | RFB bytes; close `1008` without a session, close `1011` when the VNC server is unreachable |
| GET | `/vnc/{asset:path}` | yes | — | A noVNC client file; `503 {"detail": "noVNC is not installed in this image"}`; `404 {"detail": "not found"}` |

The WebSocket is the reason the login view needs no second port. x11vnc listens
on the container's loopback only (`docker/start-vnc.sh`), and this route is the
sole way in:

| Property | Value |
|---|---|
| Auth | The `goodreads_session` cookie from the handshake. HTTP middleware does not cover WebSockets, so the check is explicit and is the only one (`app/main.py:227-229`) |
| Failure before accept | `close(1008)` — policy violation |
| Subprotocol | `binary` when the client offers it, otherwise none (`app/main.py:231-232`) |
| Payload | Raw RFB bytes in both directions, unframed: browser frames are written straight to the VNC socket and VNC data is sent back in 64 KiB chunks (`app/main.py:241-256`). Text frames are ignored |
| Upstream | The loopback VNC server on port 5900 (`app/main.py:43-44`). If the connection fails the error is logged and the socket closes `1011` |

The client files are served from the image's noVNC directory
(`app/main.py:42`, `app/main.py:280-292`); `GET /vnc/` resolves to
`vnc.html`. The path is resolved against that directory and rejected if it
escapes it. Outside the container the directory does not exist, so every `/vnc/`
request answers `503` — the app itself is fine.

The UI embeds this in an iframe and points the noVNC client at the bridge with
`path=vnc/ws` (`app/static/goodreads.html:70`). A client that is not a browser
page would speak RFB itself over this WebSocket and send the cookie on the
handshake.

### Pages and static

| Method | Path | Session | Returns |
|---|---|---|---|
| GET | `/` | yes | `index.html`, the SPA shell |
| GET | `/login` | no | `login.html` |
| GET | `/goodreads` | yes | `goodreads.html`, the login view with the noVNC iframe |
| GET | `/static/...` | yes | A file from `app/static` (`app/main.py:1129-1130`) |
| GET | `/{unknown:path}` | yes | `index.html`, or `404 {"detail": "not found"}` for anything under `api/`, `static/`, `vnc/` or named `login`/`goodreads` |

The three pages are read from disk and served with `Cache-Control: no-cache,
must-revalidate` and an asset version stamped into `app.js`/`app.css` query
strings, so a deploy cannot be masked by a cached stylesheet
(`app/main.py:298-308`). The catch-all is registered last so it cannot shadow a
real route, and it is a `GET` route — an unknown path under any other method is
answered by the router with `405` before it is reached (`app/main.py:1133-1145`).

FastAPI's own `/docs`, `/redoc` and `/openapi.json` are also served, and are
gated by the same middleware: a browser gets the `303` to `/login`.

## Response shapes that are not stable

These are worth knowing before building on them, because each one is a shape the
UI happens to render rather than a shape designed for a client:

| Route | Why it moves |
|---|---|
| `GET /api/state`, `GET /api/books/{id}` | `books` entries are `SELECT *` rows plus parsed `genres` and a `stages` map; the fields are the columns of the `books` and `stage_runs` tables, which change with migrations (`app/db.py:22-75`) |
| `POST /api/sweep` | `discover` and `health` are always present, carrying `null` until their interval elapsed or the force flag was set (`app/pipeline.py:137`); only `reconcile` is added to the dict conditionally, so a client testing for that key must test for `null` too (`app/pipeline.py:170-178`). The whole body is replaced by `{"skipped": ...}` when a sweep is in flight (`app/pipeline.py:136-241`) |
| `GET /api/issues` | `detail`, `sample` and `fix` are generated strings, not enumerated values (`app/main.py:628-807`) |
| `GET /api/services/{name}` | Embeds a health row and event rows verbatim, and `events_inferred` tells you whether the events were attributed or guessed |
| `GET /openapi.json` | Generated from `-> dict` annotations, so every response schema is an unconstrained object. It is accurate about parameters and silent about bodies |

JSON is not the only tell: `GET /api/state` returns every book on every call, so
its cost grows with the shelf, and the `progress` key adds one upstream query per
poll while anything is downloading.

## What writes, and what it writes

Route-level, for a reader deciding what to put behind a scheduled job:

| Route | Writes | Effect |
|---|---|---|
| `PUT /api/settings` | `credentials` rows, encrypted | Changes what the pipeline can authenticate as |
| `POST /api/auto_shelve` | `settings` row | Parks or unparks every future `shelve` |
| `POST /api/books/{id}/auto_shelve` | `books.auto_shelve` | Same, for one book |
| `POST /api/books/{id}/category` | `books.category`, `needs_review`, resets `place` | Moves the book's folder on the next sweep |
| `POST /api/books/{id}/retry/{stage}` | Stage rows | Queues one book's stage and everything after it |
| `POST /api/reconcile` | Stage rows | Queues re-shelving for anything drifted; reads Goodreads, writes nothing there |
| `POST /api/health/services/check` | `service_health` rows | Makes six network calls; changes no book |
| `POST /api/settings/test/{service}` | Nothing | One network call, nothing recorded |
| `POST /api/goodreads/login` | `browser-profile/` on the data volume; `save` writes the session state file | Starts a real browser in the container |
| `POST /api/auth/login` | An `events` row per evaluated attempt, failures and successes alike | Sign-in only; `/logout` writes nothing and just clears the cookie |
| `POST /api/sweep` | Everything the pipeline writes | Downloads into staging, renames files into the library, rescans services, writes notebook sources, moves shelves on Goodreads. The last of these is irreversible |

Only `POST /api/sweep` writes to the library tree, and only because it runs the
stages. It is not, however, the only route that reaches Goodreads:
`POST /api/reconcile` reads the account's shelves and
`POST /api/goodreads/login` drives a real browser to goodreads.com. Nothing else
writes to the library tree, and only a sweep walks it in bulk. Outside the
database the reads are small and specific — the image's noVNC directory
(`GET /vnc/{asset:path}`), the login session state on the data volume,
`categories.yml` (`GET /api/categories`) and the app's own static files, the
HTML pages and `app.js`/`app.css` being read on every page render while
`/static` is served straight off disk (`app/main.py:304`, `app/main.py:319-326`,
`app/main.py:1129-1130`).

## Dry runs

The API has no dry-run parameter on any route, no preview and no undo. The
reviewable versions live in the CLI, which is why the two disagree where a
maintenance operation has a route:

| Operation | API | CLI | Dry run |
|---|---|---|---|
| Sweep | `POST /api/sweep` (applies) | — | none |
| Reconcile shelves | `POST /api/reconcile` (always applies) | `reconcile` | CLI is a dry run unless `--apply` |
| Retry a stage | `POST /api/books/{id}/retry/{stage}` | `repair --apply` (picks the earliest retryable failure itself) | CLI is a dry run unless `--apply` |
| Rename audiobooks | — | `rename --apply` | CLI is a dry run unless `--apply` |
| Duplicate and collision report | — | `audit` | read-only, always |
| Shelf and failure report | `GET /api/state`, `GET /api/issues` | `report` | read-only |
| Re-resolve genres and re-categorise | — | `backfill-genres` | **no dry run — it writes** |
| Accounts | — | `set-password --generate`, `users`, `delete-user` | CLI only |

Subcommands, arguments and the exact blast radius of each are in
[OPERATIONS.md](OPERATIONS.md) — the table above exists only so a client author
does not go looking for an API equivalent of `--apply` that is not there.

## What this API is not

- **An API for the app's own UI.** `app/static/app.js` is the only client in the
  repository; routes and fields exist because a view needs them. There is no
  versioning and no deprecation window, so a script should tolerate a field
  appearing, moving or being renamed on any deploy.
- **Scoped.** One session is full authority: read the library, write
  credentials, start a browser inside a personal account, drive the pipeline.
  There is no read-only role and no per-resource permission. The consequences
  are set out in [SECURITY.md](SECURITY.md).
- **A way to run the CLI.** Accounts, file renames, genre backfill and the
  duplicate audit have no routes, deliberately: they are operations someone
  should watch.
- **A container for long work.** `sweep` and `reconcile` hold the request open
  for their duration. Everything else returns immediately and lets the scheduler
  do the work.
