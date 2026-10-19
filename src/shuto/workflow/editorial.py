"""Switch a photo between a commercial description and an editorial caption.

The caption prefix (place and date) comes from the photo's facts, never from the model; the
model only writes the sentence. Toggling editorial on or off, or a guardrail forcing it during
an edit, goes through ``apply_editorial`` so the text always matches the flag. No AI call.
"""

from __future__ import annotations

from typing import Any

from shuto.core import text as core_text
from shuto.core.models import Photo
from shuto.metadata import editorial
from shuto.places.context import photo_context
from shuto.places.resolve import resolve_place


def apply_editorial(
    photo: Photo,
    data: dict[str, Any],
    *,
    default_city: str = "",
    default_country: str = "",
    english: bool = True,
    include_gps: bool = True,
) -> dict[str, Any]:
    """A copy of ``data`` whose description fits its ``editorial`` flag.

    Editorial and the description is not a caption: the caption is built from the stored
    sentence (or the description when the owner edited it), the photo's place and capture date;
    the old description is kept as ``commercial_description``. Not editorial and the description
    is a caption: the commercial description is restored (or the caption's sentence). Anything
    that already fits is left alone.
    """
    out = dict(data)
    description = str(out.get("description") or "")
    sentence = editorial.caption_sentence_of(description)
    stored = str((photo.metadata or {}).get("description") or "")
    edited = description != stored
    if out.get("editorial"):
        if sentence is not None:
            if edited:
                out["caption_sentence"] = sentence
            return out
        facts = photo_context(photo, english, include_gps)
        place = resolve_place(
            out.get("location") or {}, default_city, default_country, facts["place"]
        )
        taken = core_text.parse_dt(photo.captured_at) or core_text.parse_dt(facts.get("captured"))
        use_description = edited or not out.get("caption_sentence")
        sentence = description if use_description else str(out["caption_sentence"])
        out["commercial_description"] = description or out.get("commercial_description", "")
        out["caption_sentence"] = editorial.capitalise(sentence)
        out["description"] = editorial.build_caption(
            place["city"],
            place["country"],
            taken.date() if taken else None,
            sentence,
            place["region"],
        )
        out["place"] = place
        return out
    if sentence is None:
        return out
    restore = "" if edited else str(out.get("commercial_description") or "")
    out["description"] = restore or editorial.capitalise(sentence)
    out["caption_sentence"] = editorial.capitalise(sentence)
    if (out.get("place") or {}).get("default"):
        facts = photo_context(photo, english, include_gps)
        out["place"] = resolve_place(out.get("location") or {}, known=facts["place"])
    return out
