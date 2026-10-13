"""Build the Shutterstock bulk-metadata CSV: one file per batch, in Shutterstock's format.

Shutterstock's Upload CSV dialog wants the column names exactly as in its own example file
(`Filename,Description,Keywords,Categories,Editorial,Mature content,illustration`, note the
lower-case "illustration"), so HEADERS copies that order and spelling. Editorial, Mature
content and illustration are yes/no per row, so commercial and editorial photos share one file.
Keywords and categories are comma-joined in one quoted cell. The file is UTF-8 without a BOM,
like Shutterstock's example.

Shutterstock ignores CSVs sent over FTPS: upload the generated CSV with the
"Upload CSV" button on the Portfolio -> Not submitted page after the photos appear there.
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
    "Editorial",
    "Mature content",
    "illustration",
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
                _yn(it.get("editorial")),
                _yn(it.get("mature")),
                _yn(it.get("illustration")),
            ]
        )
    return buf.getvalue()
