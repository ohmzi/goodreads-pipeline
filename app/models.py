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
    #:   busy      — the service refused work *now* (408/425/429); transient, retry
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
    #: Set only when the pipeline parked this stage because a *service* is
    #: down, rather than because of anything about the book. The value is the
    #: canonical service name; the row is written `blocked` and the breaker
    #: releases it by itself once that service answers again.
    #:
    #: A separate field from `service` even though "blocked, and a service is
    #: named" already reads as "blocked because of that service", because the
    #: UI has to tell three different kinds of `blocked` apart: an outage hold
    #: (this), an in-flight download (also `blocked`, but `service` is `''`),
    #: and a one-off forgiven blip (carries `service`, no `held_by`, and ages
    #: out on its own 24h clock). Empty is a real answer here too.
    held_by: str = ""
    #: Seconds the upstream asked us to wait, when it said (`Retry-After`).
    #: Only a rate-limited service ever sends one, and it is the one piece of
    #: evidence about *when* to look again that we do not have to guess.
    retry_after: float | None = None
    #: True when the stage reached the service this result is about and got an
    #: answer out of it, whatever that answer was.
    #:
    #: This exists because `kind` cannot carry it, and reading it off `kind`
    #: is what produced F1's false negative. `data` is returned by two
    #: situations that are opposite in exactly this respect: the free-space
    #: guard in `acquire._queue` returns it *before* the dial (our disk refused
    #: the book; the service was never asked) and a successful search that
    #: finds no release returns it *after* one (the service answered, and the
    #: answer was "nothing"). A breaker that reads the first as unanswered —
    #: correct, and the whole of F1's fix — therefore reads the second as
    #: unanswered too, and a half-open probe against a perfectly healthy
    #: service re-opens the breaker, forever: measured at 20 cooldowns with 25
    #: successful dials, state still open, 5 books held with no reset path.
    #: In a real library that is not exotic, because "no audiobook exists in
    #: any configured source" is the dominant outstanding category, so the
    #: books that lead the rotation are exactly the ones that wedge it.
    #:
    #: Set by the stage that dialed, on the branch that returned after the
    #: service replied. Read by `breaker.probe_verdict`, which is the only
    #: place the distinction is needed: it is a fact about one run against one
    #: service, not about the book, so nothing persists it and `failure_kind`
    #: on the row stays what it always was (`data` for both cases) — the
    #: issues panel groups on that, and a new *kind* would have split "no
    #: audiobook exists" into two groups and dragged `cli`'s `failure_kind`
    #: queries with it.
    #:
    #: False is the safe default in both directions: a result that never
    #: dialed must not claim one (the disk-space guard, a stage that raised
    #: before reaching out, `_advance`'s own `unhandled error` synthesis), and
    #: a dial that happened but was not flagged just leaves the probe
    #: unanswered, which costs one cooldown rather than a wedge.
    answered: bool = False
    #: True when whoever built this hold has already fed the failure behind it
    #: to `breaker.record_failure`.
    #:
    #: The pipeline used to infer this from `kind` being empty, on the grounds
    #: that `breaker.hold()` clears `kind` and so only a stage that went
    #: through it could be holding with `''`. That inference is about the
    #: wrapper rather than about the fact, and it fails in the direction that
    #: loses evidence: a stage that holds without recording is read as having
    #: recorded, and the upstream's own words never reach the breaker's one
    #: line for that cooldown. `_advance` reads this instead — see its
    #: `refused_probe` block — and records on the stage's behalf when it is
    #: False, which is the belt the original comment claimed to be.
    recorded: bool = False

    @classmethod
    def ok(cls, detail: str = "", artifact: str = "",
           service: str = "") -> "StageResult":
        """A stage succeeded. `service` is the one it just used, where it has one.

        Naming the service is what makes `breaker.record_success` reachable:
        `pipeline._advance` feeds the breaker from an `ok` that names a service,
        and if none ever does, a service's failure counter has no reset on
        healthy traffic at all — three unrelated transient failures, however far
        apart, were enough to hold it. Fan-out stages (`index`, `verify`) and
        local ones (`classify`, `place`) genuinely have no single service and
        leave this empty, exactly as they do for a failure.
        """
        return cls(OK, detail, artifact, service=service)

    @classmethod
    def failed(cls, detail: str, kind: str = "", service: str = "",
               retry_after: float | None = None,
               answered: bool = False) -> "StageResult":
        """A stage that did not do its job. `answered` says whether it asked.

        A failed result can still be the service's own answer — "no release
        exists" is the case this argument exists for — and the breaker reads
        that when a half-open probe comes back this way. See the field.
        """
        return cls(FAILED, detail, kind=kind, service=service,
                   retry_after=retry_after, answered=answered)

    @classmethod
    def skipped(cls, detail: str = "") -> "StageResult":
        return cls(SKIPPED, detail)

    @classmethod
    def blocked(cls, detail: str, service: str = "") -> "StageResult":
        return cls(BLOCKED, detail, service=service)

    @classmethod
    def held(cls, detail: str, service: str,
             recorded: bool = False) -> "StageResult":
        """Blocked because the *service* is down, not because of this book.

        `recorded` is the holder saying it has already told the breaker about
        the failure this hold stands in for; see the field. It defaults to
        False because a hold built without saying so is the case the pipeline
        has to act on.
        """
        return cls(BLOCKED, detail, service=service, held_by=service,
                   recorded=recorded)

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL
