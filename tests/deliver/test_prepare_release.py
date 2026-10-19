"""prepare.release_check: the caption rules shared with the CSV, and the release warning."""

from __future__ import annotations

from datetime import date

from shuto.deliver import prepare

CAPTION = "SHIBUYA, TOKYO, JAPAN - OCTOBER 5, 2026: Commuters wait on the platform at the station."
META = {"description": CAPTION, "keywords": ["japan"], "editorial": True}


def test_a_valid_caption_passes_and_the_date_must_match_the_capture_date():
    assert prepare.release_check(META, {}, []) == []
    assert prepare.release_check(META, {}, [], taken=date(2026, 10, 5)) == []
    wrong = prepare.release_check(META, {}, [], taken=date(2026, 10, 6))
    assert wrong and "capture date" in wrong[0]


def test_problems_use_the_shared_wording():
    bad = {**META, "description": "SHIBUYA, JAPAN - UNKNOWN: Commuters wait at the station."}
    (problem,) = prepare.release_check(bad, {}, [])
    assert "CITY, PREFECTURE, COUNTRY - MONTH D, YYYY: Sentence." in problem
    assert "UNKNOWN" in problem


def test_releases_on_an_editorial_photo_warn_but_do_not_block():
    notes: list[str] = []
    assert prepare.release_check(META, {}, ["m-1"], warnings=notes) == []
    assert len(notes) == 1 and "does not accept releases on editorial" in notes[0]
    assert "do not attach" in notes[0]


def test_no_warning_without_releases_or_for_commercial_photos():
    notes: list[str] = []
    prepare.release_check(META, {}, [], warnings=notes)
    commercial = {**META, "editorial": False, "description": "A quiet street at dusk in Tokyo"}
    prepare.release_check(commercial, {}, ["m-1"], warnings=notes)
    assert notes == []
    prepare.release_check(META, {}, ["m-1"])  # no out-param: nothing to fill, no error
