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
        """Series matching `query`.

        Kavita's search route is `?queryString=` — `?query=` is rejected with a
        400, which looks like a broken endpoint rather than a wrong parameter.
        """
        if not query.strip():
            return []
        data = self.json(
            "GET", "/api/Search/search", "search",
            params={"queryString": query}, headers=self._headers(),
        )
        series = (data or {}).get("series") or []
        out = []
        for item in series:
            if not isinstance(item, dict):
                continue
            out.append({
                "name": str(item.get("name") or item.get("title") or ""),
                "library_id": item.get("libraryId"),
            })
        return out

    def health(self) -> str:
        resp = self.get("/api/health", "health check")
        return resp.text.strip() or "unknown"
