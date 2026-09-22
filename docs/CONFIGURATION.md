# Configuration

Everything here is either an environment variable read once at import into
`app/config.py`, or a row in the `settings` and `credentials` tables. There is
no runtime config file other than `app/categories.yml`, which is a rule table
rather than a set of settings.

The app reads plain environment variables (`app/config.py:21-22`). It does not
parse `.env` itself — `python-dotenv` is not in `requirements.txt`. `.env`
arrives because of the `env_file` stanza in `docker-compose.yml`. Running
uvicorn outside the container means exporting these yourself.

Secrets are split by lifetime:

- `.env` holds what must exist before the database can be read: the key that
  signs sessions and encrypts credentials, and optionally the Goodreads user id.
- Service credentials — API keys, usernames, passwords — are never environment
  variables. They are typed into the Settings page, encrypted with Fernet, and
  stored in the `credentials` table under a key derived from
  `GOODREADS_SECRET_KEY` (`app/crypto.py:19-26`).

## .env

```bash
cp .env.example .env
python3 -c "import secrets; print(secrets.token_urlsafe(48))"   # -> GOODREADS_SECRET_KEY
chmod 600 .env
```

`.env` must be mode 600. It is the only file in the tree that holds a
plaintext secret, and it is excluded along with `.env.*` by
`.gitignore:1-4`, which re-includes `.env.example` through `!.env.example`.

`.env.example` covers identity and the behaviour knobs a new install is likely
to change. It is not the full set. These are read by `config.py` and are absent
from it:

| Variable | Where it is set instead | Default if unset |
|---|---|---|
| `DATA_DIR` | `docker-compose.yml:18` | `/data` |
| `BOOKS_ROOT` | `docker-compose.yml:19` | `/books` |
| `AUDIOBOOKS_ROOT` | `docker-compose.yml:20` | `/audiobooks` |
| `OPENNOTEBOOK_LIBRARY_ROOT` | `docker-compose.yml:23` | `/app/data/uploads/library` |
| `STAGING_DIRNAME` | nowhere | `newDownloads` |
| `ABS_LIBRARY_ROOT` | `docker-compose.override.yml` or `.env` | `/library/Audiobooks` |
| `AUTO_SHELVE_DEFAULT` | nowhere | `1` |
| `TRUST_PROXY` | nowhere | `0` |
| `SHELFMARK_URL` … `SABNZBD_URL` | nowhere | container-name URLs |

Two more are read by `docker compose` itself rather than by the app, so they
belong in `.env` and are covered under *Operational gotchas* below:

| Variable | What it decides | Default if unset |
|---|---|---|
| `PUBLISH_HOST` | Host address the UI is published on | every interface |
| `PUBLIC_ORIGIN` | What the app is reached *as*, for the same-origin check | the request's own `Host` |

`ABS_LIBRARY_ROOT` has no `.env.example` entry and no entry in the tracked
compose file, so the code default applies until someone sets it. That default is
the literal `/library/Audiobooks` (`app/config.py:63-67`), which is a placeholder
rather than a guess at your layout. Set it in `docker-compose.override.yml` or
`.env` to the path Audiobookshelf itself sees the library at; a value that does
not match makes every audiobook silently fail to index. See *Library roots*
below.

## Identity

| Variable | Type | Default | Effect |
|---|---|---|---|
| `GOODREADS_SECRET_KEY` | string | empty | Signs session cookies (`app/auth.py:92-95`) and derives the Fernet key that encrypts the credentials table (`app/crypto.py:31-37`). Empty means `cipher()` and `_signing_key()` raise, so no credential can be stored and no session can be issued. Rotating it invalidates every stored credential and every outstanding session. |
| `GOODREADS_USER_ID` | string | empty | The numeric Goodreads user id whose shelf is watched. Optional: `resolve_user_id` checks the `goodreads_user_id` row in `settings` first, then this variable, then detects the id from the saved browser session and stores it (`app/goodreads.py:102-130`). Set it only to override detection or to pin a specific profile. |

## Storage

| Variable | Type | Default | Effect |
|---|---|---|---|
| `DATA_DIR` | path | `/data` | State directory. Holds `goodreads.db` (`app/config.py:116-118`), `goodreads_state.json`, the Playwright profile (`app/login.py:88`), and the optional `categories.yml` override. |
| `BOOKS_ROOT` | path | `/books` | Ebook library root. Category folders are created under it (`app/stages/place.py:160`), the free-space readout measures it (`app/main.py:509`), and the staging directory is derived from it. |
| `AUDIOBOOKS_ROOT` | path | `/audiobooks` | Audiobook library root, and the directory the free-space floor is checked against (`app/stages/acquire.py:149`). |
| `STAGING_DIRNAME` | string | `newDownloads` | Directory name, relative to `BOOKS_ROOT`, where Shelfmark drops finished ebook downloads. Used to build `staging_dir`. |
| `OPENNOTEBOOK_LIBRARY_ROOT` | path | `/app/data/uploads/library` | Where **Open Notebook** mounts the same host tree. Paths sent to its API are rewritten into this namespace. |
| `ABS_LIBRARY_ROOT` | path | `/library/Audiobooks` | Where **Audiobookshelf** sees the audiobook tree. The default is a placeholder, not a working value — it has to be set per deployment (`docker-compose.override.yml` or `.env`) to match how Audiobookshelf mounts the library. Paths are rewritten into this namespace before ABS is asked which library holds a file. |

Derived, not settable (`app/config.py:111-125`):

| Property | From |
|---|---|
| `staging_dir` | `BOOKS_ROOT / STAGING_DIRNAME` |
| `db_path` | `DATA_DIR / goodreads.db` |
| `categories_path` | `DATA_DIR / categories.yml` if that file exists, else the copy shipped in the image |

## Behaviour

| Variable | Type | Default | Effect |
|---|---|---|---|
| `MIN_FREE_SPACE_GB` | int | `50` | Floor checked before an **audiobook** download is queued; below it the stage fails with `kind="data"` (`app/stages/acquire.py:149-155`). The ebook path is not checked. |
| `POLL_INTERVAL` | int | `15` | Seconds between scheduler sweeps, passed to `Scheduler` at import (`app/main.py:57`). |
| `BIND` | string | `0.0.0.0` | Read into `settings.bind`. Nothing in the app reads it back: uvicorn is started by the Dockerfile with a literal `--host 0.0.0.0` (`Dockerfile:46`). Inert unless the container command is changed. |
| `PORT` | int | `8090` | Same: read into `settings.port`, never read back. The Dockerfile hardcodes `--port 8090` and `EXPOSE 8090` (`Dockerfile:42-46`). |
| `AUTO_SHELVE_DEFAULT` | bool | `1` | Read into `settings.auto_shelve_default`, which nothing reads. The global switch is the `auto_shelve` row in `settings`, defaulting to `"1"` (`app/stages/shelve.py:34-35`, `app/stages/discover.py:60`) and toggled from the UI. |
| `COOKIE_SECURE` | bool | `0` | Sets the `Secure` flag on the session cookie (`app/main.py:382`). See *Operational gotchas*. |
| `TRUST_PROXY` | bool | `0` | Makes rate limiting use `X-Forwarded-For` instead of the socket peer (`app/main.py:252`). See *Operational gotchas*. |
| `PUBLIC_ORIGIN` | string | empty | Compared against a request's `Origin` on state-changing requests and on the VNC handshake, in place of the request's own `Host` (`app/main.py:122`). Set it behind a proxy that rewrites `Host`. See *Operational gotchas*. |

Parsing (`_env` and `_env_int` in `app/config.py:21-30`, and the `== "1"`
boolean fields) is literal:

- Every string is `.strip()`ped.
- Integers fall back to the default on a `ValueError`, silently. `POLL_INTERVAL=15s` becomes 15 without complaint; a typo'd `MIN_FREE_SPACE_GB` becomes 50 and disables the floor that was being raised.
- Booleans are true only when the value is exactly `1`. `COOKIE_SECURE=true` and `COOKIE_SECURE=yes` both evaluate false.

## Service endpoints

Defaults point at container names on the shared docker networks, never
`localhost:<published-port>` — ufw's default-deny blocks container-to-host
published ports, so a localhost URL resolves to the container itself and fails
in a way that reads as the service being down (`app/config.py:1-7`).

| Variable | Default | Consumer |
|---|---|---|
| `SHELFMARK_URL` | `http://shelfmark:8084` | search and download (ebook, audiobook) |
| `KAVITA_URL` | `http://kavita:5000` | index + verify |
| `BOOKLORE_URL` | `http://booklore:6060` | index + verify |
| `GRIMMORY_URL` | `http://grimmory:6060` | index + verify |
| `ABS_URL` | `http://audiobookshelf:80` | audiobook index + verify |
| `OPENNOTEBOOK_URL` | `http://open_notebook:5055` | notebook stage |
| `SABNZBD_URL` | `http://sabnzbd:8080` | diagnostics only |

Base URLs and credential keys are joined into a cache key
(`app/clients/booklore.py:55`), so changing a URL re-probes with a clean slate.
All seven are surfaced read-only through `_service_urls()`
(`app/main.py:910-919`); none of them affect which credentials are used.

## Credentials stored in the database

The `credentials` table holds key/value rows with the value encrypted
(`app/db.py:77`, `452-470`). Keys the UI renders are listed in
`CREDENTIAL_FIELDS` (`app/main.py:62-74`):

| Key | Service | Required for |
|---|---|---|
| `kavita_api_key` | Kavita | index, verify |
| `booklore_username`, `booklore_password` | BookLore | index, verify |
| `grimmory_username`, `grimmory_password` | Grimmory | index, verify |
| `abs_username`, `abs_password` | Audiobookshelf | index, verify |
| `opennotebook_password` | Open Notebook | notebook (leave blank if its auth is disabled) |
| `sabnzbd_api_key` | SABnzbd | none; storable and displayed, never read |
| `prowlarr_api_key` | Prowlarr | none; storable and displayed, never read |
| `goodreads_appsync_key` | Goodreads | genre lookup; falls back to the key embedded in the code |
| `googlebooks_api_key` | Google Books | optional; without it that provider is skipped |
| `hardcover_api_key` | Hardcover | optional, not yet used |

`sabnzbd_api_key` and `prowlarr_api_key` have no consumer: they are listed in
`CREDENTIAL_FIELDS` (`app/main.py:61-75`) so they can be typed into Settings and
shown back, and no other code path references either name.

A missing credential is reported as `kind="auth"` rather than an outage, so it
lands in the credentials group in the UI instead of being forgiven as a
transient failure (`app/clients/base.py:60-73`). Losing `GOODREADS_SECRET_KEY`
makes every row undecryptable and each one has to be re-entered; the error
raised says so (`app/crypto.py:38-50`).

## Library roots

Four variables describe the same two host directories as seen from four
different mount namespaces. Rewriting between them is the whole reason they
exist.

| Goodreads sees | Service sees | Where the rewrite happens |
|---|---|---|
| `BOOKS_ROOT` (`/books`) | Shelfmark, Kavita, BookLore, Grimmory mount the same host directory at `/books` | no rewrite needed |
| `BOOKS_ROOT` | Open Notebook at `OPENNOTEBOOK_LIBRARY_ROOT` | `to_opennotebook_path()` (`app/clients/opennotebook.py:171-185`) |
| `AUDIOBOOKS_ROOT` (`/audiobooks`) | Audiobookshelf at `ABS_LIBRARY_ROOT` | `library_for_local_path()` (`app/clients/abs_client.py:92-110`) |

Open Notebook validates every path it is given against its own uploads root and
rejects anything that escapes it, so a path expressed in Goodreads' namespace is
refused outright. Audiobookshelf is the one app that mounts the audiobook
directory at its own full host path rather than a neutral one, so comparing its
paths to ours matches nothing and the audiobook never indexes.

Both are namespaces, not preferences: each value has to equal how the other
service actually mounts the tree. A value that merely looks plausible produces
the symptom of a scan that finds nothing.

`staging_dir` sits inside `BOOKS_ROOT` for a related reason. `place` moves
files with `os.rename` and raises on `EXDEV` rather than falling back to a copy
(`app/pathing.py:276-302`), so staging and the category folders have to be on
one filesystem. Splitting them across mounts turns every placement into a
hard failure, which is the intended behaviour.

## Operational gotchas

**`COOKIE_SECURE=1` over plain HTTP breaks login.** The cookie is issued with
`secure=True`, the browser refuses to send it back over `http://`, and the
sign-in page reloads as if nothing happened — no error, because from the
server's perspective no request ever arrived with a session. Turn it on only
once goodreads is reachable over HTTPS exclusively (`app/main.py:194-198`,
[SECURITY.md](SECURITY.md)).

**`TRUST_PROXY=1` without a proxy hands out a rate-limit bypass.** Client
addresses for login throttling come from `X-Forwarded-For` when this is on
(`app/main.py:120-133`). That header is set by the caller. With no proxy in
front to overwrite it, a client rotates the value on each attempt and the
per-address throttle never fires, leaving only the per-username counter. Enable
it when a reverse proxy you control rewrites the header, and not otherwise.

**`PUBLISH_HOST` decides which of the host's addresses the UI answers on**, and
getting it wrong is an outage rather than a warning. It is interpolated into the
compose `ports:` mapping, so it is read by `docker compose` rather than by the
app, and it lives in `.env` rather than `docker-compose.override.yml` because
`ports: !override:` does not work: Compose merges port lists by
`(target, published, protocol)` and ignores the tag, so an override publishes
its mapping *and* the base file's (verified on Compose v2.36.2, which does honour
`!override` on ordinary fields).

Unset, the port is published on every interface — and a published port is
**not** covered by ufw, because docker's iptables rules are evaluated before the
firewall's. So narrowing is worth doing, and only after checking what the front
end dials:

```bash
docker compose logs goodreads | grep 'GET /login'
```

The left-hand address is the front end. The trap: a proxy or tunnel running in a
**container** reaches you as its bridge gateway (`172.x.0.1`), never as
`127.0.0.1` — so `127.0.0.1` is correct only for a proxy running on the host
itself, and a containerised one needs `0.0.0.0`. Set it wrongly and every
request is refused before it reaches this app, so nothing appears in the app's
own logs at all. See [SECURITY.md](SECURITY.md) and `.env.example`.

This is also not what makes the app reachable from the internet: a tunnel in
front reaches it whichever value is set. See *Reverse-proxy deployment* in
[SECURITY.md](SECURITY.md).

**`PUBLIC_ORIGIN` is required behind a proxy that rewrites `Host`.** The
same-origin check on state-changing requests and on the VNC handshake compares a
request's `Origin` against the host it was sent to (`app/main.py:122`). A proxy
that terminates TLS on a public name and forwards to `goodreads:8090` makes
those two disagree, so every such request is refused with 403 and the VNC panel
never connects — with nothing on screen connecting that to the proxy. Set it to
the origin a browser actually uses, scheme and port included, no trailing slash.

**`BIND`, `PORT` and `AUTO_SHELVE_DEFAULT` do nothing** as shipped. The first
two are overridden by the container command; the third is a dead read. Use the
UI toggle for auto-shelve and the compose port mapping for the listen port.

**Boolean and integer typos fail quietly.** See *Behaviour* above.

## categories.yml

The rule table that decides where a book lands. `categories_path` prefers
`DATA_DIR / categories.yml` and falls back to the copy baked into the image
(`app/config.py:120-125`), so tuning survives a rebuild by living in the data
directory. The file is re-read on every call rather than cached
(`app/config.py:127-129`), so an edit takes effect on the next book that
classifies, with no restart.

Three top-level keys:

| Key | Shape | Meaning |
|---|---|---|
| `fallback` | category key | used when nothing matches; the book is flagged `needs_review` |
| `categories` | key → `{folder, notebook}` | folder is the directory name under `BOOKS_ROOT`; notebook is the Open Notebook notebook name |
| `genres` | genre needle → category key | the matching rules |

Folder and notebook are independent fields, and the shipped file encodes a
mismatch rather than deriving one from the other: Fantasy and Fiction share both
`folder: Fiction` and `notebook: Fictional` (`app/categories.yml:25-30`), while
History files into the `History` folder but the `historical` notebook
(`app/categories.yml:31-33`). An empty `notebook` means the notebook stage skips
that category.

Matching is substring, case-insensitive, and longest needle first
(`app/stages/classify.py:22-38`). Sorting by length rather than trusting YAML
order is what lets `science fiction` beat `fiction` and `space opera` beat
`space` regardless of where either line sits in the file. A book resolves to
exactly one category: genres are scanned in the source's own relevance order
and the first genre that hits any rule wins.

When every genre source comes back empty, `resolve_from_title` re-reads the same
`genres` map and matches those same needles against the book title
(`app/stages/classify.py:40-57`). One rule set serves both positions, so a needle
has to be safe in both, which is why the shipped file keeps the title-facing
entries multi-word or unambiguous — a bare `architecture` would sweep in books
about buildings (`app/categories.yml:91-105`).

## Genre resolution

Four providers are tried in order and the chain stops at the first that returns
anything (`app/genres.py:187-192`). Order is deliberate: the classifier takes
the first genre that matches a rule, so a source returning genres in genuine
relevance order is worth more than one returning an unordered set.

| # | Source name | Requirements | Notes |
|---|---|---|---|
| 1 | `goodreads` | book has a `goodreads_id` | Ranked by popularity. Uses the `goodreads_appsync_key` credential when set, otherwise the literal at `app/goodreads.py:52`. That literal is Goodreads' own public web-client key, not this app's secret, and it is kept on purpose; any credential you store overrides it. |
| 2 | `openlibrary` | none | Queried by ISBN, else title plus first author. Subjects are folksonomy, so `app/genres.py:42-47` filters out New York Times lists, format names and other noise before they can substring-match a rule. |
| 3 | `googlebooks` | `googlebooks_api_key` set | Skipped entirely without a key, because an unkeyed call is rate-limited almost immediately (`app/genres.py:112-116`). |
| 4 | `embedded` | a real epub on disk | `dc:subject` read out of the file, resolved through `META-INF/container.xml`. No network. Only reachable when a caller passes a file path, which `classify` does not — `place` re-runs classification after the file lands (`app/stages/classify.py:99-116`). |

Each provider is isolated: one raising or timing out records
`<name>: error <Type>` and the chain continues. Every attempt is recorded in
`tried`, which is what the UI shows for a book still sitting on the fallback.

The source that answered is stored per book as `genre_source` — one of
`goodreads`, `openlibrary`, `googlebooks`, `embedded`, `title`, or empty when
genres were stored directly by `discover`.

## Container mapping

| Host | Container | Notes |
|---|---|---|
| published `8091` | `8090` | uvicorn listens on 8090 inside the container; the host port differs because another service on the host already holds 8090 (`docker-compose.yml:24-29`). |
| `/path/to/books` | `/books` | matches `BOOKS_ROOT` |
| `/path/to/audiobooks` | `/audiobooks` | matches `AUDIOBOOKS_ROOT` |
| data directory | `/data` | matches `DATA_DIR` |
| — | — | noVNC has no port of its own; x11vnc binds loopback inside the container and the app bridges to it over an authenticated WebSocket (`docker/start-vnc.sh:36-39`) |

The healthcheck runs `GET /api/health` against `localhost:8090` inside the
container (`docker-compose.yml:38-44`). That route is unauthenticated by design,
so the probe needs no session, and it is one of the five paths in
`PUBLIC_PATHS` (`app/main.py:49-54`).

The tracked compose file stays portable: relative volume placeholders, no
subnet, no external networks, and the default network only. The host-specific
half lives in `docker-compose.override.yml` — the real library paths, an explicit
subnet for the default network when the host's docker address pools are exhausted
and their nominally free slots collide with the LAN, and the networks the other
services live on (`media`, `booklore_default`, `grimmory_default`,
`open-notebook_default`). That file is gitignored and differs per host
(`.gitignore:5-6`). Compose loads it automatically, but passing any explicit `-f`
disables that loading, so keep it off the command line (`docker-compose.yml:1-8`).
