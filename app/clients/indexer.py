"""Shared shape for the four apps that index the library.

Kavita, BookLore, Grimmory and Audiobookshelf all expose "list my libraries"
and "rescan library N", but disagree on the JSON. Normalising them here keeps
the `index` stage from growing four special cases.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import PurePosixPath


@dataclass
class Library:
    id: str
    name: str
    folders: list[str] = field(default_factory=list)

    def contains(self, container_path: str) -> bool:
        """True if `container_path` sits inside one of this library's folders."""
        target = PurePosixPath(container_path)
        for folder in self.folders:
            try:
                target.relative_to(PurePosixPath(folder))
                return True
            except ValueError:
                continue
        return False

    def depth_of_match(self, container_path: str) -> int:
        """How specific the match is — the longest folder wins."""
        target = PurePosixPath(container_path)
        best = -1
        for folder in self.folders:
            try:
                target.relative_to(PurePosixPath(folder))
                best = max(best, len(PurePosixPath(folder).parts))
            except ValueError:
                continue
        return best


def extract_folders(raw: dict) -> list[str]:
    """Pull folder paths out of a library object, whatever it calls them.

    The apps disagree: Kavita sends `folders: ["/books/Space"]`, Abs sends
    `folders: [{"fullPath": "..."}]`, the BookLore family uses one of several
    keys depending on version. Rather than guess per-version, accept all of
    them and flatten.
    """
    for key in ("folders", "paths", "libraryPaths", "folderPath", "path", "directories"):
        value = raw.get(key)
        if not value:
            continue
        if isinstance(value, str):
            return [value]
        out: list[str] = []
        for item in value:
            if isinstance(item, str):
                out.append(item)
            elif isinstance(item, dict):
                for sub in ("fullPath", "path", "folderPath", "libraryPath"):
                    if item.get(sub):
                        out.append(str(item[sub]))
                        break
        if out:
            return out
    return []


def pick_library(libraries: list[Library], container_path: str) -> Library | None:
    """The most specific library containing `container_path`, if any.

    Longest-folder-wins matters because Kavita currently has both a
    `/books/newDownloads` library and per-category ones; a staged file must
    resolve to the staging library, not to `/books`.
    """
    candidates = [lib for lib in libraries if lib.contains(container_path)]
    if not candidates:
        return None
    return max(candidates, key=lambda lib: lib.depth_of_match(container_path))


def find_by_name(libraries: list[Library], name: str) -> Library | None:
    """Exact, then case-insensitive, then substring — in that order."""
    for lib in libraries:
        if lib.name == name:
            return lib
    lowered = name.lower()
    for lib in libraries:
        if lib.name.lower() == lowered:
            return lib
    for lib in libraries:
        if lowered in lib.name.lower():
            return lib
    return None
