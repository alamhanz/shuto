"""Build the Shutterstock bulk-metadata CSV.

Shutterstock ignores CSVs sent over FTPS: upload the generated CSV with the
"CSV" button on the Submit page after the photos appear in your queue.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Iterable

HEADERS = [
    "Filename",
    "Description",
    "Keywords",
    "Categories",
    "Illustration",
    "Mature Content",
    "Editorial",
]


def _yn(v) -> str:
    return "yes" if bool(v) else "no"


def build_csv(items: Iterable[dict]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf, quoting=csv.QUOTE_MINIMAL, lineterminator="\n")
    writer.writerow(HEADERS)
    for it in items:
        writer.writerow(
            [
                it["filename"],
                it["description"],
                ",".join(it["keywords"]),
                ",".join(it["categories"]),
                _yn(it.get("illustration")),
                _yn(it.get("mature")),
                _yn(it.get("editorial")),
            ]
        )
    return buf.getvalue()
