"""Which place a photo gets, and where each part of it came from.

The whole chain, from the photo file to the caption:

1. ``ingest.photo_meta`` reads the file's own location fields; XMP wins over IPTC.
2. ``places.geo`` reverse-geocodes GPS offline, ``places.context.build_context`` merges the
   two with ``merge_known_place``: the GPS place wins, a GPS fix that only knows a region or
   country takes its city from a usable file location.
3. ``resolve_place`` (here) decides per part: known (GPS or file) > the AI's answer > the
   known region (city only) > the default place, which is never used against a known region
   or country. ``default_flag`` and ``short_place_text`` read the result back for reports.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

from shuto.metadata import rules


def _ascii_upper(text: str | None) -> str:
    text = unicodedata.normalize("NFKD", rules.ascii_fold(text))
    text = "".join(c for c in text if not unicodedata.combining(c)).upper()
    text = re.sub(r"[^A-Z .'/-]", " ", text)
    return re.sub(r"\s+", " ", text).strip(" .-'/")


PLACE_SOURCES = (
    "gps",
    "file",
    "gps + AI",
    "file + AI",
    "ai",
    "default",
    "partly default",
    "unknown",
)
NO_PLACE_WARNING = "No place known: set a default place in Settings or edit the caption"


def resolve_place(
    location: Any,
    default_city: str = "",
    default_country: str = "",
    known: dict | None = None,
) -> dict:
    """The city and country for an editorial caption and where each came from.

    Order of trust per part: the place found in the photo file (``known``, from GPS or the
    file's own location fields), then the AI's answer, then (city only) the known region or
    prefecture, then the default place, which is never used against a known region or country.
    ``source`` is
    "gps" or "file" (everything came from the photo itself), "gps + AI" or "file + AI" (a part
    from the photo, e.g. the country, and the rest from the AI, e.g. the city), "ai" (nothing
    from the file, nothing defaulted), "default" (all from the default), "partly default"
    (a mix with the default) or "unknown" (nothing known). Parts stay empty when still unknown.
    """
    if isinstance(location, dict):
        city, country = location.get("city"), location.get("country")
    else:
        city, country = getattr(location, "city", None), getattr(location, "country", None)
    known = known or {}
    known_source = known.get("source") if known.get("source") in ("gps", "file") else "file"
    known_region = known.get("region") or known.get("prefecture")
    known_country = _ascii_upper(known.get("country"))
    # The default city never contradicts a place the photo itself names: it is skipped when the
    # photo knows a region (used as the city instead) or a different country.
    default_fits = not known_region and (
        not known_country or known_country == _ascii_upper(default_country)
    )
    parts: dict[str, tuple[str, str]] = {}  # part -> (text, where it came from)
    for part, candidates in (
        (
            "city",
            (
                (known.get("city"), known_source),
                (city, "ai"),
                (known_region, known_source),
                (default_city if default_fits else "", "default"),
            ),
        ),
        (
            "country",
            ((known.get("country"), known_source), (country, "ai"), (default_country, "default")),
        ),
    ):
        for text, where in candidates:
            if value := _ascii_upper(text):
                parts[part] = (value, where)
                break
    wheres = {where for _, where in parts.values()}
    if not parts:
        source = "unknown"
    elif "default" in wheres:
        source = "default" if wheres == {"default"} else "partly default"
    elif known_source in wheres:
        source = f"{known_source} + AI" if "ai" in wheres else known_source
    else:
        source = "ai"
    return {
        "city": parts.get("city", ("", ""))[0],
        "country": parts.get("country", ("", ""))[0],
        "source": source,
        "default": "default" in wheres,
    }


def short_place_text(place: Any) -> str:
    """'TOKYO, JAPAN' from a stored place; blank when no part is known."""
    if not isinstance(place, dict):
        return ""
    return ", ".join(str(place[k]) for k in ("city", "country") if place.get(k))


def default_flag(place: Any) -> str:
    """'yes' when a stored place came wholly from the default place, 'no' if not, else blank."""
    if not isinstance(place, dict):
        return ""
    return "yes" if place.get("default") else "no"


def merge_known_place(
    gps_place: dict | None, file_place: dict | None, neighborhood: bool = True
) -> dict | None:
    """The one place the photo itself knows, from its GPS place and its file location fields.

    The GPS place wins; when it has no city, the file's city fills that gap and the GPS region
    and country stay. ``neighborhood=False`` (the GPS location is not kept in the upload) drops
    the GPS neighbourhood.
    """
    place = gps_place
    if place is None:
        place = file_place
    elif file_place and not place["city"] and file_place["city"]:
        place["city"] = file_place["city"]  # keep the GPS region and country, add the city
    if place is not None and not neighborhood:
        place = {**place, "neighborhood": None}
    return place
