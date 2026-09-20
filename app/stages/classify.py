"""classify — decide the single category a book belongs to.

The book's genres come from Goodreads, ordered by popularity. We scan them in
that order and take the first one that matches a rule, which is the
"highest-rated genre wins, exactly one category" behaviour that was asked for.

Matching prefers the longest rule: for the genre "science fiction", both
"science fiction" and "fiction" would substring-match, and the longer one is
obviously the right answer. Sorting by length rather than relying on YAML
order means adding a rule can never silently change an unrelated book.
"""

from __future__ import annotations

import json

from .. import models
from .. import genres as genres_lookup
from ..config import settings
from ..db import db


def resolve_category(genres: list[str], rules: dict) -> tuple[str, bool]:
    """Return (category_key, needs_review)."""
    genre_rules: dict = rules.get("genres") or {}
    fallback = str(rules.get("fallback") or "Fiction")

    # Longest needle first, so the most specific rule always wins.
    ordered = sorted(genre_rules.items(), key=lambda kv: len(kv[0]), reverse=True)

    for genre in genres:
        needle_hay = (genre or "").lower()
        for needle, category in ordered:
            if needle.lower() in needle_hay:
                return str(category), False

    return fallback, True


def resolve_from_title(title: str, rules: dict) -> str | None:
    """Last-resort match against the title itself.

    Only reached when every genre source came back empty. It exists because
    some books simply have no genre metadata anywhere — "C++ Programming" and
    "Software Architecture with Kotlin" are unmistakably Technical from their
    titles alone. A title match is a genuine match against a rule you wrote,
    so it is not flagged for review, but it is recorded with the source
    `title` so you can tell an inferred category from a sourced one.
    """
    genre_rules: dict = rules.get("genres") or {}
    haystack = (title or "").lower()
    if not haystack:
        return None
    for needle, category in sorted(genre_rules.items(), key=lambda kv: len(kv[0]), reverse=True):
        if needle.lower() in haystack:
            return str(category)
    return None


def run(book: dict) -> models.StageResult:
    book_id = book["id"]
    genres = list(book.get("genres") or [])
    source = book.get("genre_source") or ""

    # Try the whole provider chain before giving up. At this point the book is
    # usually not downloaded yet, so the embedded-metadata source has nothing
    # to read; that is why `place` retries this once the file exists.
    tried: list[str] = []
    if not genres:
        found = genres_lookup.lookup(book)
        tried = found.tried
        if found.found:
            genres = found.genres
            source = found.source
            db().set_genres(book_id, json.dumps(genres), source)

    rules = settings.load_categories()
    category, needs_review = resolve_category(genres, rules)

    if needs_review:
        from_title = resolve_from_title(str(book.get("title") or ""), rules)
        if from_title:
            db().set_genres(book_id, json.dumps([str(book.get("title"))]), "title")
            db().set_category(book_id, from_title, needs_review=False)
            return models.StageResult.ok(
                f"no genres from any source; matched the title -> {from_title}"
            )

    db().set_category(book_id, category, needs_review=needs_review)

    if needs_review:
        return models.StageResult.ok(
            f"no genre matched for {genres[:3] or '(none found)'} — fell back to "
            f"{category}. Sources tried: {', '.join(tried) or 'n/a'}"
        )
    return models.StageResult.ok(f"[{source or 'stored'}] {genres[:4]} -> {category}")


def reclassify_with_file(book: dict, file_path: str) -> tuple[str, str]:
    """Second chance at classification once the book is on disk.

    The embedded `dc:subject` source needs the actual epub, which does not
    exist while `classify` first runs. So `place` calls this afterwards. It
    returns (category, note); the caller decides whether the move it already
    made needs redoing.
    """
    if book.get("category") and not book.get("needs_review"):
        return book["category"], "already classified"

    found = genres_lookup.lookup(book, file_path=file_path)
    if not found.found:
        return book.get("category") or "", f"no genres; tried {', '.join(found.tried)}"

    rules = settings.load_categories()
    category, needs_review = resolve_category(found.genres, rules)
    db().set_genres(book["id"], json.dumps(found.genres), found.source)
    db().set_category(book["id"], category, needs_review=needs_review)
    if category != (book.get("category") or ""):
        return category, f"[{found.source}] {found.genres[:3]} -> {category} (changed)"
    return category, f"[{found.source}] {found.genres[:3]} -> {category}"


def destination_for(category: str) -> tuple[str, str]:
    """(folder, notebook) for a category key."""
    rules = settings.load_categories()
    entry = (rules.get("categories") or {}).get(category) or {}
    return str(entry.get("folder") or category), str(entry.get("notebook") or "")
