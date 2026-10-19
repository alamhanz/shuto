"""Prepare approved photos for delivery: safe unique filenames, JPEG copy with IPTC/XMP."""

from __future__ import annotations

import os
import re
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any

from shuto.ingest import imaging
from shuto.metadata import embed
from shuto.metadata.model import is_editorial_caption
from shuto.metadata.rules import has_foreign_script

MAX_NAME_CHARS = 80


class _NoDate:
    def __format__(self, spec: str) -> str:
        return "nodate"


def _slug(title: str, words: int) -> str:
    text = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode("ascii")
    parts = re.findall(r"[a-z0-9]+", text.lower())
    return "-".join(parts[: max(words, 0)])


def safe_filename(
    captured: datetime | None,
    title: str,
    n: int,
    pattern: str,
    slug_words: int,
    taken: set[str],
) -> str:
    """ASCII `[a-z0-9-]` .jpg name, at most 80 chars, unique ignoring case.

    `taken` holds lowercase names already used; the returned name is added to it.
    """
    raw = pattern.format(date=captured or _NoDate(), slug=_slug(title, slug_words), n=n)
    text = unicodedata.normalize("NFKD", raw).encode("ascii", "ignore").decode("ascii")
    stem = re.sub(r"-{2,}", "-", re.sub(r"[^a-z0-9-]+", "-", text.lower())).strip("-") or "photo"
    stem = stem[: MAX_NAME_CHARS - len(".jpg")].strip("-") or "photo"
    name, k = f"{stem}.jpg", 1
    while name.lower() in taken:
        k += 1
        suffix = f"-{k:03d}"
        name = f"{stem[: MAX_NAME_CHARS - len('.jpg') - len(suffix)].rstrip('-')}{suffix}.jpg"
    taken.add(name.lower())
    return name


def release_check(
    meta: dict[str, Any],
    detected: dict[str, Any] | None,
    release_ids: list[str],
    strict: bool = True,
) -> list[str]:
    """Problems that block preparation (empty list = fine).

    Release ids starting with `property:` count as property releases, anything else as model.
    """
    detected = detected or {}
    problems: list[str] = []
    editorial = bool(meta.get("editorial"))
    has_model = any(not r.startswith("property:") for r in release_ids)
    people = bool(detected.get("recognizable_people") or detected.get("people"))
    logos = bool(
        detected.get("logos_or_brands") or detected.get("logos") or detected.get("has_logos")
    )
    if not editorial:
        if people and not has_model:
            problems.append("Recognizable people need a model release or editorial status.")
        if logos:
            problems.append(
                "Visible logos/brands are trademarks (a property release does not clear them): "
                "make the photo editorial or remove them."
            )
    description = str(meta.get("description") or "").strip()
    if not description:
        problems.append("Description is empty.")
    elif editorial and "UNKNOWN" in description.split(":", 1)[0].upper():
        problems.append("Editorial caption has an UNKNOWN city, country or date; fill it in.")
    elif editorial and not is_editorial_caption(description):
        problems.append('Editorial caption must look like "CITY, COUNTRY - MONTH D, YYYY: text".')
    if not meta.get("keywords"):
        problems.append("No keywords.")
    texts = [str(meta.get(k) or "") for k in ("title", "description")]
    texts += [str(k) for k in meta.get("keywords") or []]
    if strict and any(has_foreign_script(t) for t in texts):
        problems.append("Text is not in English: Shutterstock needs English, Latin letters.")
    return problems


def prepare_file(
    src: Path,
    dest_dir: Path,
    name: str,
    meta: dict[str, Any],
    jpeg_quality: int = 95,
    include_gps: bool = False,
) -> Path:
    """Write a JPEG copy of `src` named `name` with IPTC/XMP embedded. `src` is never modified.

    The copy has no GPS location unless `include_gps` is set; other EXIF (date, camera) stays.
    """
    src, dest_dir = Path(src), Path(dest_dir)
    data = src.read_bytes()
    if data[:2] != b"\xff\xd8":
        data = imaging.to_jpeg_bytes(src, jpeg_quality)
    description = str(meta.get("description") or "")
    title = str(meta.get("title") or description[:64])
    out = embed.embed_metadata(
        data,
        title,
        description,
        list(meta.get("keywords") or []),
        strip_gps=not include_gps,
    )
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / name
    tmp = dest.with_name(dest.name + ".tmp")
    tmp.write_bytes(out)
    os.replace(tmp, dest)
    return dest
