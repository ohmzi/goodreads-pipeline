"""notebook — add the book to the matching Open Notebook notebook.

Uses `POST /api/sources/json` with a file path rather than the multipart
upload route, because upload would store a second copy of the epub inside
Open Notebook. The file is referenced where it already sits.

The path has to be expressed in Open Notebook's namespace, not ours — see
`to_opennotebook_path`.
"""

from __future__ import annotations

import zipfile

from pathlib import PurePosixPath

from .. import models
from ..clients.base import ClientError
from ..clients.opennotebook import OpenNotebookClient, to_opennotebook_path
from . import classify
from .place import placed_paths

#: Formats Open Notebook can actually extract text from. It answers
#: `415 Unable to determine file type for: …` for anything else, which is a
#: statement about the format rather than a fault — so an `.azw3` or `.mobi`
#: download is skipped with that reason instead of being retried to death.
INDEXABLE_SUFFIXES = {".epub", ".pdf", ".txt", ".md", ".docx", ".html", ".htm"}


def unreadable_reason(path: str) -> str:
    """Why Open Notebook cannot read this file, or "" if it can.

    The extension check alone is not enough. A `.epub` that is really a zip of
    an extracted epub folder — no `mimetype` entry, no
    `META-INF/container.xml` — passes every filename test and is then rejected
    by Open Notebook with `415 Unsupported file type: application/zip`, which
    surfaced as a failure needing attention for a book that was simply a bad
    rip. Detecting it here turns that into an honest skip.
    """
    suffix = PurePosixPath(path).suffix.lower()
    if suffix not in INDEXABLE_SUFFIXES:
        return f"Open Notebook cannot read {suffix or 'this format'} files"

    if suffix == ".epub":
        try:
            with zipfile.ZipFile(path) as archive:
                names = set(archive.namelist())
        except (zipfile.BadZipFile, OSError):
            return "the file is not a readable archive"
        if "META-INF/container.xml" not in names:
            return ("this .epub is not a valid epub (no META-INF/container.xml) — "
                    "it looks like a zip of an extracted folder")
    return ""


def run(book: dict) -> models.StageResult:
    book_id = book["id"]
    paths = placed_paths(book_id)
    ebook = paths.get("ebook", "")

    if not ebook:
        # Open Notebook consumes text from the ebook; an audiobook alone gives
        # it nothing to index.
        return models.StageResult.skipped("no ebook placed — nothing to add")

    reason = unreadable_reason(ebook)
    if reason:
        return models.StageResult.skipped(f"{reason} — the book is in the library but not indexed")

    category = book.get("category") or ""
    _folder, notebook_name = classify.destination_for(category)
    if not notebook_name:
        return models.StageResult.skipped(
            f"category {category} maps to no notebook (see categories.yml)"
        )

    client = OpenNotebookClient()
    try:
        on_path = to_opennotebook_path(ebook)

        notebook = client.notebook_named(notebook_name)
        if notebook is None:
            # `answered=True`: the service listed its notebooks and this one was
            # not among them, so it is talking. Same distinction as the acquire
            # stage's empty search — a `data` failure that reached the service
            # versus one that never dialed — and the same consequence if it is
            # lost: a half-open probe reads a working Open Notebook as silence
            # and re-opens the breaker. See `models.StageResult.answered`.
            return models.StageResult.failed(
                f"no Open Notebook notebook named '{notebook_name}'. Create it, "
                f"or point category {category} at one that exists in categories.yml.",
                kind="data", answered=True,
            )
        notebook_id = str(notebook.get("id") or "")

        # Reuse an existing source for this file rather than adding the book a
        # second time. `source_exists_for_path` reads the list endpoint, which
        # does carry `asset`; only the notebook membership is missing there.
        source_id = client.source_exists_for_path(on_path)
        reused = bool(source_id)
        if not source_id:
            source_id = client.create_source_from_path(on_path, title=book["title"])

        attached = client.ensure_attached(notebook_id, source_id)

        # Read it back. Everything above can report success while the book is
        # not actually usable, and a silent gap here is exactly what makes the
        # UI say "in the notebook" while you cannot find it there.
        try:
            state = client.source_state(source_id)
        except ClientError as exc:
            # The list endpoint can hand back an id that has since gone. Rather
            # than failing the book, forget it — the next attempt recreates the
            # source from the file, which is cheap and always correct.
            if exc.status == 404:
                return models.StageResult.blocked(
                    f"source {source_id} has gone from Open Notebook — will recreate it",
                    service=exc.service,
                )
            raise
        if not attached or notebook_id not in state["notebooks"]:
            return models.StageResult.failed(
                f"source {source_id} was created but is not attached to "
                f"'{notebook_name}' — it will not appear there",
                kind="server",
            )
        if state["status"] in ("error", "failed"):
            # `answered=True` again: this status came back from the service's own
            # `source_state` read. A source it could not process is a book's
            # problem, not evidence the service is unreachable.
            return models.StageResult.failed(
                f"Open Notebook failed to process source {source_id} "
                f"({state['status']}) — the book is attached but has no readable text",
                kind="data", answered=True,
            )

        verb = "reused existing source" if reused else "added"
        detail = f"{verb} {source_id} in '{notebook_name}' ({state['status']}"
        if state["chunks"]:
            detail += f", {state['chunks']} chunks"
        detail += ")"
        if len(state["notebooks"]) > 1:
            detail += f" [also in {len(state['notebooks']) - 1} other notebook(s)]"
        # Named so `pipeline._advance` can feed it to `breaker.record_success`.
        return models.StageResult.ok(detail, artifact=source_id,
                                     service=client.name)
    except ClientError as exc:
        return models.StageResult.failed(
            str(exc), kind=exc.kind, service=exc.service
        )
    finally:
        client.close()
