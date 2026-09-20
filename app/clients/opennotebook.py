"""Open Notebook.

The important choice here is `POST /api/sources/json` with a `file_path`
rather than the multipart `POST /api/sources`. The multipart route stores its
own copy of the book inside Open Notebook, which is exactly the duplication
the whole design is built to avoid; the JSON route references the existing
file in place.

Two constraints follow from how the server validates that path:

  * `file_path` must resolve inside Open Notebook's own `./data/uploads` root
    — on this host the book library is mounted there read-only at
    `/app/data/uploads/library`, which satisfies it exactly.
  * A symlink will NOT work. `_build_content_state` rejects any path that
    `.resolve()` escapes from that root, and `.resolve()` follows symlinks.

So callers must pass a real path under the library mount, in Open Notebook's
namespace — `to_opennotebook_path` below does that translation.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath

from ..config import settings
from .base import ClientError, ServiceClient, cred


class OpenNotebookClient(ServiceClient):
    name = "open notebook"
    base_url = settings.opennotebook_url

    def _auth(self) -> dict[str, str]:
        # Auth is disabled on this instance today, so an empty password simply
        # means "send no header" rather than an error.
        password = cred("opennotebook_password")
        return {"Authorization": f"Bearer {password}"} if password else {}

    def notebooks(self) -> list[dict]:
        data = self.json("GET", "/api/notebooks", "list notebooks", headers=self._auth())
        items = data if isinstance(data, list) else (data or {}).get("notebooks", [])
        return [item for item in items if isinstance(item, dict)]

    def notebook_named(self, name: str) -> dict | None:
        wanted = name.strip().lower()
        for nb in self.notebooks():
            if str(nb.get("name") or "").strip().lower() == wanted:
                return nb
        for nb in self.notebooks():
            if wanted in str(nb.get("name") or "").strip().lower():
                return nb
        return None

    def create_source_from_path(self, container_path: str, title: str) -> str:
        """Register an existing file as a source. Returns the source id.

        Deliberately does NOT attach it to a notebook. Passing `notebooks` here
        attaches on creation, and the caller then attaching again produces a
        *second* link — Open Notebook's attach endpoint appends rather than
        being idempotent, so every retry added another. Creation and
        attachment are kept as separate, individually verifiable steps.
        """
        body: dict = {
            "type": "upload",
            "file_path": container_path,
            "title": title,
            "async_processing": True,
            "embed": True,
            "delete_source": False,
        }
        data = self.json("POST", "/api/sources/json", "create source",
                         json=body, headers=self._auth())
        source_id = str((data or {}).get("id") or (data or {}).get("source_id") or "")
        if not source_id:
            raise ClientError(self.name, f"source created but no id returned: {data}")
        return source_id

    def source_notebooks(self, source_id: str) -> list[str]:
        """Which notebooks a source is attached to.

        Read from the per-source endpoint, NOT the list endpoint: `/api/sources`
        omits the `notebooks` field entirely, so a caller that reads it from
        there concludes nothing is attached when everything is. That mistake
        made a verification pass report all 40 books as missing.
        """
        data = self.json("GET", f"/api/sources/{source_id}", "read source",
                         headers=self._auth())
        notebooks = (data or {}).get("notebooks") or []
        return [str(n) for n in notebooks] if isinstance(notebooks, list) else []

    def source_state(self, source_id: str) -> dict:
        data = self.json("GET", f"/api/sources/{source_id}", "read source",
                         headers=self._auth())
        return {
            "status": str((data or {}).get("status") or "").lower(),
            "embedded": bool((data or {}).get("embedded")),
            "chunks": int((data or {}).get("embedded_chunks") or 0),
            "has_text": bool((data or {}).get("full_text")),
            "notebooks": [str(n) for n in ((data or {}).get("notebooks") or [])],
        }

    def attach(self, notebook_id: str, source_id: str) -> None:
        """Attach a source to a notebook. No request body on this route."""
        self.post(
            f"/api/notebooks/{notebook_id}/sources/{source_id}",
            "attach source to notebook",
            headers=self._auth(),
        )

    def detach(self, notebook_id: str, source_id: str) -> None:
        """Remove one source↔notebook link.

        Note this drops *all* links between that pair, so callers re-attach
        once afterwards when collapsing duplicates.
        """
        self.request("DELETE", f"/api/notebooks/{notebook_id}/sources/{source_id}",
                     "detach source from notebook", headers=self._auth())

    def ensure_attached(self, notebook_id: str, source_id: str) -> bool:
        """Attach exactly once. Returns True if the link now exists.

        Checks first rather than attaching blind, because the attach endpoint
        appends duplicates instead of being idempotent.
        """
        existing = self.source_notebooks(source_id)
        if existing.count(notebook_id) == 1:
            return True
        if existing.count(notebook_id) > 1:
            # Collapse the duplicates this code used to create.
            self.detach(notebook_id, source_id)
        self.attach(notebook_id, source_id)
        return notebook_id in self.source_notebooks(source_id)

    def source_status(self, source_id: str) -> str:
        data = self.json("GET", f"/api/sources/{source_id}/status", "source status",
                         headers=self._auth())
        return str((data or {}).get("status") or "").lower()

    def source_exists_for_path(self, container_path: str) -> str | None:
        """Find an existing source already pointing at this file, if any.

        Guards against double-adding when a stage is retried. The path is not
        returned in a clean field: Open Notebook reports it inside `asset` as a
        *stringified Python dict* — the literal text
        `{'file_path': '/app/data/uploads/library/…'}` — so an equality check
        against the path never matches and every retry silently creates
        another copy of the book. Match on containment, and handle the case
        where it is a real dict.
        """
        data = self.json("GET", "/api/sources", "list sources", headers=self._auth())
        items = data if isinstance(data, list) else (data or {}).get("sources", [])
        for item in items:
            if not isinstance(item, dict):
                continue
            for key in ("file_path", "path", "asset", "url"):
                value = item.get(key)
                if not value:
                    continue
                if isinstance(value, dict):
                    if str(value.get("file_path") or "") == container_path:
                        return str(item.get("id") or "")
                elif container_path in str(value):
                    return str(item.get("id") or "")
        return None

    def health(self) -> str:
        data = self.json("GET", "/api/config", "health check")
        return str((data or {}).get("version") or "unknown")


def to_opennotebook_path(host_relative: str | PurePosixPath) -> str:
    """Translate a path under the book library into Open Notebook's namespace.

    goodreads mounts the library at `settings.books_root`; Open Notebook mounts
    the same host directory at `settings.opennotebook_library_root`.
    """
    rel = PurePosixPath(str(host_relative))
    try:
        rel = rel.relative_to(PurePosixPath(settings.books_root))
    except ValueError as exc:
        raise ClientError(
            "open notebook",
            f"{rel} is not under the library root {settings.books_root}",
        ) from exc
    return str(PurePosixPath(settings.opennotebook_library_root) / rel)


def host_path_for(local: Path | str) -> str:
    """Path as the other containers see it, given our own mount."""
    return str(PurePosixPath(str(local)))
