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

CREATE INDEX IF NOT EXISTS idx_stage_book ON stage_runs (book_id);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events (ts DESC);
"""


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
            UPDATE stage_runs SET status=?, detail=?,
                   artifact=CASE WHEN ?='' THEN artifact ELSE ? END,
                   failure_kind=?,
                   service=?,
                   attempts=MAX(attempts + ?, 0), finished_at=?
             WHERE book_id=? AND stage=?
            """,
            (
                result.status, result.detail, result.artifact, result.artifact,
                result.kind if result.status == models.FAILED else "",
                # Written unconditionally: `ok()` carries no service, so a
                # successful re-run clears whatever the previous attempt blamed.
                result.service,
                penalty, _now(), book_id, stage,
            ),
        )

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
        """Clear a stage (and everything downstream) so it runs again."""
        idx = models.STAGES.index(stage)
        for downstream in models.STAGES[idx:]:
            self.execute(
                """
                UPDATE stage_runs SET status=?, attempts=0, detail='', artifact='',
                       failure_kind='', service='',
                       queued_at=NULL, output_path=NULL,
                       started_at=NULL, finished_at=NULL
                 WHERE book_id=? AND stage=?
                """,
                (models.PENDING, book_id, downstream),
            )

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
        self.execute(
            "INSERT INTO events (ts, level, book_id, stage, service, message) "
            "VALUES (?,?,?,?,?,?)",
            (_now(), level, book_id, stage, service, message),
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
