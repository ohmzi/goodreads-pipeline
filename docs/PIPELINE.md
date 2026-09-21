# Pipeline

Every book on the to-read shelf is one row in `books`, plus one row per stage in
`stage_runs` keyed on `(book_id, stage)`. The scheduler sweeps that table and
advances each book as far as it can go. A stage is a pure function of the book
row and the world outside it: it reads what is already recorded, does one piece
of work, and hands back a `StageResult`.

The authoritative stage list is `models.STAGES`, and its order is also the
dependency order — a stage may assume every earlier stage has settled. The
stage-level prerequisite graph is narrower than that order, which is what lets
the two acquisition stages run alongside each other while `place` stays pinned
behind `classify`.

Stages are independent once their prerequisites are met. A book whose audiobook
is stuck still gets its ebook classified, placed, indexed and added to a
notebook.

## Stages at a glance

| Stage | Consumes | Produces | Retry behaviour |
|---|---|---|---|
| `classify` | `books.genres`, `genre_source`, `categories.yml` | `books.category`, `books.needs_review`, `genre_source` | 3 failed runs, then parked |
| `acquire_ebook` | title, author, `ShelfmarkClient` | `queued_at`, attempted release ids in `output_path` | 5 failed runs, then parked |
| `acquire_audiobook` | same, against the audiobook sources, plus the free-space floor | as above | 5 failed runs, then parked |
| `place` | the placed formats, the staging dir, `books.category` | `output_path` = `{"ebook": …, "audiobook": …}` | 3 failed runs, then parked |
| `index` | the paths in `place`'s `output_path` | a rescan per library app; library ids cached in `service_map` | 4 failed runs, then parked |
| `notebook` | the ebook path, the category's notebook name | `artifact` = Open Notebook source id | 4 failed runs, then parked |
| `verify` | the placed paths, each service's search | `output_path` = forced-rescan counter, 0–3 | 6 failed runs, then parked |
| `shelve` | both acquire statuses, `verify` | `artifact` = the shelf moved to; the shelf change itself | 3 failed runs, then parked |

`MAX_ATTEMPTS` is the per-stage budget, and it is not uniform: acquisition gets
5 because a dead release is common, `verify` gets 6 because search endpoints lag
a rescan by seconds, and the rest get 3 or 4.

`discover` is not in that table because it is not done to a book — it is where
books come from. The scheduler's other two periodic jobs, the credential probe
and `reconcile`, are also outside the per-book machine.

## How a stage is run

A stage row holds a `status`, an `attempts` count, a `detail` string, a
`failure_kind`, a `service`, and — for the stages that need to hand state
forward — `output_path` and `artifact`.

Statuses are `pending`, `running`, `ok`, `failed`, `skipped` and `blocked`.
`ok`, `failed` and `skipped` are terminal; `skipped` and `ok` both count as
*satisfied* for the purpose of releasing a downstream stage.

`blocked` is the important one. It means the stage is waiting on the outside
world, not failing:

- `mark_running` increments `attempts` before the stage runs, and `mark_done`
  subtracts one again whenever the result is `blocked`. A blocked run therefore
  costs nothing against `MAX_ATTEMPTS`, which is what lets a download wait for
  an hour without exhausting its own retries.
- A blocked stage keeps its `service` (so the per-service view can say "blocked
  because Shelfmark is unreachable") but never carries a `failure_kind`, because
  it has not failed.
- A `blocked` result always carries a detail string saying what it is waiting
  for, and where possible how long, because "blocked" with no reason is
  indistinguishable from a wedged stage.

`failed` carries a `failure_kind` — `auth`, `network`, `server`, `busy`,
`notfound`, `data`, or empty — and, where one service is to blame, its name. The
kind is the thing the UI acts on: `auth` needs a human in Settings, `data` is
usually a fact about the world, and `auth`, `network`, `server` and `busy` are
treated as actionable. `busy` is a 408/425/429, which is a rate limit rather
than a refusal: transient, and forgiven on the clock like a 5xx. `service` is
empty for `place`, which is local disk, and for `index` and `verify`, which
each fan out over several services and name them in the detail instead of
picking one to blame.

## The scheduler

One background thread (`goodreads-sweep`) runs a sweep every `POLL_INTERVAL`
seconds, 15 by default. A sweep does its periodic jobs first, then the book
work:

- **Credential probe** every 5 minutes — calls something that requires auth
  wherever one exists (listing notebooks, listing libraries) rather than a bare
  health route, so a wrong key cannot read green. Shelfmark is the exception:
  that instance has no auth, so its health route is the honest check.
- **`discover`** every 15 minutes — reads the Goodreads shelf and upserts.
- **`reconcile`** every 6 hours — compares recorded shelves against Goodreads
  and resets `shelve` on anything that has drifted.
- **Adopt the breaker backlog** — opens on any recent outage that parked books
  before the breaker existed, and reclaims them. Idempotent, and it runs after
  the probe so the breaker is never deciding against stale health.

`POST /api/sweep` forces all three and then sweeps.

The book pass is bounded and concurrent. Each sweep gets a 120-second budget and
advances up to 6 books at once, because the work is nearly all network waiting;
a sweep that overruns 300 seconds logs a warning, and one that reaches twice its
budget stops regardless. Books are served least-recently-touched first, so a
bounded pass still rotates through the whole list instead of re-trying the same
head of it. "Least recently touched" is the *most recent* timestamp on the book,
not the oldest: an oldest-first key never changes once set, so the same books
led every sweep and the untouched remainder never got its turn.

A second sweep cannot start while one is running — `sweep()` takes a lock
non-blockingly and returns `{"skipped": "a sweep is already running"}`.

Inside a book, `_advance` walks `models.STAGES` in order:

1. If the stage's status is satisfied, skip it.
2. If the stage has failed and `attempts` has reached `MAX_ATTEMPTS`, stop — the
   book is parked, and nothing downstream runs.
3. If the stage's prerequisites are not all satisfied, skip it for this pass.
4. Otherwise run it, record the result, and let the next stage see it.

Three wake-ups live in that loop, because a stage that produced nothing and was
therefore a no-op has to be brought back when the thing it was waiting for
arrives:

- A completed acquisition whose format is not in `place`'s `output_path` resets
  `place`. Without it, a download that finished after placement gave up left the
  file sitting in staging while `place` stayed satisfied.
- A completed `acquire_audiobook` on a book already shelved as `collected-pdf`
  resets `shelve`, so it moves to the both-formats shelf.
- The loop top re-checks that on every later sweep, so a `place` that finished
  `skipped` is not left satisfied by an empty answer. Only a *completed*
  acquisition triggers it: a download still in flight is `blocked` or
  `running`, never `ok`, and once `place` has returned `skipped` both acquire
  stages are terminal, so nothing can be in flight. The check is against the
  placed formats, so an already-filed book is never pushed back to pending.

Resetting a stage resets it and everything downstream of it in `models.STAGES`,
clearing status, attempts, detail, artifact, `failure_kind`, `service`,
`held_by`, `queued_at`, `output_path` and the timestamps. That is what both the
UI's Retry button and `repair` use — including for a stage the breaker was
holding, which is why a manual Retry during an outage re-holds the book rather
than doing nothing.

## discover

Reads the to-read shelf and registers anything new. Idempotent by construction:
books are keyed on the Goodreads id, so a re-run refreshes metadata without
creating a duplicate or resetting progress.

Genres are only looked up for books that have not been seen before, so a large
shelf does not turn into hundreds of API calls every sweep. The upsert never
downgrades: an empty genre list or an empty category from the scraper leaves the
stored value alone, because the shelf scrape carries no genres and writing them
blindly would revert every classified book to the fallback on the next sweep.

## classify

Consumes the book's genres and `categories.yml`; produces exactly one category.

`resolve_category` scans the genres in the source's own relevance order and
takes the first rule that matches, with rules sorted longest-needle-first. The
longest rule wins so that "science fiction" beats "fiction" regardless of the
order they appear in the file. Matching nothing falls back to the configured
fallback category and sets `needs_review`.

When no rule matched any genre — whether the genres were empty or none of them
matched — it tries the title against the same rules. A title match is a real
match against a rule that was written by hand, so it is not flagged for review,
but it is stored with `genre_source = title` so an inferred category is always
distinguishable from a sourced one.

Retried 3 times. A `classify` that ends `needs_review` still returns `ok` — the
book is placed in the fallback folder and flagged, not failed.

## acquire_ebook and acquire_audiobook

Both are the same two-phase machine over `ShelfmarkClient`, differing only in
the content type, the root they check on disk, and the extension set they
accept.

**Phase 1, not queued.** Check the free-space floor, search, rank, hand the best
release to Shelfmark, and record `queued_at`. The presence of `queued_at` is
what stops the next sweep queueing a second copy of the same book. The release
ids already attempted are kept in `output_path`, capped at the last 4.

**Phase 2, queued.** Watch the task.

The free-space guard applies to audiobooks only, against
`AUDIOBOOKS_ROOT`:

```
MIN_FREE_SPACE_GB=50
```

Below that floor the stage fails with `kind="data"` and a detail naming both the
free space and the floor, and the UI turns that into "free space, then retry".
Audiobooks are the ones that can arrive as 400 MB of files and then need a
second copy during processing; an ebook is not going to fill a disk. The check
happens before the search, so a full disk does not waste an upstream query.

Three budgets bound the stage:

| Budget | Value | What it is for |
|---|---|---|
| `MAX_RELEASE_ATTEMPTS` | 4 | Different releases tried for one book before the stage fails |
| `QUEUE_ABANDON_SECONDS` | 600 | A `queued_at` marker with no matching task behind it is stale |
| `MAX_TRANSIENT_HOURS` | 24 | How long an upstream 5xx is forgiven before the book is written off |

**Nothing found** is a fact, not a fault. The stage resets the transient
counter and reports it as `kind="data"`, which keeps it out of the pile that
needs a human.

There are two forms of "nothing found", and both read as the *same* fact.
Zero releases at all is `no audiobook releases found for '<title>'`. Releases
that came back and were all rejected as the wrong format — a film wearing an
audiobook's clothes, an epub for an audiobook search — is the same sentence
with the rejection detail appended: `no audiobook releases found for '<title>'
— the 1 release(s) the search returned were not audiobooks. E.g. …`. It used to
lead with *"but every one was the wrong format — not a book"*, which read as a
fault with a rejection reason bolted on and put one alarming row per book on
the panel; an audiobook search that only finds an epub has established that no
audiobook exists, which is not something a human can act on. The detail names
how many were rejected and shows two of them, because someone looking at that
*one* book still wants to know what the search threw away.

**A 5xx or a 429 from the search** is the opposite case: Shelfmark routinely
answers `503 every indexer failed` while the identical search succeeds a
moment later, and Anna's Archive answers `429` when too many searches are in
flight at once. Rather than marking books failed for that, the stage blocks
with the outage duration in the detail, and only after 24 hours of continuous
failure does it return `kind="server"`. It also tells the service breaker at
that point — see below. A `429` is `kind="busy"`, a rate limit rather than a
refusal, and if the service sent `Retry-After` the breaker's cooldown honours
it rather than guessing.

**The release fails.** Shelfmark reporting `error`/`failed`/`cancelled` on the
chosen release is not a dead book — a dead NZB is common and the same title
usually has several working copies. The `queued_at` marker is cleared, the
release stays in the attempted list, and the stage blocks so the next sweep
picks the next-best candidate. After 4 different releases it fails.

**The task vanishes.** Shelfmark keeps its queue in memory, so restarting the
container drops every pending task. If no task matches by release id or by
title and author, the stage looks on disk first — a task that completed and was
cleared from the status listing leaves a file, and finding it is success. Only
if there is also nothing on disk and the marker is older than 10 minutes does it
re-queue. The first version only noticed a lost queue when the queue was
*entirely* empty, which it rarely is, and then waited the full hour.

**Already-queued is not an error.** Shelfmark answers HTTP 500 when the release
is already in its queue, so that specific message is read as "queued", recorded,
and the stage blocks.

## place

Turns a finished download into a file at its final path. Same-filesystem rename
throughout — explicitly never a copy, so a duplicate is impossible rather than
merely unlikely.

Placement succeeds as soon as **one** format is in place. Requiring both stalled
the whole chain behind an audiobook that might never arrive: the ebook was in
the library and nothing downstream would touch it. The other format is picked up
later, because a subsequently completed acquisition resets this stage.

**Ebooks.** The destination folder is `<category folder>/<book folder name>`.
The source is the best-matching entry in the staging dir (`BOOKS_ROOT` +
`STAGING_DIRNAME`, default `newDownloads`). If the move crosses a filesystem,
`move_into_place` raises rather than falling back to a copy, and the stage
reports that as a failure with no service attached — it is local disk, and the
fix is a mount. If Shelfmark left the file inside a folder of its own, the
now-empty folder is removed.

**Audiobooks.** These arrive in the right tree but the wrong shape: the `audiobook`
category completes straight into the audiobooks root with raw usenet names.
Only entries sitting *directly* in a staging root are considered — the audiobooks
root, and `.incoming` beneath it. Anything already nested under an author folder
is treated as organised and left alone, which is what keeps the stage idempotent
and stops it re-shuffling a library that is already fine. The destination is
`<Author>/<Title>`, which is the layout Audiobookshelf matches on. If two
differently-named copies match, only the best-scoring one is moved and the other
is left for `audit` to report — merging two rips automatically would be worse
than leaving them.

**Placement is idempotent**, checked at the top of each path: an ebook already
sitting in its category folder under a folder name scoring at least 0.55 for the
book is returned as placed, and an audiobook already in `Author/Title` with media
in it is returned likewise. Both were added because re-running the stage — which
re-classification and the repair command do — otherwise reported the file missing
from staging, where it has not been since the first run.

**A second look at the category.** The embedded-metadata genre source needs the
real file, which did not exist when `classify` ran. Once the ebook is on disk the
stage re-resolves the category and, if it improved, moves the file to the new
category folder. It only ever does this for a book that was unclassified or
flagged for review.

**Output.** `output_path` holds a JSON object of the format to final path:
`{"ebook": "/path/to/books/Fiction/…", "audiobook": "/path/to/audiobooks/…"}`.
Every downstream stage reads the world through it.

**Failure.** If neither format was acquired and both acquire stages are
terminal, the stage returns `skipped("nothing downloaded — nothing to place")`
rather than blocking forever. That distinction matters: it releases `index`,
`notebook` and `verify`, and it lets `shelve` be the stage that tells the
operator nothing was found.

## index

Makes the library apps notice the file. Every app already mounts the same
directory, so this stage never imports or uploads anything — it asks each app to
rescan. One file on disk, four readers.

Ebooks trigger Kavita, BookLore and Grimmory; audiobooks trigger
Audiobookshelf. An app whose tree did not change is not attempted at all, so a
book with only an ebook does not spend its retries on Audiobookshelf. For each
app the stage finds the library containing the file locally, caches the name to
id mapping in `service_map`, and asks for a scan.

**Partial failure is reported but does not stop the pipeline.** The file is
already on disk and readable; blocking the rest of the chain because one app is
misconfigured is the wrong trade. A run where at least one app accepted the
rescan returns `ok`, with the failures appended after `FAILED:` in the detail.
Only when every attempted app failed does the stage return `failed`, and it
recovers the failure kind from the collected messages — `auth` checked first,
because a rejected key is the one a human must fix.

Retried 4 times.

## notebook

Adds the ebook to the Open Notebook notebook mapped to its category, by file
path rather than by upload, because the multipart route would store a second
copy of the epub inside Open Notebook. The path is translated into Open
Notebook's namespace first.

An audiobook alone gives Open Notebook nothing to index, so a book with no
placed ebook is `skipped`. A format Open Notebook cannot read is also `skipped`,
with the reason in the detail — retrying an `.azw3` forever would be a permanent
failure for a format neither app supports. The extension check is not enough on
its own: a `.epub` that is really a zip of an extracted folder passes every
filename test and is then rejected upstream with a 415. So an epub is opened and
checked for `META-INF/container.xml` before anything is sent, which turns a bad
rip into an honest skip.

Then it resolves the notebook by name. A category pointing at a notebook that
does not exist fails with `kind="data"` and a detail naming both the notebook and
the category, because that is a configuration error a human fixes in
`categories.yml`.

For the source itself it reuses an existing source for the same path if there is
one, and only otherwise creates it. Either way it **reads the book back** before
returning success:

- Attachment is not trusted from the attach call, because that endpoint appends
  rather than being idempotent. If the source is not in the notebook afterwards,
  the stage fails; duplicates are reported separately by the UI and collapsed by
  `repair`.
- If reading the source back answers 404, the id handed out by the list endpoint
  has since gone. The stage blocks rather than failing — the next attempt
  recreates the source from the file, which is cheap and always correct.
- A source the service itself marked `error` or `failed` is `kind="data"`:
  the book is attached but has no readable text, and retrying will not change
  that.

Retried 4 times. The source id is kept in `artifact`.

## verify

Proves the book is actually present in each service. Every earlier stage reports
what it *asked* a service to do; none of them confirms the service ended up
holding the book, and that gap is how a book gets marked complete that cannot be
found anywhere. This runs after `notebook` and before `shelve`, so a book is
never moved onto a collected shelf on the strength of an unchecked claim.

It searches each service for the title and records what it found. Three things
make that less trivial than it looks:

- **Search endpoints are not uniform.** Kavita is queried with a single
  distinctive word, because its search is a literal match against its own series
  names and a leading article or a multi-word query returns nothing at all;
  `titles_match` decides between the candidates that come back. BookLore and
  Grimmory are queried with the full title and filtered locally.
- **Rescans are asynchronous.** A miss within 10 minutes of `index` finishing is
  treated as "not yet" rather than as a failure — and only on the *first* check,
  because applying the grace window every time left books oscillating inside it
  forever.
- **Open Notebook is only checked** when the format is one it can read, and only
  when the category maps to a notebook.

This stage writes no state to any service. It produces proof, or it does not.

**When something is missing**, past the grace window, the stage forces another
rescan before failing: it sets `index` back to `pending` with `attempts = 0` and
increments its own counter in `output_path`. Services do skip files on a scan and
pick them up on the next one. That is bounded at 3 forced rescans, after which
the stage fails with `kind="server"` and a detail saying it needs a look at the
service itself.

Retried 6 times, with the longest budget of any stage, precisely because a
missed search hit should cost a sweep rather than a book.

## shelve

Moves the book off to-read onto the shelf that reflects what actually landed:

| What landed | Shelf |
|---|---|
| both formats | `collected-pdf-audiobook` |
| ebook only | `collected-pdf` |
| audiobook only | `collected-audiobook` |

The hyphen in the both-formats shelf name is load-bearing. An ampersand cannot
be used: a shelf named `collected-pdf&audiobook` cannot be addressed through
`?shelf=`, so Goodreads renders the whole account instead, the shelf add
silently does nothing, and the book is then removed from to-read and lands
nowhere.

This is the only irreversible step in the pipeline, so it is the most
conservative about when it runs. Its prerequisites are `place` and `verify`, and
on top of that it defers while an acquisition stage is *running right now*. Only
`running` counts — a stage that is `blocked` or `pending` may be waiting on an
outage or a queued slot, and waiting for it means waiting indefinitely. That
distinction is what let fully-verified books sit unshelved behind an audiobook
blocked on an upstream quota.

If nothing landed at all, Goodreads is left alone: the stage logs an error and
returns `skipped`, with a detail saying the book was left on to-read. "We found
nothing" is not "we collected it", and the book should still be visible on the
to-read list.

Two switches can hold it: `auto_shelve` on the book, and the global setting of
the same name. Both return `blocked`, not `failed` — they are a choice, not a
fault.

Failures are sorted by whether a human can act:

- An expired Goodreads session fails with `kind="auth"` and the service named.
  The kind is load-bearing rather than decoration: without it the failure is not
  actionable, so every book would sit stuck on the irreversible stage with
  nothing in the attention list and no prompt to sign in again.
- A rate limit or an HTTP error from Goodreads **blocks** instead of failing.
  Goodreads throttles and times out under load; that is not a reason to count a
  strike against a book.
- Anything else is failed and classified from the text.

On success the shelf name is stored in `artifact`, which is what `reconcile`
compares against reality.

## Retries and the transient clock

Retry cadence is the sweep: a stage that is neither satisfied nor parked is
re-run the next time the book comes up. There is no per-stage backoff timer and
no sleep — the sweep interval and the least-recently-touched ordering do that
job.

A **blocked** stage is not a retry at all. It costs no attempt, so a book
waiting on a download or on an upstream outage can wait indefinitely without
burning anything.

A **transient failure** is forgiven on a clock, not on a count. Retrying costs a
couple of seconds, so a count budget is burned through in minutes by an outage
that lasts hours, and books were being marked permanently failed before the
source ever came back. How long something has been down is what actually
separates "temporarily unavailable" from "broken". The budget is 24 hours,
measured from when the outage started rather than from how many times the
pipeline happened to look.

The clock state lives on the stage row:

- `transient_since` is set only on the first forgiven fault of a run, so it
  measures the outage, not our polling.
- `transient_count` counts forgiven faults, and appears in the detail so the
  scale of the problem is visible: `[transient: 3.4h of 24h, 11 attempts]`.
- When the stage finally fails, the detail says how long the source has been
  down, so "not a passing outage" is a stated fact rather than an inference.
- A stage that reaches the sources and comes back empty calls `reset_transient`,
  because it proved the source was reachable; the next outage gets a fresh
  clock. Both directions of that matter — an outage forgiven since yesterday
  must not expire a book the moment a search merely succeeds but finds nothing.

Two implementations of the same rule exist, deliberately. The acquire stages
apply it inside themselves, because Shelfmark's 5xx (`every indexer failed`) is
a routine answer to a routine search and belongs to that stage's own semantics.
The scheduler applies it to any stage that returns a *failed* result with
`kind` of `network` or `server` — which covers the agent-less failures: a
service being restarted, a transient 5xx under load. Either way the outcome is
the same: the stage is set back to `blocked`, the `failure_kind` is cleared, the
`service` is kept, and the detail explains the clock.

The scheduler's version runs after the result is recorded, and it overwrites the
status on the row directly. A later successful run clears `service` on its own,
because `mark_done` writes that column unconditionally.

`network`, `server` and `busy` are forgiven this way. `auth` is never forgiven
— it needs a human — and `data` is a fact about the world that will not improve
by waiting.

## The service breaker

The transient clock above measures **one book's patience**. The breaker in
`app/breaker.py` measures something else: **whether a service is usable at
all**. They are deliberately separate, and they never both run for one failure.

The distinction exists because of what the first one alone produces. When
Anna's Archive stopped resolving from the host, Shelfmark's every release
search answered `503 Unable to reach download source`. Each of the 169 affected
books ran its own acquire stage, waited out its own 24-hour clock and then
parked, so one unreachable host arrived as 155 book titles on the panel and a
dashboard tile reading "169 books need a human". The bug was not the outage; it
was that a *service-level* condition was stored, aged and reported as a
*book-level* one, 169 times over.

So the clock moved to where the fault actually is. One row per service in
`service_breaker`, shared by every book:

| | breaker | transient clock |
|---|---|---|
| Scope | one service, all books | one book, one stage |
| Question | "is this service usable right now?" | "has this failed long enough to be the book's problem?" |
| Action | do not attempt the stage at all | attempt, then write `blocked` instead of `failed` |
| Ceiling | a cooldown, capped at 1 h | 24 h, then the failure stands |

The state machine is `closed → open → half_open → closed`:

- **closed** — normal. Three *consecutive* transient failures against one named
  service, each within a day of the one before it, with no `ok` in between,
  trip it. `ADVANCE_WORKERS` is 6, so a real outage clears that inside its
  first batch; one book's unlucky 500 against an otherwise healthy service
  leaves the counter at 1 and the transient clock handles it exactly as before.
- **open** — no stage that depends on that service is run. Books already
  failing for it are *reclaimed* into `blocked` rows with `held_by` set, so the
  panel stops reading them as failures, and every sweep from then on costs one
  dial per cooldown instead of one per book.
  The sweep that *begins* the outage is the exception and worth stating
  plainly, because it used to be written as "one more book": `_sweep` takes its
  `holding` snapshot before its workers start, so on that first sweep nothing
  is held yet and nothing is gated, and `acquire._run` can only check the
  breaker *after* its dial — the 503 that says the service is down is the same
  call that spent the search. Every book that sweep reaches is therefore dialed
  once, and during a rate-limit outage each of those is one more search into
  the hole. Measured at 155 in this operator's library, and pinned at 24 in
  `test_one_probe_per_cooldown_however_many_books_are_waiting`. It is one dial
  per book per *outage*, not per sweep, three of those dials are the evidence
  that trips the breaker at all, and none of them writes a book off — the
  failures are downgraded to holds. What it costs is upstream requests.
- **half_open** — after the cooldown, exactly one book is allowed to run one
  stage. That probe is the only honest test of whether the service is back: a
  green `/api/health` is not, as the Shelfmark instance proves (its container
  answered `200` in 5 ms throughout the outage — the fault was upstream of it).
  The probe's verdict comes from the *raw* result and only from evidence the
  service itself produced: `ok`, the service's own `auth` and `notfound`, a
  download already queued with it, and — since the first version of this
  verdict held the opposite and silently wedged healthy libraries — a `data`
  failure the stage says it *dialed* for (`StageResult.answered`), which is
  what "no release exists in any configured source" is. A transient failure, a
  result that never reached the service (the free-space check that runs before
  the search, an unhandled stage error), and a hold the stage took on the
  breaker's behalf all re-open it on a doubled cooldown. The probing book is
  never failed for it.
  `data` is why the flag exists rather than a rule about kinds: the free-space
  check and the empty search return the same kind from the same function, and
  only one of them asked.

The cooldown doubles from 5 minutes to a 1-hour cap, and any `Retry-After` the
service sends wins if it is longer. A claimed probe that never answers is
released after 15 minutes, so a process restart mid-probe cannot wedge it.

**There is also a way out by hand**, because everything above closes a breaker
by *proving* something, and the failure mode left over is a breaker nothing can
prove anything about: it holds a working service, nothing logs an error, and no
clock, restart or button ends it. `breaker.clear` — `POST
/api/services/{name}/breaker/clear`, offered as **Clear hold** on the held row
and on the service page, behind a confirmation — releases the books exactly as
a recovery does, including the per-book clock reset, and is honest about what
it did rather than what the operator might hope: the log line is a `warning`
carrying the reason, the response says the service has *not* been proved up,
and `last_ok_at` is left where it was. If the service really is down the next
sweep trips it again at the base cooldown, on fresh evidence.

Three things keep it from being the thing that hides a broken upstream:

- while it is open, `_sweep` records **one** event naming the service, instead
  of one per book;
- the panel shows **one** row per held service, with the held books listed
  under it, and its `fix` line changes once the outage outlives the 24-hour
  grace to point at the service itself;
- `adopt_backlog()` runs once per sweep and trips on recent `failed` rows the
  breaker never witnessed — the 169 that were already parked when it was
  deployed. A parked book is never attempted again, so it can never feed a
  failure in, and without this the breaker would sit closed while the panel
  showed them.

**Closing clears the per-book clock** for everything the outage was holding. A
`blocked` stage is not satisfied, so `_advance` picks it up on the very next
sweep with no operator action; but a book whose `transient_since` was set
during the outage would otherwise be at 25 hours the first time it failed
afterwards and would park on the spot, having "failed for 25 hours" without
having been tried for 24 of them. That reset is scoped to the service that was
holding — nothing a book earned on its own is forgiven by it.

What the breaker deliberately does **not** do:

- **`auth` never trips it** and is never reclaimed. A wrong API key is a
  misconfiguration a human must fix, and it stays loud at whatever scale it
  happens.
- **`data` is never reclaimed.** "No audiobook exists", "every candidate was
  the wrong format" and "Shelfmark reported error after 4 releases" are facts
  about releases. They keep their own groups and their own fix text; they
  simply stop growing while the service is held, because no new release is
  attempted.
- **`index` and `verify` are not gated.** They fan out over up to five apps,
  and holding the whole chain because one is down is the trade `index.py`
  explicitly refuses to make. The honest version needs a per-book dependency
  set declared by the stage.
- **`ADVANCE_WORKERS` is not lowered.** Six concurrent searches is what makes a
  429 likely, but throttling it would slow every healthy sweep to pay for an
  outage. The breaker is the sharper version of the same idea — once a service
  is held it runs *zero* books at a time — and it reverts by itself.

The state is visible two ways without parsing prose: `breaker` in the sweep
summary at `/api/state`, and a `breaker` object on each entry of
`/api/health/services` and `/api/services/{name}`.

## Self-healing inside stages

Most of the failure handling is not in the scheduler but in the stages
themselves, because only the stage knows what the failure meant.

| Symptom | Cause | What the stage does |
|---|---|---|
| `no ebook/audiobook releases found` | Nothing exists in any source — a fact, not a fault | Fails `kind="data"` after resetting the transient counter; the book reads as *partial* or *unavailable*, not as needing attention |
| `found N release(s) … but every one was the wrong format` | The indexer answered an audiobook query with films | Releases are filtered on the indexer's own Newznab category, size and video-release name patterns before ranking; the detail shows two examples |
| `Shelfmark reported error: …` | The chosen release is dead | The release is remembered and the next-best candidate is queued; up to 4 releases |
| `no matching task in Shelfmark after N min` | Shelfmark keeps its queue in memory and a restart drops it | Disk is checked first, then the marker is re-queued rather than waiting out an abandon timeout |
| `queued, waiting for Shelfmark to start` | The queue is genuinely slow | Blocks, which costs no attempt, for as long as the task is discoverable |
| `every indexer failed` (503/429) | An upstream source is down or out of API quota | Blocked on the 24-hour clock; fails only after the source has been down a full day |
| `already in the download queue` | Shelfmark answers 500 for a duplicate queue | Read as queued, not as a failure |
| `nothing matching it is in newDownloads yet` after a re-run | `place` was re-run after the file had already been filed | Placement checks the destination first and returns it, so a re-run is a no-op |
| `MISSING from Kavita…` | A service silently skipped the file on a scan | A rescan is forced and re-checked, up to 3 times, before a human is asked |
| `first check after a rescan` | Rescans are asynchronous | A miss inside the 10-minute grace window blocks rather than fails |
| `source N was created but is not attached to …` | Open Notebook's attach endpoint appends rather than being idempotent | Attachment is verified by reading the source back; a source that has gone is recreated |
| Open Notebook 415 on a `.epub` | The file is a zip of an extracted folder, not an epub | Detected locally by looking for `META-INF/container.xml`; skipped with that reason instead of retried |
| `the queue is not persisted, so a restart loses it` | As above, at the task level | Re-queued immediately |
| A download completed after placement gave up | `place` was satisfied by an empty answer | The scheduler resets `place` when a format is not in `output_path` |
| An audiobook arrived on a book already shelved as `collected-pdf` | Shelf was chosen when only one format existed | The scheduler resets `shelve` so it moves to the both-formats shelf |

## When a stage fails permanently

A stage reaches its permanent end when `status = failed` and `attempts >=
MAX_ATTEMPTS`. At that point the scheduler stops: `_advance` returns `parked`
and does not run that stage or anything after it again. Downstream stages stay
unmet-prerequisite and are skipped on every later sweep, so a parked book costs
one failed stage, not a chain of them.

Parking is not deletion. The book keeps its row, its stage history, its attempt
count, the real error text and the paths that did land, and the UI shows it with
a Retry button on the stage that is blocking it. `Retry` resets that stage and
everything downstream, which clears the attempts counter and lets the pipeline
try again from that point — for the case where the world changed.

What the parked book *looks like* is a separate decision from the fact that it
is parked, because lumping "we could not find an audiobook that does not exist"
together with "your Kavita key is wrong" made the attention list useless. A book
is classified into one of five states:

| State | Meaning |
|---|---|
| `done` | Every stage is `ok` or `skipped` |
| `working` | Something is genuinely outstanding and may still progress |
| `partial` | The book is in the library, but one format could not be found — nothing is wrong and there is nothing to fix |
| `unavailable` | No format was found in any source, so there is no book at all |
| `failed` | Something a human can act on; the only state that belongs in "needs attention" |

The rules behind that: any failure in a stage after placement is `failed`,
because the book is supposed to be in the services and is not. If only an
acquisition stage failed, it is `failed` when its kind is actionable
(`auth`/`network`/`server`) and otherwise `partial` when something was placed
and `unavailable` when nothing was. And a book whose only outstanding stage is
the audiobook counts as `partial`, not `working`, once that stage is blocked,
failed or pending and the book has been placed — otherwise books were described
as in progress while waiting on a source that was never coming.

The trade the pipeline makes is that `data` is a fact, not a fault. A book with
no audiobook in any configured source will exhaust its 5 acquisition attempts,
park, and read as *partial*. It is not in the attention list, because there is
nothing to attend to.

Read-only reporting and repair are separate commands; none of them change
pipeline state except `repair --apply`, which resets the earliest *retryable*
failing stage on each affected book and leaves acquisition failures with
`kind="data"` alone on purpose — resetting those also resets everything
downstream, which once knocked already-shelved books back to pending for no
reason.
