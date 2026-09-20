"""shelve — move the book off to-read onto the right collected-* shelf.

This is the only irreversible step in the pipeline, so it waits until both
acquire stages have settled. Shelving a book as `collected-pdf` while its
audiobook is still retrying would be a lie, and un-shelving is manual work.

The shelf name reflects what actually landed:
    both formats  -> collected-pdf&audiobook
    ebook only    -> collected-pdf
    audiobook only-> collected-audiobook

If *nothing* landed the book stays on to-read deliberately. Goodreads is left
alone and an error is logged, because "we found nothing" is not "we collected
it" — you should still see it on your to-read list.
"""

from __future__ import annotations

import httpx

from .. import goodreads, models
from ..db import db
from .acquire import acquired_formats
from .place import placed_paths

ACQUIRE_STAGES = ("acquire_ebook", "acquire_audiobook")

#: This stage only ever talks to one service, and Goodreads raises its own
#: exception hierarchy (`goodreads.GoodreadsError`) rather than `ClientError`,
#: so there is no `.service` to read — it is named here instead.
GOODREADS = "goodreads"


def auto_shelve_enabled() -> bool:
    return db().get_setting("auto_shelve", "1") == "1"


def _actively_downloading(book_id: int) -> str:
    """An acquire stage that is running *right now*, if any.

    Only `running` counts. A stage that is `blocked` or `pending` is not
    currently making progress — it may be waiting on an outage, a quota, or a
    queued slot — and waiting for it means waiting indefinitely. That is what
    left 52 fully-verified books unshelved: their audiobook was blocked on an
    upstream quota, so `collected-pdf` never happened.
    """
    for stage in ACQUIRE_STAGES:
        row = db().stage(book_id, stage) or {}
        if row.get("status") == models.RUNNING:
            return stage
    return ""


def run(book: dict) -> models.StageResult:
    book_id = book["id"]

    # --- only wait for a download that is actually happening ------------
    busy = _actively_downloading(book_id)
    if busy:
        return models.StageResult.blocked(
            f"waiting for {busy.replace('_', ' ')} to finish downloading"
        )

    has_ebook, has_audio = acquired_formats(book_id)
    placed = placed_paths(book_id)
    has_ebook = has_ebook or "ebook" in placed
    has_audio = has_audio or "audiobook" in placed

    if not has_ebook and not has_audio:
        db().log(
            "nothing downloaded — left on to-read so it is not lost",
            level="error",
            book_id=book_id,
            stage="shelve",
        )
        return models.StageResult.skipped(
            "nothing downloaded; left on to-read. Fix the acquire failures and "
            "retry, or shelve it by hand."
        )

    # --- respect the switches -------------------------------------------
    if not book.get("auto_shelve"):
        return models.StageResult.blocked("auto-shelve is switched off for this book")
    if not auto_shelve_enabled():
        return models.StageResult.blocked("auto-shelve is switched off globally")

    shelf = (
        models.SHELF_BOTH
        if (has_ebook and has_audio)
        else (models.SHELF_EBOOK if has_ebook else models.SHELF_AUDIO)
    )

    session = goodreads.GoodreadsSession()
    try:
        goodreads.move_to_shelf(session, str(book["goodreads_id"]), shelf)
    except goodreads.SessionExpired as exc:
        # `kind="auth"` is load-bearing, not decoration: without it the failure
        # is not in ACTIONABLE_KINDS, so an expired session left every book
        # stuck on the *irreversible* stage with nothing in the attention list
        # and no prompt to re-login.
        return models.StageResult.failed(
            f"{exc} (shelf move needs a live Goodreads session)",
            kind="auth",
            service=GOODREADS,
        )
    except goodreads.RateLimited as exc:
        # Not a failure — Goodreads is throttling. Back off without burning
        # an attempt.
        return models.StageResult.blocked(str(exc), service=GOODREADS)
    except goodreads.GoodreadsError as exc:
        return models.StageResult.failed(
            str(exc), kind=_goodreads_kind(exc), service=GOODREADS
        )
    except httpx.HTTPError as exc:
        # Goodreads times out under load fairly often. That is not a reason to
        # count a strike against the book — retry without burning an attempt.
        return models.StageResult.blocked(
            f"Goodreads request failed: {exc}", service=GOODREADS
        )

    return models.StageResult.ok(f"moved to '{shelf}'", artifact=shelf)


def _goodreads_kind(exc: Exception) -> str:
    """Classify a Goodreads failure for the UI.

    An expired session is an auth problem a human must fix; a rejected write is
    usually data. Keeping them apart stops the UI telling you to re-login when
    the real problem was something else.
    """
    text = str(exc).lower()
    if "session" in text or "re-login" in text or "sign in" in text:
        return "auth"
    if "rate" in text or "throttl" in text:
        return "network"
    return "data"
