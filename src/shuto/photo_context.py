"""What shuto already knows about a photo from its own file, ready for the describe prompt.

Order of trust for the place of a photo (highest first):
  1. GPS in the file, reverse-geocoded offline (source "gps")
  2. IPTC/XMP location fields written by the photographer or an app (source "file")
  3. the AI's own guess, 4. the default place from settings (set by other code).
A GPS fix that only resolves to region/country (no city nearby) keeps those and takes the
city from a usable file location. Coordinates are never put in the context or the hints: only
place names.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Any

from . import geo
from .metadata_rules import english_only
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


def _clean_text(value: object, limit: int, english: bool = True) -> str:
    """One line of English text: accents folded, words in other scripts left out (the prompt
    asks for English only, so a Japanese title in the file must not leak into the answer).
    With ``english=False`` (another metadata language) the words are kept as they are."""
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    text = english_only(text) if english else text
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _place_from_gps(gp: dict | None) -> dict[str, Any] | None:
    if not gp or not (gp.get("city") or gp.get("region") or gp.get("country")):
        return None
    return {
        "city": gp.get("city"),
        "region": gp.get("region"),
        "country": gp.get("country"),
        "sublocation": None,
        "neighborhood": gp.get("neighborhood"),
        "ward": gp.get("ward"),
        "prefecture": gp.get("region"),
        "source": "gps",
        "confidence": gp.get("confidence") or "low",
    }


_PLACE_PART_OK = re.compile(r"[A-Za-z][A-Za-z .'-]*")
_MAX_PLACE_PART = 60


def _usable_place_part(value: object) -> str | None:
    """A place name from the photo file as plain English letters, else None.

    Words in other scripts are dropped; what is left must look like a name (letters, spaces,
    . ' -, at least two letters) or the part is not trusted at all."""
    text = english_only(re.sub(r"\s+", " ", str(value or "")).strip())
    if not text or len(text) > _MAX_PLACE_PART or not _PLACE_PART_OK.fullmatch(text):
        return None
    return text if sum(c.isalpha() for c in text) >= 2 else None


def _place_from_file(hint: dict | None) -> dict[str, Any] | None:
    if not hint:
        return None
    parts = {k: _usable_place_part(hint.get(k)) for k in _PLACE_KEYS}
    if not any(parts.values()):
        return None
    return {
        **parts,
        "source": "file",
        "confidence": "medium",
    }


def place_text(place: dict[str, Any] | None, english: bool = True) -> str:
    """ "Shibuya, Tokyo, Japan" style text from a context place."""
    if not place:
        return ""
    parts: list[str] = []
    for key in _PLACE_KEYS:
        value = _clean_text(place.get(key), 60, english)
        if value and value.lower() not in (p.lower() for p in parts):
            parts.append(value)
    return ", ".join(parts)


def build_context(
    meta: PhotoMeta,
    gps_place: dict | None = None,
    captured_fallback: Any = None,
    english: bool = True,
    neighborhood: bool = True,
) -> dict[str, Any]:
    """Known facts plus a short hints string (at most 300 characters).

    ``neighborhood=False`` (the GPS location is not kept in the upload) leaves the GPS
    neighbourhood out, so only the ward or city can reach the prompt, caption and keywords."""
    captured = meta.captured or captured_fallback
    place = _place_from_gps(gps_place)
    file_place = _place_from_file(meta.place_hint)
    if place is None:
        place = file_place
    elif file_place and not place["city"] and file_place["city"]:
        place["city"] = file_place["city"]  # keep the GPS region and country, add the city
    if place is not None and not neighborhood:
        place = {**place, "neighborhood": None}
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
    ctx["hints"] = _hints(ctx, english)
    return ctx


def _gps_place_sentence(place: dict[str, Any], english: bool = True) -> str:
    """E.g. "Place from GPS: Sakuragaokacho (neighborhood), Shibuya (ward), Tokyo (prefecture),
    Japan. Use the ward for the caption; neighborhood may be a keyword." """
    hood = _clean_text(place.get("neighborhood"), 60, english)
    ward = _clean_text(place.get("ward"), 60, english)
    city = _clean_text(place.get("city"), 60, english)
    main_label = "ward" if ward else "city"
    main = ward or city
    if hood and hood.lower() == main.lower():
        hood = ""
    levels: list[tuple[str, str]] = [
        (hood, "neighborhood"),
        (main, main_label),
        (_clean_text(place.get("prefecture"), 60, english), _region_label(place)),
        (_clean_text(place.get("country"), 60, english), ""),
    ]
    shown = [(n, label) for n, label in levels if n]
    if not shown:
        return ""
    text = ", ".join(f"{n} ({label})" if label else n for n, label in shown)
    advice = []
    if main:
        advice.append(f"Use the {main_label} for the caption")
    if hood:
        advice.append("neighborhood may be a keyword")
    tail = "; ".join(advice)
    return f"Place from GPS: {text}." + (f" {tail}." if tail else "")


def _region_label(place: dict[str, Any]) -> str:
    return "prefecture" if place.get("country") == "Japan" else "region"


def _hints(ctx: dict[str, Any], english: bool = True) -> str:
    sentences: list[str] = []
    where = place_text(ctx["place"], english)
    when = ctx["captured"][:10] if ctx["captured"] else ""
    place = ctx["place"]
    if place and where and place["source"] == "gps":
        if when:
            sentences.append(f"Taken {when}.")
        sentences.append(_gps_place_sentence(place, english) or f"Place from GPS: {where}.")
    elif place and where:
        sentences.append(f"Taken {when + ' ' if when else ''}in {where} (from the photo file).")
    elif when:
        sentences.append(f"Taken {when}.")
    title = _clean_text(ctx["title"], 60, english)
    if title:
        sentences.append(f"Photographer title: {title}.")
    kws = [_clean_text(k, 30, english) for k in ctx["keywords"]]
    kws = [k for k in kws if k][:_MAX_KEYWORDS_IN_HINTS]
    if kws:
        sentences.append("Photographer keywords: " + ", ".join(kws) + ".")
    caption = _clean_text(ctx["caption"], 80, english)
    if caption:
        sentences.append(f"Photographer caption: {caption}.")
    if ctx["lens"] or ctx["focal_35mm"]:
        gear = _clean_text(ctx["lens"], 40, english) if ctx["lens"] else f"{ctx['focal_35mm']:g} mm"
        sentences.append(f"Lens: {gear}.")
    text = ""
    for sentence in sentences:  # whole sentences only, in priority order
        candidate = f"{text} {sentence}".strip()
        if len(candidate) <= MAX_HINTS_CHARS:
            text = candidate
    return text


def photo_context(photo: Any, english: bool = True, include_gps: bool = True) -> dict[str, Any]:
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
    return build_context(
        PhotoMeta.from_dict(file_meta), gps_place, captured, english, neighborhood=include_gps
    )
