"""A book filed as `Author - Title` must still verify as present.

`place` names every folder with `book_folder_name`, which is
`{Author} - {Title} ({Year})`. Kavita takes its series name from that folder
when the file carries no embedded title, and BookLore and Grimmory store the
same string as the book's title. So the library is full of entries named after
this app's own convention.

`title_key` strips from the first " - " onwards to remove a subtitle, which on
those entries removes the title and keeps the author: "Bartol, Vladimir -
Alamut" reduced to "bartol vladimir". Every such book verified as missing from
all three services while sitting in all three, and after three forced rescans
each, parked as a failure for a human to read.

The other half of this file is the property that must survive the fix: a looser
match would let a genuinely absent book pass verification, which is worse than
the bug, because `verify` is the gate in front of the irreversible Goodreads
shelf move.
"""

from __future__ import annotations

import pytest

from app.pathing import title_keys, titles_match


#: Real strings, taken from the three services on the operator's host.
PRESENT = [
    ("Bartol, Vladimir - Alamut", "Alamut"),
    ("Wladimir Bartol, Atilla Dirim - Alamut", "Alamut"),
    (
        "Bishop, Christopher M (author);Jordan, M (editor) - "
        "Pattern Recognition and Machine Learning",
        "Pattern Recognition and Machine Learning",
    ),
    (
        "Freeman, Eric;Sierra, Kathy;Bates, Bert - Head First Design Patterns",
        "Head First Design Patterns",
    ),
    (
        "Streatfeild, Noel - Noel Streatfeild's Christmas Stories",
        "Noel Streatfeild's Christmas Stories",
    ),
    (
        "Barton, John J - Scientific and Engineering C++- An Introduction",
        "Scientific and Engineering C++: An Introduction",
    ),
]

#: The matches that already worked, kept here so the fix cannot trade one
#: class of false negative for another.
STILL_PRESENT = [
    ("Decline in Prophets", "A Decline in Prophets (Rowland Sinclair #2)"),
    ("The MUQADDIMAH : An Introduction to History", "The Muqaddimah: An Introduction"),
    ("Alamut", "Alamut"),
]

#: Different books that share a distinctive word. Kavita's search is literal
#: and returns these as candidates, so `titles_match` is the only thing
#: standing between them and a book being shelved as verified.
ABSENT = [
    ("21 Lessons for the 21st Century", "Less"),
    ("Tobacco-Stained Mountain Goat", "The Tobacco-Stained Sky"),
    ("Ben Claudius", "I, Claudius (Claudius, #1)"),
    ("Harry Potter: A History of Magic", "The Murder of History"),
    ("Shades of Magic", "The Magic of Oz (Oz, #13)"),
    ("Social Engineering", "Scientific and Engineering C++: An Introduction"),
    ("Copernicus on the Revolutions", "On The Revolutions of Heavenly Spheres"),
    # An author prefix must not become a wildcard: the title after the dash
    # still has to be the title being looked for.
    ("Bartol, Vladimir - Alamut", "The Sleeping Car Porter"),
]


@pytest.mark.parametrize("candidate,want", PRESENT + STILL_PRESENT)
def test_a_book_the_service_holds_is_found(candidate, want):
    assert titles_match(candidate, want) is True


@pytest.mark.parametrize("candidate,want", ABSENT)
def test_a_book_the_service_does_not_hold_is_not_found(candidate, want):
    assert titles_match(candidate, want) is False


def test_the_title_survives_an_author_prefix():
    """The specific reduction that lost the title, named.

    `title_key` on its own still answers "bartol vladimir" — that is its
    documented contract and other punctuation cases rely on it. What changed is
    that it is no longer the *only* form compared.
    """
    forms = title_keys("Bartol, Vladimir - Alamut")
    assert "bartol vladimir" in forms
    assert any("alamut" in form for form in forms)


def test_empty_input_matches_nothing():
    assert titles_match("", "Alamut") is False
    assert titles_match("Alamut", "") is False
