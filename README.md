<div align="center">

# 📚 Goodreads-Pipeline

**Watches a Goodreads _to-read_ shelf and carries each new book all the way to a
shelved, indexed library — without ever making a second copy of a file.**

[![Version](https://img.shields.io/badge/version-1.3.0-382110?style=for-the-badge)](docs/VERSION.md)
[![License](https://img.shields.io/badge/license-Apache--2.0-00635d?style=for-the-badge)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.10-3670A0?style=for-the-badge&logo=python&logoColor=ffdd54)](requirements.txt)
[![FastAPI](https://img.shields.io/badge/FastAPI-005571?style=for-the-badge&logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com)
[![Docker](https://img.shields.io/badge/Docker-2CA5E0?style=for-the-badge&logo=docker&logoColor=white)](Dockerfile)
[![SQLite](https://img.shields.io/badge/SQLite-07405E?style=for-the-badge&logo=sqlite&logoColor=white)](docs/ARCHITECTURE.md)

Self-hosted · single-operator · one container

</div>

---

## 🔗 The pipeline

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

Two periodic jobs run alongside that chain: `discover` reads the shelf, and
`reconcile` checks what was recorded against what Goodreads actually shows.

---

## ✨ Features

### 📖 Tracks the shelf

- Reads the to-read shelf through a captured browser session, keyed on the
  Goodreads id — a re-run refreshes metadata instead of creating duplicates
- Paginates a shelf that has outgrown one page
- Reconciles recorded shelves against Goodreads **every 6 hours** and resets
  anything that drifted
- Moves finished books off the exclusive to-read shelf using the
  shelve / confirm / destroy / re-shelve / confirm sequence Goodreads requires,
  creating the destination shelf first if it does not exist

### ⬇️ Finds and downloads

- Searches, ranks, hands the best release to Shelfmark, and watches the task
- Rejects releases on the indexer's Newznab category, on size, and on
  video-release name patterns **before** ranking — so a film cannot be queued
  as an audiobook
- Records every release id tried, so a dead release falls through to the
  next-best candidate (up to **4** per book) rather than failing the book
- Re-queues a task missing from Shelfmark's in-memory queue for **10 minutes**
- Refuses to start an audiobook download below a configurable free-space floor

### 🏷️ Classifies into exactly one category

- Walks four genre providers — Goodreads AppSync, OpenLibrary, Google Books,
  embedded epub `dc:subject` — stopping at the first that answers
- Each provider is isolated, so one timing out cannot stop the next; the
  embedded source works with every API down
- Falls back to matching the **title** against the same rules, stored as
  `genre_source = title`, so an inferred category is always distinguishable
- Flags anything matching nothing as **needs review** rather than misfiling it

### 📁 Files without ever duplicating

- Ebooks → `<Category>/Author - Title (Year)/`, audiobooks → `Author/Title/`
- Every placement is `os.rename`: a move that would cross a filesystem **raises
  rather than falling back to a copy**, so a duplicate is impossible
- An existing destination is never clobbered — the incoming file is dropped
  only when it is provably the same inode; everything else gets a suffix
- Re-running placement is a no-op

### 🔍 Indexes, then proves it

- Rescans Kavita, BookLore and Grimmory for books, Audiobookshelf for
  audiobooks — nothing is imported or uploaded
- Skips an app whose tree did not change rather than failing it
- `verify` searches each service **before** anything irreversible happens on
  Goodreads, matching on a normalised word-boundary form with the leading
  article ignored, treating a differing series number as disqualifying
- A miss within 10 minutes of a rescan blocks rather than fails, because
  rescans are asynchronous

### 📓 Feeds Open Notebook

- Each ebook is attached to the notebook its category maps to, handed over as a
  `file_path` and never as an upload, so no second copy is stored
- Attachment is verified by reading the source back; a source attached more
  than once is collapsed

### 🛡️ Survives outages without writing off books

- A per-service **circuit breaker** holds every book waiting on a service after
  **3** consecutive transient failures — an upstream outage becomes one row
  naming the service instead of hundreds of failed books
- A half-open probe lets one book through to discover when it is back, and a
  stuck hold can be released by hand
- Separately, each book gets a **24-hour** grace on transient faults, measured
  from when the outage started — not from how many times it was retried

### ⚡ Stays responsive under load

- Each sweep gets a **120-second** budget and advances up to **6** books
  concurrently, serving least-recently-touched first, so a bounded pass still
  rotates through the whole list and publishes its result

### 🚨 Tells you what is wrong and what to do

- Every service is probed on a **5-minute** timer with an _authenticated_ call
  — never a bare health endpoint that a wrong API key would pass
- Every failed stage carries a `failure_kind` (`auth`, `network`, `server`,
  `busy`, `data`), and the UI groups failures **by cause rather than by book**,
  with a suggested next step in plain language and a _Retry all_ where retrying
  helps
- A broken credential raises a banner on every page with a link to the fix

---

## 🛠️ Built with

| Layer | What it uses |
|---|---|
| **API + UI** | FastAPI, Uvicorn — UI served straight from `app/static/`, no framework and no build step |
| **Storage** | SQLite (WAL), one file, no ORM |
| **Goodreads session** | Playwright + Chromium on a virtual display, streamed to the browser over noVNC |
| **HTTP** | httpx, BeautifulSoup |
| **Crypto** | `cryptography` (Fernet) for credentials, scrypt for passwords |
| **Packaging** | Docker, on Playwright's own image so Chromium is version-matched |

---

## 🚀 Quick start

```bash
git clone https://github.com/ohmzi/goodreads-pipeline.git goodreads
cd goodreads
cp .env.example .env
python3 -c "import secrets; print(secrets.token_urlsafe(48))"   # -> GOODREADS_SECRET_KEY
chmod 600 .env
docker compose up -d --build
```

Create a login, then open `http://<host>:8091`:

```bash
docker compose exec goodreads python -m app.cli set-password you --generate
```

There is deliberately no default account. Library paths and any extra networks
belong in a gitignored `docker-compose.override.yml`; the committed compose
uses placeholders so a clone runs unedited.

> 📘 **Full instructions** — mounts, networks, first run in the UI, and running
> without the container — are in **[SETUP.md](docs/SETUP.md)**.

### 💻 From the command line

```bash
python -m app.cli report            # what completed, what failed, and why
python -m app.cli repair            # fix notebook links and stuck stages (dry run)
python -m app.cli audit             # duplicate audiobooks and titles in two places
python -m app.cli rename            # tidy loose audiobooks into Author/Title
python -m app.cli reclassify        # re-run classification over the library
python -m app.cli backfill-genres   # re-resolve genres and re-categorise
```

`report` and `audit` are read-only. `repair`, `rename`, `reconcile` and
`reclassify` change nothing without `--apply`.

---

## 🔐 Security

> This app is the most sensitive service you will run. It holds the key that
> decrypts every stored credential, it can drive a browser signed into a real
> personal account, and it can write to a real library tree.

Passwords are hashed with scrypt and compared in constant time. Sessions are
stateless HMAC-SHA256 tokens carrying a password epoch, so changing a password
evicts every outstanding session. Failed logins are throttled by username _and_
client address with a progressive delay rather than a lockout, so nobody can
shut the owner out of their own UI. The noVNC desktop has no published port:
x11vnc binds to loopback inside the container and the app bridges to it over a
session-checked WebSocket, leaving `8091` as the only way in.

> ⚠️ The threat model, credential storage design, reverse-proxy deployment, and
> an explicit list of known limits — including that the session cookie is a
> bearer token and that traffic is plain HTTP without a TLS terminator in front
> — are in **[SECURITY.md](docs/SECURITY.md)**. Read it before exposing this
> beyond a host you control.

---

## ⚙️ Configuration

> Secrets are split by lifetime: `.env` holds what must exist before the
> database can be read; service credentials are typed into the Settings page,
> encrypted with Fernet, and stored in the database.

`app/categories.yml` is the rule table that decides where a book lands — a
category key per genre needle, mapped to a folder and an Open Notebook
notebook. Matching is substring, case-insensitive, and longest-needle-first, so
`science fiction` beats `fiction` regardless of file order. The file ships in
the image and `/data/categories.yml` overrides it, so tuning survives a
rebuild, and it is re-read per classification, so an edit needs no restart.

> 📗 Every environment variable, every credential, the genre provider chain, and
> the library-root mapping each service sees are in
> **[CONFIGURATION.md](docs/CONFIGURATION.md)**.

---

## 🤔 Why the Goodreads login is manual

Goodreads removed its public API in 2020, then removed public shelf pages, and
`/user/sign_in` is now a stub that hands off to Amazon's sign-in flow. There is
no form to POST a password to — every maintained Goodreads tool today reuses a
persistent browser session instead.

So this runs Chromium on a virtual display inside its own container and streams
it to the login page over noVNC. You sign in once, the cookies are saved, and
everything afterwards runs over plain HTTP with them. When the session
eventually expires the UI says so and you do it again.

---

## 📖 Documentation

| Document | What is in it |
|---|---|
| 🚀 [SETUP.md](docs/SETUP.md) | Install, mounts and networks, first login, first run, bare-metal |
| ⚙️ [CONFIGURATION.md](docs/CONFIGURATION.md) | Every variable, credential, and path mapping; `categories.yml`; genre resolution |
| 🔐 [SECURITY.md](docs/SECURITY.md) | Threat model, credential storage, sessions, reverse proxy, known limits |
| 🔧 [OPERATIONS.md](docs/OPERATIONS.md) | Upgrading, backup and restore, the maintenance CLI, health, troubleshooting |
| 🏗️ [ARCHITECTURE.md](docs/ARCHITECTURE.md) | Components, module map, the database, concurrency, the path namespace problem |
| 🔄 [PIPELINE.md](docs/PIPELINE.md) | Every stage in detail, the scheduler, retries, the service breaker |
| 🔌 [INTEGRATIONS.md](docs/INTEGRATIONS.md) | The surrounding services, the settings each needs, and what went wrong |
| 🌐 [API.md](docs/API.md) | The HTTP API |
| 📝 [VERSION.md](docs/VERSION.md) | Release notes |

---

## 📄 License

[Apache-2.0](LICENSE) © 2026 Omar
