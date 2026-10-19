"""Build the Shutterstock bulk-metadata CSV: one file per batch, in Shutterstock's format.

Shutterstock's Upload CSV dialog wants the column names exactly as in its own example file
(`Filename,Description,Keywords,Categories,Editorial,Mature content,illustration`, note the
lower-case "illustration"), so HEADERS copies that order and spelling. Editorial, Mature
content and illustration are yes/no per row, so commercial and editorial photos share one file.
Keywords and categories are comma-joined in one quoted cell. The file is UTF-8 without a BOM,
like Shutterstock's example.

Shutterstock ignores CSVs sent over FTPS: upload the generated CSV with the
"Upload CSV" button on the Portfolio -> Submit content page (Not submitted tab) after the photos
appear there.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Iterable

from shuto.core.text import yes_no
from shuto.metadata.editorial import FORMAT_HINT, caption_problems, fix_caption
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


class EditorialCaptionError(ValueError):
    """An editorial row has no valid caption. Shutterstock rejects it; the file is not written."""


def editorial_problem(description: str) -> str | None:
    """The first problem with an editorial caption after the safe fixes, or None."""
    problems = caption_problems(fix_caption(description))
    return problems[0] if problems else None


def build_csv(items: Iterable[dict], strict: bool = True) -> str:
    """The Shutterstock CSV text. ``strict`` (English only) refuses non-Latin cells; with
    strict off (another metadata language) the cells are written as they are, in UTF-8."""
    buf = io.StringIO()
    writer = csv.writer(buf, quoting=csv.QUOTE_MINIMAL, lineterminator="\n")
    writer.writerow(HEADERS)
    for it in items:
        description = it["description"]
        if it.get("editorial"):
            description = fix_caption(description)
            if problems := caption_problems(description):
                raise EditorialCaptionError(
                    f"{it['filename']}: the editorial description must look like "
                    f"'{FORMAT_HINT}' ({problems[0]})"
                )
        row = [
            it["filename"],
            description,
            ",".join(it["keywords"]),
            ",".join(it["categories"]),
            yes_no(it.get("editorial")),
            yes_no(it.get("mature")),
            yes_no(it.get("illustration")),
        ]
        if strict and any(has_foreign_script(cell) for cell in row[:4]):
            raise NotEnglishError(f"{it['filename']}: the text is not in English")
        writer.writerow(row)
    return buf.getvalue()
