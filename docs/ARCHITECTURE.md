# Architecture

How goodreads is put together: what the pieces are, what talks to what, where
state lives, which threads run, and the two constraints that shaped the rest —
a browser that cannot share a thread and a library that every service sees at a
different path.

## Components

| Component | What it is for | How goodreads talks to it |
|---|---|---|
| **Goodreads** | Source of truth for the to-read shelf, and the final destination of a finished book | httpx with cookies scraped from a captured browser session; one AppSync GraphQL call for genres |
| **Shelfmark** | Release search and download (SABnzbd and Prowlarr sit behind it) | HTTP: `/api/releases`, `/api/releases/download`, `/api/status` |
| **The filesystem** | Holds the single copy of every file | `os.rename` only, never a copy |
| **Kavita, BookLore, Grimmory** | Index the books tree | Authenticated "list libraries" and "rescan library N" |
| **Audiobookshelf** | Indexes the audiobooks tree | Authenticated login, library list, library scan, library search |
| **Open Notebook** | Text extraction and embeddings per category | JSON source creation from a `file_path`, then notebook attach |
| **SQLite in `/data`** | System of record: books, stage progress, credentials, users, health, events | One module-level connection behind a lock |
| **goodreads itself** | FastAPI app: the scheduler, the JSON API and the single-page UI | — |

Shelfmark runs with `auth_mode=none` here, so it is the one client with no
credential (`app/clients/shelfmark.py:1-13`). SABnzbd and Prowlarr keys are
stored for diagnostics only — no pipeline stage calls either directly
(`app/main.py:70-71`).

## The pipeline

The unit of work is a book, and a book is a row of independent stages. Redrawn
from the README's chain diagram with two additions: `discover`, which that
diagram folds into its first line, and `verify`, which sits in the stage tuple
between `notebook` and `shelve` and is the reason `shelve` can be trusted:

```
Goodreads to-read
  -> discover          read the shelf, register new books
  -> Shelfmark         search + download (ebook, audiobook)
  -> classify          one category, from the book's genres
  -> place             ebooks: rename into <Category>/
                       audiobooks: rename into Author/Title/
  -> index             rescan Kavita, BookLore, Grimmory, Audiobookshelf
  -> notebook          add to the matching Open Notebook notebook
  -> verify            confirm each service actually holds the book
  -> shelve            move off to-read onto collected-pdf / -audiobook / both
```

`discover` is not a stage; it is the source of books and runs on its own timer
(`app/stages/discover.py:1-9`). Everything from `classify` down is a per-book
stage, and they are retried independently: a book with a stuck audiobook still
gets its ebook classified, placed, indexed and added to a notebook
(`app/pipeline.py:1-15`).

Prerequisite gating and irreversibility set both the order below and how many
times each stage is allowed to fail before it is handed to a human.

| Stage | Runs once these are satisfied | Attempts before parking | Why that bound |
|---|---|---|---|
| `classify` | — | 3 | Genre lookups are deterministic once resolved |
| `acquire_ebook` | — | 5 | Several releases, each of which can be dead |
| `acquire_audiobook` | — | 5 | Same, and the audiobook indexers are thinner |
| `place` | `classify` | 3 | A same-filesystem rename either works or is a real error |
| `index` | `place` | 4 | Rescans are asynchronous and occasionally skipped |
| `notebook` | `place` | 4 | Attach is verified by reading the source back |
| `verify` | `place`, `index`, `notebook` | 6 | Search endpoints lag a rescan by seconds |
| `shelve` | `place`, `verify` | 3 | The only irreversible step; it waits for proof |

`MAX_ATTEMPTS` is in `app/models.py:36-47`; the prerequisite graph is
`PREREQUISITES` in `app/pipeline.py:32-43`. Satisfied means `ok` *or*
`skipped` (`app/models.py:30`, `app/pipeline.py:30`), and `skipped` is "nothing
for this stage to do". The stages from `place` onward return it: `place` skips on
"nothing downloaded", and `index`, `verify` and `shelve` follow on the same fact
— so the chain runs to its end instead of burying the book under failures, and
`shelve` is the stage that reports it (`app/stages/place.py:65`,
`app/stages/index.py:36-40`). `notebook` skips independently of that fact: a
format Open Notebook cannot read, or a category that maps to no notebook, is
skipped with the reason rather than retried to death
(`app/stages/notebook.py:66-75`). The acquire stages never skip. A book with no
audiobook release gets `failed(kind="data")` from `acquire_audiobook`, and once
its five attempts are spent the book parks there. `acquire_audiobook` sits
ahead of `place`, `index`, `notebook`, `verify` and `shelve` in `models.STAGES`,
so a parked book stops there: the stages behind it never run again and the book
is never shelved.

The two acquire stages share a two-phase state machine, because queueing and
finishing are separated by minutes: not queued means search, rank, hand the
best release to Shelfmark; queued means watch the task and finish when
Shelfmark says complete. The marker that separates the phases is
`stage_runs.queued_at` (`app/stages/acquire.py:1-22`).

Three feedback edges exist because a stage succeeding can invalidate an earlier
one's conclusion:

- `place` succeeded as "skipped" (nothing available) while a download was still
  in flight, so a later-completing acquire resets `place`
  (`app/pipeline.py:259-274`, `:344-357`).
- An audiobook arriving after the book was shelved as ebook-only resets
  `shelve`, so it moves to the both-formats shelf (`app/pipeline.py:358-366`).
- `verify` finding a book missing resets `index`, up to 3 rounds, because
  services do skip files on a scan (`app/stages/verify.py:246-262`).

## Module map

Every file, and what it is actually responsible for.

### `app/`

| File | Responsibility |
|---|---|
| `__init__.py` | Package docstring; no code. |
| `main.py` | The FastAPI app: session middleware and public-path list, the noVNC WebSocket bridge and noVNC static serving, page rendering with an asset-version stamp, startup/shutdown, and every JSON route (`/api/state`, `/api/issues`, `/api/books/{id}`, `/api/services/{name}`, `/api/settings`, the Goodreads login routes, the SPA fallback). |
| `pipeline.py` | The scheduler: one sweep loop, the per-book stage machine (`_advance`), prerequisite gating, the time budget, transient-failure forgiveness, and the least-recently-touched ordering. |
| `db.py` | The SQLite schema, the migration that adds columns to an existing file, the stage-row backfill, and every query the app makes. |
| `models.py` | Domain vocabulary: the stage tuple and its order, the six stage statuses, `MAX_ATTEMPTS`, the three Goodreads shelf names, the `Book` and `StageResult` dataclasses. |
| `config.py` | `Settings`, a dataclass built from environment variables: identity, the storage roots and the service URL defaults, plus the derived `staging_dir`, `db_path` and `categories_path`. |
| `crypto.py` | Fernet encryption for stored credentials, keyed from `GOODREADS_SECRET_KEY`; raises rather than writing something it cannot read back. |
| `auth.py` | scrypt password hashing and constant-time verification, HMAC-signed stateless session tokens with a per-user epoch, and the in-memory login throttle. |
| `goodreads.py` | The Goodreads HTTP session: cookie jar from the stored browser state, CSRF priming, shelf scraping, the AppSync genre call, and the shelf-write sequence. |
| `genres.py` | The genre provider chain (Goodreads, OpenLibrary, Google Books, embedded epub metadata) and the lookup that walks it. |
| `health.py` | The service registry, the per-service impact and stage mappings, the authenticated probe, and the stored health summary. |
| `reconcile.py` | Reads the account's shelves and compares them against what each book's `shelve` stage claims; queues a retry for drift, changes nothing on Goodreads. |
| `login.py` | The interactive Goodreads login: a daemon thread owning a real Chromium on the virtual display, and the start/save/cancel state the UI drives. |
| `pathing.py` | Filesystem helpers: junk detection, name sanitising, the book-folder convention, disk-space check, title normalisation and matching (`title_key`, `titles_match`, `volume_number`, `match_score`), and `move_into_place`, which is the no-duplicates guarantee. |
| `cli.py` | Maintenance commands run inside the container: `audit`, `rename`, `set-password`, `users`, `delete-user`, `backfill-genres`, `report`, `repair`, `reconcile`. |
| `categories.yml` | The rule map: genres to exactly one category, and each category to a folder and a notebook name. |

### `app/clients/`

Every client here is a thin wrapper. They exist to be the one place that knows
a service's URLs, its auth shape and its JSON quirks.

| File | Responsibility |
|---|---|
| `__init__.py` | Package docstring; no code. |
| `base.py` | `ServiceClient` (httpx client, error checking, JSON helper), `ClientError` carrying service name, HTTP status and a derived failure kind, and `cred` / `require_cred` for decrypting stored credentials. |
| `indexer.py` | The shared shape for the four apps that index the library: `Library` with `contains` and `depth_of_match`, `extract_folders` to flatten four different JSON spellings of "my folders", `pick_library` (longest folder wins) and `find_by_name`. |
| `kavita.py` | Kavita: API-key header, `/api/Library/libraries`, the `?libraryId=`-based scan route, and `?queryString=` series search. |
| `booklore.py` | BookLore and, by subclass, Grimmory — same API shape, different base URL and token lifetime. Holds the module-wide token cache and the full-library cache that verification reads. |
| `abs_client.py` | Audiobookshelf: long-lived token from `/login` with one retry on 401, library list, forced scan, and the translation of our audiobook path into ABS's namespace before asking which library holds it. |
| `opennotebook.py` | Open Notebook: notebooks, source creation from a `file_path`, attach and detach, read-back state, the existing-source lookup that survives its stringified-dict `asset` field, and `to_opennotebook_path`. |
| `shelfmark.py` | Shelfmark: release search, `reject_reason` (the Newznab-category and video-name filter), ranking, queueing a download, and finding and interpreting the task in `/api/status`. |

### `app/stages/`

| File | Responsibility |
|---|---|
| `__init__.py` | `REGISTRY`, the stage name to runner mapping the scheduler drives. |
| `discover.py` | Reads the to-read shelf and upserts every book, fetching genres only for books not seen before. |
| `classify.py` | Resolves the one category from the genre list (longest rule first), the title-only fallback, `reclassify_with_file` for the second look once the epub exists, and `destination_for` (folder and notebook for a category). |
| `acquire.py` | The two acquire stages: free-space floor for audiobooks, search and rank, queue, then watch the task; records tried releases; re-queues when Shelfmark's in-memory queue has dropped the task. |
| `place.py` | Files the book: ebook renamed into `<Category>/Author - Title (Year)/` with re-classification and a re-file if the category improves; audiobook moved into `Author/Title/` from a staging root. Idempotent in both directions. |
| `index.py` | Asks each library app that owns the relevant tree to rescan; skips the ones whose tree did not change; reports partial failure without stopping the chain. |
| `notebook.py` | Creates or reuses the Open Notebook source for the placed ebook, attaches it once, and reads the source back to confirm the text is extractable. |
| `verify.py` | Searches each service for the title and records presence or absence; the one-pass scan grace window, and the bounded forced-rescan retries. |
| `shelve.py` | The irreversible step: picks the shelf from what actually landed, moves the book, and leaves it on to-read when nothing landed. |

### `app/static/`

No framework and no build step; served from disk and stamped with a content
hash so a deploy cannot be masked by a cached asset (`app/main.py:298-326`).

| File | Responsibility |
|---|---|
| `app.js` | The whole UI: hash routing, pure render functions over the last `/api/state` payload, the polling loop, and the per-view fetches. |
| `app.css` | The theme and layout. |
| `index.html` | The main shell: dashboard, books, book detail, services, activity. |
| `login.html` | The sign-in form. |
| `goodreads.html` | The Goodreads login view, embedding the noVNC desktop in an iframe pointed at `/vnc/vnc.html?...&path=vnc/ws`. |

## The database

A single SQLite file, `DATA_DIR/goodreads.db` (the `db_path` property,
`app/config.py:117-118`). Plain
`sqlite3` rather than an ORM, WAL journalling, foreign keys on, one
module-level connection with `check_same_thread=False` guarded by a re-entrant
lock (`app/db.py:1-7`, `:139-150`).

Schema creation is `CREATE TABLE IF NOT EXISTS`, which silently does nothing to
an existing table — so new columns are added explicitly by `_migrate`
(`app/db.py:152-185`). Stages added after a book exists get their row from
`ensure_stages`, run at startup; without it a new stage would have no row and
so could never record a result (`app/db.py:202-226`, `app/main.py:353-358`).

| Table | Columns | Purpose |
|---|---|---|
| `books` | `id`, `goodreads_id` (unique), `title`, `author`, `isbn`, `isbn13`, `year`, `cover_url`, `goodreads_url`, `genres` (JSON array text), `genre_source`, `category`, `needs_review`, `auto_shelve`, `discovered_at`, `updated_at` | One row per book. `genre_source` records which provider answered, so an inferred category is distinguishable from a sourced one. |
| `stage_runs` | `id`, `book_id` (FK, cascade), `stage`, `status`, `attempts`, `detail`, `failure_kind`, `service`, `held_by`, `transient_count`, `transient_since`, `artifact`, `queued_at`, `output_path`, `started_at`, `finished_at`; `UNIQUE (book_id, stage)` | The pipeline state machine. One row per book per stage. |
| `credentials` | `key`, `value` (encrypted BLOB), `updated_at` | Service credentials, entered in the UI, encrypted with the key from the environment. |
| `users` | `username`, `password_hash`, `session_epoch`, `created_at`, `updated_at` | Logins. `session_epoch` is baked into every session token, which is what makes a password change evict outstanding sessions. |
| `settings` | `key`, `value`, `updated_at` | Small key/value state: global `auto_shelve`, and the Goodreads user id once known. |
| `service_map` | `service`, `name`, `remote_id`, `updated_at`; PK `(service, name)` | Cache of a library name to its id in that service, so a rescan does not re-list libraries first. |
| `service_health` | `service` (PK), `ok`, `detail`, `failure_kind`, `checked_at`, `ok_since` | Latest probe result per service, plus how long it has been continuously healthy. |
| `service_breaker` | `service` (PK), `state`, `failures`, `trips`, `opened_at`, `open_until`, `probe_book_id`, `probe_stage`, `probe_at`, `last_failure`, `last_ok_at`, `changed_at` | The service breaker: whether a service is currently usable at all. One row per service that has ever failed, created lazily, shared by every book. See "The service breaker" in PIPELINE.md. |
| `events` | `id`, `ts`, `level`, `book_id`, `stage`, `service`, `message` | The activity log. `service` is written at the point the line is about a service, so a service's page does not have to guess from the text. |

Two indexes: `idx_stage_book` on `stage_runs(book_id)` and `idx_events_ts` on
`events(ts DESC)` (`app/db.py:130-131`).

### How stage progress is represented

`stage_runs` is the whole answer. A stage is one row, and its `status` is one
of six values (`app/models.py:26-31`):

| Status | Meaning |
|---|---|
| `pending` | Not attempted, or reset to run again |
| `running` | In flight right now |
| `ok` | Done |
| `failed` | Terminal for this attempt; parked once `attempts` reaches the stage's `MAX_ATTEMPTS` |
| `skipped` | Deliberately not applicable — nothing for this stage to do: no download found, a format nothing can read, or a category that maps to no notebook |
| `blocked` | Waiting on the outside world; retried automatically |

Blocked is the load-bearing distinction. A blocked stage has not failed, so it
must not burn an attempt — `mark_done` applies a penalty of `-1` to `attempts`
for exactly that reason, or a slow download would exhaust its retries while it
was progressing normally (`app/db.py:350-354`). `attempts` increments in
`mark_running`, which is also where `detail` is deliberately preserved rather
than cleared, because callers stash durable state there (`app/db.py:338-348`).

Parking is a decision, not a status: `_advance` returns `"parked"` and stops
working that book entirely once a stage is `failed` and out of attempts, so
nothing downstream runs either (`app/pipeline.py:279-281`). Retry from the UI
resets the stage *and everything downstream of it*, which is why `reset_stage`
walks the tail of `models.STAGES` rather than clearing one row
(`app/db.py:437-450`).

Three columns carry stage-specific state, and each one exists because losing it
caused duplication or a stall:

- `queued_at` — set when a download is handed to Shelfmark. Its presence is
  what stops the acquire stage re-queueing the same book on every sweep.
- `output_path` — where the work landed. `place` stores a JSON object,
  `{"ebook": …, "audiobook": …}`, and every downstream stage reads it from
  here; `acquire` stores the release ids already tried.
- `artifact` — the compact result: the Shelfmark task id for a completed
  acquire, and the final shelf name for `shelve`, which is what `reconcile`
  compares against Goodreads.

`failure_kind` (`auth`, `network`, `server`, `busy`, `notfound`, `data`, or
empty) is
what lets the UI separate "your credentials are wrong" from "try again later",
and `service` names the service at the point of failure rather than being
parsed back out of `detail` (`app/db.py:50-58`, `app/clients/base.py:28-49`).

Transient forgiveness has its own pair of columns. `transient_count` and
`transient_since` are bumped per forgiven failure, and `transient_since` is set
only on the first one, so it measures how long the *source* has been
unavailable rather than how many times we happened to look
(`app/db.py:386-404`). A `network`, `server` or `busy` failure becomes `blocked`
with a note about how far into the 24-hour grace it is, and stays that way until
the grace expires (`app/pipeline.py:375-403`).

`held_by` is a third, narrower thing again: set when the *breaker* held the
stage because the service is down, cleared by any other result. It is a column
rather than something inferred from "blocked, and a service is named" because
the UI has to tell three different kinds of `blocked` apart — an outage hold
(this), an in-flight download (also `blocked`, but `service` is empty) and a
one-off forgiven blip (`service` set, no `held_by`, ageing out on its own
clock).

## Threading and concurrency

Four kinds of thread run inside one uvicorn process. There is no worker
process, no task queue and no external scheduler — the app is the scheduler.

| Thread | Created in | Lifetime | What it does |
|---|---|---|---|
| `goodreads-sweep` | `Scheduler.start`, `app/pipeline.py:107-112` | Daemon, whole process | Calls `sweep()` every `POLL_INTERVAL` seconds (default 15) and survives any exception |
| `goodreads-adv[0-5]` | `ThreadPoolExecutor`, `app/pipeline.py:190-192` | Per sweep | Advances one book each; 6 at a time |
| `goodreads-login` | `LoginSession.start`, `app/login.py:54-61` | Until Save or Cancel | Owns a Chromium on the virtual display |
| uvicorn worker threads | Starlette | Whole process | Run the route handlers; anything blocking is pushed off the event loop — the login route is `async def` and dispatches scrypt to the threadpool (`app/main.py:162`) |

One sweep, in order (`app/pipeline.py:136-242`):

1. **Health**, if 5 minutes have passed — probe every service and store the
   result (`HEALTH_INTERVAL_SECONDS`, `app/pipeline.py:48-51`).
2. **Discover**, if 15 minutes have passed — read the shelf and register new
   books (`DISCOVER_INTERVAL_SECONDS`).
3. **Reconcile**, if 6 hours have passed — compare shelf records against
   Goodreads and queue retries for drift (`RECONCILE_INTERVAL_SECONDS`).
4. **Advance**, for up to 120 seconds, six books at a time.

Each of those has its own clock, so a slow book pass never delays credential
checking, and the three timers are independent of `POLL_INTERVAL`. A
non-blocking lock makes the sweep re-entrant-safe: a manual "Sweep now" during
a scheduled sweep returns `{"skipped": "a sweep is already running"}` instead
of starting a second pass (`app/pipeline.py:127-134`). The manual route passes
`force_discover=True`, so it also runs health, discover and reconcile
immediately rather than waiting for their timers (`app/main.py:895-897`).

The budget is time, not a book count. A serial sweep spent its whole budget on
one slow upstream search; measured at one book per two minutes, three hundred
and fifty books took twelve hours, the summary was never published and the UI
looked idle while the pipeline was working. A *time* budget adapts on its own —
when upstream is slow each sweep simply does less
(`app/pipeline.py:63-91`). Overrunning 300 seconds logs a warning so a
pathological sweep is findable rather than mysterious.

Books are served least-recently-touched first (`_last_touched`,
`app/pipeline.py:438-456`). The subtlety there is that the sort key must be the
*most recent* stage timestamp: taking the oldest produces a key that never
changes once set, so the same books lead every sweep. That happened — the sweep
burned its budget re-trying books stuck on an upstream quota while books that
had never been attempted sat untouched behind them.

The executor inside a sweep is bounded in two directions at once: it never
submits past the budget, and it never holds more than `ADVANCE_WORKERS`
futures, waiting on `FIRST_COMPLETED` so a finished worker is refilled
immediately (`app/pipeline.py:195-229`).

### Why the login is a thread of its own

Playwright's synchronous API cannot run on an event loop — it owns a greenlet
loop and asserts it is not inside an asyncio loop — and it is not safe to use
the same browser objects from more than one thread. Uvicorn runs the FastAPI
event loop on the main thread. Both facts together mean the browser has to be
driven from a dedicated thread that no request handler ever enters
(`app/login.py:8-11`).

That thread also has to outlive a single HTTP request by design. The login is a
human typing a password, answering an OTP, and solving a CAPTCHA, which can
take minutes; a request handler that blocked on it would be a handler that
times out. So the API is reduced to four small calls against shared state
guarded by a lock: start, save, cancel, status (`app/main.py:1079-1098`).

The browser thread itself does very little
(`app/login.py:77-124`): launch a persistent context on `DISPLAY=:99`, navigate
to the Goodreads sign-in URL, then wait on a stop event. On Save it writes
`storage_state` to `DATA_DIR/goodreads_state.json`; everything afterwards is
plain HTTP with those cookies, because driving the DOM on every poll would be
slower and break more often (`app/goodreads.py:12-27`).

### Shared mutable state and how it is protected

Every long-lived cache is module-level, because every stage constructs its own
client and a per-instance cache would be a per-book re-login:

| State | Guard | Why it is shared |
|---|---|---|
| SQLite connection | `threading.RLock` around every execute and query | Six concurrent advance workers plus the API threads |
| BookLore/Grimmory token, full library | `_TOKEN_LOCK`, `_BOOKS_LOCK` | BookLore answers a second login with a data-conflict 400 until the first token retires, so logging in per call works exactly once (`app/clients/booklore.py:1-14`) |
| Audiobookshelf token | `_TOKEN_LOCK` | A fresh login per book per sweep earns `429 Too many authentication requests` (`app/clients/abs_client.py:20-23`) |
| Login state | `LoginSession._lock` | Three HTTP routes and one browser thread |
| Login throttle | `LoginThrottle._lock` | Written on every failed sign-in, read on every attempt |
| Download ETA samples | `_PROGRESS_SAMPLES`, a plain dict | Touched only by the `/api/state` handler, which is the only caller of `_live_progress` |

The SQLite connection is created with `check_same_thread=False` and serialised
by the lock rather than being one connection per thread. WAL plus a lock is
enough for a single-process service, and one file stays inspectable when
something sticks (`app/db.py:1-7`).

Two operations are deliberately pushed off the event loop. scrypt is
memory-hard and CPU-bound, so password verification runs in a threadpool — a
handful of concurrent sign-ins would otherwise stall every other request in the
process — and a semaphore caps how many sign-ins are in flight at once, because
without it a flood of attempts pins the CPU (`app/main.py:136-162`).

## The path namespace problem

This is the constraint the rest of the design bends around. There is one file
per book, at one path, and every app reads it in place. But each app mounts
that same host directory at a different container path, so *the same file has a
different absolute path in every process that looks at it*.

| App | Its own view of the books tree | Its own view of the audiobooks tree |
|---|---|---|
| goodreads | `/books` (`BOOKS_ROOT`) | `/audiobooks` (`AUDIOBOOKS_ROOT`) |
| Shelfmark, Kavita, BookLore, Grimmory | `/books` | `/audiobook` (Shelfmark only) |
| Open Notebook | `/app/data/uploads/library`, read-only | not mounted |
| Audiobookshelf | not mounted | its own full absolute path (`ABS_LIBRARY_ROOT`) |

The goodreads row and the two namespace roots are `Settings` defaults
(`app/config.py:41-64`); the rest is the mounting arrangement each service is
configured with.

Three of these matter to the code. The neutral `/books` mount is the friendly
case: a path goodreads computes is also the path Kavita, BookLore and Grimmory
see, so `index` and `verify` pass it straight through
(`app/clients/kavita.py:38-39`, `app/clients/booklore.py:128-129`). The other
two do not, and each fails silently if the translation is skipped — which is
why the translation is written down next to the constant it depends on rather
than being left implicit.

**Open Notebook** validates `file_path` against its own uploads root, and it
refuses symlinks: `_build_content_state` rejects any path that `.resolve()`
escapes from that root, and `.resolve()` follows symlinks. So the file cannot
be linked into place, it has to be a real path under the library mount
expressed in Open Notebook's namespace. `to_opennotebook_path` strips
`BOOKS_ROOT`, refuses if the path is not under it, and re-attaches
`OPENNOTEBOOK_LIBRARY_ROOT` (`app/clients/opennotebook.py:171-185`). Every
caller that touches the Open Notebook API — `notebook` and `verify` — goes
through it, and the constants sit together in `Settings` with the reason
written above them (`app/config.py:49-55`).

**Audiobookshelf** is the awkward one. Every other app mounts the library at a
neutral path; ABS mounts the host directory at its own full path, so its view
of a file is genuinely different from ours. `library_for_local_path` takes our
path, strips `AUDIOBOOKS_ROOT`, prepends `ABS_LIBRARY_ROOT`, and only then asks
which library contains it (`app/clients/abs_client.py:92-110`). Comparing our
path against ABS's library folders directly matches nothing, and the symptom is
not an error — it is an audiobook that is never indexed, silently, because
there is no library that appears to contain it.

**Shelfmark** never gets the chance to translate, which is why audiobook
placement is goodreads' job and not Shelfmark's. Shelfmark sees the files at
its own mount; SABnzbd reports their paths in the host's namespace; the
mismatch defeats Shelfmark's move step, so `place` does the move itself
(`app/stages/place.py:214-227`).

### Why the same-filesystem rename is the other half of this

Namespace translation is about *naming* one file correctly. The no-duplicates
guarantee is about never creating a second one, and it is enforced at the
filesystem level: `move_into_place` calls `os.rename` and, on `EXDEV`, raises
instead of falling back to a copy (`app/pathing.py:276-302`). A same-filesystem
rename is atomic and leaves exactly one file; a copy leaves two. Since staging
and the category folders are both under the books root, and SABnzbd's
audiobook staging is under the audiobooks root, every legitimate move is
within one mount — so a cross-device move means a misconfiguration, and the
right response is to stop rather than to duplicate the library.

The same reasoning excludes the two shortcuts that would otherwise be obvious.
Open Notebook is fed a `file_path`, never a multipart upload, because the
upload route stores its own copy of the ebook
(`app/clients/opennotebook.py:1-19`). And the index stage only rescans — no app
imports or re-stores anything (`app/stages/index.py:1-16`).

## Packaging

One image, one process, one published port.

The base is Playwright's own Python image, so Chromium and every shared library
it needs are present and version-matched; installing Chromium by hand on a slim
base is the usual way this goes wrong (`Dockerfile:1-4`). `playwright==1.49.1`
in `requirements.txt` matches the base image's version. The build also sets
`DEBIAN_FRONTEND` to `noninteractive`, because without it it hangs rather than
fails — the X server's dependency chain pulls in packages with debconf prompts
and dpkg sits in `--configure --pending` waiting for input that never arrives
(`Dockerfile:10-14`).

The virtual display is four packages: `xvfb`, `x11vnc`, `novnc` and `procps`
(`Dockerfile:21-28`).

- `Xvfb :99` at 1280x900x24 with `-nolisten tcp` is where Chromium draws.
- `x11vnc` serves that display on port 5900 with `-localhost -nopw -forever
  -shared`. `-localhost` is load-bearing: it binds the RFB socket to loopback
  inside the container, so the authenticated bridge in the app is the only way
  in. The script verifies the bind actually took rather than trusting the
  flags (`docker/start-vnc.sh:38-54`).
- `websockify` is deliberately **not** installed. noVNC's static client is
  served by the app itself at `/vnc/{asset:path}` out of `/usr/share/novnc`
  (`app/main.py:280-292`), and the RFB traffic is carried over
  `/vnc/ws`, a WebSocket that closes with policy code 1008 before
  authentication and otherwise pumps bytes both ways between the browser and
  `127.0.0.1:5900` (`app/main.py:218-278`).

The startup command starts the display first and then `exec`s uvicorn, so a
headless failure never stops the service from coming up — `start-vnc.sh` is
best-effort and guarded at every step (`Dockerfile:44-46`,
`docker/start-vnc.sh:1-10`).

`docker-compose.yml` publishes exactly one port, `8091:8090`. The container
listens on 8090 and the app's own `PORT` default matches; the host side is 8091
only because 8090 is already taken on this machine by an unrelated service.
Nothing else is exposed: the VNC desktop has no port of its own, which is the
point. It was previously a real hole — noVNC published on `0.0.0.0:6080` would
have let anyone on the LAN drive a browser logged into a personal account.

The container's healthcheck is an unauthenticated `GET /api/health`, so the
probe does not need a session (`docker-compose.yml:38-44`, `app/main.py:109-112`).

Volume mounts, in the app's own vocabulary (`docker-compose.yml:35-37` — those
sources are relative placeholders, and the override supplies the real host
paths):

| Container path | Holds |
|---|---|
| `/data` | The SQLite database, the captured Goodreads session, the Chromium profile |
| `/books` | The ebook library, including Shelfmark's staging directory inside it |
| `/audiobooks` | The audiobook library and its staging roots |

`/data` is a declared volume in the image, and it is the only mutable state
that has to survive a rebuild: the database, the browser session, and the
`categories.yml` override if one is placed there (`Dockerfile:39-40`,
`app/config.py:120-125`).

Host-specific mount paths stay out of the committed compose file. They live in
a `docker-compose.override.yml` that Compose loads automatically alongside the
base file, and `.gitignore` excludes it; volumes and networks merge by key, so
the override supplies the real paths while the image, the published port and
the healthcheck are inherited unchanged. `.env` is excluded the same way.

The app joins four external networks besides its own, because each library app
sits on its own isolated network and container-to-published-host-port traffic
is blocked here, so the services have to be reachable by container name. Those
networks are declared only in the host-specific, gitignored
`docker-compose.override.yml` (its `networks` stanza); the committed
`docker-compose.yml` attaches the default network alone, so a clone runs
without it. The default network's subnet is pinned in that override too, rather
than left to Docker's address pool allocation, because the pools are exhausted
on this machine and a bare `docker compose up` fails with "all predefined
address pools have been fully subnetted". The service URLs default to container
names for the same reason a localhost URL would not work — it would resolve to
the container itself and fail in a way that looks like the service is down
(`app/config.py:1-7`).

`.dockerignore` keeps `.env`, `data/`, `__pycache__` and the database files out
of the image entirely; `.gitignore` does the same for version control, plus
`*_state.json`, `browser-profile/` and the `data/` tree. The credentials that
do need to persist live encrypted in `/data/goodreads.db`, keyed by
`GOODREADS_SECRET_KEY`, which exists only in the environment
(`app/crypto.py:1-7`).
