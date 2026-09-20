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

`failed` carries a `failure_kind` — `auth`, `network`, `server`, `notfound`,
`data`, or empty — and, where one service is to blame, its name. The kind is the
thing the UI acts on: `auth` needs a human in Settings, `data` is usually a fact
about the world, and only `auth`, `network` and `server` are treated as
actionable. `service` is empty for `place`, which is local disk, and for `index`
and `verify`, which each fan out over several services and name them in the
detail instead of picking one to blame.

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
`queued_at`, `output_path` and the timestamps. That is what both the UI's Retry
button and `repair` use.

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
counter, then distinguishes the two cases: zero releases at all, versus
releases that were all rejected as the wrong format — in which case the detail
names how many were rejected and shows two of them. Both return `kind="data"`,
which keeps them out of the pile that needs a human.

**A 5xx from the search** is the opposite case: Shelfmark routinely answers
`503 every indexer failed` while the identical search succeeds a moment later.
Rather than marking books failed for that, the stage blocks with the outage
duration in the detail, and only after 24 hours of continuous failure does it
return `kind="server"`. See the transient clock below.

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

Only `network` and `server` are forgiven this way. `auth` is never forgiven —
it needs a human — and `data` is a fact about the world that will not improve by
waiting.

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
