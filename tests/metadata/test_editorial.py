"""Editorial captions: build, parse, fix and check (pure, no model, no store)."""

from __future__ import annotations

from datetime import date

import pytest

from shuto.metadata import editorial as ed

TODAY = date(2026, 10, 20)
SENTENCE = "Commuters wait on the platform at Shinjuku Station"
GOOD = f"SHIBUYA, TOKYO, JAPAN - OCTOBER 5, 2026: {SENTENCE}."


def test_build_with_and_without_prefecture():
    assert ed.build_caption("Shibuya", "Japan", date(2026, 10, 5), "x y z.", "Tokyo") == (
        "SHIBUYA, TOKYO, JAPAN - OCTOBER 5, 2026: X y z."
    )
    assert ed.build_caption("Paris", "France", date(2026, 1, 9), "a b c") == (
        "PARIS, FRANCE - JANUARY 9, 2026: A b c"
    )


def test_build_marks_missing_parts_unknown_and_fails_the_check():
    text = ed.build_caption("", "", None, SENTENCE)
    assert text.startswith("UNKNOWN, UNKNOWN - UNKNOWN: ")
    assert ed.caption_problems(text)
    assert not ed.is_editorial_caption(text)


def test_parse_parts():
    p = ed.parse_caption(GOOD)
    assert p is not None
    assert (p.city, p.region, p.country) == ("SHIBUYA", "TOKYO", "JAPAN")
    assert p.date == date(2026, 10, 5) and p.year_only is None
    assert p.sentence.startswith("Commuters")
    two = ed.parse_caption("PARIS, FRANCE - MAY 1, 2026: A crowd gathers by the river.")
    assert two is not None and two.region == "" and two.city == "PARIS"
    old = ed.parse_caption("PARIS, FRANCE - 1998: A crowd gathers by the river.")
    assert old is not None and old.year_only == 1998 and old.date is None


@pytest.mark.parametrize(
    "text",
    [
        "",
        "Shibuya, Japan - OCTOBER 5, 2026: Crowd.",
        "SHIBUYA, JAPAN - Oct 5 2026: Crowd.",
        "SHIBUYA, JAPAN - UNKNOWN: Crowd gathers here.",
        "SHIBUYA, JAPAN - FEBRUARY 30, 2026: Crowd gathers here.",
        "SHIBUYA, JAPAN - OKTOBER 5, 2026: Crowd gathers here.",
        "SHIBUYA, JAPAN - OCTOBER 5, 2026:",
        "SHIBUYA - OCTOBER 5, 2026: Crowd gathers here.",
        "A quiet street with a parked bicycle at dusk",
    ],
)
def test_parse_refuses_malformed(text):
    assert ed.parse_caption(text) is None
    assert ed.caption_problems(text)


@pytest.mark.parametrize(
    "raw,fixed",
    [
        (
            "Shibuya, Tokyo, Japan – July 31 2026: crowds cross the road at night",
            "SHIBUYA, TOKYO, JAPAN - JULY 31, 2026: Crowds cross the road at night",
        ),
        (
            "shibuya, japan — July 31st 2026 : crowds cross",
            "SHIBUYA, JAPAN - JULY 31, 2026: Crowds cross",
        ),
        (
            "PUERTO VALLARTA, MEXICO - January 28th 2020 - Photo of folklore dancers",
            "PUERTO VALLARTA, MEXICO - JANUARY 28, 2020: Photo of folklore dancers",
        ),
        (
            "LONDON, UK - 27 April 2019:fish and chips van",
            "LONDON, UK - APRIL 27, 2019: Fish and chips van",
        ),
        ("PARIS, FRANCE - 1998: crowd by the river", "PARIS, FRANCE - 1998: Crowd by the river"),
    ],
)
def test_fix_repairs_small_slips(raw, fixed):
    assert ed.fix_caption(raw) == fixed


def test_fix_leaves_everything_else_alone():
    assert ed.fix_caption(GOOD) == GOOD
    assert ed.fix_caption("A quiet street with a parked bicycle") == (
        "A quiet street with a parked bicycle"
    )
    assert ed.fix_caption("TOKYO, JAPAN - UNKNOWN: Crowd") == "TOKYO, JAPAN - UNKNOWN: Crowd"


def test_good_caption_has_no_problems():
    assert ed.caption_problems(GOOD, date(2026, 10, 5), TODAY) == []
    assert ed.is_editorial_caption(GOOD)


@pytest.mark.parametrize(
    "text,taken,needle",
    [
        ("UNIDENTIFIED, UNIDENTIFIED - OCTOBER 5, 2026: Crowd gathers here.", None, "unknown"),
        ("TOKYO, UNKNOWN - OCTOBER 5, 2026: Crowd gathers here.", None, "country is unknown"),
        ("JAPAN, JAPAN - OCTOBER 5, 2026: Crowd gathers here.", None, "repeats the country"),
        ("TOKYO, JAPAN - OCTOBER 5, 2027: Crowd gathers here.", None, "future"),
        ("TOKYO, JAPAN - OCTOBER 5, 2026: Crowd gathers here.", date(2026, 10, 6), "differs"),
        ("TOKYO, JAPAN - 2005: Crowd gathers here.", None, "before 2000"),
        ("TOKYO, JAPAN - 1998: Crowd gathers here.", date(1999, 1, 1), "year differs"),
        ("TOKYO, JAPAN - OCTOBER 5, 2026: Crowds.", None, "at least 3 words"),
        (
            "TOKYO, JAPAN - OCTOBER 5, 2026: TOKYO, JAPAN - OCTOBER 5, 2026: Crowd here.",
            None,
            "repeats",
        ),
        ("TOKYO, JAPAN - OCTOBER 5, 2026: crowd gathers here.", None, "capital"),
    ],
)
def test_problems_table(text, taken, needle):
    found = " | ".join(ed.caption_problems(text, taken, TODAY))
    assert needle in found.lower()


def test_city_states_may_repeat_the_country_and_old_years_may_stand_alone():
    assert ed.caption_problems("SINGAPORE, SINGAPORE - MAY 1, 2026: Crowd gathers here.") == []
    assert ed.caption_problems("TOKYO, JAPAN - 1998: Crowd gathers here.", None, TODAY) == []


def test_sentence_problems_skip_the_word_count_on_request():
    assert ed.sentence_problems("Crowds", min_words=0) == []
    assert ed.sentence_problems("Crowds")
    assert ed.sentence_problems("October 5, 2026 crowds gather here")  # repeats the date


def test_japanese_sentence_is_not_flagged_for_words_or_capitals():
    assert ed.caption_problems("TOKYO, JAPAN - OCTOBER 5, 2026: 自転車が並ぶ静かな住宅街") == []


def test_caption_sentence_of_reads_broken_prefixes():
    assert ed.caption_sentence_of(GOOD) == SENTENCE + "."
    assert ed.caption_sentence_of("TOKYO, JAPAN - UNKNOWN: Crowd here") == "Crowd here"
    assert ed.caption_sentence_of("A quiet street, Tokyo - at dusk") is None


def test_moved_names_are_still_importable():
    from shuto.ai.prompts import MONTHS
    from shuto.metadata.model import is_editorial_caption

    assert MONTHS[0] == "JANUARY" and len(MONTHS) == 12
    assert is_editorial_caption(GOOD)
