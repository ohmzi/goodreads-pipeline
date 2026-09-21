# goodreads

Watches a Goodreads **to-read** shelf and carries each new book all the way to
a shelved, indexed library — without ever making a second copy of a file.

```
Goodreads to-read
  -> acquire           Shelfmark: search + download (ebook, audiobook)
  -> classify          one category, from the book's genres
  -> place             ebooks: rename into <Category>/
                       audiobooks: rename into Author/Title/
  -> index             rescan Kavita, BookLore, Grimmory, Audiobookshelf
  -> notebook          add to the matching Open Notebook notebook
  -> verify            confirm each service really sees the file
  -> shelve            move off to-read onto collected-pdf / -audiobook / both
```

Two periodic jobs run alongside that chain: `discover` reads the shelf and
`reconcile` checks what we recorded against what Goodreads actually shows.

Both `place` paths are a same-filesystem rename, never a copy.

Reference documentation lives in `docs/` — [architecture](docs/ARCHITECTURE.md),
[pipeline stages](docs/PIPELINE.md), [configuration](docs/CONFIGURATION.md),
[operations](docs/OPERATIONS.md), [security](docs/SECURITY.md), the
[HTTP API](docs/API.md) and the [version page](docs/VERSION.md). This file is the
design rationale: why things are the way they are, and what went wrong on the way.

## Quick start

```bash
git clone https://github.com/ohmzi/goodreads-pipeline.git
cd goodreads-pipeline
cp .env.example .env
python3 -c "import secrets; print(secrets.token_urlsafe(48))"   # paste into GOODREADS_SECRET_KEY
$EDITOR .env                                                     # set GOODREADS_USER_ID
chmod 600 .env
docker compose up -d --build
```

Then open `http://<host>:8091` and sign in.

Library paths and any extra networks belong in a `docker-compose.override.yml`
beside the compose file, which is gitignored — the committed compose uses
placeholders so a clone starts without editing it. See
[docs/OPERATIONS.md](docs/OPERATIONS.md).

### Creating a login

The UI is protected, so a login has to exist before you can use it:

```bash
docker compose exec goodreads python -m app.cli set-password you --generate
docker compose exec goodreads python -m app.cli users
docker compose exec goodreads python -m app.cli delete-user someone
```

`--generate` prints a strong password once and never stores it recoverably;
omit it to type your own (minimum 12 characters). There is deliberately no
default account and no "first visitor becomes admin" path.

## Security

This app is the most sensitive service you will run — it holds the key that
decrypts every stored credential, and it can drive a browser signed into a real
personal account. What that buys:

- **Passwords** are hashed with scrypt (memory-hard) and compared in constant
  time. Nothing reversible is stored, because unlike a service API key there is
  never a reason to read a password back.
- **Sessions** are stateless HMAC-SHA256 tokens, verified before the payload is
  parsed, so there is no session table to leak. They last 7 days.
- **The cookie** is `HttpOnly` and `SameSite=Lax`. Lax is what stops another
  site POSTing to `/api/...` with your cookie attached. Set `COOKIE_SECURE=1`
  once this is only ever reached over HTTPS — leaving it on over plain HTTP
  makes the browser drop the cookie and sign-in appears to fail silently.
- **Failed logins** are throttled by username *and* by client address with a
  progressive delay — three free attempts, then a doubling wait up to eight
  seconds. Deliberately not a hard lockout, so nobody can shut the owner out of
  their own UI by hammering a known username. A nonexistent username is verified
  against a dummy hash so it costs the same as a wrong password.
- **The noVNC desktop is not exposed.** x11vnc binds to loopback inside the
  container and the app bridges to it over a session-checked WebSocket, so
  `8091` is the only published port. This was previously a real hole: noVNC
  was published on `0.0.0.0:6080`, which would have let anyone on the LAN
  drive a browser logged into Goodreads.
- All of `/api`, the pages, `/static` and `/vnc` require a session. Only
  `/login`, `/api/auth/login`, `/api/auth/status`, `/api/health` and
  `/favicon.ico` are open.

### Known limits

- The session cookie is a bearer token: anyone who reads it is you, until it
  expires. Each token carries a password epoch, so changing a password signs out
  every outstanding session for that user.
- Login throttling is in-memory, so restarting clears the accumulated delay.
- Traffic is plain HTTP unless you put a TLS terminator in front.

## First run

Once you are signed in:

1. **Settings** — paste the API keys and passwords, and use the **Test** buttons
   until each service reports ✓.
2. **Goodreads login** — press *Start browser*, sign in (Amazon OTP/CAPTCHA and
   all), then *Save session*. This is a one-off; see below for why.
3. **Sweep now** — or just wait; it sweeps every `POLL_INTERVAL` seconds.

## Why the Goodreads login is manual

Goodreads removed its public API in 2020, then removed public shelf pages
(between 2026-01-13 and 2026-02-09), and `/user/sign_in` is now a stub that
hands off to Amazon's AP flow. There is no form to POST a password to — every
maintained Goodreads tool today reuses a persistent browser session instead.

So goodreads runs Chromium on a virtual display inside its own container and
streams it to the login page over noVNC. You sign in once; the cookies are
saved to `/data/goodreads_state.json` and everything afterwards runs over plain
HTTP with those cookies. When the session eventually expires the book list says
so and you come back and do it again.

## No duplicates — how it is actually guaranteed

There is one file per book, at one path, and every app reads it in place.

| App | Host mount | Its own path |
|---|---|---|
| Shelfmark | `…/Complete/Books` | `/books` |
| Kavita | `…/Complete/Books` | `/books` |
| BookLore | `…/Complete/Books` | `/books` |
| Grimmory | `…/Complete/Books` | `/books` |
| Open Notebook | `…/Complete/Books` | `/app/data/uploads/library` (ro) |
| Audiobookshelf | `…/Complete/Audiobooks` | `/data/<library>/Complete/Audiobooks` |

Three deliberate choices hold that up:

- **Placement is `os.rename`**, not a copy. `pathing.move_into_place` raises
  rather than falling back to a copy if the move would cross a filesystem, so a
  duplicate is impossible rather than merely unlikely.
- **Open Notebook is fed a `file_path`**, never an upload. Its multipart
  `/api/sources` route would store its own copy of the epub. It also refuses
  symlinks — `_build_content_state` rejects any path that `.resolve()` escapes
  from its uploads root — which is why the real file is moved rather than linked.
- **The index stage only rescans.** No app imports or re-stores anything.

## The UI

Four views, plus per-book and per-service detail pages, themed after Goodreads —
warm brown surfaces, cream text, one gold accent, serif titles. No framework and
no build step: it is a few render functions and a polling loop, served straight
from `app/static/`.

| View | What it is for |
|---|---|
| **Dashboard** | Counts at a glance, service health, and the short list of things that actually need a human. |
| **Books** | Filter by *needs attention* / in progress / completed / genre review, or search. Each row shows the stage that is blocking it and why. |
| **Book detail** | Every stage with its status, attempt count, real error, and a Retry button — plus the file paths and that book's log. |
| **Services** | Per-service health, credential entry, and Test buttons. |
| **Activity** | The full event log. |

### Failures arrive with their cause and their fix

A failure is only useful if you can act on it, so every failed stage carries a
`failure_kind` — `auth`, `network`, `server`, `busy`, `data` — and the UI groups
failures by *cause*, not by book. Twenty books with no audiobook available is
one fact about the world, not twenty problems, and it is shown separately from
the handful of things you can actually fix.

Each group carries a suggested next step in plain language ("Credentials
rejected — open Services and use Test to fix this one"), and a Retry all button
for the classes of failure where retrying helps.

### Knowing when authentication fails

This was the weakest part of the design: a bad credential used to be invisible
until a book happened to reach the stage that used it, and it surfaced as an
opaque stage error.

- Every service is probed **on a timer** (5 minutes) and the result is stored,
  so a broken key is visible within minutes rather than hours.
- The probe is an **authenticated** call — listing libraries, listing
  notebooks — never a bare health endpoint. A wrong API key against an open
  `/health` would otherwise report green, which is the exact failure this
  exists to catch.
- A failing credential raises a **red banner on every page** naming the service
  and what it breaks, with a link straight to Settings.
- Saving credentials re-probes immediately and toasts the result, so you learn
  whether they work while you are still looking at the form.
- `auth` failures are visually distinct from everything else (a bold
  `credentials` chip rather than a generic `failed`).

## Failure handling

Every failure the pipeline can produce has a known cause and, where one exists,
an automatic remedy. This section is the reference for "why is this book
stuck", and each entry says what the app does about it by itself.

### Self-healing built into the stages

| Symptom | Cause | What the app does |
|---|---|---|
| `no ebook/audiobook releases found` | Nothing exists in any source — a fact, not a fault | Book is marked **Partial**, not failed, and kept out of the attention list |
| `found N release(s) … but every one was the wrong format` | The indexer answered an audiobook query with films | Releases are filtered on the indexer's own Newznab category, on size, and on video-release name patterns before ranking |
| `Shelfmark reported error: …` | The chosen release is dead (expired NZB, failed RAR, dead mirror) | The failed release is recorded and the **next-best candidate** is queued automatically, up to 4 releases |
| `Shelfmark's queue is empty and our task is gone` | Shelfmark keeps its queue **in memory**, so a restart drops it | The abandoned task is re-queued once it has been missing for 10 minutes (`QUEUE_ABANDON_SECONDS`), rather than waiting out a longer timeout |
| `every indexer failed` (HTTP 503 / 429) | An upstream source is down, rate-limited, or out of API quota | Forgiven for **24 hours**, measured from when the outage started — not from how many times we happened to retry |
| `nothing matching it is in /books/newDownloads yet` | `place` was re-run after the file had already been filed | `place` checks the destination first, so re-running it is a no-op instead of an error |
| `source N was created but is not attached to …` | Open Notebook's attach endpoint appends rather than being idempotent | Attachment is verified by reading the source back; duplicates are collapsed |
| `MISSING from Kavita…` | A service silently skipped the file on a scan | A rescan is forced and re-checked, up to 3 times, before a human is asked |
| `within 10 min of a rescan, so this is probably still indexing` | Rescans are asynchronous | A miss inside the grace window blocks rather than fails, keeping retries for real gaps |

### Configuration that had to be right

Three of these were silent misconfigurations rather than code faults, and all
three are worth knowing about because they are not obvious from any UI:

- **`pdf` was missing from Shelfmark's `SUPPORTED_FORMATS`** (it shipped with
  epub/mobi/azw3/fb2/djvu/cbz/cbr). Shelfmark would find a book, then refuse it
  with *"format not supported (.pdf). Enable in Settings"*. Added.
- **AudiobookBay ships disabled with no hostname.** Enabling it
  (`ABB_ENABLED=true`, `ABB_HOSTNAME=audiobookbay.lu`) took *Atomic Habits* and
  *The 7 Habits* from **0 audiobook releases to 7 and 8**. Prowlarr's single
  indexer had 0–1 audiobooks per title against 27–30 ebooks, which is why so
  many books looked like they had no audiobook at all.
- **SABnzbd's `audiobook` category must complete into a staging folder**, not
  into the Audiobookshelf library, or raw usenet names become library entries.

### The one that was actively harmful

Shelfmark's audiobook search returns whatever Prowlarr gives it, and Prowlarr
was answering audiobook queries with **films**. The ranker picked the best title
match, so it queued `Five.Feet.Apart.2019.2160p.MA.WEB-DL…` — 24.6 GB — as an
audiobook. **88.5 GB of movies** accumulated in the audiobook staging folder
before this was caught. The category filter exists because of it, and it is the
reason `reject_reason()` checks the indexer's category before anything else.

### The scheduler is bounded and concurrent

A sweep used to walk every book, one at a time, before finishing. With three
hundred books and a multi-second upstream search per acquisition that meant a
single sweep ran for **hours**, its summary was never published, and the UI
looked idle while the pipeline was in fact grinding along invisibly. It was
also measured at *one book per two minutes* — twelve hours to clear.

Now each sweep gets a **120-second budget** and advances up to **6 books
concurrently** (the work is nearly all network waiting). A pass now covers ~185
books and publishes its result, so progress is steady and visible. Books are
served least-recently-touched first, so a bounded pass still rotates through
the whole list rather than re-trying the same head of it.

### Why transient failures are forgiven on a clock

The retry budget for a temporarily unavailable source is **time**, not a count.
Retrying costs a couple of seconds, so a count budget is burned through in
minutes by an outage that lasts hours — and books were being marked
*permanently* failed before the source ever came back. How long something has
been down is what actually separates "temporarily unavailable" from "broken".

### Commands

```bash
python -m app.cli report            # what completed, what failed, and why
python -m app.cli repair            # fix notebook links and stuck stages (dry run)
python -m app.cli repair --apply
python -m app.cli backfill-genres   # re-resolve genres and re-categorise
```

## Configuration

`app/categories.yml` decides where a book goes. A book resolves to **exactly
one** category: its genres are scanned in the source's own relevance order and
the first one matching a rule wins, with the longest rule taking precedence so
"science fiction" beats "fiction" regardless of file order.

### Genres come from a chain of sources, not one

Genre lookup walks four providers and stops at the first that answers — a
single source is a single point of failure, and Goodreads in particular returns
nothing for books it does not know:

| # | Source | Notes |
|---|---|---|
| 1 | **Goodreads** (AppSync) | Ranked by popularity, so best quality. Keyless by default; the key rotates, so it can be overridden in Settings. |
| 2 | **OpenLibrary** | Keyless. Its `search` endpoint carries rich subjects where the plain edition endpoint usually has none. |
| 3 | **Google Books** | Good BISAC-style categories, but rate-limits hard unkeyed — only tried when an API key is set in Settings. |
| 4 | **Embedded** | `dc:subject` read out of the epub itself. Fully offline, so it works when every API is down. About a third of the epubs in this library carry them, and when present they are good ("Historical fiction", "Mystery fiction", "Science"). |

Each provider is isolated: one raising or timing out cannot stop the next.
Every attempt is recorded, so a book still on the fallback reports exactly
which sources were tried and what each said.

If all four come up empty, a final pass matches the **title** against the same
rules — "Software Architecture with Kotlin" is unmistakably Technical. That
result is stored with `genre_source = title`, so an inferred category is always
distinguishable from a sourced one.

**`genre_source` is recorded per book** (goodreads / openlibrary / googlebooks
/ embedded / title) and shown in the UI.

A book matching nothing falls back and is flagged **needs review** rather than
being quietly misfiled. `docker compose exec goodreads python -m app.cli
backfill-genres` re-resolves every book missing genres and re-runs
classification — it is also the repair command if a category ever looks wrong.

The file ships in the image, but `/data/categories.yml` overrides it if present
— so tuning survives a rebuild.

## Maintenance commands

```bash
docker compose exec goodreads python -m app.cli audit
docker compose exec goodreads python -m app.cli rename            # dry run
docker compose exec goodreads python -m app.cli rename --apply
docker compose exec goodreads python -m app.cli reconcile         # dry run
docker compose exec goodreads python -m app.cli reconcile --apply
```

`audit` is read-only: it reports duplicate audiobooks and titles that appear in
more than one place. Duplicates are found by size and then by a cheap SHA-256 of
the first and last 1 MiB, so treat the result as strong evidence rather than
proof. AppleDouble `._` stubs are ignored — this library has thousands of them
and they would otherwise read as duplicates.

`rename` moves loose and badly-named audiobooks into `Author/Title`, using
embedded tags and filename parsing together. Neither alone is reliable:
audiobook `artist` tags are frequently the **narrator** ("Aldous Huxley - 2008
- Brave New World" tags as `Michael York`), while filenames are sometimes
reversed ("Based on a True Story - Norm Macdonald"). So when the filename's
leading segment matches the tag's *title*, the tags are trusted; when the
filename yields an author that appears nowhere in the tag author, the filename
wins. Each proposal prints which rule fired. It changes nothing without
`--apply`.

## Layout

```
app/
  main.py        FastAPI routes + UI hosting
  pipeline.py    the scheduler and per-book stage machine
  stages/        classify, acquire_ebook, acquire_audiobook, place, index,
                 notebook, verify, shelve — plus discover, the shelf reader
  clients/       one thin wrapper per service
  goodreads.py   session, shelf scraping, genre lookup, shelf writes
  pathing.py     rename-not-copy, filename matching
  cli.py         the nine maintenance subcommands
  categories.yml the rule map
```

## Upstream fixes this depends on

All four are applied here; recorded because they are not obvious from the code
and would otherwise be silently re-broken.

- **Shelfmark's audiobook destination was unmounted.** `docker-compose.dev.yml`
  mounted only `/books` while `plugins/downloads.json` set
  `DESTINATION_AUDIOBOOK: "/audiobook"`, so every audiobook transfer wrote into
  the container's writable layer and vanished on the next recreate. Fixed by
  adding `…/Complete/Audiobooks:/audiobook`; verified writable from inside.
- **SABnzbd completed audiobooks straight into the Audiobookshelf library.**
  Its `audiobook` category pointed at `/data/<library>/Complete/Audiobooks/`,
  the live ABS library root, so raw usenet names became library entries before
  anything organised them — the origin of the flat mess. Repointed at
  `…/Audiobooks/.incoming/`.
- **`FILE_ORGANIZATION_AUDIOBOOK` was `rename`**, which drops files flat instead
  of building `Author/Title`. Set to `organize`.
- **Anna's Archive is unparseable** by current Shelfmark, so
  `DEFAULT_RELEASE_SOURCE: direct_download` returns zero releases. That setting
  only chooses which tab the UI opens on — `GET /api/releases?provider=manual`
  searches every enabled source regardless — so ebook acquisition was never
  actually blocked by it. Set to `libgen` anyway so the UI opens on a source
  that works.

### Why goodreads places audiobooks itself

The obvious design was to let Shelfmark organise audiobooks into `Author/Title`
and have goodreads just verify. It does not work in practice: Shelfmark sees
the files at `/audiobook`, while SABnzbd reports their paths as
`/data/<library>/...`, and that namespace mismatch defeats Shelfmark's move
step. So `place` does the move itself — from `.incoming`, or from the Audiobooks
root for anything downloaded before that change.

`place` only ever moves entries sitting *directly* in a staging root. Anything
already nested under an author folder is treated as organised, which keeps the
stage idempotent and stops it re-shuffling a library that is already fine.

If two differently-named copies of one book are found, only the best match is
moved and the other is left alone — merging two rips automatically would be
worse than leaving them. `audit` reports those.

## License

Apache-2.0 — see [LICENSE](LICENSE).

Copyright 2026 Omar
