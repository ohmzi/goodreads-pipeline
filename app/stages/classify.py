"""classify — decide the single category a book belongs to.

The book's genres come from Goodreads, ordered by popularity. We scan them in
that order and take the first one that matches a rule, which is the
"highest-rated genre wins, exactly one category" behaviour that was asked for.

Matching prefers the longest rule: for a genre that hits two needles, the
longer one wins. Sorting by length rather than relying on YAML order means
adding a rule can never silently change an unrelated book.

The exception is the `precedence` block in categories.yml, which is consulted
before any needle and wins outright. It exists for the one case where
"longest wins" gives the wrong answer: a historical novel is tagged
["Historical Fiction", ...], and "historical fiction" being longer than
"fiction" filed it under History. Anything that *names* fiction is fiction
here, whatever a longer rule would otherwise say.

Negation is handled in the same block and is the reason matching is not a
plain substring test: `"fiction" in "nonfiction"` is true, and that alone put
every nonfiction book into the Fiction folder. A `never` phrase is masked out
of the genre before anything — this block or the needle scan — looks at it.
"""

from __future__ import annotations

import json
import re

from .. import models
from .. import genres as genres_lookup
from ..config import settings
from ..db import db


def _masked(text: str, precedence: list) -> str:
    """`text` with every `never` phrase taken out of it.

    Replaced with a space rather than deleted, so removing a phrase cannot
    fuse the words on either side into a new one. A genre with nothing to mask
    comes back unchanged, which is what keeps every other rule matching
    exactly what it matched before.
    """
    for entry in precedence:
        for phrase in entry.get("never") or []:
            text = text.replace(str(phrase).lower(), " ")
    return text


def _claimed_by(text: str, precedence: list) -> str:
    """The category a precedence rule claims `text` for, or "".

    Whole words only: `\\bfiction\\b` matches "fiction" and "historical
    fiction" but not "nonfiction", where there is no boundary before the word.
    """
    for entry in precedence:
        category = str(entry.get("category") or "")
        if not category:
            continue                      # a rule with no destination claims nothing
        for word in entry.get("match") or []:
            if re.search(rf"\b{re.escape(str(word).lower())}\b", text):
                return category
    return ""


def resolve_category(genres: list[str], rules: dict) -> tuple[str, bool]:
    """Return (category_key, needs_review)."""
    genre_rules: dict = rules.get("genres") or {}
    precedence: list = rules.get("precedence") or []
    fallback = str(rules.get("fallback") or "Fiction")

    # Longest needle first, so the most specific rule always wins.
    ordered = sorted(genre_rules.items(), key=lambda kv: len(kv[0]), reverse=True)

    masked = [_masked((genre or "").lower(), precedence) for genre in genres]

    # A precedence rule outranks the scan below, and is checked across the
    # WHOLE genre list rather than one genre at a time. The rule is about what
    # a book carries, not about where a genre happens to sit in the list:
    # Goodreads tags The Night Circus ["Fantasy", "Fiction", "Romance"], and a
    # book carrying "Fiction" anywhere is Fiction — Fantasy is listed first and
    # would otherwise take it.
    #
    # This is deliberately the more destructive of the two readings, and it was
    # chosen with the cost measured and accepted: 61 of the 62 Fantasy books
    # carry that bare "Fiction" tag, so /books/Fantasy stops filling, and the
    # SciFi rules below become unreachable through genres entirely. They are
    # left in place, still correct for a book whose genres never say fiction.
    for hay in masked:
        claimed = _claimed_by(hay, precedence)
        if claimed:
            return claimed, False

    for needle_hay in masked:
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
    precedence: list = rules.get("precedence") or []
    haystack = _masked((title or "").lower(), precedence)
    if not haystack.strip():
        return None

    # Same precedence as the genre path, and the same negation exclusion: a
    # title is matched against the rules you wrote, so it has to answer the
    # same way. Note this reads titles that the genre scan would have read
    # differently — "A Brief History of Fiction" now resolves Fiction, where
    # the equal-length "history"/"fiction" tie used to hand it to History.
    claimed = _claimed_by(haystack, precedence)
    if claimed:
        return claimed

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
