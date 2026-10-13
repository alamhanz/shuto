"""What shuto already knows about a photo from its own file, ready for the describe prompt.

Order of trust for the place of a photo (highest first):
  1. GPS in the file, reverse-geocoded offline (source "gps")
  2. IPTC/XMP location fields written by the photographer or an app (source "file")
  3. the AI's own guess, 4. the default place from settings (set by other code).
A GPS fix that only resolves to region/country (no city nearby) loses to a file location that
names a city. Coordinates are never put in the context or the hints: only place names.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Any

from . import geo
from .photo_meta import PhotoMeta, read_photo_meta

MAX_HINTS_CHARS = 300
_MAX_KEYWORDS_IN_HINTS = 8
_PLACE_KEYS = ("sublocation", "city", "region", "country")


def compute_file_facts(path: str | Path) -> dict[str, Any]:
    """Values to store in a photo's metadata JSON: {"file_meta": {...}, "gps_place": {...}}.

    Never raises. Keys are left out when there is nothing to store.
    """
    out: dict[str, Any] = {}
    try:
        meta = read_photo_meta(path)
    except Exception:
        return out
    data = meta.to_dict()
    if data:
        out["file_meta"] = data
    if meta.gps:
        place = geo.reverse_geocode(*meta.gps)
        if place:
            out["gps_place"] = place.to_dict()
    return out


def _clean_text(value: object, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _place_from_gps(gp: dict | None) -> dict[str, Any] | None:
    if not gp or not (gp.get("city") or gp.get("region") or gp.get("country")):
        return None
    return {
        "city": gp.get("city"),
        "region": gp.get("region"),
        "country": gp.get("country"),
        "sublocation": None,
        "source": "gps",
        "confidence": gp.get("confidence") or "low",
    }


def _place_from_file(hint: dict | None) -> dict[str, Any] | None:
    if not hint or not any(hint.get(k) for k in _PLACE_KEYS):
        return None
    return {
        "city": hint.get("city"),
        "region": hint.get("region"),
        "country": hint.get("country"),
        "sublocation": hint.get("sublocation"),
        "source": "file",
        "confidence": "medium",
    }


def place_text(place: dict[str, Any] | None) -> str:
    """ "Shibuya, Tokyo, Japan" style text from a context place."""
    if not place:
        return ""
    parts: list[str] = []
    for key in _PLACE_KEYS:
        value = _clean_text(place.get(key), 60)
        if value and value.lower() not in (p.lower() for p in parts):
            parts.append(value)
    return ", ".join(parts)


def build_context(
    meta: PhotoMeta, gps_place: dict | None = None, captured_fallback: Any = None
) -> dict[str, Any]:
    """Known facts plus a short hints string (at most 300 characters)."""
    captured = meta.captured or captured_fallback
    place = _place_from_gps(gps_place)
    file_place = _place_from_file(meta.place_hint)
    if file_place and (place is None or (not place["city"] and file_place["city"])):
        place = file_place
    ctx: dict[str, Any] = {
        "captured": captured.isoformat() if captured else None,
        "place": place,
        "title": meta.title,
        "caption": meta.caption,
        "keywords": list(meta.keywords),
        "camera": meta.camera,
        "lens": meta.lens,
        "focal_mm": meta.focal_mm,
        "focal_35mm": meta.focal_35mm,
    }
    ctx["hints"] = _hints(ctx)
    return ctx


def _hints(ctx: dict[str, Any]) -> str:
    sentences: list[str] = []
    where = place_text(ctx["place"])
    when = ctx["captured"][:10] if ctx["captured"] else ""
    if ctx["place"] and where:
        src = "from GPS" if ctx["place"]["source"] == "gps" else "from the photo file"
        sentences.append(f"Taken {when + ' ' if when else ''}in {where} ({src}).")
    elif when:
        sentences.append(f"Taken {when}.")
    if ctx["title"]:
        sentences.append(f"Photographer title: {_clean_text(ctx['title'], 60)}.")
    if ctx["keywords"]:
        kws = [_clean_text(k, 30) for k in ctx["keywords"][:_MAX_KEYWORDS_IN_HINTS]]
        sentences.append("Photographer keywords: " + ", ".join(k for k in kws if k) + ".")
    if ctx["caption"]:
        sentences.append(f"Photographer caption: {_clean_text(ctx['caption'], 80)}.")
    if ctx["lens"] or ctx["focal_35mm"]:
        gear = _clean_text(ctx["lens"], 40) if ctx["lens"] else f"{ctx['focal_35mm']:g} mm"
        sentences.append(f"Lens: {gear}.")
    text = ""
    for sentence in sentences:  # whole sentences only, in priority order
        candidate = f"{text} {sentence}".strip()
        if len(candidate) <= MAX_HINTS_CHARS:
            text = candidate
    return text


def photo_context(photo: Any) -> dict[str, Any]:
    """Facts for one stored photo; uses values saved at import, else reads the file now."""
    stored = photo.metadata or {}
    file_meta = stored.get("file_meta")
    gps_place = stored.get("gps_place")
    if file_meta is None and "file_meta" not in stored:
        facts = compute_file_facts(photo.src_path)
        file_meta, gps_place = facts.get("file_meta"), facts.get("gps_place")
    captured = None
    if photo.captured_at:
        try:
            captured = datetime.fromisoformat(photo.captured_at)
        except ValueError:
            captured = None
    return build_context(PhotoMeta.from_dict(file_meta), gps_place, captured)
