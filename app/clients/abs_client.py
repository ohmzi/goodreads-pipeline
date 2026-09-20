"""Audiobookshelf.

`POST /login` returns a legacy `user.token` that does not expire plus a 1h
`accessToken`; we use the long-lived one and re-login on a 401. Refresh lives
at the root `/auth/refresh`, not under `/api`.

There is no per-path library lookup convenience here worth the extra call —
ABS libraries are few, so `library_named` is what the index stage uses.
"""

from __future__ import annotations

import threading
from pathlib import PurePosixPath

from ..config import settings
from .base import ClientError, ServiceClient, require_cred
from .indexer import Library, extract_folders, find_by_name, pick_library

#: base_url -> bearer token. Shared across instances because every stage builds
#: its own client, and Audiobookshelf rate-limits repeated logins.
_TOKEN_CACHE: dict[str, str] = {}
_TOKEN_LOCK = threading.Lock()


class AudiobookshelfClient(ServiceClient):
    name = "audiobookshelf"
    base_url = settings.abs_url

    def __init__(self, base_url: str | None = None):
        super().__init__(base_url)
        self._token = ""

    def login(self) -> str:
        """Log in once and share the token.

        Every stage builds its own client, so logging in per instance meant a
        fresh login for every book, every sweep — and Audiobookshelf answers
        `429 Too many authentication requests` long before the work is done.
        The token is cached module-wide for the same reason it is for
        BookLore and Grimmory.
        """
        key = self.base_url
        with _TOKEN_LOCK:
            if _TOKEN_CACHE.get(key):
                self._token = _TOKEN_CACHE[key]
                return self._token

            username = require_cred("abs_username", "Audiobookshelf username")
            password = require_cred("abs_password", "Audiobookshelf password")
            data = self.json(
                "POST", "/login", "login", json={"username": username, "password": password}
            )
            user = (data or {}).get("user") or {}
            token = str(user.get("token") or user.get("accessToken") or "")
            if not token:
                raise ClientError(self.name, "login succeeded but returned no token")
            # `user.token` does not expire, so it can be held for the process's
            # lifetime; a 401 still forces a fresh login via `_authed`.
            _TOKEN_CACHE[key] = token
            self._token = token
            return token

    def _auth(self) -> dict[str, str]:
        if not self._token:
            self.login()
        return {"Authorization": f"Bearer {self._token}"}

    def _authed(self, method: str, path: str, what: str, **kwargs):
        try:
            return self.request(method, path, what, headers=self._auth(), **kwargs)
        except ClientError as exc:
            if exc.status in (401, 403):
                _TOKEN_CACHE.pop(self.base_url, None)
                self._token = ""
                return self.request(method, path, what, headers=self._auth(), **kwargs)
            raise

    def libraries(self) -> list[Library]:
        data = self._authed("GET", "/api/libraries", "list libraries").json()
        items = (data or {}).get("libraries") or []
        return [
            Library(
                id=str(item.get("id")),
                name=str(item.get("name") or ""),
                folders=extract_folders(item),
            )
            for item in items
            if isinstance(item, dict)
        ]

    def library_for_local_path(self, local_path: str) -> Library | None:
        """Which ABS library holds this file.

        `local_path` is how *goodreads* sees it, under our `/audiobooks` mount.
        Audiobookshelf is the one app that does not mount the library at a
        neutral path — it mounts the host directory at its own full path — so
        the path has to be rewritten into its namespace before it can be
        matched. Comparing the two directly silently matches nothing, which
        shows up as the audiobook never being indexed.
        """
        try:
            relative = PurePosixPath(local_path).relative_to(
                PurePosixPath(str(settings.audiobooks_root))
            )
        except ValueError:
            # Not under the audiobooks root, so it cannot be ours to index.
            return None
        translated = str(PurePosixPath(settings.abs_library_root) / relative)
        return pick_library(self.libraries(), translated)

    def library_named(self, name: str) -> Library | None:
        return find_by_name(self.libraries(), name)

    def scan(self, library_id: str | int) -> None:
        # Returns 200 immediately; the scan itself is async.
        self._authed("POST", f"/api/libraries/{library_id}/scan?force=1",
                     f"scan library {library_id}")

    def find_books(self, title: str, library_id: str | None = None) -> list[dict]:
        """Audiobooks matching `title` in a library (the first one by default)."""
        needle = (title or "").strip().lower()
        if not needle:
            return []
        if library_id is None:
            libs = self.libraries()
            if not libs:
                return []
            library_id = libs[0].id
        data = self._authed(
            "GET", f"/api/libraries/{library_id}/search", "search library",
            params={"q": title},
        ).json()
        out = []
        for item in (data or {}).get("book") or []:
            if isinstance(item, dict):
                out.append({
                    "title": str(item.get("title") or ""),
                    "author": str(item.get("author") or ""),
                })
        return out

    def health(self) -> str:
        data = self.json("GET", "/status", "health check")
        return str(data.get("version") or "unknown")
