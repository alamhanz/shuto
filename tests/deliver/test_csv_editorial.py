"""The CSV never carries an editorial row without a valid caption."""

from __future__ import annotations

import csv
import io

import pytest

from shuto.deliver.csv_export import EditorialCaptionError, build_csv

GOOD = "SHIBUYA, TOKYO, JAPAN - OCTOBER 5, 2026: Commuters wait on the platform at the station."


def row(description, editorial=True, name="a-001.jpg"):
    return {
        "filename": name,
        "description": description,
        "keywords": ["japan"],
        "categories": ["Travel"],
        "editorial": editorial,
    }


def cells(out):
    return list(csv.reader(io.StringIO(out)))[1:]


def test_a_good_caption_is_written_as_it_is():
    (got,) = cells(build_csv([row(GOOD)]))
    assert got[1] == GOOD and got[4] == "yes"


def test_a_fixable_caption_is_fixed():
    messy = (
        "Shibuya, Tokyo, Japan – October 5th 2026: commuters wait on the platform at the station."
    )
    (got,) = cells(build_csv([row(messy)]))
    assert got[1] == GOOD


@pytest.mark.parametrize(
    "bad",
    [
        "A quiet residential street in Tokyo with parked bicycles",
        "UNIDENTIFIED, UNIDENTIFIED - OCTOBER 5, 2026: Commuters wait on the platform.",
        "SHIBUYA, JAPAN - UNKNOWN: Commuters wait on the platform at the station.",
        "SHIBUYA, JAPAN - OCTOBER 5, 2999: Commuters wait on the platform at the station.",
        "",
    ],
)
def test_a_bad_caption_raises_and_names_the_photo(bad):
    with pytest.raises(EditorialCaptionError) as ei:
        build_csv([row(GOOD, name="ok.jpg"), row(bad, name="bad-002.jpg")])
    assert "bad-002.jpg" in str(ei.value) and "CITY, PREFECTURE, COUNTRY" in str(ei.value)
    assert isinstance(ei.value, ValueError)


def test_commercial_rows_are_left_alone():
    plain = "A quiet residential street in Tokyo with parked bicycles"
    (got,) = cells(build_csv([row(plain, editorial=False)]))
    assert got[1] == plain and got[4] == "no"


def test_other_languages_keep_their_sentence_with_an_english_prefix():
    jp = "TOKYO, JAPAN - OCTOBER 5, 2026: 自転車が並ぶ静かな住宅街"
    (got,) = cells(build_csv([row(jp)], strict=False))
    assert got[1] == jp
