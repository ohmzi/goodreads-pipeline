"""Domain vocabulary: what a book is, and what the pipeline does to it."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


# Per-book stages, in the order they run. Ordering is also the dependency
# order: a stage may assume every earlier stage has settled.
STAGES: tuple[str, ...] = (
    "classify",
    "acquire_ebook",
    "acquire_audiobook",
    "place",
    "index",
    "notebook",
    # Confirms the book is really in each service before we claim it is, and
    # before anything irreversible happens on Goodreads.
    "verify",
    "shelve",
)

# A stage ends in exactly one of these.
PENDING = "pending"
RUNNING = "running"
OK = "ok"
FAILED = "failed"
SKIPPED = "skipped"      # deliberately not applicable (e.g. no audiobook exists)
BLOCKED = "blocked"      # waiting on an earlier stage; retried automatically

TERMINAL = (OK, FAILED, SKIPPED)

# How many times a stage is retried before it parks for a human.
MAX_ATTEMPTS = {
    "classify": 3,
    "acquire_ebook": 5,
    "acquire_audiobook": 5,
    "place": 3,
    "index": 4,
    "notebook": 4,
    # Search endpoints can lag a rescan by a few seconds, so give verification
    # room to succeed on a later sweep rather than parking the book early.
    "verify": 6,
    "shelve": 3,
}

# Shelf names on Goodreads, chosen by what actually landed.
#
# NOTE the hyphen in SHELF_BOTH. An ampersand cannot be used: a shelf named
# `collected-pdf&audiobook` cannot be addressed through `?shelf=`, so Goodreads
# renders the whole account instead of that shelf, `add_to_shelf` silently does
# nothing, and the book is then removed from to-read and lands nowhere. That
# combination lost seven books once already.
SHELF_BOTH = "collected-pdf-audiobook"
SHELF_EBOOK = "collected-pdf"
SHELF_AUDIO = "collected-audiobook"

#: The pseudo-shelf that means "everything". Used to detect the fallback above.
ALL_SHELF = "%23ALL%23"


@dataclass
class Book:
    goodreads_id: str
    title: str
    author: str = ""
    isbn: str = ""
    isbn13: str = ""
    year: int | None = None
    cover_url: str = ""
    goodreads_url: str = ""
    genres: list[str] = field(default_factory=list)
    category: str = ""
    needs_review: bool = False
    auto_shelve: bool = True
    id: int | None = None

    @property
    def display_author(self) -> str:
        return self.author or "Unknown"

    def as_row(self) -> dict[str, Any]:
        return {
            "goodreads_id": self.goodreads_id,
            "title": self.title,
            "author": self.author,
            "isbn": self.isbn,
            "isbn13": self.isbn13,
            "year": self.year,
            "cover_url": self.cover_url,
            "goodreads_url": self.goodreads_url,
            "genres": json.dumps(self.genres),
            "category": self.category,
            "needs_review": int(self.needs_review),
            "auto_shelve": int(self.auto_shelve),
        }


@dataclass
class StageResult:
    """What a stage hands back to the scheduler."""

    status: str
    detail: str = ""
    artifact: str = ""
    #: Why it failed, at a level the UI can act on:
    #:   auth      — credentials rejected or missing; a human must fix Settings
    #:   network   — could not reach the service; transient, retry
    #:   server    — the service returned a 5xx; transient, retry
    #:   notfound  — the service says the thing does not exist; usually data
    #:   data      — nothing to work with (no release exists, format unusable)
    #:   ""        — unclassified
    kind: str = ""
    #: Which service this came from, as the client named it ("kavita",
    #: "shelfmark", "open notebook", ...). Recorded rather than re-derived:
    #: `ClientError` already knows the service at the point of failure, and the
    #: UI used to recover it by substring-matching the detail text, which broke
    #: the moment a message changed.
    #:
    #: Empty is a real answer, not a gap. A `place` failure is local disk, and
    #: `index`/`verify` each fan out over up to five services, so neither has
    #: one service to name — those name the services in `detail` instead of
    #: picking one to blame.
    service: str = ""

    @classmethod
    def ok(cls, detail: str = "", artifact: str = "") -> "StageResult":
        return cls(OK, detail, artifact)

    @classmethod
    def failed(cls, detail: str, kind: str = "", service: str = "") -> "StageResult":
        return cls(FAILED, detail, kind=kind, service=service)

    @classmethod
    def skipped(cls, detail: str = "") -> "StageResult":
        return cls(SKIPPED, detail)

    @classmethod
    def blocked(cls, detail: str, service: str = "") -> "StageResult":
        return cls(BLOCKED, detail, service=service)

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL
