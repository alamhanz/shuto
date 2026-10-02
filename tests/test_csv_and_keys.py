import csv
import io

from shuto.csv_export import HEADERS, build_csv
from shuto.handlers import parse_key


def test_build_csv_quotes_and_flags():
    out = build_csv(
        [
            {
                "filename": "IMG_0001.jpg",
                "description": "Mount Fuji at sunrise, Japan",
                "keywords": ["mount fuji", "sunrise", "japan"],
                "categories": ["Nature", "Parks/Outdoor"],
                "editorial": True,
            }
        ]
    )
    rows = list(csv.reader(io.StringIO(out)))
    assert rows[0] == HEADERS
    assert rows[1] == [
        "IMG_0001.jpg",
        "Mount Fuji at sunrise, Japan",
        "mount fuji,sunrise,japan",
        "Nature,Parks/Outdoor",
        "no",
        "no",
        "yes",
    ]


def test_parse_key():
    assert parse_key("incoming/2026-W40/IMG_1.JPG") == ("2026-W40", "IMG_1.JPG")
    assert parse_key("incoming/2026-W40/notes.txt") is None
    assert parse_key("incoming/IMG_1.jpg") is None
    assert parse_key("incoming/a/b/IMG_1.jpg") is None
    assert parse_key("metadata/2026-W40/IMG_1.jpg") is None
