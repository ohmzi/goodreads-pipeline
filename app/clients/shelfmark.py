"""Shelfmark — search for a release and queue it.

This instance runs `auth_mode=none`, so no credentials are involved. If that
ever changes, the session-cookie login lives at POST /api/auth/login.

Two calls, per the upstream source:
    GET  /api/releases?provider=manual&title=&author=&content_type=
    POST /api/releases/download  {source, source_id, title, format, content_type, ...}

`provider=manual` searches by title/author with no metadata lookup, which is
what we want: we already have the metadata from Goodreads and do not want
Shelfmark's OpenLibrary lookup (which is flaky on this host) in the path.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass

from ..config import settings
from .base import ClientError, ServiceClient

EBOOK = "ebook"
AUDIOBOOK = "audiobook"

# Newznab category ranges, so a release can be judged by what the indexer says
# it is rather than by trusting the search that returned it.
_NEWZNAB_MOVIES = range(2000, 3000)
_NEWZNAB_TV = range(5000, 6000)
_NEWZNAB_AUDIOBOOK = 3030
_NEWZNAB_BOOKS = range(7000, 8000)

#: Release names that mean "this is a film or an episode", not a book.
_VIDEO_RELEASE = re.compile(
    r"\b(2160p|1080p|720p|480p|WEB[-_. ]?DL|WEB[-_. ]?Rip|BluRay|BDRip|BRRip|HDRip"
    r"|DVDRip|x264|x265|H[._ ]?26[45]|HEVC|REMUX|DDP?[._ ]?5[._ ]?1|AAC5"
    r"|S\d{2}[._ ]?E\d{2}|Season[._ ]?\d|COMPLETE[._ ]?S\d|DTS[-_. ]?HD|Atmos"
    r"|MULTi|REPACK|PROPER)\b",
    re.IGNORECASE,
)

#: An audiobook is spoken word, so it is small. Anything past this is a film
#: wearing an audiobook's clothes — a 24 GB "audiobook" is a 4K movie.
MAX_AUDIOBOOK_BYTES = 5 * 1024 ** 3
MAX_EBOOK_BYTES = 2 * 1024 ** 3


class ReleaseRejected(Exception):
    """Why a release was thrown away, for logging and for the UI."""


def reject_reason(release: "Release", content_type: str) -> str:
    """Return why this release is not the thing we asked for, or "" if it is.

    This exists because the audiobook path was silently downloading films.
    Prowlarr was asked for an audiobook and answered with `Five.Feet.Apart.2019.
    2160p.MA.WEB-DL...` at 24.6 GB, which the ranker then picked as the best
    match — 120 GB of movies accumulated in the audiobook staging folder before
    anyone noticed. The indexer's own category is the most reliable signal, so
    it is checked first; title patterns and size catch what is mislabelled.
    """
    raw = release.raw or {}
    extra = raw.get("extra") or {}
    categories = [c for c in (extra.get("categories") or []) if isinstance(c, int)]
    title = release.title or ""
    size = raw.get("size_bytes") or 0

    if content_type == AUDIOBOOK:
        if any(c in _NEWZNAB_MOVIES or c in _NEWZNAB_TV for c in categories):
            return f"categorised as video by the indexer ({categories})"
        if any(c in _NEWZNAB_BOOKS for c in categories):
            return "categorised as a book, not an audiobook"
        if size and size > MAX_AUDIOBOOK_BYTES:
            return f"{size / 1024**3:.1f} GB is far too large for an audiobook"
    else:
        if any(c in _NEWZNAB_MOVIES or c in _NEWZNAB_TV for c in categories):
            return f"categorised as video by the indexer ({categories})"
        if size and size > MAX_EBOOK_BYTES:
            return f"{size / 1024**3:.1f} GB is far too large for an ebook"

    match = _VIDEO_RELEASE.search(title)
    if match:
        return f"release name looks like video ({match.group(0)})"
    return ""

# Everything Shelfmark may report a queued task as. `complete` is success;
# the rest are terminal failures we should not keep waiting on.
TASK_OK = {"complete", "completed", "done", "success"}
TASK_FAILED = {"error", "failed", "cancelled", "canceled"}
TASK_ACTIVE = {"queued", "locating", "resolving", "downloading", "processing", "in_progress"}


@dataclass
class Release:
    source: str
    source_id: str
    title: str
    format: str = ""
    size: str = ""
    extra: dict | None = None
    raw: dict | None = None

    @property
    def label(self) -> str:
        bits = [self.title or "(untitled)"]
        if self.format:
            bits.append(self.format)
        if self.size:
            bits.append(self.size)
        return " · ".join(bits)


class ShelfmarkClient(ServiceClient):
    name = "shelfmark"
    base_url = settings.shelfmark_url

    #: Shelfmark paces itself between sources (`DEFAULT_SLEEP` is 5s) and walks
    #: several of them, so a release search legitimately takes far longer than
    #: a normal API call. The shared 30s timeout was too aggressive: under load
    #: searches timed out and 53 books were marked failed for what was really
    #: "Shelfmark is busy".
    SEARCH_TIMEOUT = 120.0

    def search(self, title: str, author: str = "", content_type: str = EBOOK) -> list[Release]:
        """Search, then throw away anything that is not the format we asked for.

        Filtering here rather than in the ranker means no caller can
        accidentally hand a film to the download queue.
        """
        params = {
            "provider": "manual",
            "book_id": "0",
            "title": title,
            "content_type": content_type,
        }
        if author:
            params["author"] = author

        data = self.json("GET", "/api/releases", "release search",
                         params=params, timeout=self.SEARCH_TIMEOUT)
        releases = data.get("releases") or []
        out: list[Release] = []
        self.last_rejections: list[str] = []
        for item in releases:
            if not isinstance(item, dict):
                continue
            source = str(item.get("source") or "")
            source_id = str(item.get("source_id") or item.get("id") or "")
            if not source or not source_id:
                continue
            release = Release(
                source=source,
                source_id=source_id,
                title=str(item.get("title") or ""),
                format=str(item.get("format") or ""),
                size=str(item.get("size") or item.get("size_human") or ""),
                extra=item.get("extra") if isinstance(item.get("extra"), dict) else {},
                raw=item,
            )
            why = reject_reason(release, content_type)
            if why:
                self.last_rejections.append(f"{release.title[:60]} — {why}")
                continue
            out.append(release)
        return out

    def rank(self, releases: list[Release], title: str, author: str = "") -> list[Release]:
        """Best-guess ordering: right title, right author, then format quality.

        Deliberately conservative — the caller picks the top entry, and a bad
        pick is more expensive than a slow one, so anything that does not look
        like the book we asked for sorts last rather than being dropped.
        """
        want_title = _norm(title)
        want_author = _norm(author)
        format_rank = {"epub": 0, "azw3": 1, "mobi": 2, "pdf": 3}

        def score(rel: Release) -> tuple:
            got_title = _norm(rel.title)
            got_author = _norm(str((rel.raw or {}).get("author") or ""))
            title_exact = 0 if got_title == want_title else 1
            title_partial = 0 if want_title and want_title in got_title else 1
            author_miss = 0 if (not want_author or want_author in got_author) else 1
            fmt = format_rank.get(rel.format.lower().lstrip("."), 5)
            return (title_exact, title_partial, author_miss, fmt)

        return sorted(releases, key=score)

    def download(self, release: Release, title: str, content_type: str = EBOOK) -> dict:
        body = {
            "source": release.source,
            "source_id": release.source_id,
            "title": title or release.title,
            "format": release.format or ("epub" if content_type == EBOOK else "m4b"),
            "content_type": content_type,
            "extra": release.extra or {},
            "priority": 0,
        }
        data = self.json("POST", "/api/releases/download", "queue download", json=body)
        if isinstance(data, dict) and data.get("error"):
            raise ClientError(self.name, f"queue rejected: {data['error']}")
        return data if isinstance(data, dict) else {}

    def iter_tasks(self):
        """Yield (task_id, state, entry) for every task Shelfmark knows about.

        `/api/status` is keyed by *state* first, then task id:

            {"queued": {"<task_id>": {...}}, "complete": {...}, "error": {...}}

        so the state is the parent key and is not necessarily repeated inside
        the entry. The shape is also not contractual, so a flat
        `{task_id: entry}` layout is accepted too — guessing wrong here would
        silently mean "no tasks found", which the acquire stage would read as
        "the download vanished".
        """
        data = self.json("GET", "/api/status", "task status")
        if not isinstance(data, dict):
            return

        inner = data.get("state")
        if isinstance(inner, dict):
            data = inner

        two_level = any(
            isinstance(value, dict) and any(isinstance(x, dict) for x in value.values())
            for value in data.values()
        )

        for key, value in data.items():
            # An empty state bucket (`"queued": {}`) carries no task and must
            # not be mistaken for one.
            if not isinstance(value, dict) or not value:
                continue
            if two_level:
                for task_id, entry in value.items():
                    if isinstance(entry, dict):
                        state = str(entry.get("status") or key).lower()
                        yield str(task_id), state, entry
            else:
                state = str(value.get("status") or "").lower()
                yield str(key), state, value

    def task_state(self, task_id: str) -> str:
        for found_id, state, _entry in self.iter_tasks():
            if found_id == str(task_id):
                return state
        return ""

    def find_task(
        self, title: str, author: str = "", source_ids: "list[str] | tuple[str, ...]" = ()
    ) -> tuple[str, str, dict] | None:
        """Find the queued task for a book. Returns (task_id, state, entry).

        Two ways to find it, in order of reliability:

        1. **By release id.** A Shelfmark task id embeds the release it came
           from — `libgen:0cd2a846…`, `audiobookbay:…` — so a task can be
           matched exactly by looking for the `source_id` we queued. This is
           what stops a book being declared lost while its task is running
           perfectly well under a differently-worded title.
        2. **By title and author**, for anything the first pass misses.

        `/api/releases/download` answers with no task id, so neither route can
        be replaced by simply remembering what was returned.
        """
        from ..pathing import match_score

        wanted = [str(s) for s in source_ids if s]
        if wanted:
            for task_id, state, entry in self.iter_tasks():
                if any(sid in task_id for sid in wanted):
                    return task_id, state, entry

        best: tuple[float, str, str, dict] | None = None
        for task_id, state, entry in self.iter_tasks():
            haystack = " ".join(
                str(entry.get(field) or "")
                for field in ("title", "book_title", "name", "author")
            )
            if not haystack.strip():
                continue
            score = match_score(haystack, title, author)
            if score < 0.5:
                continue
            if best is None or score > best[0]:
                best = (score, task_id, state, entry)
        if best is None:
            return None
        return best[1], best[2], best[3]

    def task_progress(self, title: str, author: str = "") -> dict | None:
        """Live progress for a book's current task, for the UI.

        Shelfmark reports a real percentage and the time the task was added, so
        we can show elapsed and a projected finish rather than an opaque
        "downloading" that looks identical to a hung task.
        """
        found = self.find_task(title, author)
        if not found:
            return None
        _task_id, state, entry = found
        try:
            progress = float(entry.get("progress") or 0)
        except (TypeError, ValueError):
            progress = 0.0
        try:
            added = float(entry.get("added_time") or 0)
        except (TypeError, ValueError):
            added = 0.0

        result = {
            "state": state,
            "progress": round(progress, 1),
            "status_message": str(entry.get("status_message") or ""),
            "format": str(entry.get("format") or ""),
            "source": str(entry.get("source_display_name") or entry.get("source") or ""),
            "retry_available": bool(entry.get("retry_available")),
            "elapsed_seconds": None,
            "eta_seconds": None,
        }
        if added:
            elapsed = max(0.0, time.time() - added)
            result["elapsed_seconds"] = int(elapsed)
            # Only project a finish from meaningful progress; below a few
            # percent the estimate swings wildly and a wrong ETA is worse than
            # none.
            if 3 <= progress < 100:
                result["eta_seconds"] = int(elapsed * (100 - progress) / progress)
        return result

    def health(self) -> str:
        data = self.json("GET", "/api/health", "health check")
        return str(data.get("status", "unknown"))


def _norm(text: str) -> str:
    """Lowercase alphanumerics only — 'Project Hail Mary: A Novel' == 'project hail mary'."""
    if not text:
        return ""
    text = text.split(":")[0]
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()
