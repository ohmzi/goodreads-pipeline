"""Every `data` acquire failure for one stage is one row, not one per book.

`_cause_bucket` already collapsed "Shelfmark reported STATUS: REASON — after N
different releases" into a single "Shelfmark failed to download these" row.
It did not catch the sibling message `_pick` writes on a later sweep — "tried
all N candidate release(s) for 'TITLE' and every download failed" — because
that check matched on `"shelfmark reported" in lowered`, and the sibling says
something else. The book's own title sits inside the first 60 characters used
as the fallback bucket key, so every book fell through to its own group: 69 of
them, on the operator's own backlog, the exact "one group per book" failure
this function's docstring says it exists to prevent.

The fix has two parts, tested separately: the specific pattern gets the
sibling message folded in, and a stage-level fallback catches anything neither
pattern recognises — including wording no version of this code even produces
any more, which was also sitting in that backlog as stale data.
"""

from __future__ import annotations

from app import models

from app.main import _actionable, _cause_bucket


def test_the_later_sweep_message_joins_the_same_bucket():
    a = _cause_bucket("acquire_audiobook", "data",
                      "Shelfmark reported error: Download not found — after 4 different releases")
    b = _cause_bucket("acquire_audiobook", "data",
                      "tried all 4 candidate release(s) for 'War and Peace' and every download failed")

    assert a == b
    assert a == ("shelfmark-error", "Shelfmark failed to download these")


def test_different_titles_do_not_split_the_bucket():
    bucket_a, _ = _cause_bucket(
        "acquire_audiobook", "data",
        "tried all 2 candidate release(s) for 'A Little Life' and every download failed")
    bucket_b, _ = _cause_bucket(
        "acquire_audiobook", "data",
        "tried all 3 candidate release(s) for 'War and Peace' and every download failed")

    assert bucket_a == bucket_b


def test_an_unrecognised_data_message_still_collapses_by_stage():
    """The safety net: wording this code has never produced, or no longer
    does, still lands in one row per stage rather than one per book."""
    bucket_a, headline_a = _cause_bucket(
        "acquire_audiobook", "data",
        "found 1 release(s) for 'Love in Lowercase' but every one was the "
        "wrong format — not a book. E.g. Francesc Miralles - Love in Lowercase")
    bucket_b, headline_b = _cause_bucket(
        "acquire_audiobook", "data",
        "found 19 release(s) for 'Red, White & Royal Blue' but every one was "
        "the wrong format — not a book. E.g. Red White and Royal Blue 2023")

    assert bucket_a == bucket_b
    assert headline_a == headline_b
    assert "audiobook" in headline_a.lower()


def test_ebook_and_audiobook_do_not_share_the_fallback_bucket():
    """A stage's own failures stay a stage's own — an ebook problem is not the
    same fact as an audiobook problem, even when both fall through to the net."""
    ebook, _ = _cause_bucket("acquire_ebook", "data", "some future wording, ebook")
    audio, _ = _cause_bucket("acquire_audiobook", "data", "some future wording, audio")

    assert ebook != audio


def test_the_named_facts_are_not_swallowed_by_the_new_net():
    """`no-audiobook` / `no-ebook` keep their own dedicated, non-actionable
    headline — the net only catches what falls through both specific checks."""
    bucket, headline = _cause_bucket(
        "acquire_audiobook", "data",
        "no audiobook releases found for 'Some Book' — the 3 release(s) the "
        "search returned were not audiobooks. E.g. Some Movie 2019 1080p")

    assert bucket == "no-audiobook"
    assert not _actionable(kind="data", bucket=bucket)


def test_the_fallback_bucket_is_actionable():
    """Unlike `no-audiobook`, this is Shelfmark failing on real candidates —
    the same class of fact `shelfmark-error` already treats as worth a look."""
    bucket, _ = _cause_bucket("acquire_ebook", "data", "some unrecognised wording")

    assert _actionable(kind="data", bucket=bucket) is True


def test_a_non_data_kind_on_these_stages_does_not_hit_the_net():
    """The net is keyed on `kind == "data"` specifically — an auth or network
    failure on an acquire stage must keep its own, more useful bucket."""
    bucket, _ = _cause_bucket("acquire_ebook", "auth", "credentials rejected")

    assert not bucket.startswith("acquire-data:")
