"""SQLite persistence.

Plain sqlite3 rather than an ORM: the schema is small, the access patterns are
trivial, and a single file is easy to inspect when something sticks. WAL mode
plus one module-level connection guarded by a lock is enough for a
single-process service.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from . import models
from .config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS books (
    id            INTEGER PRIMARY KEY,
    goodreads_id  TEXT NOT NULL UNIQUE,
    title         TEXT NOT NULL,
    author        TEXT DEFAULT '',
    isbn          TEXT DEFAULT '',
    isbn13        TEXT DEFAULT '',
    year          INTEGER,
    cover_url     TEXT DEFAULT '',
    goodreads_url TEXT DEFAULT '',
    genres        TEXT DEFAULT '[]',
    -- Which provider supplied the genres: goodreads / openlibrary /
    -- googlebooks / embedded. Empty when nothing has resolved them yet.
    genre_source  TEXT DEFAULT '',
    category      TEXT DEFAULT '',
    needs_review  INTEGER DEFAULT 0,
    auto_shelve   INTEGER DEFAULT 1,
    discovered_at TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS stage_runs (
    id          INTEGER PRIMARY KEY,
    book_id     INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
    stage       TEXT NOT NULL,
    status      TEXT NOT NULL,
    -- How many times this stage has run, and the last outcome.
    attempts    INTEGER NOT NULL DEFAULT 0,
    detail      TEXT DEFAULT '',
    -- auth / network / server / notfound / data / ''. Lets the UI separate
    -- "your credentials are wrong" from "try again later".
    failure_kind TEXT DEFAULT '',
    -- Which service failed, as the client named it. Recorded at the point of
    -- failure rather than parsed back out of `detail`, which is what the UI
    -- used to do. '' is meaningful: local-disk stages and the ones that fan
    -- out over several services genuinely have no single service to name.
    service     TEXT DEFAULT '',
    -- Set when the pipeline held this stage because a *service* is down, to
    -- the canonical service name. Distinct from `service` on purpose: the UI
    -- has to tell an outage hold from an in-flight download (also blocked, but
    -- with service='') and from a one-off forgiven blip (service set, no
    -- held_by, ageing out on its own clock).
    held_by     TEXT DEFAULT '',
    -- How many times a transient fault has been forgiven, and since when.
    -- The count alone was wrong for an outage: retries are cheap, so a source
    -- that is down for an hour burned through the budget and the book was
    -- written off permanently *before* the source came back.
    transient_count INTEGER NOT NULL DEFAULT 0,
    transient_since TEXT,
    artifact    TEXT DEFAULT '',
    -- Set when a download has been handed to Shelfmark. Its presence is what
    -- stops the acquire stage re-queuing the same book on every sweep.
    queued_at   TEXT,
    -- Where the finished file actually landed, recorded by `place` and read
    -- by every downstream stage.
    output_path TEXT,
    started_at  TEXT,
    finished_at TEXT,
    UNIQUE (book_id, stage)
);

CREATE TABLE IF NOT EXISTS credentials (
    key        TEXT PRIMARY KEY,
    value      BLOB NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    username      TEXT PRIMARY KEY,
    password_hash TEXT NOT NULL,
    -- Rotated whenever the password changes, and baked into every session
    -- token. It is what makes a password change actually evict old sessions.
    session_epoch TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS service_map (
    service    TEXT NOT NULL,
    name       TEXT NOT NULL,
    remote_id  TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (service, name)
);

CREATE TABLE IF NOT EXISTS service_health (
    service      TEXT PRIMARY KEY,
    ok           INTEGER NOT NULL DEFAULT 0,
    detail       TEXT DEFAULT '',
    failure_kind TEXT DEFAULT '',
    checked_at   TEXT NOT NULL,
    ok_since     TEXT
);

CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY,
    ts      TEXT NOT NULL,
    level   TEXT NOT NULL,
    book_id INTEGER,
    stage   TEXT,
    -- Which service this line is about, when it is about one. Set by the
    -- health probes and by stages that talk to a single service, so the
    -- per-service page can show that service's log without guessing from
    -- the message text.
    service TEXT DEFAULT '',
    message TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS service_breaker (
    service       TEXT PRIMARY KEY,
    -- closed | open | half_open. Read through `breaker.effective_state`, which
    -- applies the lazy open -> half_open flip on the clock rather than on a
    -- timer, so a reader never has to write.
    state         TEXT NOT NULL DEFAULT 'closed',
    -- Consecutive service-level failures while closed. Reset by any success,
    -- and bounded in time by `failures_since`: a failure arriving more than
    -- `breaker.FAILURE_WINDOW_SECONDS` after the previous one is the first of a
    -- new streak rather than the next of this one.
    failures      INTEGER NOT NULL DEFAULT 0,
    -- When the newest failure of the current streak was counted. It is
    -- re-stamped on every count, because what `_streak_live` bounds is the gap
    -- *between* failures, not the age of the streak. Cleared by a trip, a close
    -- and by an aged-out streak.
    failures_since TEXT,
    -- Consecutive trips with no clean recovery. Drives the backoff; reset to 0
    -- only when a probe closes the breaker.
    trips         INTEGER NOT NULL DEFAULT 0,
    opened_at     TEXT,
    -- When a probe is allowed again. Read as "still open" until this passes.
    open_until    TEXT,
    -- Half-open ownership: which single book is allowed through to discover
    -- whether the service is back. Stale claims are released.
    probe_book_id INTEGER,
    probe_stage   TEXT DEFAULT '',
    probe_at      TEXT,
    -- The message that tripped it, for the operator's one line.
    last_failure  TEXT DEFAULT '',
    last_ok_at    TEXT,
    changed_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_stage_book ON stage_runs (book_id);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events (ts DESC);
"""


#: Longest event message kept, in characters. Generous next to anything the app
#: writes itself — the longest real message is a stage failure naming a service
#: and its response — and it exists to bound what a caller can put in the feed,
#: not to shorten ours.
_MAX_EVENT_CHARS = 500


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(SCHEMA)
            self._migrate()
            self._conn.commit()

    def _migrate(self) -> None:
        """Add columns introduced after a database was first created.

        `CREATE TABLE IF NOT EXISTS` silently does nothing on an existing
        table, so new columns have to be added explicitly or an upgraded
        container would fail on its first query.
        """
        wanted = {
            "stage_runs": {
                "queued_at": "TEXT",
                "output_path": "TEXT",
                "failure_kind": "TEXT DEFAULT ''",
                "service": "TEXT DEFAULT ''",
                "held_by": "TEXT DEFAULT ''",
                "transient_count": "INTEGER NOT NULL DEFAULT 0",
                "transient_since": "TEXT",
            },
            "events": {
                "service": "TEXT DEFAULT ''",
            },
            "users": {
                "session_epoch": "TEXT NOT NULL DEFAULT ''",
            },
            "books": {
                "genre_source": "TEXT DEFAULT ''",
            },
            "service_breaker": {
                "failures_since": "TEXT",
            },
        }
        for table, columns in wanted.items():
            existing = {
                row["name"]
                for row in self._conn.execute(f"PRAGMA table_info({table})").fetchall()
            }
            for name, decl in columns.items():
                if name not in existing:
                    self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")

    # -- plumbing --------------------------------------------------------
    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, tuple(params))
            self._conn.commit()
            return cur

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(sql, tuple(params)).fetchall())

    def query_one(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def ensure_stages(self) -> int:
        """Give every book a row for every stage in models.STAGES.

        Stages are created when a book is inserted, so a book that predates a
        newly added stage has no row for it — and a stage with no row can never
        record a result, so it would re-run forever without ever appearing to
        progress. Adding a stage therefore needs this backfill.
        """
        created = 0
        for stage in models.STAGES:
            cur = self.execute(
                """
                INSERT INTO stage_runs (book_id, stage, status)
                SELECT b.id, ?, ? FROM books b
                WHERE NOT EXISTS (
                    SELECT 1 FROM stage_runs s
                    WHERE s.book_id = b.id AND s.stage = ?
                )
                """,
                (stage, models.PENDING, stage),
            )
            created += cur.rowcount or 0
        if created:
            self.log(f"added {created} missing stage row(s) for new stages")
        return created

    # -- books -----------------------------------------------------------
    def upsert_book(self, book: models.Book) -> tuple[int, bool]:
        """Insert or refresh a book. Returns (id, created)."""
        row = book.as_row()
        existing = self.query_one(
            "SELECT id FROM books WHERE goodreads_id = ?", (book.goodreads_id,)
        )
        if existing:
            book_id = existing["id"]
            # Re-discovery refreshes metadata, but must never *downgrade* it.
            # The shelf scrape carries no genres, so writing them blindly wiped
            # every backfilled genre list on each sweep — and since the sweep
            # runs every 15 minutes, books silently reverted to the fallback
            # category and the needs-review pile kept refilling. Same reasoning
            # for category: a human's choice is not the scraper's to overwrite.
            self.execute(
                """
                UPDATE books SET title=?, author=?, isbn=?, isbn13=?, year=?,
                       cover_url=?, goodreads_url=?,
                       genres = CASE
                           WHEN ? NOT IN ('', '[]') THEN ?
                           ELSE genres
                       END,
                       category = CASE
                           WHEN category = '' AND ? <> '' THEN ?
                           ELSE category
                       END,
                       updated_at=?
                 WHERE id=?
                """,
                (
                    row["title"], row["author"], row["isbn"], row["isbn13"],
                    row["year"], row["cover_url"], row["goodreads_url"],
                    row["genres"], row["genres"],
                    row["category"], row["category"],
                    _now(), book_id,
                ),
            )
            return book_id, False

        cur = self.execute(
            """
            INSERT INTO books (goodreads_id, title, author, isbn, isbn13, year,
                               cover_url, goodreads_url, genres, category,
                               needs_review, auto_shelve, discovered_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                row["goodreads_id"], row["title"], row["author"], row["isbn"],
                row["isbn13"], row["year"], row["cover_url"], row["goodreads_url"],
                row["genres"], row["category"], row["needs_review"],
                row["auto_shelve"], _now(), _now(),
            ),
        )
        book_id = int(cur.lastrowid)
        for stage in models.STAGES:
            self.execute(
                "INSERT OR IGNORE INTO stage_runs (book_id, stage, status) VALUES (?,?,?)",
                (book_id, stage, models.PENDING),
            )
        return book_id, True

    def get_book(self, book_id: int) -> sqlite3.Row | None:
        return self.query_one("SELECT * FROM books WHERE id = ?", (book_id,))

    def list_books(self, only_review: bool = False) -> list[dict]:
        sql = "SELECT * FROM books"
        params: list[Any] = []
        if only_review:
            sql += " WHERE needs_review = 1"
        sql += " ORDER BY discovered_at DESC"
        out = []
        for row in self.query(sql, params):
            book = dict(row)
            book["genres"] = json.loads(book["genres"] or "[]")
            book["stages"] = {
                s["stage"]: dict(s)
                for s in self.query(
                    "SELECT * FROM stage_runs WHERE book_id = ?", (row["id"],)
                )
            }
            out.append(book)
        return out

    def set_genres(self, book_id: int, genres_json: str, source: str = "") -> None:
        """Record resolved genres and which provider produced them."""
        self.execute(
            "UPDATE books SET genres=?, genre_source=?, updated_at=? WHERE id=?",
            (genres_json, source, _now(), book_id),
        )

    def set_category(self, book_id: int, category: str, needs_review: bool = False) -> None:
        self.execute(
            "UPDATE books SET category=?, needs_review=?, updated_at=? WHERE id=?",
            (category, int(needs_review), _now(), book_id),
        )

    def set_auto_shelve(self, book_id: int, enabled: bool) -> None:
        self.execute(
            "UPDATE books SET auto_shelve=?, updated_at=? WHERE id=?",
            (int(enabled), _now(), book_id),
        )

    # -- stages ----------------------------------------------------------
    def stage(self, book_id: int, stage: str) -> dict | None:
        row = self.query_one(
            "SELECT * FROM stage_runs WHERE book_id=? AND stage=?", (book_id, stage)
        )
        return dict(row) if row else None

    def mark_running(self, book_id: int, stage: str) -> None:
        # `detail` is deliberately preserved: callers stash durable state there
        # (e.g. that a download is already queued) and a re-run must not lose it.
        self.execute(
            """
            UPDATE stage_runs SET status=?, attempts=attempts+1,
                   started_at=?, finished_at=NULL
             WHERE book_id=? AND stage=?
            """,
            (models.RUNNING, _now(), book_id, stage),
        )

    def mark_done(self, book_id: int, stage: str, result: models.StageResult) -> None:
        # A blocked stage is waiting on the outside world, not failing. It must
        # not burn an attempt, or a slow download would exhaust its retries
        # while it is still progressing normally.
        penalty = -1 if result.status == models.BLOCKED else 0
        self.execute(
            """
            UPDATE stage_runs SET status=?,
                   detail=CASE WHEN ? <> '' AND queued_at IS NOT NULL
                               THEN detail ELSE ? END,
                   artifact=CASE WHEN ?='' THEN artifact ELSE ? END,
                   failure_kind=?,
                   service=?,
                   held_by=?,
                   attempts=MAX(attempts + ?, 0), finished_at=?
             WHERE book_id=? AND stage=?
            """,
            (
                # A hold that came back from a stage that *ran* — the half-open
                # probe, whose poll of an in-flight download failed — leaves the
                # row's own words alone for exactly the reason `mark_held` does:
                # the download is still running, and "queued from X" is still
                # what is happening to it. Every other result writes its detail,
                # including the `ok` that ends the hold.
                result.status, result.held_by, result.detail,
                result.artifact, result.artifact,
                result.kind if result.status == models.FAILED else "",
                # Written unconditionally. An `ok` that names the service it
                # used keeps it; one that does not — a fan-out or local stage,
                # or any result that failed — clears whatever the previous
                # attempt blamed.
                result.service,
                # Likewise unconditional, and for the same reason: only a
                # breaker hold sets it, so any other result means this stage is
                # no longer being held and must stop reading as if it were.
                result.held_by,
                penalty, _now(), book_id, stage,
            ),
        )

    def mark_held(self, book_id: int, stage: str, service: str, detail: str) -> None:
        """Write the breaker's hold onto a stage row, once per hold.

        An UPDATE rather than a `mark_done` call because the pipeline never ran
        the stage — the breaker refused it before it started — so there is no
        result to record, only a reason to wait.

        Three things are deliberate:

        * the write is guarded on "not already held by this service", so a
          week-long outage costs one UPDATE per book rather than one per book
          per 15-second sweep. The guard is `status <> blocked OR held_by <>
          service` and both halves are load-bearing: `status <> blocked` alone
          skipped every row the caller actually calls this for — a book
          forgiven on the per-book clock is `blocked` with `held_by=''`, which
          is exactly the shape `pipeline._advance` asks to have marked, so the
          UPDATE matched zero rows and those books stayed in no group at all
          while the panel's count said they did not exist. Comparing `held_by`
          against the *inbound service* rather than against `''` is what keeps
          a hold from one service from blocking the write for another.
        * `attempts` is zeroed because the breaker is the clock now. The stage's
          own budget is what parks a genuinely broken book, and it must not be
          spent on an outage. That cannot loop forever: once the service is
          healthy a failure is no longer transient and parks normally;
        * `queued_at`, `output_path` and `artifact` are left alone — and so is
          the `detail` of a row that has a `queued_at`, which is the one field
          this write does not overwrite. An in-flight download keeps its
          `queued_at` and its artifact, which is what the progress view and
          `_watch` read to pick the task back up, and it keeps its own words:
          "queued from MyAnonaMouse (a good release)", the release it is
          fetching. Nothing about that sentence stops being true while the
          breaker is open — the download runs on without us, and what the hold
          costs it is our *poll* — so replacing it with "Shelfmark cannot reach
          its sources" sent the operator looking for a problem on a book that
          was downloading fine and threw away the one line naming what it had
          picked. Every other row gets the hold's detail: for a book that is
          not downloading, "waiting on a downed service" is exactly why nothing
          is happening to it. The hold itself is still written for a queued row
          (`held_by`, `attempts`, the cleared clock) because `book_state` reads
          `held_by` to tell a book waiting on a source from one whose audiobook
          "does not exist anywhere" — dropping the row instead would restore
          that false reading.
        """
        self.execute(
            """
            UPDATE stage_runs SET status=?, held_by=?, failure_kind='', service=?,
                   detail=CASE WHEN queued_at IS NULL THEN ? ELSE detail END,
                   attempts=0, transient_count=0, transient_since=NULL,
                   finished_at=?
             WHERE book_id=? AND stage=? AND (status <> ? OR held_by <> ?)
            """,
            (models.BLOCKED, service, service, detail, _now(), book_id, stage,
             models.BLOCKED, service),
        )

    def hold_rows(self, ids: "list[int]", service: str, detail: str) -> int:
        """Convert already-failed stage rows into breaker holds. Returns how many.

        One statement per 500 rows rather than one per row: an outage parks
        hundreds of books at once and the trip has to be cheap, and SQLite's
        bound-parameter limit is 999 so the chunking is not optional.

        This is the *reclaim* path — `breaker._trip` handing the failures it
        explains to the breaker — and it is deliberately not identical to
        `mark_held`, which writes the same hold on a live refusal: reclaim
        reaches rows that already failed, and a row can fail while a download
        is in flight. The `queued_at` guard on `detail` is the one that was
        missing here and present there, and it is the same invariant
        `mark_held` states at length: "queued from MyAnonaMouse (a good
        release)" is the only line saying what the book is fetching, and
        replacing it with the outage sentence loses the release name for good
        — the row is written `blocked` with `attempts=0` and nothing ever
        writes that sentence again. Message loss only, which is why it is
        small, but it is loss all the same.
        """
        if not ids:
            return 0
        stamp = _now()
        changed = 0
        for start in range(0, len(ids), 500):
            chunk = list(ids[start:start + 500])
            marks = ",".join("?" * len(chunk))
            cur = self.execute(
                f"""
                UPDATE stage_runs SET status=?, held_by=?, failure_kind='',
                       service=?,
                       detail=CASE WHEN queued_at IS NULL THEN ? ELSE detail END,
                       attempts=0, transient_count=0,
                       transient_since=NULL, finished_at=?
                 WHERE id IN ({marks})
                """,
                [models.BLOCKED, service, service, detail, stamp, *chunk],
            )
            changed += cur.rowcount or 0
        return changed

    def mark_queued(self, book_id: int, stage: str, detail: str = "queued") -> None:
        self.execute(
            "UPDATE stage_runs SET queued_at=?, detail=? WHERE book_id=? AND stage=?",
            (_now(), detail, book_id, stage),
        )

    def clear_queued(self, book_id: int, stage: str) -> None:
        self.execute(
            "UPDATE stage_runs SET queued_at=NULL WHERE book_id=? AND stage=?",
            (book_id, stage),
        )

    def bump_transient(self, book_id: int, stage: str) -> tuple[int, str]:
        """Count a forgiven transient fault. Returns (count, since-iso).

        `transient_since` is only set on the first failure of a run, so it
        measures how long the source has been unavailable rather than how many
        times we happened to look.
        """
        row = self.query_one(
            "SELECT transient_count, transient_since FROM stage_runs "
            "WHERE book_id=? AND stage=?",
            (book_id, stage),
        )
        since = (row["transient_since"] if row else None) or _now()
        self.execute(
            "UPDATE stage_runs SET transient_count = transient_count + 1, "
            "transient_since = ? WHERE book_id=? AND stage=?",
            (since, book_id, stage),
        )
        return (int(row["transient_count"]) + 1 if row else 1), since

    def reset_transient(self, book_id: int, stage: str) -> None:
        self.execute(
            "UPDATE stage_runs SET transient_count=0, transient_since=NULL "
            "WHERE book_id=? AND stage=?",
            (book_id, stage),
        )

    def transient_age_hours(self, since: str | None) -> float:
        if not since:
            return 0.0
        try:
            started = datetime.fromisoformat(since)
        except ValueError:
            return 0.0
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - started).total_seconds() / 3600

    def set_output(self, book_id: int, stage: str, path: str) -> None:
        self.execute(
            "UPDATE stage_runs SET output_path=? WHERE book_id=? AND stage=?",
            (path, book_id, stage),
        )

    def output_of(self, book_id: int, stage: str) -> str:
        row = self.query_one(
            "SELECT output_path FROM stage_runs WHERE book_id=? AND stage=?",
            (book_id, stage),
        )
        return (row["output_path"] or "") if row else ""

    def reset_stage(self, book_id: int, stage: str) -> None:
        """Clear a stage (and everything downstream) so it runs again.

        The per-book forgiveness clock is cleared with it, and that is not
        bookkeeping: the clock ages on the wall, so a book whose `transient_since`
        was set 25 hours ago — by an outage that is over — would be "25 hours
        into" a clock the retry did not spend, and the first retried failure
        would be escalated to a real `failed` on its very first attempt. The
        operator clicks Retry, the book parks instantly, and every retry after
        that repeats it. `bump_transient` reuses an existing `transient_since`,
        so nothing else would have cleared it either. This is the same false
        park `breaker._close` clears the clock to prevent, reached by hand.
        """
        idx = models.STAGES.index(stage)
        for downstream in models.STAGES[idx:]:
            self.execute(
                """
                UPDATE stage_runs SET status=?, attempts=0, detail='', artifact='',
                       failure_kind='', service='', held_by='',
                       transient_count=0, transient_since=NULL,
                       queued_at=NULL, output_path=NULL,
                       started_at=NULL, finished_at=NULL
                 WHERE book_id=? AND stage=?
                """,
                (models.PENDING, book_id, downstream),
            )

    # -- the breaker's view of the stage rows ----------------------------
    def failed_transient_rows(self, since: str, kinds: tuple[str, ...]) -> list[dict]:
        """`failed` rows recent enough to still be the *same* outage.

        The vocabulary is passed in rather than hard-coded so `db` stays
        import-free of `breaker` (which imports `db`): the caller owns the
        definition of "transient".
        """
        if not kinds:
            return []
        marks = ",".join("?" * len(kinds))
        return [
            dict(r)
            for r in self.query(
                f"""
                SELECT id, book_id, stage, status, failure_kind, service, detail,
                       finished_at, attempts
                  FROM stage_runs
                 WHERE status = ? AND failure_kind IN ({marks})
                   AND finished_at IS NOT NULL AND finished_at >= ?
                 ORDER BY finished_at ASC
                """,
                [models.FAILED, *kinds, since],
            )
        ]

    def clear_transient_for_service(self, service: str, opened_at: str = "") -> int:
        """Reset the per-book transient clock for everything a service held.

        Needed because that clock ages on the wall, not on attempts: a book
        whose `transient_since` was set during an outage would be at 25h the
        first time it failed after recovery, and would park on the spot —
        having "failed for 25 hours" without having been tried for 24 of them.
        Without this pass the breaker would manufacture exactly the false parks
        it exists to remove.

        The predicate is "the row is `blocked` and names this service". Both
        halves are load-bearing:

        * `held_by = ?` is the books the breaker actually held;
        * `status='blocked' AND service=?` catches the rows written *before* the
          trip. Those reached the acquire grace branch in the same sweep, so
          they are `blocked`, with `failure_kind=''` and no `held_by`, but they
          carry a live clock that started when the outage did. Note the bound
          here is the *service*, not `transient_since >= opened_at`: the rows
          this exists for are written before the trip by definition, so their
          clock necessarily starts a few seconds *earlier* than `opened_at` and
          that comparison would miss every one of them. A blocked stage row
          naming a service is by construction a forgiven failure *of that
          service*, which is precisely the set the recovered service owns.
        """
        from .health import canonical  # deferred: health -> clients -> db

        if not service:
            return 0
        candidates = self.query(
            "SELECT id, status, service, held_by FROM stage_runs "
            "WHERE held_by <> '' OR status = ?",
            (models.BLOCKED,),
        )
        ids = [
            r["id"]
            for r in candidates
            if canonical(r["held_by"] or "") == service
            or canonical(r["service"] or "") == service
        ]
        changed = 0
        for start in range(0, len(ids), 500):
            chunk = list(ids[start:start + 500])
            marks = ",".join("?" * len(chunk))
            cur = self.execute(
                f"UPDATE stage_runs SET transient_count=0, transient_since=NULL "
                f"WHERE id IN ({marks})",
                chunk,
            )
            changed += cur.rowcount or 0
        return changed

    # -- credentials -----------------------------------------------------
    def set_credential(self, key: str, encrypted: bytes) -> None:
        self.execute(
            """
            INSERT INTO credentials (key, value, updated_at) VALUES (?,?,?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
            """,
            (key, encrypted, _now()),
        )

    def get_credential(self, key: str) -> bytes | None:
        row = self.query_one("SELECT value FROM credentials WHERE key=?", (key,))
        return row["value"] if row else None

    def credential_keys(self) -> list[str]:
        return [r["key"] for r in self.query("SELECT key FROM credentials ORDER BY key")]

    def delete_credential(self, key: str) -> None:
        self.execute("DELETE FROM credentials WHERE key=?", (key,))

    # -- users -----------------------------------------------------------
    def set_user(self, username: str, password_hash: str, session_epoch: str) -> None:
        """Create or update a user. A new epoch invalidates their old sessions."""
        self.execute(
            """
            INSERT INTO users (username, password_hash, session_epoch, created_at, updated_at)
            VALUES (?,?,?,?,?)
            ON CONFLICT(username) DO UPDATE SET password_hash=excluded.password_hash,
                   session_epoch=excluded.session_epoch, updated_at=excluded.updated_at
            """,
            (username, password_hash, session_epoch, _now(), _now()),
        )

    def get_user(self, username: str) -> sqlite3.Row | None:
        return self.query_one("SELECT * FROM users WHERE username = ?", (username,))

    def session_epoch_for(self, username: str) -> str:
        row = self.query_one("SELECT session_epoch FROM users WHERE username = ?", (username,))
        return (row["session_epoch"] or "") if row else ""

    def list_users(self) -> list[str]:
        return [r["username"] for r in self.query("SELECT username FROM users ORDER BY username")]

    def delete_user(self, username: str) -> None:
        self.execute("DELETE FROM users WHERE username = ?", (username,))

    def user_count(self) -> int:
        row = self.query_one("SELECT COUNT(*) AS n FROM users")
        return int(row["n"]) if row else 0

    # -- service health --------------------------------------------------
    def set_service_health(
        self, service: str, ok: bool, detail: str, failure_kind: str = ""
    ) -> None:
        previous = self.query_one(
            "SELECT ok, ok_since FROM service_health WHERE service = ?", (service,)
        )
        # Track how long it has been continuously healthy, so the UI can say
        # "last good 3 hours ago" rather than just "down".
        if ok:
            ok_since = (previous["ok_since"] if previous and previous["ok"] else None) or _now()
        else:
            ok_since = None
        self.execute(
            """
            INSERT INTO service_health (service, ok, detail, failure_kind, checked_at, ok_since)
            VALUES (?,?,?,?,?,?)
            ON CONFLICT(service) DO UPDATE SET ok=excluded.ok, detail=excluded.detail,
                   failure_kind=excluded.failure_kind, checked_at=excluded.checked_at,
                   ok_since=excluded.ok_since
            """,
            (service, int(ok), detail, failure_kind, _now(), ok_since),
        )

    def service_health(self) -> list[dict]:
        return [dict(r) for r in self.query("SELECT * FROM service_health ORDER BY service")]

    def unhealthy_services(self) -> list[dict]:
        return [
            dict(r)
            for r in self.query(
                "SELECT * FROM service_health WHERE ok = 0 ORDER BY service"
            )
        ]

    # -- settings --------------------------------------------------------
    def set_setting(self, key: str, value: str) -> None:
        self.execute(
            """
            INSERT INTO settings (key, value, updated_at) VALUES (?,?,?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
            """,
            (key, value, _now()),
        )

    def get_setting(self, key: str, default: str = "") -> str:
        row = self.query_one("SELECT value FROM settings WHERE key=?", (key,))
        return row["value"] if row else default

    # -- resolved-id cache ----------------------------------------------
    def cache_remote_id(self, service: str, name: str, remote_id: str) -> None:
        self.execute(
            """
            INSERT INTO service_map (service, name, remote_id, updated_at) VALUES (?,?,?,?)
            ON CONFLICT(service, name) DO UPDATE SET remote_id=excluded.remote_id,
                   updated_at=excluded.updated_at
            """,
            (service, name, remote_id, _now()),
        )

    def get_remote_id(self, service: str, name: str) -> str | None:
        row = self.query_one(
            "SELECT remote_id FROM service_map WHERE service=? AND name=?", (service, name)
        )
        return row["remote_id"] if row else None

    # -- events ----------------------------------------------------------
    def log(self, message: str, level: str = "info", book_id: int | None = None,
            stage: str | None = None, service: str = "") -> None:
        """Record an event, normalised and bounded.

        Both happen here because this is the only `INSERT INTO events` in the
        repo, and most of what reaches it is not ours to trust: exception text,
        service responses, and — on the sign-in path, which is reachable with
        no session — a caller-supplied username. `/api/state` re-sends the
        newest 60 events to every open browser every six seconds, so a single
        unbounded row was enough to push every real line out of the operator's
        activity feed. One edit here covers all of it; a call site added later
        inherits the bound rather than having to remember it.
        """
        text = " ".join(str(message).split())
        if len(text) > _MAX_EVENT_CHARS:
            # Clipped, and *visibly* clipped. A silently shortened line reads
            # as a whole one, and the one message that mattered is exactly the
            # one long enough to be cut.
            text = text[: _MAX_EVENT_CHARS - 1] + "…"
        self.execute(
            "INSERT INTO events (ts, level, book_id, stage, service, message) "
            "VALUES (?,?,?,?,?,?)",
            (_now(), level, book_id, stage, service, text),
        )

    def recent_events(self, limit: int = 100, service: str | None = None,
                      level: str | None = None, stage: str | None = None,
                      book_id: int | None = None) -> list[dict]:
        """Newest first, optionally narrowed.

        Filtering happens in SQL rather than in Python after the fact: the UI
        wants one service's log, and fetching every event to discard most of
        them both costs more and silently truncates — a busy book could push a
        service's own lines past the limit entirely.
        """
        clauses, params = [], []
        for column, value in (
            ("service", service), ("level", level),
            ("stage", stage), ("book_id", book_id),
        ):
            if value is not None:
                clauses.append(f"{column} = ?")
                params.append(value)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        return [
            dict(r)
            for r in self.query(
                f"SELECT * FROM events{where} ORDER BY id DESC LIMIT ?", tuple(params)
            )
        ]


_db: Database | None = None


def db() -> Database:
    global _db
    if _db is None:
        _db = Database(settings.db_path)
    return _db
