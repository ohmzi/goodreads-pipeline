"""Filesystem helpers.

The no-duplicates guarantee lives in `move_into_place`: it insists on
`os.rename`, which is only possible within one filesystem. If it ever crosses
a device boundary we raise rather than fall back to a copy, because a silent
copy is precisely the failure this project exists to prevent.
"""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

EBOOK_EXTS = {".epub", ".mobi", ".azw3", ".azw", ".pdf", ".fb2", ".djvu", ".cbz", ".cbr"}
AUDIO_EXTS = {".mp3", ".m4a", ".m4b", ".flac", ".ogg", ".opus", ".aac", ".wav"}

_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def is_junk(path: Path) -> bool:
    """True for files that look like media but are not.

    This library is full of `._name.mp3` companions copied from macOS — tiny
    AppleDouble metadata stubs that sit next to the real audio. They carry an
    audio extension and often a title-like name, so without this they get
    matched as content, reported as thousands of duplicate audiobooks, and
    even moved into the library in place of the actual book.
    """
    name = path.name
    return name.startswith("._") or (name.startswith(".") and name not in (".", ".."))


def is_media_file(path: Path, exts: set[str], min_bytes: int = 0) -> bool:
    """A real, non-junk file of one of these types."""
    if not path.is_file() or is_junk(path):
        return False
    if path.suffix.lower() not in exts:
        return False
    if min_bytes:
        try:
            if path.stat().st_size < min_bytes:
                return False
        except OSError:
            return False
    return True


def sanitize_component(name: str, fallback: str = "Unknown") -> str:
    """Make a single path component safe, without mangling readable names."""
    cleaned = _ILLEGAL.sub("-", (name or "").strip())
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    return cleaned[:180] or fallback


def book_folder_name(author: str, title: str, year: int | None = None) -> str:
    """`Author - Title (Year)`, the convention the library already uses."""
    stem = f"{sanitize_component(author)} - {sanitize_component(title)}"
    if year:
        stem = f"{stem} ({year})"
    return stem[:200]


def free_space_gb(path: Path | str) -> float:
    target = Path(path)
    while not target.exists() and target != target.parent:
        target = target.parent
    usage = shutil.disk_usage(target)
    return usage.free / (1024 ** 3)


def _normalise(text: str) -> str:
    text = (text or "").lower()
    text = re.sub(r"\(.*?\)|\[.*?\]", " ", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _stripped(text: str) -> str:
    """Lowercased, with series and edition suffixes removed."""
    text = (text or "").lower()
    text = re.sub(r"\(.*?\)|\[.*?\]", " ", text)      # series / edition suffixes
    return text


def title_key(text: str) -> str:
    """A comparable form of a title, for cross-service matching.

    Services punctuate differently from Goodreads and from each other —
    BookLore stores "The MUQADDIMAH : An Introduction to History" with a space
    before the colon where Goodreads has none. Stripping punctuation and
    subtitle suffixes gives a form that compares equal.
    """
    text = re.sub(r"\s*[:–—-]\s.*$", "", _stripped(text))   # subtitles
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def title_keys(text: str) -> list[str]:
    """Every form of `text` worth comparing, longest first.

    `title_key` alone is not enough because it cannot tell a subtitle from an
    author. It strips from the first " - " onwards, and the library's own
    folder convention is `Author - Title` (`book_folder_name`), which the
    indexers then adopt as the name of the thing they hold: Kavita calls the
    Alamut folder the series "Bartol, Vladimir - Alamut", and BookLore stores
    "Wladimir Bartol, Atilla Dirim - Alamut". Reduced by `title_key` those
    become "bartol vladimir" — the author survives and the title, the one part
    being compared, is thrown away. Every such book verified as absent while
    sitting in all three services, which is how books this app placed itself
    became books it could not find.

    So the whole string is kept as a candidate form too. That is not a
    loosening: `titles_match` still requires the *entire* wanted title to
    appear word-bounded, so "less" does not match "21 Lessons for the 21st
    Century" any more than it did before — "lessons" has no boundary after
    "less". What it adds is that the wanted title is now allowed to be found
    past a dash, which is exactly where an author prefix leaves it.
    """
    full = re.sub(r"[^a-z0-9]+", " ", _stripped(text)).strip()
    short = title_key(text)
    return [form for form in dict.fromkeys((full, short)) if form]


_LEADING_ARTICLE = re.compile(r"^(?:a|an|the)\s+", re.IGNORECASE)


def _without_article(text: str) -> str:
    return _LEADING_ARTICLE.sub("", text or "")


def titles_match(candidate: str, want: str) -> bool:
    """Word-boundary title comparison, tolerant of a leading article.

    Services index the *embedded* metadata title, which routinely differs from
    Goodreads by an article: Goodreads holds "A Decline in Prophets (Rowland
    Sinclair #2)" while BookLore and Kavita both hold "Decline in Prophets".
    Requiring the article made every such book look absent, and 55 books sat
    failing verification while sitting perfectly well in every service.

    Still not a substring test: normalised "less" appears inside
    "21 lessons for the 21st century", which would let a genuinely absent book
    pass.

    Both sides are compared in every form `title_keys` produces, because a
    service is free to name a book after the `Author - Title` folder it found
    it in and `title_key` alone reads that author as the whole title. The
    word-boundary rule below is what keeps the extra form safe: the entire
    wanted title still has to appear as whole words.
    """
    lefts = title_keys(candidate)
    rights = title_keys(want)
    if not lefts or not rights:
        return False

    for left in lefts:
        for right in rights:
            for one, two in ((left, right),
                             (_without_article(left), _without_article(right))):
                if not one or not two:
                    continue
                if one == two:
                    return True
                if re.search(rf"\b{re.escape(two)}\b", one):
                    return True
    return False


_VOLUME = re.compile(r"#\s*(\d+)|book\s+(\d+)|vol(?:ume)?\.?\s*(\d+)", re.IGNORECASE)


def volume_number(text: str) -> str:
    """The series position in a title, if it states one.

    `(Oz, #4)` -> "4". This matters more than it looks: the series number is
    often the *only* thing distinguishing two books, so anything that discards
    it will happily treat "The Road to Oz (#5)" and "The Emerald City of Oz
    (#6)" as the same book.
    """
    for match in _VOLUME.finditer(text or ""):
        for group in match.groups():
            if group:
                return str(int(group))
    return ""


def _main_title(text: str) -> str:
    """The part before any subtitle or series parenthetical.

    "Sapiens: A Brief History of Humankind" -> "sapiens". Matching on the
    subtitle instead is how a different book by the same author — "Nexus: A
    Brief History of Information Networks" — scores as a match.
    """
    text = re.sub(r"\(.*?\)|\[.*?\]", " ", text or "")
    text = re.split(r"[:;]|\s+[-–—]\s+", text, maxsplit=1)[0]
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def _candidate_title(candidate: str) -> str:
    """The title portion of a `{Author} - {Title} ({Year})` filename.

    The author prefix has to come off first, or `_main_title` splits on the
    author's own dash and the "main title" becomes the author's name — which
    made every correct match look like a mismatch.
    """
    text = candidate or ""
    if " - " in text:
        text = text.split(" - ", 1)[1]
    return text


def match_score(candidate: str, title: str, author: str = "") -> float:
    """How well a downloaded filename/dirname matches a book. 0.0 - 1.0.

    Shelfmark renames downloads to `{Author} - {Title} ({Year})`, so both
    halves are present in the name and can be matched directly.

    Two guards exist because their absence put the *wrong file* into the
    library for six books: a differing series number is disqualifying, and the
    main title has to match — a shared subtitle is not enough.
    """
    hay = _normalise(candidate)
    if not hay:
        return 0.0
    want_title = _normalise(title)
    want_author = _normalise(author)

    # A stated series position that differs means this is a different book in
    # the same series. No amount of title similarity compensates.
    want_vol = volume_number(title)
    got_vol = volume_number(candidate)
    if want_vol and got_vol and want_vol != got_vol:
        return 0.0

    score = 0.0
    if want_title:
        if want_title == hay:
            score += 0.7
        elif want_title in hay:
            score += 0.55
        else:
            # Fall back to token overlap so a subtitle or series suffix does
            # not sink an otherwise correct match.
            tokens = [t for t in want_title.split() if len(t) > 2]
            if tokens:
                hit = sum(1 for t in tokens if t in hay) / len(tokens)
                score += 0.55 * hit

    # Matching only on the subtitle is not a match. "Nexus: A Brief History of
    # Information Networks" shares "A Brief History" with "Sapiens: A Brief
    # History of Humankind", and that was enough to file the wrong book.
    if score > 0:
        want_main = _main_title(title)
        got_main = _main_title(_candidate_title(candidate))
        if want_main and got_main and want_main != got_main and \
                want_main not in got_main and got_main not in want_main:
            score *= 0.4

    if want_author:
        surname = want_author.split()[-1] if want_author.split() else ""
        if want_author in hay:
            score += 0.3
        elif surname and len(surname) > 2 and surname in hay:
            score += 0.18
    return min(score, 1.0)


def find_matching_entries(
    root: Path, title: str, author: str = "", min_score: float = 0.55,
    since: float | None = None,
) -> list[tuple[float, Path]]:
    """Entries directly under `root` matching a book, best first.

    Single pass, one level deep plus one level into likely book folders —
    Shelfmark may leave either a bare file or a folder.
    """
    if not root.exists():
        return []

    candidates: list[Path] = []
    for entry in root.iterdir():
        if is_junk(entry):
            continue
        if since is not None:
            try:
                if entry.stat().st_mtime < since:
                    continue
            except OSError:
                continue
        candidates.append(entry)
        if entry.is_dir():
            # Go one level in: an ebook may sit inside a folder Shelfmark
            # created, and an audiobook is a directory of files rather than a
            # single file. Both files and directories are candidates — the
            # scorer decides.
            for child in entry.iterdir():
                if is_junk(child):
                    continue
                if child.is_file() and child.suffix.lower() not in (EBOOK_EXTS | AUDIO_EXTS):
                    continue
                candidates.append(child)

    scored = [(match_score(p.name, title, author), p) for p in candidates]
    good = [(s, p) for s, p in scored if s >= min_score]
    good.sort(key=lambda item: (-item[0], -item[1].stat().st_mtime))
    return good


def has_media(path: Path, exts: set[str]) -> bool:
    if path.is_file():
        return is_media_file(path, exts)
    return any(
        is_media_file(child, exts) for child in path.rglob("*")
    )


#: What a category or author folder created for the library should end up as.
#:
#: The process runs under `umask 0o077` (see `app/permissions.py`) so that
#: everything written into the data volume is owner-only without anyone having
#: to remember. The media library is the opposite case: it exists to be read by
#: other applications, which are separate containers with their own uid, so a
#: directory made owner-only would make every book beneath it unindexable —
#: and that failure is silent, because the app itself can still read its own
#: tree. This is the mode such a directory had before the umask changed
#: (`0o777 & ~0o022`), so nothing about the library's accessibility moves.
LIBRARY_DIR_MODE = 0o755


def makedirs_for_library(path: Path) -> None:
    """`mkdir -p`, leaving every level it created reachable by the other apps.

    Every *missing* level is corrected, not just the leaf. `mkdir(parents=True)`
    creates the whole chain — `books_root/<Category>/<Author - Title (Year)>/` —
    and the levels above the destination are as load-bearing as the destination
    itself: a book in a readable folder inside an unreadable one is a book no
    other service can find.

    Levels that already existed are left exactly as they are, mode included,
    because a folder the operator set up deliberately is not this code's to
    change.
    """
    missing: list[Path] = []
    probe = path
    while not probe.exists():
        missing.append(probe)
        if probe.parent == probe:  # reached the root; nothing left to walk
            break
        probe = probe.parent

    path.mkdir(parents=True, exist_ok=True)

    for created in missing:
        try:
            os.chmod(created, LIBRARY_DIR_MODE)
        except OSError:
            # Best effort. The alternative — refusing to place the book — would
            # turn a permissions oddity into a stalled library.
            pass


def move_into_place(source: Path, destination: Path) -> Path:
    """Move `source` to `destination` with a same-filesystem rename.

    Returns the final path. Raises if the move would cross a device, because
    that would mean copying — the thing this design forbids.
    """
    makedirs_for_library(destination.parent)
    final = destination
    if final.exists():
        # Never clobber. If the same content is already there we are done;
        # otherwise suffix rather than overwrite someone's book.
        if source.is_file() and final.is_file() and _same_file(source, final):
            source.unlink()
            return final
        final = _dedupe(destination)

    try:
        os.rename(source, final)
    except OSError as exc:
        if exc.errno == 18:  # EXDEV
            raise RuntimeError(
                f"Refusing to move {source} to {final}: different filesystems, "
                "which would copy the file and create a duplicate. Check that "
                "the staging dir and the category folders share a mount."
            ) from exc
        raise
    return final


def _dedupe(path: Path) -> Path:
    stem, suffix, parent = path.stem, path.suffix, path.parent
    for n in range(2, 100):
        candidate = parent / f"{stem} ({n}){suffix}"
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"Could not find a free name near {path}")


def _same_file(a: Path, b: Path) -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def relative_to_library(path: Path, library_root: Path) -> str:
    try:
        return str(path.relative_to(library_root))
    except ValueError:
        return str(path)
