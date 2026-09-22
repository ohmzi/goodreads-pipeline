"""Kavita.

Auth is an API key, accepted either as a `?apiKey=` query param or an
`x-api-key` header — the header is preferred. Note `GET /api/Library` is NOT
the list endpoint (it requires `?libraryId=N`); the list is
`GET /api/Library/libraries`.
"""

from __future__ import annotations

from ..config import settings
from .base import ServiceClient, require_cred
from .indexer import Library, extract_folders, find_by_name, pick_library


class KavitaClient(ServiceClient):
    name = "kavita"
    base_url = settings.kavita_url
    #: Every call carries the API key. See `ServiceClient.quotes_error_bodies`.
    quotes_error_bodies = False

    def _headers(self) -> dict[str, str]:
        return {"x-api-key": require_cred("kavita_api_key", "Kavita API key")}

    def libraries(self) -> list[Library]:
        data = self.json("GET", "/api/Library/libraries", "list libraries",
                         headers=self._headers())
        if not isinstance(data, list):
            return []
        return [
            Library(
                id=str(item.get("id")),
                name=str(item.get("name") or ""),
                folders=extract_folders(item),
            )
            for item in data
            if isinstance(item, dict)
        ]

    def library_for_local_path(self, local_path: str) -> Library | None:
        return pick_library(self.libraries(), local_path)

    def library_named(self, name: str) -> Library | None:
        return find_by_name(self.libraries(), name)

    def scan(self, library_id: str | int) -> None:
        # Returns 200 with an empty body (not 204) and runs asynchronously.
        self.post(
            f"/api/Library/scan?libraryId={library_id}&force=true",
            f"scan library {library_id}",
            headers=self._headers(),
        )

    def find_series(self, query: str) -> list[dict]:
        """Things Kavita would call a match for `query`: series names, and
        standalone volumes that have no series of their own.

        Kavita's search route is `?queryString=` — `?query=` is rejected with a
        400, which looks like a broken endpoint rather than a wrong parameter.

        The response's own `series` array is not the whole answer. A book
        Kavita cannot assign a series to — the ordinary case for a standalone
        novel with no series in its embedded metadata — comes back with
        `series: []` and its real, searchable title sitting instead in
        `chapters[].titleName` (Kavita's per-volume title, read off the file's
        own metadata rather than the folder name `series[].name` would have
        used). Reading only `series` made this method blind to every such
        book: three of L. Frank Baum's own Oz novels scanned into this
        library came back exactly this way — present, found by this same
        search, and invisible to a caller that only looked at `series`. So
        both are read here, into one list, because `verify` cannot fix a
        false "missing" by rescanning something that was never missing.
        """
        if not query.strip():
            return []
        data = self.json(
            "GET", "/api/Search/search", "search",
            params={"queryString": query}, headers=self._headers(),
        )
        data = data or {}
        out = []
        for item in data.get("series") or []:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or item.get("title") or "")
            if name:
                out.append({"name": name, "library_id": item.get("libraryId")})
        for chapter in data.get("chapters") or []:
            if not isinstance(chapter, dict):
                continue
            name = str(chapter.get("titleName") or "")
            # `library_id` has no direct equivalent on a chapter; every caller
            # of this method reads only `name`, so leaving it unset costs
            # nothing real and is honest about not knowing it.
            if name:
                out.append({"name": name, "library_id": None})
        return out

    def health(self) -> str:
        resp = self.get("/api/health", "health check")
        return resp.text.strip() or "unknown"
