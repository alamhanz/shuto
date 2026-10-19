import csv
import io

from shuto.deliver.csv_export import HEADERS, build_csv


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
        "yes",
        "no",
        "no",
    ]


def test_header_matches_shutterstocks_example_exactly():
    header = "Filename,Description,Keywords,Categories,Editorial,Mature content,illustration"
    assert ",".join(HEADERS) == header
    assert build_csv([]) == header + "\n"


def test_editorial_and_commercial_rows_share_one_file():
    out = build_csv(
        [
            {"filename": "a.jpg", "description": "d", "keywords": ["k"], "categories": []},
            {
                "filename": "b.jpg",
                "description": "d",
                "keywords": ["k, with comma"],
                "categories": [],
                "editorial": True,
                "mature": True,
                "illustration": True,
            },
        ]
    )
    rows = list(csv.reader(io.StringIO(out)))
    assert [r[4:] for r in rows[1:]] == [["no", "no", "no"], ["yes", "yes", "yes"]]
    assert rows[2][2] == "k, with comma"
    assert '"k, with comma"' in out
