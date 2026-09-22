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

    #: The largest page `GET /api/sources` will serve. Its own default is 50,
    #: and it rejects anything above 100 with a 422.
    SOURCE_PAGE_SIZE = 100

    #: How many pages `iter_sources` will walk before giving up.
    #:
    #: A ceiling rather than "until it ends", because the thing this bounds is
    #: the pathological case: the library that produced this code held 4,665
    #: sources and each page costs about six seconds, so an unbounded walk is
    #: five minutes inside a stage that runs once per book per sweep. 50 pages
    #: is 5,000 sources — far past any real library, and past the duplicate
    #: pile too — and a scan that hits the ceiling has already proved that
    #: scanning is the wrong way to answer the question. That is what
    #: `source_exists_for_path`'s recorded-id path is for.
    MAX_SOURCE_PAGES = 50

    def iter_sources(self):
        """Every source, a page at a time.

        `GET /api/sources` is paginated and *silently* so: with no parameters
        it answers with the newest 50 rows and nothing that says there are
        more. Reading that one page as though it were the whole list is what
        created 4,271 duplicate sources — see `source_exists_for_path`.
        """
        offset = 0
        for _ in range(self.MAX_SOURCE_PAGES):
            data = self.json(
                "GET", "/api/sources", "list sources",
                params={"limit": self.SOURCE_PAGE_SIZE, "offset": offset},
                headers=self._auth(),
            )
            items = data if isinstance(data, list) else (data or {}).get("sources", [])
            items = [item for item in items if isinstance(item, dict)]
            if not items:
                return
            yield from items
            if len(items) < self.SOURCE_PAGE_SIZE:
                return
            offset += len(items)

    def source_exists(self, source_id: str) -> bool:
        """Whether this exact source is still there.

        One request against `/api/sources/{id}`, rather than hunting for it in
        a list that has to be paged through. Callers that recorded the id when
        they created the source should ask this instead.
        """
        if not source_id:
            return False
        try:
            data = self.json("GET", f"/api/sources/{source_id}", "read source",
                             headers=self._auth())
        except ClientError as exc:
            if exc.status == 404:
                return False
            raise
        return bool((data or {}).get("id"))

    def delete_source(self, source_id: str) -> None:
        """Remove a source outright.

        Only ever called to clear this app's own duplicates, and only from
        `cli repair --apply`, never from a stage: a per-book stage that deletes
        rows in another service as a side effect is not something an operator
        can predict or undo.
        """
        self.request("DELETE", f"/api/sources/{source_id}", "delete source",
                     headers=self._auth())

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

    @staticmethod
    def _points_at(item: dict, container_path: str) -> bool:
        """Whether this source row references `container_path`.

        The path is not returned in a clean field: Open Notebook reports it
        inside `asset` as a *stringified Python dict* — the literal text
        `{'file_path': '/app/data/uploads/library/…'}` — so an equality check
        against the path never matches. Match on containment, and handle the
        case where it is a real dict.
        """
        for key in ("file_path", "path", "asset", "url"):
            value = item.get(key)
            if not value:
                continue
            if isinstance(value, dict):
                if str(value.get("file_path") or "") == container_path:
                    return True
            elif container_path in str(value):
                return True
        return False

    def sources_for_path(self, container_path: str) -> list[str]:
        """Every source id pointing at this file, oldest first.

        More than one is not hypothetical — it is what this code produced for
        a year. See `source_exists_for_path` for why, and `notebook` for what
        is now done about the extras.
        """
        found: list[tuple[str, str]] = []
        for item in self.iter_sources():
            if self._points_at(item, container_path):
                found.append((str(item.get("created") or ""), str(item.get("id") or "")))
        found.sort()
        return [sid for _created, sid in found if sid]

    def source_exists_for_path(self, container_path: str) -> str | None:
        """Find an existing source already pointing at this file, if any.

        Guards against double-adding when a stage is retried, which makes the
        completeness of the search the whole point of the function. It used to
        read one unparameterised `GET /api/sources`, and that endpoint
        paginates silently: 50 rows, newest first, with nothing in the body
        saying there are more. So the guard only ever saw the 50 most recently
        touched sources, every book outside that window was judged absent, and
        the stage added it again — which made *that* source the newest and
        pushed another book out of the window. It compounds: this library went
        from 78 sources in a day to 331 to 4,169, ending at 4,665 sources for
        394 real files, one book added 174 times, each re-embedded.

        `verify` read the same helper, so those books also reported "no source
        for this file", sat through three forced rescans each and parked as
        failures — 104 of the 105 books on the operator's panel.

        Walking every page fixes the correctness. It does not make this cheap,
        so callers that know the id they created should use `source_exists`.
        """
        ids = self.sources_for_path(container_path)
        return ids[0] if ids else None

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
