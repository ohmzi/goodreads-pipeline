"""Genre lookup with fallbacks across every source we have.

One provider is not enough. Goodreads has the best data (ranked by popularity,
which the classifier depends on) but returns nothing for a book it does not
know, and its AppSync key rotates. So genre resolution walks a chain and stops
at the first source that returns anything:

  1. Goodreads    — ranked genres, highest quality. Needs no key by default.
  2. OpenLibrary  — keyless, and its search endpoint carries rich subject lists
                    where the plain edition endpoint usually has none.
  3. Google Books — good BISAC-style categories, but rate-limits hard without
                    an API key, so it is only tried when one is configured.
  4. Embedded     — `dc:subject` from the actual epub on disk. Fully offline,
                    works when every API is down or blocked.

Order matters and is not alphabetical: the classifier takes the *first* genre
that matches a rule, so a source that returns genres in genuine relevance
order is worth more than one that returns an unordered set. Every attempt is
recorded, so when a book still ends up on the fallback category the report can
say exactly which sources were tried and what each said.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

import httpx

from .clients.base import cred
from .config import settings

TIMEOUT = httpx.Timeout(20.0, connect=8.0)
UA = {"User-Agent": "goodreads/0.1 (personal library manager)"}

#: Largest XML document read out of an epub, in bytes. `ZipInfo.file_size` is
#: what the archive declares about itself, and a corrupt or hostile one can
#: declare — or hide behind — a great deal. This is the bound on what gets
#: pulled into memory and handed to a parser.
MAX_OPF_BYTES = 1024 * 1024


class _UnreadableEntry(Exception):
    """A zip entry that is larger than `MAX_OPF_BYTES`, or carries entities."""

#: OpenLibrary subjects are folksonomy, not a controlled vocabulary, so they
#: arrive with a lot of noise — "nyt:hardcover-fiction=2021-05-23" and friends
#: would otherwise substring-match a `fiction` rule and drag books to Fiction.
_NOISE = re.compile(
    r"=|^nyt:|new york times|bestseller|best seller|book club|large print|"
    r"audiobook|audio cd|ebook|^fiction$|^general$|^accessible book$|"
    r"^protected daisy$|^in library$|^overdrive$|^series$|^collection$",
    re.IGNORECASE,
)


@dataclass
class GenreLookup:
    genres: list[str] = field(default_factory=list)
    source: str = ""
    tried: list[str] = field(default_factory=list)

    @property
    def found(self) -> bool:
        return bool(self.genres)


# --------------------------------------------------------------------------
# providers
# --------------------------------------------------------------------------
def _from_goodreads(book: dict) -> list[str]:
    from .goodreads import APPSYNC_KEY_DEFAULT, fetch_genres

    goodreads_id = str(book.get("goodreads_id") or "")
    if not goodreads_id:
        return []
    key = cred("goodreads_appsync_key") or APPSYNC_KEY_DEFAULT
    return fetch_genres(goodreads_id, api_key=key)


def _openlibrary_query(book: dict) -> str:
    if book.get("isbn13"):
        return f"isbn:{book['isbn13']}"
    if book.get("isbn"):
        return f"isbn:{book['isbn']}"
    title = str(book.get("title") or "").split(":")[0].strip()
    author = str(book.get("author") or "").split(",")[0].strip()
    query = f"title:{title}"
    if author:
        query += f" author:{author}"
    return query


def _from_openlibrary(book: dict) -> list[str]:
    query = _openlibrary_query(book)
    if not query.strip("title: author:"):
        return []
    resp = httpx.get(
        "https://openlibrary.org/search.json",
        params={"q": query, "fields": "title,subject", "limit": 2},
        headers=UA,
        timeout=TIMEOUT,
        follow_redirects=True,
    )
    if resp.status_code != 200:
        return []
    subjects: list[str] = []
    for doc in (resp.json().get("docs") or []):
        for subject in (doc.get("subject") or []):
            text = str(subject).strip()
            if text and not _NOISE.search(text) and text.lower() not in [
                s.lower() for s in subjects
            ]:
                subjects.append(text)
    return subjects


def _from_google_books(book: dict) -> list[str]:
    key = cred("googlebooks_api_key")
    if not key:
        # Without a key the endpoint answers 429 almost immediately, so there
        # is no point spending a request on it.
        return []
    query = book.get("isbn13") or book.get("isbn") or ""
    params = {"q": f"isbn:{query}" if query else f"intitle:{book.get('title', '')}"}
    resp = httpx.get(
        "https://www.googleapis.com/books/v1/volumes",
        params={**params, "key": key},
        headers=UA,
        timeout=TIMEOUT,
    )
    if resp.status_code != 200:
        return []
    categories: list[str] = []
    for item in (resp.json().get("items") or [])[:2]:
        for category in ((item.get("volumeInfo") or {}).get("categories") or []):
            # Google returns "Science Fiction / General"; the classifier works
            # on substrings, so keep the whole string and let rules match it.
            text = str(category).strip()
            if text and text.lower() not in [c.lower() for c in categories]:
                categories.append(text)
    return categories


def _from_embedded(book: dict, file_path: str | None) -> list[str]:
    """`dc:subject` values out of the epub itself. No network at all."""
    if not file_path:
        return []
    path = Path(file_path)
    if not path.is_file() or path.suffix.lower() != ".epub":
        return []
    try:
        with zipfile.ZipFile(path) as archive:
            opf_name = _opf_path(archive)
            if not opf_name:
                return []
            root = ET.fromstring(_read_entry(archive, opf_name, MAX_OPF_BYTES))
    except (zipfile.BadZipFile, KeyError, ET.ParseError, OSError, _UnreadableEntry):
        return []

    subjects: list[str] = []
    for node in root.iter():
        if not node.tag.endswith("subject") or not (node.text or "").strip():
            continue
        text = node.text.strip()
        if text.lower() not in [s.lower() for s in subjects]:
            subjects.append(text)
    return subjects


def _read_entry(archive: zipfile.ZipFile, name: str, limit: int) -> bytes:
    """Read one XML entry from an epub, refusing an oversized or entity-bearing one.

    Shared by `META-INF/container.xml` and the package document it points at,
    because both are XML that arrives inside a downloaded file and neither is
    more trustworthy than the other — guarding only the OPF would leave the
    smaller file in front of it unguarded.

    The size is taken from `ZipInfo` *before* reading, and there is deliberately
    no branch for "read and then notice it was too long": `zipfile` clamps a
    read to the size the central directory declares, so such a branch could
    never fire. `<!ENTITY` is what is refused, not `<!DOCTYPE` — a bare DOCTYPE
    is ordinary in the OPF files this has to keep accepting, and rejecting it
    would fail books that parse perfectly well today.
    """
    info = archive.getinfo(name)
    if info.file_size > limit:
        raise _UnreadableEntry(f"{name} is {info.file_size} bytes, over the {limit} cap")
    data = archive.read(name)
    if b"<!ENTITY" in data:
        raise _UnreadableEntry(f"{name} defines XML entities")
    return data


def _opf_path(archive: zipfile.ZipFile) -> str:
    """Resolve the OPF package document via META-INF/container.xml."""
    try:
        container = ET.fromstring(
            _read_entry(archive, "META-INF/container.xml", MAX_OPF_BYTES)
        )
    except (KeyError, ET.ParseError, _UnreadableEntry):
        return ""
    for node in container.iter():
        if node.tag.endswith("rootfile") and node.get("full-path"):
            return str(node.get("full-path"))
    return ""


# --------------------------------------------------------------------------
# the chain
# --------------------------------------------------------------------------
def lookup(book: dict, file_path: str | None = None) -> GenreLookup:
    """Resolve genres for a book, trying each source until one answers.

    `file_path` is optional and only enables the embedded-metadata source; the
    chain works without it. Every provider is isolated — one raising or timing
    out must not stop the next, which is the whole point of having fallbacks.
    """
    result = GenreLookup()
    providers = [
        ("goodreads", lambda: _from_goodreads(book)),
        ("openlibrary", lambda: _from_openlibrary(book)),
        ("googlebooks", lambda: _from_google_books(book)),
        ("embedded", lambda: _from_embedded(book, file_path)),
    ]

    for name, fetch in providers:
        try:
            genres = fetch()
        except Exception as exc:  # noqa: BLE001 - a source must never break the chain
            result.tried.append(f"{name}: error {type(exc).__name__}")
            continue
        if genres:
            result.genres = genres
            result.source = name
            result.tried.append(f"{name}: {len(genres)} genres")
            return result
        result.tried.append(f"{name}: none")

    return result


def local_file_for(book_id: int) -> str | None:
    """The epub on disk for this book, if it has been downloaded already.

    Checks the placed library path first, then anywhere in staging — the
    embedded source is most useful precisely when the APIs have failed, and by
    then the file is usually sitting in one of those two places.
    """
    from .db import db
    from .pathing import find_matching_entries

    try:
        from .stages.place import placed_paths

        placed = placed_paths(book_id)
        if placed.get("ebook"):
            return placed["ebook"]
    except Exception:  # noqa: BLE001 - placement bookkeeping is best-effort here
        pass

    row = db().get_book(book_id)
    if row is None:
        return None
    title = row["title"]
    author = row["author"] or ""
    for _score, path in find_matching_entries(settings.staging_dir, title, author):
        if path.is_file() and path.suffix.lower() in (".epub", ".mobi", ".azw3", ".pdf"):
            return str(path)
    return None
