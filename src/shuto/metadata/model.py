"""Shutterstock metadata model: cleaning, limits and validation warnings (no LLM code)."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

from shuto.metadata.categories import normalize_categories
from shuto.metadata.editorial import FORMAT_HINT, is_editorial_caption
from shuto.metadata.rules import MIN_COMMERCIAL_WORDS

MAX_KEYWORDS = 50
MIN_KEYWORDS = 7  # Shutterstock requires at least 7
MAX_DESCRIPTION_CHARS = 200

__all__ = ["PhotoMetadata", "is_editorial_caption", "normalize"]


@dataclass
class PhotoMetadata:
    filename: str
    description: str
    keywords: list[str]
    categories: list[str]
    editorial: bool = False
    mature: bool = False
    illustration: bool = False
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def _clean_keywords(raw) -> list[str]:
    if isinstance(raw, str):
        raw = raw.split(",")
    out: list[str] = []
    seen: set[str] = set()
    for kw in raw or []:
        k = re.sub(r"\s+", " ", str(kw)).strip().strip('"').lower()
        if k and k not in seen:
            seen.add(k)
            out.append(k)
    return out[:MAX_KEYWORDS]


def _as_bool(v) -> bool:
    if isinstance(v, str):
        return v.strip().lower() in ("true", "yes", "1")
    return bool(v)


def normalize(filename: str, data: dict) -> PhotoMetadata:
    """Validate/clean a metadata dict (from the model or a hand-edited JSON file)."""
    warnings: list[str] = []

    description = re.sub(r"\s+", " ", str(data.get("description", ""))).strip()
    if len(description) > MAX_DESCRIPTION_CHARS:
        description = description[:MAX_DESCRIPTION_CHARS].rsplit(" ", 1)[0]
        warnings.append("description truncated")
    if not description:
        warnings.append("empty description")

    keywords = _clean_keywords(data.get("keywords"))
    if len(keywords) < MIN_KEYWORDS:
        warnings.append(f"only {len(keywords)} keywords (min {MIN_KEYWORDS})")

    editorial = _as_bool(data.get("editorial", False))
    if editorial and "UNKNOWN" in description:
        warnings.append("editorial caption missing city/date")
    if editorial and description and not is_editorial_caption(description):
        warnings.append(f"editorial caption format should be '{FORMAT_HINT}'")
    if not editorial and description and len(description.split()) < MIN_COMMERCIAL_WORDS:
        warnings.append(f"commercial title under {MIN_COMMERCIAL_WORDS} words")

    return PhotoMetadata(
        filename=filename,
        description=description,
        keywords=keywords,
        categories=normalize_categories(data.get("categories")),
        editorial=editorial,
        mature=_as_bool(data.get("mature", False)),
        illustration=_as_bool(data.get("illustration", False)),
        warnings=warnings,
    )
