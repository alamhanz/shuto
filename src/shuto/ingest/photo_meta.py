"""Facts already stored inside a photo file: EXIF, IPTC and XMP.

Read with Pillow only. Every function is defensive: broken or odd tags are skipped, never raised.
Originals are only read. GPS stays local: it is never sent to a model or an online service.
"""

from __future__ import annotations

import math
import re
import unicodedata
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from PIL import Image

from shuto.core.exif_tags import (
    EXIF_IFD,
    GPS_IFD,
    T_DATETIME,
    T_DT_ORIGINAL,
    T_FOCAL,
    T_FOCAL35,
    T_LENS,
    T_MAKE,
    T_MODEL,
    T_ORIENTATION,
)
from shuto.ingest.imaging import _parse_exif_date, open_image

_NS = {
    "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
    "dc": "http://purl.org/dc/elements/1.1/",
    "photoshop": "http://ns.adobe.com/photoshop/1.0/",
    "Iptc4xmpCore": "http://iptc.org/std/Iptc4xmpCore/1.0/xmlns/",
}
_MAX_XMP_BYTES = 2_000_000
_MAX_KEYWORDS = 100
_MAX_CAPTION_CHARS = 2000
_MAX_TITLE_CHARS = 200
_MAX_KEYWORD_CHARS = 64
_IPTC_UTF8 = b"\x1b%G"  # IPTC 1:90 CodedCharacterSet for UTF-8
_IPTC_LATIN1 = b"\x1b-A"  # ISO 8859-1


@dataclass
class PhotoMeta:
    captured: datetime | None = None
    gps: tuple[float, float] | None = None
    altitude_m: float | None = None
    camera: str | None = None
    lens: str | None = None
    focal_mm: float | None = None
    focal_35mm: float | None = None
    orientation: int | None = None
    title: str | None = None
    caption: str | None = None
    keywords: list[str] = field(default_factory=list)
    place_hint: dict[str, str] | None = None  # city / region / country / sublocation

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe copy (for the photo's stored metadata). Empty fields are dropped."""
        data = asdict(self)
        data["captured"] = self.captured.isoformat() if self.captured else None
        data["gps"] = list(self.gps) if self.gps else None
        return {k: v for k, v in data.items() if v not in (None, [], {})}

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> PhotoMeta:
        data = data or {}
        gps = data.get("gps")
        try:
            captured = datetime.fromisoformat(data["captured"]) if data.get("captured") else None
        except (TypeError, ValueError):
            captured = None
        return cls(
            captured=captured,
            gps=(float(gps[0]), float(gps[1])) if isinstance(gps, (list, tuple)) else None,
            altitude_m=data.get("altitude_m"),
            camera=data.get("camera"),
            lens=data.get("lens"),
            focal_mm=data.get("focal_mm"),
            focal_35mm=data.get("focal_35mm"),
            orientation=data.get("orientation"),
            title=data.get("title"),
            caption=data.get("caption"),
            keywords=[str(k) for k in data.get("keywords") or []],
            place_hint=data.get("place_hint") or None,
        )


def _clean(value: object, charset: bytes = b"") -> str | None:
    """One tidy line: undecodable text is dropped, control characters (C0/C1, bidi marks)
    are removed and line breaks become spaces."""
    if isinstance(value, bytes):
        value = _decode(value, charset)
    if not isinstance(value, str):
        return None
    text = re.sub(r"[\t\r\n]+", " ", value.replace("\x00", ""))
    text = "".join(c for c in text if unicodedata.category(c) not in ("Cc", "Cf", "Cs", "Co", "Cn"))
    text = text.strip()
    return text or None


def _decode(raw: bytes, charset: bytes = b"") -> str | None:
    """Text from IPTC bytes: as the record declares (1:90), else UTF-8, else Shift-JIS when the
    bytes have C1 bytes (0x80-0x9F, which Western Latin-1 text never has), else Latin-1 when it
    reads as plain letters. Anything else is dropped (None): wrongly decoded text is worse than
    none."""
    if charset == _IPTC_LATIN1:
        return raw.decode("latin-1")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        pass
    if charset == _IPTC_UTF8:
        return None
    if any(0x80 <= b <= 0x9F for b in raw):
        try:
            text = raw.decode("cp932")
        except UnicodeDecodeError:
            return None
        if any(unicodedata.category(c) == "Co" or 0xFF61 <= ord(c) <= 0xFF9F for c in text):
            return None
        return text
    text = raw.decode("latin-1")
    return text if all(c.isalpha() or c in " .,'-/&()0123456789" for c in text) else None


def _cap(text: str | None, limit: int) -> str | None:
    return text[:limit].rstrip() if text else text


def _num(value: object) -> float | None:
    try:
        number = float(value)  # IFDRational, int, float, numeric str
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    return number if math.isfinite(number) else None


def _dms(value: object, ref: object, negative: str) -> float | None:
    try:
        deg, minutes, seconds = (_num(x) for x in value)  # type: ignore[attr-defined]
    except (TypeError, ValueError):
        return None
    if deg is None or minutes is None or seconds is None:
        return None
    result = deg + minutes / 60 + seconds / 3600
    if _clean(ref) and str(_clean(ref)).upper().startswith(negative):
        result = -result
    return result


def gps_from_ifd(gps: dict) -> tuple[float, float, float | None] | None:
    """(lat, lon, altitude m or None) from a GPS IFD; None when absent, invalid or 0,0."""
    lat = _dms(gps.get(2), gps.get(1), "S")
    lon = _dms(gps.get(4), gps.get(3), "W")
    if lat is None or lon is None or not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    if lat == 0 and lon == 0:  # cameras without a fix often write zeros
        return None
    alt = _num(gps.get(6))
    if alt is not None:
        ref = gps.get(5)
        below = ref == 1 or ref == b"\x01"
        alt = -alt if below else alt
    return lat, lon, alt


def _exif_part(im: Image.Image, meta: PhotoMeta) -> None:
    try:
        exif = im.getexif()
    except Exception:
        return
    try:
        sub = exif.get_ifd(EXIF_IFD)
    except Exception:
        sub = {}
    captured = _parse_exif_date(sub.get(T_DT_ORIGINAL)) or _parse_exif_date(exif.get(T_DATETIME))
    meta.captured = captured
    make, model = _clean(exif.get(T_MAKE)) or "", _clean(exif.get(T_MODEL)) or ""
    if make and model and model.lower().startswith(make.lower()):
        meta.camera = model
    else:
        meta.camera = " ".join(p for p in (make, model) if p) or None
    meta.lens = _clean(sub.get(T_LENS))
    meta.focal_mm = _positive(_num(sub.get(T_FOCAL)))
    meta.focal_35mm = _positive(_num(sub.get(T_FOCAL35)))
    orientation = exif.get(T_ORIENTATION)
    meta.orientation = (
        orientation if isinstance(orientation, int) and 1 <= orientation <= 8 else None
    )
    try:
        found = gps_from_ifd(dict(exif.get_ifd(GPS_IFD)))
    except Exception:
        found = None
    if found:
        meta.gps = (round(found[0], 6), round(found[1], 6))
        meta.altitude_m = round(found[2], 1) if found[2] is not None else None


def _positive(value: float | None) -> float | None:
    return round(value, 1) if value is not None and value > 0 else None


def _iptc_part(im: Image.Image, meta: PhotoMeta, place: dict[str, str]) -> None:
    from PIL import IptcImagePlugin

    try:
        info = IptcImagePlugin.getiptcinfo(im)
    except Exception:
        return
    if not info:
        return

    declared = info.get((1, 90))
    declared = declared[0] if isinstance(declared, list) and declared else declared
    charset = declared.strip() if isinstance(declared, bytes) else b""

    def texts(key: tuple[int, int]) -> list[str]:
        raw = info.get(key)
        items = raw if isinstance(raw, list) else [raw]
        return [t for t in (_clean(i, charset) for i in items) if t]

    meta.title = _cap((texts((2, 5)) or [None])[0], _MAX_TITLE_CHARS)
    meta.caption = _cap((texts((2, 120)) or [None])[0], _MAX_CAPTION_CHARS)
    meta.keywords = [k for k in (_cap(t, _MAX_KEYWORD_CHARS) for t in texts((2, 25))) if k][
        :_MAX_KEYWORDS
    ]
    for key, name in (((2, 90), "city"), ((2, 95), "region"), ((2, 101), "country")):
        if texts(key):
            place[name] = texts(key)[0]
    if texts((2, 92)):
        place["sublocation"] = texts((2, 92))[0]


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _lang_text(node: ET.Element | None) -> str | None:
    """Text of a simple property or of the x-default (else first) entry of an Alt/Bag/Seq."""
    if node is None:
        return None
    items = node.findall(".//rdf:li", _NS)
    if not items:
        return _clean(node.text)
    chosen = next(
        (i for i in items if i.get("{http://www.w3.org/XML/1998/namespace}lang") == "x-default"),
        items[0],
    )
    return _clean(chosen.text)


def parse_xmp(packet: bytes | str | None) -> dict[str, Any]:
    """title, caption, keywords and place fields from an XMP packet. Safe against XXE/bombs.

    DOCTYPE/ENTITY declarations are rejected outright (XMP never needs them), so no entity is
    ever expanded and nothing external is loaded.
    """
    if isinstance(packet, str):
        packet = packet.encode("utf-8", errors="replace")
    if not packet or len(packet) > _MAX_XMP_BYTES:
        return {}
    lowered = packet.lower()
    if b"<!doctype" in lowered or b"<!entity" in lowered:
        return {}
    start = packet.find(b"<x:xmpmeta")
    if start < 0:
        start = packet.find(b"<rdf:RDF")
    end = packet.rfind(b"</x:xmpmeta>")
    end = end + len(b"</x:xmpmeta>") if end >= 0 else packet.rfind(b"</rdf:RDF>") + 10
    if start < 0 or end <= start:
        return {}
    try:
        root = ET.fromstring(packet[start:end])
    except (ET.ParseError, ValueError):
        return {}
    out: dict[str, Any] = {"keywords": [], "place": {}}
    attrs = {
        "title": "dc:title",
        "caption": "dc:description",
        "city": "photoshop:City",
        "region": "photoshop:State",
        "country": "photoshop:Country",
        "sublocation": "Iptc4xmpCore:Location",
    }
    for desc in root.iter(f"{{{_NS['rdf']}}}Description"):
        for key, qname in attrs.items():
            prefix, name = qname.split(":")
            value = _clean(desc.get(f"{{{_NS[prefix]}}}{name}")) or _lang_text(
                desc.find(qname, _NS)
            )
            if not value:
                continue
            if key in ("title", "caption"):
                out.setdefault(
                    key, _cap(value, _MAX_CAPTION_CHARS if key == "caption" else _MAX_TITLE_CHARS)
                )
            else:
                out["place"].setdefault(key, value)
        subject = desc.find("dc:subject", _NS)
        if subject is not None:
            for li in subject.findall(".//rdf:li", _NS):
                text = _cap(_clean(li.text), _MAX_KEYWORD_CHARS)
                if text and text not in out["keywords"]:
                    out["keywords"].append(text)
    out["keywords"] = out["keywords"][:_MAX_KEYWORDS]
    return out


def _xmp_part(im: Image.Image, meta: PhotoMeta, place: dict[str, str]) -> None:
    raw = im.info.get("xmp") or im.info.get("XML:com.adobe.xmp")
    if raw is None:
        return
    data = parse_xmp(raw)
    if not data:
        return
    meta.title = meta.title or data.get("title")
    meta.caption = meta.caption or data.get("caption")
    for kw in data.get("keywords", []):
        if kw not in meta.keywords:
            meta.keywords.append(kw)
    meta.keywords = meta.keywords[:_MAX_KEYWORDS]
    for key, value in data.get("place", {}).items():
        place[key] = value  # XMP is UTF-8 by spec, so it wins over a doubtful IPTC reading


def read_photo_meta(path: str | Path | bytes) -> PhotoMeta:
    """Everything useful already inside the file. Never raises; unreadable files give empty."""
    meta = PhotoMeta()
    try:
        im = open_image(path)
    except Exception:
        return meta
    place: dict[str, str] = {}
    try:
        for part in (_exif_part, _iptc_part, _xmp_part):
            try:
                if part is _exif_part:
                    part(im, meta)
                else:
                    part(im, meta, place)
            except Exception:
                continue
    finally:
        im.close()
    meta.place_hint = place or None
    return meta
