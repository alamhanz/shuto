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

from shuto.metadata.rules import has_foreign_script

HEADERS = [
    "Filename",
    "Description",
    "Keywords",
    "Categories",
    "Editorial",
    "Mature content",
    "illustration",
]


class NotEnglishError(ValueError):
    """A CSV cell has non-Latin text. Shutterstock needs English; the file is not written."""


def _yn(v) -> str:
    return "yes" if bool(v) else "no"


def build_csv(items: Iterable[dict], strict: bool = True) -> str:
    """The Shutterstock CSV text. ``strict`` (English only) refuses non-Latin cells; with
    strict off (another metadata language) the cells are written as they are, in UTF-8."""
    buf = io.StringIO()
    writer = csv.writer(buf, quoting=csv.QUOTE_MINIMAL, lineterminator="\n")
    writer.writerow(HEADERS)
    for it in items:
        row = [
            it["filename"],
            it["description"],
            ",".join(it["keywords"]),
            ",".join(it["categories"]),
            _yn(it.get("editorial")),
            _yn(it.get("mature")),
            _yn(it.get("illustration")),
        ]
        if strict and any(has_foreign_script(cell) for cell in row[:4]):
            raise NotEnglishError(f"{it['filename']}: the text is not in English")
        writer.writerow(row)
    return buf.getvalue()
