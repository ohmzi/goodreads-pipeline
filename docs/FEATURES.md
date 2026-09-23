# Features

Everything the pipeline does, one capability at a time. The [README](../README.md) has the
short version; [PIPELINE.md](PIPELINE.md) walks through each stage, the scheduler, retries and
the service breaker in full.

## The pipeline

```
Goodreads to-read
  -> classify          one category, from the book's genres
  -> acquire           Shelfmark: search + download (ebook, audiobook)
  -> place             ebooks: rename into <Category>/
                       audiobooks: rename into Author/Title/
  -> index             rescan Kavita, BookLore, Grimmory, Audiobookshelf
  -> notebook          add to the matching Open Notebook notebook
  -> verify            confirm each service really sees the file
  -> shelve            move off to-read onto collected-pdf / -audiobook / both
```

Two periodic jobs run alongside that chain: `discover` reads the shelf, and
`reconcile` checks what was recorded against what Goodreads actually shows.

## 📖 Tracks the shelf

- Reads the to-read shelf through a captured browser session, keyed on the
  Goodreads id — a re-run refreshes metadata instead of creating duplicates
- Paginates a shelf that has outgrown one page
- Reconciles recorded shelves against Goodreads **every 6 hours** and resets
  anything that drifted
- Moves finished books off the exclusive to-read shelf using the
  shelve / confirm / destroy / re-shelve / confirm sequence Goodreads requires,
  creating the destination shelf first if it does not exist

## ⬇️ Finds and downloads

- Searches, ranks, hands the best release to Shelfmark, and watches the task
- Rejects releases on the indexer's Newznab category, on size, and on
  video-release name patterns **before** ranking — so a film cannot be queued
  as an audiobook
- Records every release id tried, so a dead release falls through to the
  next-best candidate (up to **4** per book) rather than failing the book
- Re-queues a task missing from Shelfmark's in-memory queue for **10 minutes**
- Refuses to start an audiobook download below a configurable free-space floor

## 🏷️ Classifies into exactly one category

- Walks four genre providers — Goodreads AppSync, OpenLibrary, Google Books,
  embedded epub `dc:subject` — stopping at the first that answers
- Each provider is isolated, so one timing out cannot stop the next; the
  embedded source works with every API down
- Falls back to matching the **title** against the same rules, stored as
  `genre_source = title`, so an inferred category is always distinguishable
- Flags anything matching nothing as **needs review** rather than misfiling it

## 📁 Files without ever duplicating

- Ebooks → `<Category>/Author - Title (Year)/`, audiobooks → `Author/Title/`
- Every placement is `os.rename`: a move that would cross a filesystem **raises
  rather than falling back to a copy**, so a duplicate is impossible
- An existing destination is never clobbered — the incoming file is dropped
  only when it is provably the same inode; everything else gets a suffix
- Re-running placement is a no-op

## 🔍 Indexes, then proves it

- Rescans Kavita, BookLore and Grimmory for books, Audiobookshelf for
  audiobooks — nothing is imported or uploaded
- Skips an app whose tree did not change rather than failing it
- `verify` searches each service **before** anything irreversible happens on
  Goodreads, matching on a normalised word-boundary form with the leading
  article ignored, treating a differing series number as disqualifying
- A miss within 10 minutes of a rescan blocks rather than fails, because
  rescans are asynchronous

## 📓 Feeds Open Notebook

- Each ebook is attached to the notebook its category maps to, handed over as a
  `file_path` and never as an upload, so no second copy is stored
- Attachment is verified by reading the source back; a source attached more
  than once is collapsed

## 🛡️ Survives outages without writing off books

- A per-service **circuit breaker** holds every book waiting on a service after
  **3** consecutive transient failures — an upstream outage becomes one row
  naming the service instead of hundreds of failed books
- A half-open probe lets one book through to discover when it is back, and a
  stuck hold can be released by hand
- Separately, each book gets a **24-hour** grace on transient faults, measured
  from when the outage started — not from how many times it was retried

## ⚡ Stays responsive under load

- Each sweep gets a **120-second** budget and advances up to **6** books
  concurrently, serving least-recently-touched first, so a bounded pass still
  rotates through the whole list and publishes its result

## 🚨 Tells you what is wrong and what to do

- Every service is probed on a **5-minute** timer with an _authenticated_ call
  — never a bare health endpoint that a wrong API key would pass
- Every failed stage carries a `failure_kind` (`auth`, `network`, `server`,
  `busy`, `data`), and the UI groups failures **by cause rather than by book**,
  with a suggested next step in plain language and a _Retry all_ where retrying
  helps
- A broken credential raises a banner on every page with a link to the fix
