# Version

goodreads watches a Goodreads **to-read** shelf and carries each book through to
a shelved, indexed library — Shelfmark, classify, place, index, notebook, shelve
— without ever making a second copy of a file. Current version: **v1.4**.

Newest version first. Each release gets a section in the same shape as v1.0
below: a date line, then one-line bullets under area headings.

## v1.4

2026-09-22

A security release. Almost nothing here changes what the app does when it is
working; it changes what it does when someone else is trying to make it do
something else, and it is worth reading before you upgrade rather than after.

### Read this before upgrading

- **`PUBLISH_HOST` can take the app offline if it is set to the wrong address,
  so leave it unset unless you have checked.** It decides which of the host's
  addresses the port answers on, and a value the front end does not dial means
  every request is refused before it reaches this app — a 502 with nothing in
  the app's own logs, because the request never arrives. Find out where your
  front end reaches you from before changing it:
  `docker compose logs goodreads | grep 'GET /login'`. A proxy or tunnel running
  in a *container* dials you as its bridge gateway (`172.x.0.1`), never as
  `127.0.0.1`, so it needs `0.0.0.0`.
- **`PUBLIC_ORIGIN` is only needed by a proxy that rewrites `Host`.** Without it
  the new same-origin check treats every state-changing request as cross-site
  (403). Test it in one request: post a deliberately wrong login to
  `/api/auth/login` through the public URL with the real `Origin` header — `401`
  means the check passed and `403` means `PUBLIC_ORIGIN` is what is missing.
- **Check `GOODREADS_SECRET_KEY` is at least 32 characters.** A shorter key does
  not stop the container, but it is refused the first time a credential is read
  or written. Replacing it means re-entering every credential.
- **`docker-compose.override.yml` cannot set the port** — `ports: !override:`
  does not work, and Compose publishes both mappings. Use `PUBLISH_HOST`.

### Sessions

- **Signing out now revokes.** It rotates the user's `session_epoch`, so every
  token minted for that user stops being accepted, on every device — a copy of
  the cookie taken earlier is dead the moment sign-out returns.
- A sign-in waits up to a second for one of the eight concurrency slots instead
  of being refused the instant none is free, so a flood can no longer turn the
  owner's own correct password into a 429.
- A username is bounded at 64 characters where it is used, rather than by a
  model-level cap that could reject an account that already exists.
- Failed sign-ins are logged on the first and every tenth attempt from a client,
  with a count, so a caller in a loop cannot push the operator's real activity
  out of the feed.

### Requests

- A request whose `Origin` disagrees with the host it was sent to is refused
  with 403, before the session is even looked at; a missing `Origin` is still
  allowed, so scripts and the healthcheck are unaffected.
- The VNC WebSocket checks the origin before the cookie, and answers both with
  the same `close(1008)`, so the handshake cannot tell you which one failed.
- The session gate compares the path the router routes on, closing a
  `%3F`-in-the-path way past it.
- Four response headers are now set: `nosniff`, `no-referrer`,
  `X-Frame-Options: SAMEORIGIN`, and `no-store` on API answers. No CSP —
  deliberately, because the useful one blanks every book cover.

### Stored data

- The data volume is owner-only: `umask 077` for the process, plus a one-off
  `chmod` at startup for what is already on disk. The activity feed says what it
  changed, once.
- The media library is explicitly **not** tightened — other applications read
  it, and a category folder made owner-only would make every book beneath it
  unindexable.
- `GOODREADS_SECRET_KEY` must be at least 32 characters; nothing stretches it,
  so it is exactly as strong as the string in `.env`.
- `goodreads forget-session` deletes the stored Goodreads session *and* the
  browser profile, and refuses while a login browser is running.

### Talking to your services

- Redirects from a service are refused rather than followed, and reported as a
  misconfiguration instead of an outage.
- Response bodies are capped at 16 MiB, refused whole rather than truncated.
- Error bodies are no longer quoted back for any client that authenticates —
  they can echo the credential, and `stage_runs.detail` is not encrypted.
- The Test button now runs the same authenticated probe as the health check, so
  a wrong API key fails it. It used to call an unauthenticated route and pass.
- An epub's two XML documents are size-capped and entity definitions refused.

### Sweeps

- The 120-second budget is real: the clock now covers the periodic phases, and
  the worker pool no longer waits for work the sweep has stopped asking for.
  Measured on a live deployment, sweeps were ending at 310-432s.

### Container

- `no-new-privileges`, `pids_limit: 512` (measured: idle 12, login browser 157),
  and all capabilities dropped except `DAC_OVERRIDE` — which is required, since
  the `/data` mount is owned by the host user's uid and uid 0 cannot write to it
  without it.

### Dependencies

- `fastapi` 0.135.0 and an explicit `starlette` 1.6.0 — the oldest starlette
  that bounds how many byte ranges it will merge. `python-multipart` removed: it
  was never used.

## v1.3

2026-09-21

### Verification

- Title matching also compares a service's whole name against the wanted
  title, not only the part left after stripping everything from the first
  dash. Kavita and BookLore often name a book after the `Author - Title`
  folder it sits in, and the old form reduced that to the author's name
  alone, verifying present books as absent.
- A book still missing after the rescan budget is reported as `notfound`, not
  `server`: every service that reaches this point searched and answered "no
  match," which the breaker no longer counts as a transient failure and the
  panel no longer tells you to retry.
- Verify and notebook ask Open Notebook about the source id they already
  recorded before falling back to a path search.

### Open Notebook

- `GET /api/sources` paginates silently at 50 rows with no total and no
  next-page marker. The "has this book already been added?" check read only
  that one page, so a library past 50 sources added the same book again on
  every run. The check now walks every page, and `repair --apply` finds and
  removes what the bug already produced.

### Issues panel

- A held service's row reads out any resume time the upstream stated in its
  own error text, instead of only "these resume on their own."
- A failure group whose service is currently held no longer offers Retry
  all, since retrying cannot get further than the hold it is already in; it
  stays on the panel with a link to what is blocking it instead.
- Two Shelfmark failure messages that embed the book's own title no longer
  split into one row per book.

## v1.2

2026-09-21

### Mobile header

- The service status/run control rail is dashboard-only now; Run control's
  own controls (sweep now, check shelf, auto-shelve) also appear inline on
  the Activity page, above its log.
- The header at phone width follows their own native app rather than their
  desktop navbar: a bare search icon on the left, the wordmark truly
  centred, and a status/account control on the right standing in for their
  avatar. Nav tabs share the row exactly five ways instead of scrolling
  sideways to fit.
- Tapping the search icon slides the input open left to right as a real
  animated overlay, instead of an instant, jarring swap that also used to
  nudge the nav row underneath it.
- The account dropdown (service status, sign out) closes on an outside tap,
  Escape, or navigating via a link inside it.

### Desktop header

- The search box, the health pill and Sign out no longer read as three
  different controls. All three now share one visual language with the nav
  tabs: the search box is a full pill matching the mobile one exactly, and
  the health pill and Sign out take the nav links' own geometry and hover
  fill instead of a separate cream chip and a bare teal link. Sign out
  keeps its own colour — the app's error red, not the nav's brown ink — since
  it ends the session rather than taking you anywhere.

### Fixes

- A search keystroke on any page but My Books used to steal keyboard focus
  and drop it on the page title, ending the keystroke — typing more than
  one character before My Books loaded was not possible. Fixed.
- The category and status filter tabs (My Books, Genres) are bordered pills
  now instead of goodreads.com's own flat underlined tabs, which stopped
  being legible once a dynamic category list ran past the two or three
  tabs the real site ever draws there.
- A book row's stage chips no longer sit flush against the detail message
  below them with no gap.
- The app-wide footer dropped its "Pages" column, which only duplicated the
  always-visible header nav one click further away.
- Pinch and double-tap zoom are disabled everywhere, so the mobile layout
  cannot be pinched out of the proportions it was built for.
- The dashboard's version pill is its own small card now, separate from Run
  control, since it is metadata about the running instance and not a
  control.

## v1.1

2026-09-21

### Reliability

- A per-service circuit breaker: three consecutive transient failures against one service holds every book waiting on it, instead of each one failing, waiting out its own clock and parking independently. A restart or a resolved outage is caught by a half-open probe on one book; a stuck hold can be released by hand from the service's page.
- The issues panel folds every book a held service is blocking into one row naming the service, with a fix message that escalates once the outage outlives the 24-hour transient grace instead of saying "nothing to do" forever.
- A 408/425/429 from a source is now its own failure kind (`busy`) instead of falling through unclassified, and a `Retry-After` header is parsed and honoured.

### Classification

- A precedence rule in `categories.yml`: any genre that names fiction resolves to Fiction ahead of the longest-needle scan, so a book tagged both "Historical Fiction" and "Fiction" no longer files under History on the strength of the longer word.
- The new `reclassify` CLI command re-runs classification over the whole library against the current rules and reports (or, with `--apply`, makes) the moves needed to match.

### Sign-in

- The sign-in page is rebuilt against the real goodreads.com sign-in page, measured rather than approximated: a white page, a narrow centred column, no card, no cream — matching its actual geometry down to the wordmark, heading and input sizes.
- A working "Keep me signed in." checkbox: leaving it ticked keeps the existing 7-day session, unticking it issues a 12-hour one instead.
- The browser tab icon is goodreads.com's own, served from a real `/favicon.ico` route instead of an inline placeholder.

### Interface

- Page chrome follows goodreads.com's classic system: a white page, cream `#f4f1ea` callouts, a `#faf8f6` header and footer, 1px `#d8d8d8` borders, and a lowercase Merriweather "goodreads" wordmark.
- A footer on every page with the section links, the project links and the running version, in place of the page just ending.
- Book rows read like shelf rows (2:3 cover, serif title, Lato byline, eight stage dots with a count) and the book page puts the cover, retry action and Goodreads link in a left column beside a serif title and a genre link row.
- Six-second polling no longer wipes what you typed into a credential field or drops keyboard focus: identical renders are skipped and typed values and focus are restored.

### Version page

- `#/version` lists every release with its date and a one-line summary, changes grouped by area, the running build tagged current.
- The right rail shows the running version with a "What's new" dot that clears once the page has been opened in this browser.
- `/api/state` carries `version` and FastAPI reads the version from `app/__init__.py`.

### Accessibility and polish

- Every link, button, input, row and tile has a visible 2px teal keyboard focus ring; service cards are real links; pages set their own tab title and move focus to the page title after navigation.
- Toasts are announced to screen readers, can be dismissed, pause on hover, and error toasts stay until closed.
- The Goodreads browser page shows an idle state with the three steps and the start button in place of a black frame.

## v1.0

2026-09-20

### Shelf tracking

- Scrapes the Goodreads `to-read` shelf with a captured browser session; books are keyed on the Goodreads id, so a re-run refreshes metadata instead of creating duplicates.
- Fetches genres only for books not seen before, so a large shelf does not turn into hundreds of lookups per sweep.
- Paginates a shelf read up to five pages, and the reconciliation pass up to eight, so a shelf that has outgrown one page is still read whole.
- Reconciles recorded shelves against Goodreads every 6 hours and resets the `shelve` stage for anything that drifted.
- Moves a finished book off the exclusive `to-read` shelf using the shelve / confirm / destroy / re-shelve / confirm sequence Goodreads requires, because both removal routes destroy the whole review record.
- Creates a missing shelf before writing to it; `add_to_shelf` silently does nothing if the shelf does not exist.
- Confirms each shelf write from the endpoint's own response rather than re-reading the shelf page, which stops being reliable once a shelf outgrows one page.
- Detects the Goodreads user id from the stored session when it is not configured, with a 10-minute cooldown on detection attempts.

### Acquisition

- Each acquire stage is a two-phase machine: search and rank, then hand the best release to Shelfmark and watch the task to completion.
- Releases are rejected on the indexer's Newznab category, on size, and on video-release name patterns before ranking, so a film cannot be queued as an audiobook.
- Records which release ids have been tried and falls through to the next-best candidate, up to four per book.
- A queued task that is absent from Shelfmark's status listing for 10 minutes (`QUEUE_ABANDON_SECONDS`) is treated as lost, since the queue lives in memory and a restart drops it, and is re-queued.
- Refuses to start an audiobook download below the configured free-space floor.
- Treats an upstream 5xx during search as a passing outage and forgives it on a 24-hour clock measured from when the outage started.

### Classification

- Genre lookup walks four providers in order — Goodreads AppSync, OpenLibrary, Google Books, embedded epub metadata — and stops at the first that answers.
- Google Books is skipped entirely unless an API key is set, because the unkeyed endpoint rate-limits immediately.
- The embedded source reads `dc:subject` straight out of the epub, so classification still works with every API down.
- Each provider is isolated: one raising or timing out cannot stop the next.
- A book resolves to exactly one category: genres are scanned in the source's own relevance order and the longest matching rule wins.
- With no genres from any source, the title is matched against the same rules and stored with `genre_source = title`.
- `genre_source` is recorded per book (goodreads / openlibrary / googlebooks / embedded / title) and shown in the UI.
- A book matching no rule falls back and is flagged needs review rather than being quietly misfiled.
- `app/categories.yml` ships in the image; `/data/categories.yml` overrides it so tuning survives a rebuild.

### Placement

- Ebooks are renamed out of staging into `<Category>/Author - Title (Year)/`; audiobooks are moved into `Author/Title/`.
- Every placement is `os.rename`. A move that would cross a filesystem raises rather than falling back to a copy, so a duplicate is impossible.
- A destination that already exists is never clobbered. The incoming file is dropped only when `os.path.samefile` says it is the same inode as the one already there; everything else gets a numbered suffix, including a byte-identical copy sitting at a different path.
- Placement succeeds as soon as one format is in place, so a missing audiobook cannot stall the ebook's whole chain.
- Re-running placement is a no-op: an already-filed ebook is found in its category folder before staging is consulted.
- Re-classifies against the embedded epub metadata once the file exists and re-files it if the category improved.
- Only entries sitting directly in a staging root are moved. Anything already nested under an author folder is treated as organised and left alone.
- When several entries match a book, only the best-scoring one is moved and the others are left where they are.

### Indexing

- Rescans Kavita, BookLore and Grimmory for the books tree and Audiobookshelf for the audiobooks tree. Nothing is imported or uploaded.
- An app whose tree did not change is skipped rather than failed, so an ebook-only book does not spend its retries on Audiobookshelf.
- Partial failure is reported by name in the stage detail but does not block the rest of the chain.
- Verification searches each service for the title before anything irreversible happens on Goodreads.
- A miss within 10 minutes of a rescan blocks rather than fails, because rescans are asynchronous.
- A book still missing past the grace window forces another rescan, up to 3 times, before it is handed to a human.
- Titles are matched on a normalised, word-boundary form with the leading article ignored, so a service holding "Decline in Prophets" matches Goodreads' "A Decline in Prophets (Rowland Sinclair #2)".
- A stated series number that differs is disqualifying, so two books in one series are never taken for each other.

### Integrations

- Six services are probed on a 5-minute timer. Five get an authenticated call — listing libraries or notebooks — rather than a bare health endpoint that would pass with a wrong key; Shelfmark runs with auth off on this instance, so its probe is the unauthenticated `GET /api/health`.
- Each service has its own page: health, its credentials, everything currently stuck on it, and its own log.
- Open Notebook is handed a `file_path`, never a multipart upload, so no second copy of the epub is stored.
- Notebook attachment is verified by reading the source back; a source attached more than once is collapsed.
- Formats Open Notebook cannot read, and `.epub` files that are really a zipped folder, are skipped with the reason instead of retried to death.
- Paths are translated into each service's own mount namespace before they are sent, because each app sees the same files at a different path.
- Credentials are entered through the UI and stored Fernet-encrypted under `GOODREADS_SECRET_KEY`; the key itself lives only in `.env`.
- A credential that was never set is reported as an auth failure pointing at Settings, not as a network outage.

### Library hygiene

- `audit` reports duplicate audiobooks and titles present in more than one place. Read-only. Audiobooks are grouped by size, then by a SHA-256 of the first and last 1 MiB: a cheap head+tail digest, so files matching at both ends but differing in the middle are reported as duplicates.
- AppleDouble `._` stubs are ignored throughout, so they do not read as thousands of duplicates.
- `rename` proposes `Author/Title` moves using embedded tags and filename parsing together, printing which rule fired, and changes nothing without `--apply`.
- Audiobook `artist` tags are frequently the narrator, so tags are trusted only when the filename's leading segment matches the tag's title.
- `repair` fixes Open Notebook link gaps and resets stuck stages, dry-run unless `--apply`.
- `backfill-genres` re-resolves every book missing genres and re-runs classification.

### Interface

- Dashboard, Books, Services and Activity views, plus per-book and per-service detail pages, served as one page with no framework and no build step.
- Failures are grouped by cause rather than by book, with a suggested next step in plain language and a Retry all for the groups where retrying helps.
- Books are classified done / partial / unavailable / failed / working, so "no audiobook exists anywhere" does not read as a fault needing attention.
- Every stage shows its status, attempt count, real error and a Retry button on the book detail page.
- A failing credential raises a banner on every page naming the service and what it breaks, with a link to the fix.
- Saving credentials re-probes immediately and toasts whether they work, while the form is still on screen.
- Service health is on screen at all times in the header, one click from the page that explains it.
- Manual controls for Sweep now, Reconcile, Test, category override and per-book auto-shelve.

### Operations

- One background thread sweeps the book list; stages are independent and retried independently, so a stuck audiobook does not stop an ebook being filed.
- A sweep has a 120-second budget and advances up to 6 books concurrently, and books are served least-recently-touched first so a bounded pass still rotates through the whole list.
- A stage that exhausts its attempts (3 to 6, per stage) parks the book rather than looping forever, and resumes when someone hits Retry.
- A sweep is lock-guarded, so a manual Sweep now cannot overlap the scheduled one.
- A stage whose download completed after placement gave up re-runs placement, and a late audiobook re-runs shelving to the both-formats shelf.
- SQLite in WAL mode behind one module-level connection and a lock. No ORM, one file to inspect.
- `/api/health` is an unauthenticated liveness probe for the container healthcheck.
- Sub-commands: `report`, `repair`, `audit`, `rename`, `reconcile`, `backfill-genres`, `set-password`, `users`, `delete-user`.
- `/api/version` reports what the running build is actually serving.

### Security

- Passwords are hashed with scrypt and compared in constant time. Nothing reversible is stored.
- Sessions are stateless HMAC-SHA256 tokens, verified before the payload is parsed. They last 7 days.
- Session tokens carry a password epoch, so changing a password evicts every outstanding session.
- The cookie is `HttpOnly` and `SameSite=Lax`, which is what stops another site POSTing to `/api/...` with the cookie attached.
- Failed logins cost a progressive delay, never a lockout, so an attacker cannot lock the owner out of their own UI by hammering a known username.
- A nonexistent username is verified against a dummy hash, so a wrong username and a wrong password take the same work.
- Concurrent sign-ins are capped, because scrypt is memory-hard and a flood would otherwise allocate far more than the box should be asked for.
- `X-Forwarded-For` is honoured for rate limiting only when `TRUST_PROXY` is set by a proxy you control.
- Everything except `/login`, `/api/auth/login`, `/api/auth/status`, `/api/health` and `/favicon.ico` requires a session.
- The noVNC desktop has no published port: x11vnc binds to loopback inside the container and the app bridges to it over a session-checked WebSocket.
- `COOKIE_SECURE=1` is available for when the service is only ever reached over HTTPS.

## Version history

| Version | Date | Summary |
|---|---|---|
| v1.4 | 2026-09-22 | A security release: sign-out revokes every session, cross-site requests are refused, the data volume is owner-only, and the calls to your other services refuse redirects and stop quoting error bodies that can carry credentials. |
| v1.3 | 2026-09-21 | A title-matching bug and an Open Notebook duplicate-source bug, both of which misreported working books as failed, plus an issues panel that stops retrying into a held service. |
| v1.2 | 2026-09-21 | A native-app-style mobile header with an animated search, a cohesive desktop header, and a stolen-focus fix. |
| v1.1 | 2026-09-20 | Goodreads-faithful redesign: sign-in page rebuilt, white page chrome with a footer, structured version page, a narrow-screen header, and keyboard focus states. |
| v1.0 | 2026-09-20 | Initial release. Goodreads to-read shelf to a shelved, indexed library, one file per book, no copies. |

## Releasing a new version

1. Add a new `## vX.Y` section **above** the previous release section, so the
   newest version is first. Use the same shape: a date line, then one-line
   bullets under short area headings. One line per feature — no sub-bullets and
   no paragraphs.
2. Add a row to the top of the **Version history** table with the version, the
   date, and a one-line summary of what that release changed.
3. Bump `__version__` in `app/__init__.py`, currently `1.4.0`. It is the only
   version literal: `FastAPI(version=__version__)`, `/api/version` and
   `/api/state` all read it, and `asset_version` is a short hash of `app.js`
   and `app.css` that changes on its own whenever either file changes. Then add
   the matching entry at the top of `VERSION_HISTORY` in `app/version_data.py`
   (major.minor, e.g. `"1.1"`).
4. Nothing else is derived from the version. There is no migration step tied to
   it.
