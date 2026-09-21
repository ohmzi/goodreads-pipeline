# Operations

How to upgrade this, back it up, and work out why a book is not moving. The
pipeline itself and the reasoning behind each stage are in
[PIPELINE.md](PIPELINE.md); this file is the runbook.

Every command below runs from the repository root, on the host, against a
running compose project. Command-line maintenance inside the container goes
through `app/cli.py`.

Installing it in the first place — mounts, networks, the first login, the
first run in the UI, and the bare-metal path — is [SETUP.md](SETUP.md).
Every variable is [CONFIGURATION.md](CONFIGURATION.md).

## Upgrading

```bash
git pull
docker compose up -d --build
```

The image is stateless — the build installs `requirements.txt` and copies
`app/` and `docker/`, and nothing else. Schema changes are applied on connect by
`Database._migrate`, which only ever runs `ALTER TABLE ... ADD COLUMN` against a
fixed list of additions. An upgrade needs no migration step, and nothing is
dropped or renamed, so a downgrade to an older commit still reads the newer
database.

Confirm what is actually serving. `GET /api/version` returns an `asset_version`
derived from the bytes of `app.js` and `app.css`, so it changes whenever a
static file changes. A UI that looks stale while that number has moved means the
browser is serving a cached bundle.

`app/categories.yml` ships in the image; `/data/categories.yml` overrides it if
present. Editing the image copy changes nothing for a container that has the
override, and the override survives a rebuild by design.

## Backup and restore

State lives in four places, and they are not equally important.

| What | Where | If it is lost |
|---|---|---|
| SQLite database | `/data/goodreads.db` (plus `-wal` and `-shm`) | Every book, stage run, event, user, stored credential and setting. History is gone. |
| Goodreads session | `/data/goodreads_state.json` | Every shelf read and write raises `SessionExpired` until you sign in again interactively. |
| Browser profile | `/data/browser-profile/` | Nothing breaks immediately. The next interactive login opens a signed-out browser, so you enter the Amazon credentials and any OTP again. |
| Category rules | `/data/categories.yml` (optional) | Falls back to the copy in the image. |

Plus the key, which is not under `/data` at all: `GOODREADS_SECRET_KEY` in
`.env`.

- **The database alone is not a backup.** The credentials table is Fernet
  ciphertext keyed by that secret. Restore the database with no key in `.env`
  and `CredentialCipher` refuses to construct at all; restore it under a
  different key and every stored credential raises *"Stored credential could
  not be decrypted"*. Back up `.env` with the database or the credential table
  is dead weight.
- **Losing the key also stops sign-in.** Session tokens are HMAC-signed with the
  same secret, so `issue_session` raises and every login attempt fails with a
  500. The stored credential blobs are not recoverable: set a new key, then
  re-enter every credential in Settings. Password hashes are scrypt, not keyed
  by this secret, so the accounts themselves survive.
- **Losing the database loses the accounts.** Start-up sees an empty user table,
  logs the "no login exists yet" error, and nobody can sign in until
  `set-password` runs again.
- **Losing the database does not touch the library.** The files under the books
  and audiobooks roots are the real library; no restore path writes them. What
  is lost is the app's memory of what happened to them. `discover` reads the
  `to-read` shelf only, so a book already moved onto a `collected-*` shelf keeps
  its file but is not re-registered.
- **The database grows with activity.** `events` is append-only and never
  pruned; there is no `DELETE FROM events` anywhere. A large database is mostly
  event history, not book data.

### Backing up

Take a consistent copy without stopping the service. The database runs in WAL
mode, so copying the `.db` file while the process is running can miss committed
transactions sitting in the `-wal`.

```bash
docker compose exec goodreads python -c \
  "import sqlite3; s=sqlite3.connect('/data/goodreads.db'); d=sqlite3.connect('/data/goodreads-backup.db'); s.backup(d); d.close()"
docker compose cp goodreads:/data/goodreads-backup.db ./goodreads.db
docker compose cp goodreads:/data/goodreads_state.json ./goodreads_state.json
cp .env ./goodreads.env
```

Keep the `.env` copy somewhere the database copy is not. A database and its key
in the same folder are one backup, not two.

Stopping the container first and archiving `/data` wholesale is equivalent and
simpler, at the cost of a restart.

### Restoring

```bash
docker compose stop
docker compose cp ./goodreads.db goodreads:/data/goodreads.db
docker compose cp ./goodreads_state.json goodreads:/data/goodreads_state.json
cp ./goodreads.env .env
docker compose up -d
```

`stop`, not `down`. `down` removes the container, and the `cp` steps then have
no container to resolve and fail with *"no container found for service
goodreads"*. The copies write through the bind mount, so a stopped container is
enough; on a project that has never run, `docker compose create` makes one to
copy into.

The browser profile is optional; leave it out and the next login starts
signed out. Restore `.env` before starting, and restore the key that encrypted
the database. A missing key and a wrong key fail differently: with no key set
`CredentialCipher` refuses to construct and everything reports
*"GOODREADS_SECRET_KEY is not set"* (app/crypto.py:21-27); with a key that is
present but wrong, each stored credential raises *"Stored credential could not
be decrypted"* and the operator sees a Settings page full of failing tests
(app/crypto.py:34-41). Only the original key clears either one, so without it
re-enter every credential.

## The maintenance CLI

All of it runs inside the container:

```bash
docker compose exec goodreads python -m app.cli <subcommand>
```

| Subcommand | Arguments | Dry run | Writes |
|---|---|---|---|
| `audit` | none | read-only, always | nothing |
| `rename` | `--apply` | yes | moves audiobook files with `--apply` |
| `set-password` | `<username>`, `--generate`, `--length N` | n/a | the `users` row |
| `users` | none | read-only | nothing |
| `backfill-genres` | none | **no — it writes** | genres, categories, `place` resets |
| `report` | `--failed-only`, `--show-all`, `--limit N` | read-only | nothing |
| `repair` | `--apply` | yes | Open Notebook links, stage resets |
| `reconcile` | `--apply` | yes | `shelve` stage resets |
| `delete-user` | `<username>` | n/a | the `users` row |

`--length` defaults to 20 and `--limit` to 0, which means no cap on the
completed listing.

### `audit`

Read-only. Reports byte-identical audiobooks found by size and then a head+tail
SHA-256 over at most 2 MB per file, and titles appearing in more than one place
under the books root. Only the duplicate scan has a size floor: it skips files
below 100 KB, which keeps short fragments from grouping into fake duplicates.
The title scan has no size floor — it considers every `.epub`, `.mobi`, `.azw3`
and `.pdf` it finds — and AppleDouble `._` stubs are excluded from both scans by
name, not by size (app/pathing.py:22-32). Both lists print paths relative to
their root.

### `rename`

Proposes moving loose and badly-named audiobook entries into `Author/Title`,
using embedded tags and filename parsing together, and prints which rule fired
for each proposal (`tags`, `tags (title-first filename)`, `filename`, or
`filename (tag author was a narrator)`). It skips anything already shaped like
an author directory, and skips a destination that exists. Dry run prints the
list and changes nothing; `--apply` performs `Path.rename` per move and reports
`N of M applied`.

Rescan Audiobookshelf afterwards — the files moved underneath it.

### `set-password` / `users` / `delete-user`

`set-password` refuses a username-less call, a missing `GOODREADS_SECRET_KEY`, a
mismatched confirmation, and any password under 12 characters. It prints the
generated password only when it generated one; a password you type is never
echoed.

`delete-user` refuses to remove the last user, because that locks the UI with no
way back in short of another `set-password`.

### `backfill-genres`

Not a dry run. It re-resolves genres for every book whose `genres` column is
null or an empty list, walking the provider chain with the embedded-epub source
included, at 0.2 s between books. Then it re-classifies **every** book
unconditionally, so the `needs_review` flag is recomputed even where the
category does not change.

For any book whose category changes it resets the `place` stage. That is the
destructive part: the category decides the destination folder, so the next sweep
moves the file to its new home. It is also the repair command when a category
looks wrong.

### `report`

Prints the shelf summary: completed, in progress, and failed, with failures
grouped by stage, service, failure kind and detail text so one cause reads as
one problem. `--failed-only` drops the completed and in-progress sections;
otherwise the completed listing prints by default, capped by `--limit N`, and
`--show-all` only silences the hint line that points at it. It ends with free
space on the books root. Read-only.

### `repair`

Three fixes, all of them gaps that verification only reports: a source attached
to its notebook more than once (the attach endpoint appends rather than being
idempotent), a source created but never attached, and books whose earliest
failing stage is worth another attempt.

Dry-run by default. With `--apply` it calls `attach`/`detach` on Open Notebook
and resets the earliest retryable failing stage of each book so the next sweep
picks it up. Failures with kind `data` on an `acquire_*` stage are excluded —
"no audiobook release exists" will not become true by trying again, and resetting
it also resets everything downstream.

### `reconcile`

Reads the four shelves goodreads writes to, compares them against what each
book's `shelve` stage claims, and reports the drift: *not on 'collected-pdf'*,
*still on to-read*. With `--apply` it queues the drifted books by resetting
their `shelve` stage.

The command itself never edits Goodreads. The write happens later, in the
`shelve` stage, on the next sweep — which is why `--apply` is a real queueing of
real work rather than a report with a different label.

### Destructive commands

`rename --apply` mutates real files on the audiobooks tree. `repair --apply`
writes to Open Notebook and resets stage rows. `reconcile --apply` queues real
shelf writes. `backfill-genres` writes immediately and queues file moves.
`delete-user` removes a login, and `set-password` writes the `users` row and
rotates the session epoch, which signs out that user's existing sessions
(app/cli.py:603). Nothing else in this CLI changes anything.

## Health

Two layers, and they answer different questions.

### Liveness

`GET /api/health` returns `{"status": "ok"}` with no session. It is in
`PUBLIC_PATHS` alongside the login page and `/api/auth/login`. It proves uvicorn
is answering; it does not touch the database or any dependency. The compose
healthcheck is a urllib GET against it on `localhost:8090`, every 60 s, 10 s
timeout, 3 retries, 30 s start period.

### Credential and reachability probes

`app/health.py` probes the six integration services. Each probe makes the
cheapest **authenticated** call the service offers, because a health route that
answers without credentials says nothing about whether the key is right:

| Service | Probe |
|---|---|
| Shelfmark | `GET /api/health` — no auth on this instance, so the bare route is the honest check |
| Kavita | list libraries |
| BookLore | list libraries |
| Grimmory | list libraries |
| Audiobookshelf | list libraries |
| Open Notebook | list notebooks |

The probes run in the background: the scheduler calls `check_all()` whenever
more than five minutes have passed since the last one, as part of a sweep. The
results land in `service_health`:

| Column | Meaning |
|---|---|
| `service` | slug: `shelfmark`, `kavita`, `booklore`, `grimmory`, `audiobookshelf`, `opennotebook` |
| `ok` | 0 or 1 |
| `detail` | e.g. `4 libraries`, `3 notebook(s)`, or the error text |
| `failure_kind` | `auth`, `network`, `server`, `busy`, `notfound`, or empty |
| `checked_at` | last probe |
| `ok_since` | start of the current unbroken healthy run; cleared on the first failure |

A transition is logged as an event: newly broken, recovered, and a roll-up
naming the services that broke in that pass — a service that broke on an
earlier pass and is still down is not named there again (app/health.py:147-150).
The UI reads the same table through `GET /api/health/services` and raises a
banner naming the service and what breaks without it.

`GET /api/issues` groups book failures by cause rather than by book, and
separates failures with kind `auth` from everything else, so "Kavita rejected
your API key" does not sit in the same list as "no audiobook release exists".

### When nothing seems to be happening

A sweep that reports `held` in its summary, or an issue row whose stage reads
`held`, is the service breaker doing its job: a service it depends on is down,
so nothing is attempted against it and nothing is written off for it. It is one
row naming the service, not a book failure, and there is genuinely nothing to
fix — the text says so — unless the row has escalated.

That escalation is the part to watch. An outage younger than 24 hours really is
not yours to fix, and saying so is honest; past the grace the same row changes
its wording and points at the service itself, which is what stops the breaker
from being the thing that hides a permanently dead upstream. Shelfmark is the
common case and the trap: its own `/api/health` answers `200` in milliseconds
even while every release search fails, because the fault is upstream of it in
Anna's Archive. A green service page therefore proves nothing here.

The books need no action. A held stage is `blocked`, not `failed`, so it is
picked up by the very next sweep that can run it, and the breaker clears the
per-book clock it was holding so nothing parks on hours it did not spend.

### When the hold outlives the outage

If you know the service is fine — you just restarted it, or fixed a rate limit
at the source — and the hold is still there, you do not have to wait for the
next probe: **Clear hold**, on the held row of the dashboard's attention list or
on the service's own page, releases everything that service is holding and
closes its breaker. It confirms first, and it tells you what it did rather than
what you want to hear: the row goes away, the log records a `warning` saying the
breaker was cleared by hand and why, and nothing claims the service answered —
because clearing cannot know that. If it is still down the next sweep trips it
again a few minutes later, which is the answer to the question you were asking.

Reach for it when the breaker is holding work and the service's own page says
it is up, or when the outage is over and you would rather not spend a cooldown
finding out. It is not a way to skip an outage: the books it releases run
against the service immediately.

### The Test button is not the probe

The Services page's Test button calls each client's own `health()` route, and
every one of those is unauthenticated: Kavita `/api/health`, BookLore
`/api/v1/healthcheck`, Audiobookshelf `/status`, Open Notebook `/api/config`,
Shelfmark `/api/health`. A wrong API key passes it.

Saving credentials in the UI calls `POST /api/health/services/check`, which
re-runs the authenticated probes above. When Test and the banner disagree, the
banner is right.

### Probing now

```bash
curl -X POST http://<host>:8091/api/health/services/check   # needs a session cookie
```

Or press *Re-check all* in the Services panel. The status rail card carries the
same control as *Re-check*. The UI also runs this automatically after Save.

## Troubleshooting

| Symptom | Likely cause | Action |
|---|---|---|
| Sign-in looks successful but you land back on the login page | `COOKIE_SECURE=1` while serving plain HTTP, so the browser drops the cookie | Set `COOKIE_SECURE=0`, or terminate TLS in front first |
| Activity shows *"no login exists yet"* | Empty user table | `set-password <username> --generate`, then sign in |
| Red banner naming a service, `credentials` chip | The authenticated probe was refused: wrong key or wrong URL | Open Services, re-enter the credential, Save, then *Re-check all* |
| Test reports ✓ but the banner stays red | Test hits an unauthenticated route; the probe lists libraries | Trust the probe. Check you are using that service's own credential |
| Every credential raises *"GOODREADS_SECRET_KEY is not set"*, or every login 500s | The key is missing or empty in `.env` | Restore the key from the `.env` backup and restart. Session tokens are HMAC-signed with it too, so sign-in fails as well (app/crypto.py:21-27) |
| Every credential raises *"Stored credential could not be decrypted"* | A key is set, but not the one that encrypted them | Restore the original key from the `.env` backup, or re-enter every credential (app/crypto.py:34-41) |
| `discover failed:` with nothing after the colon | A read timeout on a Goodreads shelf read. The client's read timeout is 30 s, and httpx's timeout exceptions stringify to an empty message | Nothing. Discover runs every 15 minutes and logs only a changed message, so a recurring timeout shows as one line. If it never recovers, check the session (next row) |
| *"Goodreads redirected to the sign-in page"* or *"Could not find a CSRF token"* | The stored browser session expired | Goodreads page, *Start browser*, sign in, *Save session* |
| *"No Goodreads session stored"* | `goodreads_state.json` missing | Same as above |
| *"Could not determine your Goodreads user id"* | No session and no id set | Log in, or set the id explicitly in Settings |
| *"Goodreads returned an AWS WAF challenge (HTTP 202)"* | IP reputation, writes are rate-limited | Wait 5–10 minutes. Do not retry in a loop |
| Books blocked with `[transient: Xh of 24h, N attempts]` | A 5xx from the source search; forgiven on a clock, not a counter | Nothing yet. It becomes a real failure after 24 h, so fix the upstream if it persists |
| An issue row whose stage is `held`, `N books — none failed` | The service breaker: three consecutive transient failures against one service, so nothing is attempted against it | Nothing yet. It resumes by itself; open the service page to see what it is saying. If the `fix` line says the grace has been outlived, the fault is upstream of that service and is now yours to look at |
| A `busy` group, "The service is rate-limiting requests" | A 429 (or 408/425) survived the breaker, so it is a rate limit that keeps happening rather than one outage | The pipeline backs off and retries. Nothing to fix unless it lasts; if a `Retry-After` was sent, the cooldown already honours it |
| Login page shows a blank panel | Xvfb or x11vnc did not start | `docker compose logs goodreads \| grep '\[vnc\]'`. The rest of the app works without it |
| Everything 4xx on `/api/...` with `authentication required` | No session cookie, or an expired one (7 day TTL) | Sign in again |
| `docker compose up` fails with *"all predefined address pools have been fully subnetted"* | The host's docker address pools are exhausted, or the subnet pinned in `docker-compose.override.yml` collides with a network already on the host | Pin another private /24 in `docker-compose.override.yml` (gitignored, host-specific) |
| `bind: address already in use` on start | Something else holds the published port | Change the left side of the `8091:8090` mapping |
| A book never leaves `place` | The file is not sitting *directly* in a staging root | Check the staging dir. `place` treats anything already nested as organised, deliberately. An existing destination is not a stall: `move_into_place` unlinks a same-inode duplicate or suffixes the name via `_dedupe` (app/pathing.py:276-302) |
| `index` fails with an auth kind but the key looks right | The stage is failed only when *nothing* was rescanned; `_kind_of` reads the collected error text inside that branch (app/stages/index.py:86-91), so an auth kind means every app it tried refused. One app 401ing while another rescans returns ok, with the failure named in the detail | Open the book detail; the detail string names each app that failed |
| Books parked after a fresh install | Stages hit their attempt cap while credentials were still unset | Enter credentials, then *Retry all* on the group |
| A book is on two Goodreads shelves at once | A shelf write reported success without applying | `reconcile` finds this; `reconcile --apply` queues the fix |

### When authentication to an integration fails

Work through it in this order:

1. **Does the probe agree?** *Re-check all* in the Services panel re-runs the
   authenticated probes and shows the real HTTP error, not a health status.
2. **Is the credential the right kind?** Kavita takes an API key from its own
   settings; BookLore, Grimmory and Audiobookshelf take username and password;
   Open Notebook's password may legitimately be blank if auth is disabled on it.
3. **Is `GOODREADS_SECRET_KEY` the one that encrypted them?** A changed key
   surfaces as a decrypt failure naming the key, not as an auth error.
4. **Is the URL right?** Service URLs default to container names on the shared
   compose networks. A `localhost:<port>` URL resolves to the container itself
   and fails in a way that looks like the service is down.
5. **Did the run start?** `docker compose logs goodreads` and the Activity view
   both carry the probe lines, which name the service and what breaks without
   it.
