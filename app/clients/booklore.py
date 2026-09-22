"""BookLore, and (by subclass) Grimmory.

Grimmory is a fork of BookLore 2.2.x, so the API is the same shape — the only
practical differences are the base URL and the token lifetime: BookLore issues
a 10h token, Grimmory a 2h one.

**Do not log in more than you have to.** BookLore's `/api/v1/auth/login`
accepts one login and then answers subsequent ones with
`400 {"message":"A data conflict occurred."}` until the issued token is
retired — it appears to keep one refresh-token row per user and collide with
itself. A client that logs in per call therefore works exactly once and then
fails, which looks like bad credentials and is not. Tokens are cached per
(base_url, username) and reused until they are near expiry.
"""

from __future__ import annotations

import threading
import time

from ..config import settings
from .base import ClientError, ServiceClient, require_cred
from .indexer import Library, extract_folders, find_by_name, pick_library
from ..pathing import titles_match

#: (base_url|username) -> (access_token, expires_at). Module-level so every
#: client instance shares one login rather than each starting its own.
_TOKEN_CACHE: dict[str, tuple[str, float]] = {}
_TOKEN_LOCK = threading.Lock()

#: (base_url|username) -> (books, fetched_at). The full library is ~1 MB, and a
#: verification sweep asks about every book in turn.
_BOOKS_CACHE: dict[str, tuple[list[dict], float]] = {}
_BOOKS_LOCK = threading.Lock()

#: Re-login this long before the token actually expires, to absorb clock skew.
_EXPIRY_MARGIN = 120.0


class BookLoreClient(ServiceClient):
    name = "booklore"
    base_url = settings.booklore_url
    #: The login body carries a password and every later call a bearer token.
    #: See `ServiceClient.quotes_error_bodies`.
    quotes_error_bodies = False

    #: Prefix for the credential keys this app reads, e.g. "booklore_username".
    cred_prefix = "booklore"

    #: How long the server's access token lasts. Overridden per subclass.
    token_ttl = 10 * 3600

    def __init__(self, base_url: str | None = None):
        super().__init__(base_url)
        self._token = ""

    def _cache_key(self) -> str:
        return f"{self.base_url}|{self.cred_prefix}"

    def login(self, force: bool = False) -> str:
        key = self._cache_key()
        with _TOKEN_LOCK:
            if not force:
                cached = _TOKEN_CACHE.get(key)
                if cached and cached[1] > time.time() + _EXPIRY_MARGIN:
                    self._token = cached[0]
                    return self._token

            username = require_cred(f"{self.cred_prefix}_username", f"{self.name} username")
            password = require_cred(f"{self.cred_prefix}_password", f"{self.name} password")

            try:
                data = self.json(
                    "POST",
                    "/api/v1/auth/login",
                    "login",
                    json={"username": username, "password": password},
                )
            except ClientError as exc:
                # A conflict here usually means a previous token is still live
                # and the server will not mint a second one. Wait briefly and
                # try once more rather than surfacing a misleading auth error.
                #
                # Read from `exc.body`, never from `str(exc)`: this client
                # withholds response bodies from the message, because the
                # request carries the password. The status cannot stand in for
                # it — BookLore answers 400 to a *wrong password* as well, so
                # branching on 400 alone would sleep and retry for every typo.
                if exc.status == 400 and "conflict" in exc.body.lower():
                    time.sleep(1.5)
                    data = self.json(
                        "POST",
                        "/api/v1/auth/login",
                        "login",
                        json={"username": username, "password": password},
                    )
                else:
                    raise

            token = str((data or {}).get("accessToken") or "")
            if not token:
                raise ClientError(self.name, "login succeeded but returned no accessToken")
            _TOKEN_CACHE[key] = (token, time.time() + self.token_ttl)
            self._token = token
            return token

    def _auth(self) -> dict[str, str]:
        if not self._token:
            self.login()
        return {"Authorization": f"Bearer {self._token}"}

    def _authed(self, method: str, path: str, what: str, **kwargs):
        """Run a call, re-logging in once if the token has expired."""
        try:
            return self.request(method, path, what, headers=self._auth(), **kwargs)
        except ClientError as exc:
            if exc.status in (401, 403):
                _TOKEN_CACHE.pop(self._cache_key(), None)
                self._token = ""
                self.login()
                return self.request(method, path, what, headers=self._auth(), **kwargs)
            raise

    def libraries(self) -> list[Library]:
        data = self._authed("GET", "/api/v1/libraries", "list libraries")
        items = data.json() if isinstance(data.json(), list) else data.json().get("libraries", [])
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
        return pick_library(self.libraries(), local_path)

    def library_named(self, name: str) -> Library | None:
        return find_by_name(self.libraries(), name)

    def scan(self, library_id: str | int) -> None:
        # PUT, not POST — and it returns 204 fire-and-forget.
        self._authed("PUT", f"/api/v1/libraries/{library_id}/refresh",
                     f"refresh library {library_id}")

    def all_books(self, max_age: float = 60.0) -> list[dict]:
        """Every book the app knows about, flattened to title/author/library.

        There is no usable search endpoint on this version — `/api/v1/books/search`
        answers 500 for every parameter tried, and `/api/v1/books` ignores
        filter params and returns the whole library. So verification fetches
        the list (about 1 MB, ~0.3s) and matches locally. Titles live under
        `metadata`, not at the top level.

        Cached briefly and shared across instances: a verification sweep asks
        about every book in turn, and re-downloading a megabyte per book would
        be absurd.
        """
        key = f"{self.base_url}|{self.cred_prefix}"
        now = time.time()
        with _BOOKS_LOCK:
            cached = _BOOKS_CACHE.get(key)
            if cached and (now - cached[1]) < max_age:
                return cached[0]

        data = self._authed("GET", "/api/v1/books", "list books").json()
        items = data if isinstance(data, list) else (data or {}).get("content", [])
        out = []
        for item in items:
            if not isinstance(item, dict):
                continue
            meta = item.get("metadata") or {}
            authors = meta.get("authors") or []
            out.append({
                "title": str(meta.get("title") or ""),
                "author": ", ".join(str(a) for a in authors) if isinstance(authors, list) else str(authors),
                "library": str(item.get("libraryName") or ""),
            })
        with _BOOKS_LOCK:
            _BOOKS_CACHE[key] = (out, now)
        return out

    def find_books(self, title: str) -> list[dict]:
        """Books whose title matches, compared on a punctuation-insensitive form."""
        if not (title or "").strip():
            return []
        return [b for b in self.all_books() if titles_match(b["title"], title)]

    def health(self) -> str:
        resp = self.get("/api/v1/healthcheck", "health check")
        return resp.text.strip()[:200] or "unknown"


class GrimmoryClient(BookLoreClient):
    name = "grimmory"
    base_url = settings.grimmory_url
    cred_prefix = "grimmory"
    token_ttl = 2 * 3600
