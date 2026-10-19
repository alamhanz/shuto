"""Shutterstock's predefined photo categories.

The CSV "Categories" column only accepts values from this list (1-2 per image).
Verify against the sample CSV on submit.shutterstock.com if a CSV is rejected.
"""

SHUTTERSTOCK_CATEGORIES: tuple[str, ...] = (
    "Abstract",
    "Animals/Wildlife",
    "Arts",
    "Backgrounds/Textures",
    "Beauty/Fashion",
    "Buildings/Landmarks",
    "Business/Finance",
    "Education",
    "Food and drink",
    "Healthcare/Medical",
    "Holidays",
    "Industrial",
    "Interiors",
    "Miscellaneous",
    "Nature",
    "Objects",
    "Parks/Outdoor",
    "People",
    "Religion",
    "Science",
    "Signs/Symbols",
    "Sports/Recreation",
    "Technology",
    "Transportation",
    "Vintage",
)

_LOOKUP = {c.lower(): c for c in SHUTTERSTOCK_CATEGORIES}


def normalize_categories(values: list[str] | None) -> list[str]:
    """Keep only valid categories (case-insensitive), dedupe, max 2."""
    out: list[str] = []
    for v in values or []:
        canon = _LOOKUP.get(str(v).strip().lower())
        if canon and canon not in out:
            out.append(canon)
        if len(out) == 2:
            break
    return out or ["Miscellaneous"]
