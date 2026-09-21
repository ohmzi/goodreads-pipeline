"""Somebody's genre says fiction, so the book is fiction.

The reported misfiling: "Once Upon a River" carries ["Historical Fiction",
"Fiction", "Literary"], and the longest-needle scan filed it under History
because "historical fiction" (18 characters) is scanned before "fiction" (7).
The rules the library wants are the other way round — a genre that names
fiction decides, however specific a longer rule is — and the genres below are
exactly the ones that decision was made against.

The negation half matters just as much: `"fiction" in "nonfiction"` is true,
so the old substring test put every nonfiction book in the Fiction folder.
Both spellings are checked here.
"""

from __future__ import annotations

import pytest

from app.config import settings
from app.stages import classify


@pytest.fixture(scope="module")
def rules() -> dict:
    return settings.load_categories()


def without_precedence(rules: dict) -> dict:
    """The same taxonomy with the precedence block deleted.

    Used to prove the rule lives in the data and nowhere else: one deletion
    reverses the decision, and `classify.py` has no special case of its own.
    """
    return {key: value for key, value in rules.items() if key != "precedence"}


# --------------------------------------------------------------------------
# the genre lists this was decided on
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "genres, expected",
    [
        # the reported book: was History, must be Fiction
        (["Historical Fiction", "Fiction", "Literary"], "Fiction"),
        # a longer, more specific rule ("historical fiction") losing to the
        # word it is built out of — the precedence rule exists for this
        (["Historical Fiction"], "Fiction"),
        # accepted explicitly, and not quietly protected: SciFi stops filling
        (["Science Fiction", "Fiction"], "Fiction"),
        (["Science Fiction"], "Fiction"),
        (["Speculative Fiction"], "Fiction"),
        (["Literary Fiction"], "Fiction"),
        (["Fiction"], "Fiction"),
        (["Fanfiction"], "Fiction"),          # the substring rule still catches compounds
        # nonfiction is not fiction, in either spelling
        (["Nonfiction", "History", "Biography"], "History"),
        (["Non Fiction", "History"], "History"),
        (["Non-Fiction", "History"], "History"),
        (["Nonfiction", "Biography"], "History"),
        # THE COST, CHOSEN WITH IT MEASURED AND ACCEPTED. A book carrying
        # "Fiction" anywhere in its genre list is Fiction, even when a more
        # specific genre is listed first. Goodreads tags 59 of the 62 Fantasy
        # books on this shelf ["Fantasy", "Fiction", ...], so /books/Fantasy
        # stops filling. The operator was shown that number and chose this.
        (["Fantasy", "Fiction", "Romance"], "Fiction"),
        (["Manga", "Fiction"], "Fiction"),
        (["Kotlin", "Fiction"], "Fiction"),
        (["Islamic", "Fiction"], "Fiction"),
        # ...and a genre that never names fiction is untouched by the rule, so
        # the categories themselves still resolve when Goodreads says nothing
        # about fiction at all
        (["Fantasy"], "Fantasy"),
        (["Manga"], "Manga"),
        (["Computer Science"], "Technical"),
        (["Islamic"], "Islamic"),
        # SciFi rules that do not name fiction are still reachable
        (["Space Opera"], "SciFi"),
        (["Cyberpunk"], "SciFi"),
        (["Dystopia"], "SciFi"),
    ],
)
def test_genres_resolve_as_the_operator_asked(rules, genres, expected):
    category, needs_review = classify.resolve_category(genres, rules)
    assert category == expected
    assert needs_review is False


def test_the_reported_book_is_the_one_that_moved(rules):
    """The whole point, stated once as a sentence."""
    assert classify.resolve_category(["Historical Fiction", "Fiction"], rules)[0] == "Fiction"


# --------------------------------------------------------------------------
# nonfiction, both spellings
# --------------------------------------------------------------------------
def test_nonfiction_is_never_claimed_by_the_fiction_rule(rules):
    """The genre is a word, and the word is not fiction.

    Nothing claims it, so it falls through to the next genre — and where there
    is no next genre, to the fallback with the review flag set. What must not
    happen is the fiction rule claiming it, which is what the naive substring
    test on "fiction" does.
    """
    for genre in ("Nonfiction", "Non Fiction", "non-fiction", "NON FICTION"):
        category, needs_review = classify.resolve_category([genre, "History"], rules)
        assert category == "History", f"{genre!r} was claimed by something"


def test_a_lone_nonfiction_genre_falls_back_and_asks_for_review(rules):
    """No other genre, no rule that fits, no good answer.

    The fallback is Fiction and is not changed by any of this, so the book does
    land in the Fiction folder — but as an unmatched book flagged for review,
    not as one the rule read as fiction. That distinction is what the review
    pile is for.
    """
    category, needs_review = classify.resolve_category(["Nonfiction"], rules)
    assert category == rules["fallback"]
    assert needs_review is True

    # And the same genre no longer wins over a rule that does fit.
    assert classify.resolve_category(["Nonfiction", "History"], rules) == ("History", False)


def test_the_old_substring_match_is_what_put_nonfiction_in_fiction(rules):
    """Proof the negation is doing the work, not the word boundary.

    "Nonfiction" is one word, so a word-boundary test never matched it anyway —
    it was the plain substring needle in `genres:` that caught it. Masking the
    negated forms is what takes it out of reach of both.
    """
    masked = classify._masked("nonfiction", rules["precedence"])
    assert "fiction" in "nonfiction"          # what used to match
    assert "fiction" not in masked            # what matches now
    assert "fiction" not in classify._masked("non fiction", rules["precedence"])


# --------------------------------------------------------------------------
# precedence beats length, and is data rather than code
# --------------------------------------------------------------------------
def test_precedence_beats_a_longer_more_specific_rule(rules):
    """Length is what decides the needle scan; precedence outranks length."""
    longest = max((len(k) for k in rules["genres"] if "fiction" in k), default=0)
    assert longest > len("fiction"), "the scan would never have the chance to lose"
    assert classify.resolve_category(["Historical Fiction"], rules)[0] == "Fiction"


def test_the_rule_is_the_data_not_the_code(rules):
    """Delete the block from the taxonomy and the old answer comes back.

    This is what makes the decision reversible without a code change, and it is
    the only thing standing between these genres and the previous behaviour.
    """
    old = without_precedence(rules)
    assert old.get("precedence") is None

    assert classify.resolve_category(["Historical Fiction", "Fiction"], old) == ("History", False)
    assert classify.resolve_category(["Science Fiction", "Fiction"], old) == ("SciFi", False)
    # ...and the nonfiction bug the precedence block also fixes was there before
    assert classify.resolve_category(["Nonfiction", "History"], old) == ("Fiction", False)


def test_an_override_without_the_block_still_works(rules):
    """`/data/categories.yml` may predate this, and must not raise."""
    assert classify.resolve_category(["Fantasy"], without_precedence(rules)) == ("Fantasy", False)


def test_the_taxonomy_names_the_rule_for_what_it_does(rules):
    assert rules["precedence"][0]["category"] == "Fiction"
    assert "fiction" in rules["precedence"][0]["match"]
    assert "nonfiction" in rules["precedence"][0]["never"]


def test_a_malformed_rule_claims_nothing(rules):
    """A precedence entry with no category is not a wildcard."""
    broken = dict(rules, precedence=[{"match": ["fiction"], "never": []}])
    assert classify.resolve_category(["Historical Fiction"], broken) == ("History", False)


# --------------------------------------------------------------------------
# the title path, which shares the rule set
# --------------------------------------------------------------------------
def test_a_title_is_read_the_same_way_as_a_genre(rules):
    assert classify.resolve_from_title("A Brief History of Fiction", rules) == "Fiction"
    assert classify.resolve_from_title("Historical Fiction", rules) == "Fiction"
    assert classify.resolve_from_title("C++ Programming", rules) == "Technical"


def test_a_title_does_not_read_nonfiction_as_fiction(rules):
    assert classify.resolve_from_title("Nonfiction", rules) is None
    assert classify.resolve_from_title("Non Fiction", rules) is None
    assert classify.resolve_from_title("Once Upon a River", rules) is None


def test_the_title_path_is_unchanged_without_the_block(rules):
    old = without_precedence(rules)
    # The equal-length "history"/"fiction" tie used to hand this to History.
    assert classify.resolve_from_title("A Brief History of Fiction", old) == "History"
