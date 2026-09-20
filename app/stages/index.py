"""index — make the library apps notice the new file.

Every app already mounts the same host directory, so this stage never imports
or uploads anything; it only asks each app to rescan. That is the whole point:
one file on disk, four readers.

Kavita, BookLore and Grimmory index the *books* tree; Audiobookshelf indexes
the *audiobooks* tree. An app whose tree did not change is skipped rather than
reported as a failure, so a book that only has an ebook does not spend its
retries on ABS.

Partial failure is reported but does not stop the pipeline: the file is
already on disk and readable, so blocking the rest of the chain because one
app is misconfigured would be the wrong trade. The detail string names exactly
which app failed so it can be fixed from the UI.
"""

from __future__ import annotations

from .. import models
from ..clients.abs_client import AudiobookshelfClient
from ..clients.base import ClientError
from ..clients.booklore import BookLoreClient, GrimmoryClient
from ..clients.kavita import KavitaClient
from ..db import db
from .place import placed_paths


def run(book: dict) -> models.StageResult:
    book_id = book["id"]
    paths = placed_paths(book_id)
    ebook = paths.get("ebook", "")
    audiobook = paths.get("audiobook", "")

    if not ebook and not audiobook:
        # `place` says "skipped" when nothing downloaded, and skipped counts as
        # satisfied for prerequisites — so we get here for books that were
        # never placed. That is not an indexing failure; `shelve` is the stage
        # that reports "nothing downloaded" to the operator.
        return models.StageResult.skipped("nothing was placed, so nothing to index")

    done: list[str] = []
    failed: list[str] = []

    # --- the three book-tree indexers -----------------------------------
    if ebook:
        for label, factory in (
            ("kavita", KavitaClient),
            ("booklore", BookLoreClient),
            ("grimmory", GrimmoryClient),
        ):
            try:
                lib = _library_for(factory, label, ebook)
                if lib is None:
                    failed.append(f"{label}: no library contains {ebook}")
                    continue
                _scan(factory, lib.id)
                done.append(f"{label}:{lib.name}")
            except ClientError as exc:
                failed.append(f"{exc}")
            except Exception as exc:  # noqa: BLE001 - one bad app must not stop the rest
                failed.append(f"{label}: {exc}")

    # --- audiobookshelf --------------------------------------------------
    if audiobook:
        try:
            lib = _library_for(AudiobookshelfClient, "audiobookshelf", audiobook)
            if lib is None:
                failed.append(f"audiobookshelf: no library contains {audiobook}")
            else:
                _scan(AudiobookshelfClient, lib.id)
                done.append(f"audiobookshelf:{lib.name}")
        except ClientError as exc:
            failed.append(str(exc))
        except Exception as exc:  # noqa: BLE001
            failed.append(f"audiobookshelf: {exc}")

    if failed:
        db().log(
            "index problems: " + "; ".join(failed),
            level="error",
            book_id=book_id,
            stage="index",
        )

    if not done:
        # Everything that was tried failed. Carry the kind of the first
        # error so an auth problem is not reported as a generic failure.
        return models.StageResult.failed(
            "no library accepted the rescan: " + "; ".join(failed), kind=_kind_of(failed)
        )

    detail = "rescanned " + ", ".join(done)
    if failed:
        detail += f" — FAILED: {'; '.join(failed)}"
    return models.StageResult.ok(detail)


def _library_for(factory, service: str, local_path: str):
    """Find the library containing a path, caching the name -> id mapping."""
    client = factory()
    try:
        lib = client.library_for_local_path(local_path)
        if lib is not None:
            db().cache_remote_id(service, lib.name, lib.id)
        return lib
    finally:
        client.close()


def _scan(factory, library_id: str) -> None:
    client = factory()
    try:
        client.scan(library_id)
    finally:
        client.close()


def _kind_of(messages: list[str]) -> str:
    """Infer a failure kind from collected error text.

    `index` gathers per-service messages into strings rather than keeping the
    exceptions, so the kind is recovered from the text. Auth is checked first
    because it is the one that needs a human.
    """
    joined = " ".join(messages).lower()
    for needle, kind in (
        ("401", "auth"), ("403", "auth"), ("unauthor", "auth"),
        ("forbidden", "auth"), ("api key", "auth"), ("credential", "auth"),
        ("timed out", "network"), ("could not reach", "network"),
        ("connection", "network"), ("http 5", "server"),
    ):
        if needle in joined:
            return kind
    return ""
