"""Goodreads.

Read this before changing anything here.

Goodreads has no usable programmatic login since the public shelf pages were
removed (Jan-Feb 2026) and `/user/sign_in` became a stub that hands off to
Amazon's AP flow. So the *only* way in is a real browser session the user
establishes by hand. That session is captured once as Playwright
`storage_state` JSON and everything else runs over plain HTTP with those
cookies.

Design consequences, each of which cost someone hours already:

  * The browser is used for **login only**. Reads and writes go over httpx.
    Driving the DOM for every poll would be slower and break more often.
  * The `jwt_token` cookie is a ~5-minute GraphQL JWT. A **stale copy makes
    Goodreads reject the entire request**, not merely fail auth, so it is
    stripped from the cookie jar before every call.
  * CSRF tokens are primed from `/review/list`, never from `/` — the root page
    is WAF-challenged.
  * A stale CSRF token surfaces as **404**, not 422. Callers must treat 404 as
    "re-prime and retry", never as a permanent failure.
  * `to-read` is an *exclusive* shelf. `POST /shelf/add_to_shelf` with a blank
    `a` adds to a custom shelf, but `a=remove` 404s on exclusive shelves — so
    leaving `to-read` is `POST /review/destroy/<book_id>`.
  * Requests without `Referer`/`Origin`/`X-Requested-With` fall into an
    infinite self-redirect loop.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote

import httpx
from bs4 import BeautifulSoup

from .config import settings
from .db import db
from .models import ALL_SHELF

BASE = "https://www.goodreads.com"
ORIGIN = "https://www.goodreads.com"

# Goodreads' public AppSync endpoint, used for genre lookup without a session.
APPSYNC_URL = "https://kxbwmqov6jgg3daaamb744ycu4.appsync-api.us-east-1.amazonaws.com/graphql"
# Embedded in every page's pageProps, so it rotates with their frontend builds.
APPSYNC_KEY_DEFAULT = "da2-d2fyuybwsbf3poyquvbp2mbiwu"

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

# A stale CSRF token reads as 404 rather than an auth error.
STALE_TOKEN_STATUS = 404
# AWS WAF challenge — back off rather than retry, writes are rate-limited.
WAF_CHALLENGE_STATUS = 202


class GoodreadsError(RuntimeError):
    pass


class SessionExpired(GoodreadsError):
    pass


class RateLimited(GoodreadsError):
    pass


@dataclass
class GoodreadsBook:
    book_id: str
    title: str
    author: str = ""
    isbn: str = ""
    isbn13: str = ""
    year: int | None = None
    cover_url: str = ""
    url: str = ""
    review_id: str = ""
    genres: list[str] = field(default_factory=list)


def state_path() -> Path:
    return settings.data_dir / "goodreads_state.json"


#: Detection makes network calls, and `resolve_user_id` is on the path of a
#: 5-second UI poll. Without a cooldown a failed detection would hammer
#: Goodreads forever; with one it costs a request per ten minutes at worst.
_DETECT_COOLDOWN_SECONDS = 600
_last_detect_attempt = 0.0


def resolve_user_id(refresh: bool = False) -> str:
    """The Goodreads user id, from the UI override, then .env, then the session.

    Deriving it from the stored session means signing in is genuinely the only
    setup step — there is no reason to make someone copy a number out of their
    profile URL when the browser already knows it.
    """
    if not refresh:
        stored = db().get_setting("goodreads_user_id", "")
        if stored:
            return stored
        if settings.goodreads_user_id:
            return settings.goodreads_user_id

    global _last_detect_attempt
    now = time.time()
    if not refresh and (now - _last_detect_attempt) < _DETECT_COOLDOWN_SECONDS:
        return ""
    _last_detect_attempt = now

    try:
        detected = GoodreadsSession().detect_user_id()
    except (GoodreadsError, OSError):
        return ""

    if detected:
        db().set_setting("goodreads_user_id", detected)
        db().log(f"detected Goodreads user id {detected} from the stored session")
    return detected


def set_user_id(user_id: str) -> None:
    """Record an explicit user id, overriding auto-detection."""
    db().set_setting("goodreads_user_id", user_id.strip())


def forget_user_id() -> None:
    db().set_setting("goodreads_user_id", "")


# --------------------------------------------------------------------------
# Session
# --------------------------------------------------------------------------
class GoodreadsSession:
    """An authenticated HTTP session built from a captured browser state."""

    def __init__(self, state_file: Path | None = None):
        self.state_file = state_file or state_path()
        self._client: httpx.Client | None = None
        self._csrf = ""

    # -- lifecycle -------------------------------------------------------
    def has_state(self) -> bool:
        return self.state_file.exists() and self.state_file.stat().st_size > 0

    def session_age_seconds(self) -> float | None:
        if not self.has_state():
            return None
        import time

        return time.time() - self.state_file.stat().st_mtime

    def _cookies(self) -> httpx.Cookies:
        if not self.has_state():
            raise SessionExpired(
                "No Goodreads session stored. Use 'Log in to Goodreads' in the UI."
            )
        raw = json.loads(self.state_file.read_text())
        jar = httpx.Cookies()
        for cookie in raw.get("cookies", []):
            name = cookie.get("name", "")
            # See module docstring: a stale JWT poisons the whole request.
            if name == "jwt_token":
                continue
            jar.set(
                name,
                cookie.get("value", ""),
                domain=cookie.get("domain") or ".goodreads.com",
                path=cookie.get("path") or "/",
            )
        if not len(jar):
            raise SessionExpired("Stored Goodreads session contains no cookies.")
        return jar

    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                base_url=BASE,
                cookies=self._cookies(),
                timeout=httpx.Timeout(30.0, connect=10.0),
                follow_redirects=False,
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
                    "Accept-Language": "en-US,en;q=0.9",
                    # Omitting these causes an infinite self-redirect loop.
                    "Origin": ORIGIN,
                    "Sec-Fetch-Site": "same-origin",
                    "Sec-Fetch-Mode": "navigate",
                    "Sec-Fetch-Dest": "document",
                },
            )
            self._csrf = ""
        return self._client

    def reset(self) -> None:
        """Drop any cached client so the next call rebuilds from disk."""
        if self._client is not None:
            self._client.close()
        self._client = None
        self._csrf = ""

    # -- CSRF ------------------------------------------------------------
    def csrf(self, force: bool = False) -> str:
        """Prime an authenticity token.

        Deliberately primed from the user's own shelf page, not `/` and not
        bare `/review/list`. `/` is WAF-challenged, and bare `/review/list`
        answers with a 302 to `/review/list/<id>` — and because this client
        does not follow redirects, priming there parses an empty body and
        reports the session as expired while it is in fact perfectly healthy.
        """
        if self._csrf and not force:
            return self._csrf

        user_id = resolve_user_id()
        path = f"/review/list/{user_id}" if user_id else "/review/list"
        resp = self.client().get(path, headers={"Referer": f"{ORIGIN}/"})

        # If we still landed on a redirect, follow it once by hand rather than
        # enabling global redirect-following, which would mask session expiry.
        if resp.status_code in (301, 302, 303, 307, 308):
            location = resp.headers.get("location", "")
            if location.startswith("/"):
                resp = self.client().get(location, headers={"Referer": f"{ORIGIN}{path}"})
            else:
                self._guard(resp)

        self._guard(resp)
        soup = BeautifulSoup(resp.text, "html.parser")
        node = soup.find("meta", attrs={"name": "csrf-token"})
        if node and node.get("content"):
            self._csrf = str(node["content"])
            return self._csrf
        node = soup.find("input", attrs={"name": "authenticity_token"})
        if node and node.get("value"):
            self._csrf = str(node["value"])
            return self._csrf
        match = re.search(r'name="authenticity_token"[^>]*value="([^"]+)"', resp.text)
        if match:
            self._csrf = match.group(1)
            return self._csrf
        raise SessionExpired(
            f"Could not find a CSRF token on {path} — the session has most "
            "likely expired. Re-login from the UI."
        )

    # -- guards ----------------------------------------------------------
    def _guard(self, resp: httpx.Response) -> None:
        if resp.status_code == WAF_CHALLENGE_STATUS or (
            resp.headers.get("x-amzn-waf-action") == "challenge"
        ):
            raise RateLimited(
                "Goodreads returned an AWS WAF challenge (HTTP 202). This is "
                "IP-reputation gated — wait 5-10 minutes rather than retrying."
            )
        if resp.status_code in (301, 302, 303, 307, 308):
            location = resp.headers.get("location", "")
            if "/user/sign_in" in location or "/ap/signin" in location:
                raise SessionExpired(
                    "Goodreads redirected to the sign-in page. The stored session "
                    "has expired — re-login from the UI."
                )
        if resp.status_code >= 400 and resp.status_code != STALE_TOKEN_STATUS:
            raise GoodreadsError(f"Goodreads returned HTTP {resp.status_code}")

    # -- reads -----------------------------------------------------------
    def detect_user_id(self) -> str:
        """Work out which account this session belongs to.

        `/review/list` with no id redirects a signed-in user to their own
        shelf, which is the most reliable signal; the home page's "My Books"
        links are the fallback. Returns "" if neither gives an answer.
        """
        try:
            resp = self.client().get("/review/list", headers={"Referer": ORIGIN})
            match = re.search(r"/review/list/(\d+)", resp.headers.get("location", ""))
            if match:
                return match.group(1)
            resp = self.client().get("/", headers={"Referer": ORIGIN})
            found = re.findall(r"/review/list/(\d+)", resp.text)
            if found:
                # The most frequent id is the owner's own shelf.
                return max(set(found), key=found.count)
        except (GoodreadsError, httpx.HTTPError):
            return ""
        return ""

    def fetch_shelf(self, shelf: str = "to-read", max_pages: int = 5) -> list[GoodreadsBook]:
        """Scrape a shelf. Paginates until a page yields no new rows."""
        user_id = resolve_user_id()
        if not user_id:
            raise GoodreadsError(
                "Could not determine your Goodreads user id. Log in from the "
                "Goodreads page, or set it explicitly in Settings."
            )

        books: list[GoodreadsBook] = []
        seen: set[str] = set()
        for page in range(1, max_pages + 1):
            resp = self.client().get(
                f"/review/list/{user_id}",
                params={"shelf": shelf, "page": page, "per_page": 100,
                        # `order=d` matters: without it Goodreads sorts oldest-first,
                        # so a newly added book lands on the *last* page.
                        "sort": "date_added", "order": "d"},
                headers={"Referer": f"{ORIGIN}/review/list/{user_id}"},
            )
            self._guard(resp)
            page_books = parse_shelf_html(resp.text)
            fresh = [b for b in page_books if b.book_id not in seen]
            if not fresh:
                break
            for book in fresh:
                seen.add(book.book_id)
            books.extend(fresh)
        return books


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------
def parse_shelf_html(html: str) -> list[GoodreadsBook]:
    """Pull shelf rows out of a /review/list page.

    Defensive on purpose: the shelf page is still Rails, but row markup has
    changed before and will again. Several selectors are tried, and a row is
    kept if *any* of them yields a book id — a missing author is recoverable,
    a missing book id is not.
    """
    soup = BeautifulSoup(html, "html.parser")
    body = soup.find(id="booksBody") or soup.find(id="books") or soup
    rows = body.select("tr.bookalike.review") or body.select('tr[id^="review_"]')
    if not rows:
        rows = soup.select('tr[id^="review_"]')

    books: list[GoodreadsBook] = []
    for row in rows:
        book_id = _book_id_from_row(row)
        if not book_id:
            continue
        books.append(
            GoodreadsBook(
                book_id=book_id,
                title=_cell_text(row, "title"),
                author=_cell_text(row, "author"),
                isbn=_cell_text(row, "isbn"),
                isbn13=_cell_text(row, "isbn13"),
                year=_year_from_row(row),
                cover_url=_cover_from_row(row),
                url=f"{ORIGIN}/book/show/{book_id}",
                review_id=str(row.get("id") or "").removeprefix("review_"),
            )
        )
    return books


def _cell_text(row, field_name: str) -> str:
    cell = row.select_one(f"td.field.{field_name}")
    if cell is None:
        return ""
    value = cell.select_one(".value") or cell
    link = value.find("a")
    text = (link or value).get_text(" ", strip=True)
    return re.sub(r"\s+", " ", text).strip()


def _book_id_from_row(row) -> str:
    # Preferred: the tooltip trigger carries the canonical Book resource id.
    node = row.select_one('div.js-tooltipTrigger[data-resource-type="Book"]')
    if node and node.get("data-resource-id"):
        return str(node["data-resource-id"])
    # The title link href is equally reliable.
    link = row.select_one("td.field.title .value a") or row.select_one('a[href*="/book/show/"]')
    if link and link.get("href"):
        match = re.search(r"/book/show/(\d+)", str(link["href"]))
        if match:
            return match.group(1)
    for attr in ("data-resource-id", "data-book-id"):
        if row.get(attr):
            return str(row[attr])
    return ""


def _cover_from_row(row) -> str:
    cell = row.select_one("td.field.cover img")
    if cell is None:
        return ""
    return str(cell.get("src") or "")


def _year_from_row(row) -> int | None:
    for field_name in ("date_pub", "date_published", "year"):
        text = _cell_text(row, field_name)
        match = re.search(r"(1[5-9]\d{2}|20\d{2})", text)
        if match:
            return int(match.group(1))
    return None


# --------------------------------------------------------------------------
# Genres (no session required)
# --------------------------------------------------------------------------
def fetch_genres(book_id: str, api_key: str = APPSYNC_KEY_DEFAULT) -> list[str]:
    """Genres for a book, via Goodreads' public AppSync GraphQL endpoint.

    The returned order is Goodreads' own popularity ranking, which is what the
    classifier relies on to choose a single category. The shape is
    `bookGenres { genre { name } }` — `genres`, and `name` directly on
    `BookGenre`, are both undefined. Getting this wrong is quiet and expensive:
    every book returns no genres and the whole shelf silently classifies to the
    fallback.

    Returns [] rather than raising: a genre miss degrades to the fallback
    category, which is a much better outcome than a failed stage.
    """
    query = """
    query BookGenres($id: Int!) {
      getBookByLegacyId(legacyId: $id) {
        title
        bookGenres { genre { name } }
      }
    }
    """
    try:
        resp = httpx.post(
            APPSYNC_URL,
            headers={"content-type": "application/json", "x-api-key": api_key},
            json={"query": query, "variables": {"id": int(book_id)}},
            timeout=20.0,
        )
        if resp.status_code >= 400:
            return []
        payload = resp.json()
    except (httpx.HTTPError, ValueError, TypeError):
        return []

    # Parse the known shape first; fall back to a tree walk so a future nesting
    # change degrades to "fewer genres" rather than "none".
    try:
        entries = payload["data"]["getBookByLegacyId"]["bookGenres"] or []
        genres = [
            str(entry["genre"]["name"]).strip()
            for entry in entries
            if isinstance(entry, dict) and (entry.get("genre") or {}).get("name")
        ]
        if genres:
            return list(dict.fromkeys(genres))
    except (KeyError, TypeError):
        pass
    return _harvest_genres(payload)


def _harvest_genres(payload) -> list[str]:
    """Collect genre names wherever they sit in the response.

    The GraphQL shape has shifted between frontend builds, so rather than
    bind to one nesting we walk the tree and take any `name` inside a
    `genres`/`genre` node, preserving document order (which is the ranking we
    use for weighting).
    """
    found: list[str] = []

    def walk(node, under_genre: bool = False):
        if isinstance(node, dict):
            for key, value in node.items():
                child_under = under_genre or key in ("genres", "genre", "bookGenres")
                if child_under and key == "name" and isinstance(value, str):
                    if value.strip() and value.strip() not in found:
                        found.append(value.strip())
                    continue
                walk(value, child_under)
        elif isinstance(node, list):
            for item in node:
                walk(item, under_genre)

    walk(payload)
    return found


# --------------------------------------------------------------------------
# Writes
# --------------------------------------------------------------------------
def add_to_shelf(session: GoodreadsSession, book_id: str, shelf: str) -> set[str]:
    """Add a book to a (non-exclusive) shelf. Returns the book's shelves after.

    `a` is deliberately empty when adding — `a=add` is not the wire format,
    however natural it looks.

    The response is the authoritative answer and is parsed here rather than
    re-fetched. Verification used to page the shelf and look for the book,
    which silently fails once a shelf passes one page: `collected-pdf` has more
    than a hundred books, the page was sorted oldest-first, and the book just
    added was therefore never on it. Two books were left on to-read for days
    because of it — the add had worked perfectly every time.
    """
    body = _post_shelf_change(
        session,
        path="/shelf/add_to_shelf",
        data={"book_id": book_id, "name": shelf, "a": "", "authenticity_token": ""},
        referer=f"{ORIGIN}/review/edit/{book_id}",
        raw=True,
    )
    return _shelves_from_response(body)


def remove_from_to_read(session: GoodreadsSession, book_id: str) -> None:
    """Leave an *exclusive* shelf.

    `a=remove` 404s for to-read/read/currently-reading, so this destroys the
    review record instead.
    """
    _post_shelf_change(
        session,
        path=f"/review/destroy/{book_id}",
        data={"authenticity_token": ""},
        referer=f"{ORIGIN}/review/list",
    )


def ensure_shelf(session: GoodreadsSession, shelf: str) -> None:
    """Create the shelf if it is not already there.

    Goodreads' `add_to_shelf` does *not* create a missing shelf — it silently
    does nothing. Combined with a successful removal from to-read that loses
    the book entirely, which is exactly what happened before this existed.
    """
    try:
        _post_shelf_change(
            session,
            path="/user_shelves",
            data={"user_shelf[name]": shelf, "commit": "add", "utf8": "✓"},
            referer=f"{ORIGIN}/review/list",
        )
    except GoodreadsError:
        # Almost always "you already have a shelf by that name", which is the
        # outcome we wanted anyway.
        pass


def _shelves_from_response(body: str) -> set[str]:
    """Shelf names out of an add_to_shelf response.

    The endpoint answers with the book's shelf widget as HTML, which is the
    authoritative record of where the book now sits — reading it avoids a
    second request that can be wrong.

    The names are taken from the link *text*, not the href. Goodreads addresses
    exclusive shelves with `?shelf=` and custom shelves with `?tag=`, so a
    href-based parse silently sees only to-read/read/currently-reading and
    concludes that a custom shelf was never applied.
    """
    names: set[str] = set()
    if not body:
        return names
    soup = BeautifulSoup(body, "html.parser")
    for anchor in soup.select("a.shelfLink"):
        text = anchor.get_text(strip=True)
        if text:
            names.add(text)
    if names:
        return names
    # Older markup without the class: fall back to reading the hrefs.
    for match in re.finditer(r'href="[^"]*[?&](?:shelf|tag)=([^"&]+)"', body):
        names.add(unquote(match.group(1)))
    return names


def book_on_shelf(session: GoodreadsSession, book_id: str, shelf: str) -> bool:
    """Is the book actually on that shelf right now?

    Checked against the newest page of the shelf. Note this is only reliable
    while the shelf fits on one page — for a shelf that has outgrown it, use
    the result of `add_to_shelf`, which needs no extra request and cannot miss.
    """
    entries = session.fetch_shelf(shelf, max_pages=1)
    ids = [entry.book_id for entry in entries]
    if str(book_id) not in ids:
        return False

    if shelf != ALL_SHELF:
        all_ids = [entry.book_id for entry in session.fetch_shelf(ALL_SHELF, max_pages=1)]
        if all_ids and all_ids == ids:
            # We were shown everything, so this shelf does not really exist.
            return False
    return True


def move_to_shelf(session: GoodreadsSession, book_id: str, shelf: str) -> None:
    """Shelve a finished book: onto the collected shelf, off to-read.

    This is more awkward than it should be, and the awkwardness is Goodreads'.

    A book cannot simply be removed from to-read. Both removal routes —
    `POST /shelf/add_to_shelf` with `a=remove`, and `POST /review/destroy/<id>`
    — destroy the whole review record, which **also removes the book from every
    custom shelf**. Verified: adding to `collected-pdf` then removing from
    to-read leaves the book on no shelf at all. The only non-destructive way
    off to-read is to move it to a different *exclusive* shelf (`read` or
    `currently-reading`), and those are meaningful states we should not fake.

    So the sequence is: shelve, confirm, destroy, re-shelve, confirm again.
    The confirmation comes from the add response itself, which is the only
    source that stays correct once a shelf outgrows one page.
    """
    ensure_shelf(session, shelf)

    # 1. Shelve it, and confirm from the response that it took.
    shelves = add_to_shelf(session, book_id, shelf)
    if shelf not in shelves and not book_on_shelf(session, book_id, shelf):
        raise GoodreadsError(
            f"could not shelve onto '{shelf}'; left on to-read rather than "
            f"removing it from there"
        )

    if "to-read" not in shelves:
        # The add response lists every shelf the book now sits on, including
        # to-read — so this is the authoritative answer, and the page-based
        # check it replaces was not. `book_on_shelf` reads one page, and
        # to-read holds hundreds of books, so a book deep in that list looked
        # absent and this returned early without removing it. 42 books ended up
        # sitting on both to-read and their collected shelf because of it.
        if not book_on_shelf(session, book_id, "to-read"):
            return  # genuinely already off to-read; nothing more to do

    # 2. Leave to-read. This takes the custom shelf with it.
    remove_from_to_read(session, book_id)

    # 3. Put the custom shelf back, and confirm again.
    shelves = add_to_shelf(session, book_id, shelf)
    if shelf not in shelves and not book_on_shelf(session, book_id, shelf):
        raise GoodreadsError(
            f"book left to-read but the re-shelve onto '{shelf}' did not take"
        )


def _post_shelf_change(
    session: GoodreadsSession, path: str, data: dict, referer: str,
    retry: bool = True, raw: bool = False,
) -> str:
    payload = dict(data)
    payload["authenticity_token"] = session.csrf()

    resp = session.client().post(
        path,
        data=payload,
        headers={
            "Referer": referer,
            "Origin": ORIGIN,
            "X-Requested-With": "XMLHttpRequest",
            "Content-Type": "application/x-www-form-urlencoded",
            "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-Mode": "cors",
        },
    )

    # A stale token reads as 404, not 422 — re-prime once and retry.
    if resp.status_code == STALE_TOKEN_STATUS and retry:
        session.csrf(force=True)
        return _post_shelf_change(session, path, data, referer, retry=False, raw=raw)

    session._guard(resp)
    if resp.status_code >= 400:
        raise GoodreadsError(
            f"Goodreads rejected {path} with HTTP {resp.status_code}: {resp.text[:200]}"
        )
    return resp.text if raw else ""
