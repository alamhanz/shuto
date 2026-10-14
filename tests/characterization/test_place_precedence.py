"""Where a photo's place comes from, and how each source is read and cleaned.

Chain: file fields (IPTC/XMP, decoded) -> GPS/file merge in the describe context -> resolve_place
(file/GPS, AI, known region, default). The golden records today's answers for each case.
"""

from __future__ import annotations

import struct
from io import BytesIO

from PIL import Image

from . import check_json


def _seam():
    from shuto.ai import describe
    from shuto.core import validate as core_validate
    from shuto.deliver import report
    from shuto.ingest import photo_meta
    from shuto.places import context as photo_context

    return {
        "resolve_place": describe.resolve_place,
        "build_context": photo_context.build_context,
        "place_text": photo_context.place_text,
        "usable_part": photo_context._usable_place_part,
        "clean_part": core_validate.clean_place_part,
        "read_photo_meta": photo_meta.read_photo_meta,
        "PhotoMeta": photo_meta.PhotoMeta,
        "report_place_text": report._place_text,
        "report_default_flag": report._default_flag,
        "PLACE_SOURCES": describe.PLACE_SOURCES,
    }


def _outcome(call):
    try:
        return {"ok": call()}
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


# --- resolve_place ---------------------------------------------------------------------------

AI = {"city": "Osaka", "country": "Japan"}
RESOLVE_CASES = {
    "gps both": (AI, "Tokyo", "Japan", {"city": "Kyoto", "country": "Japan", "source": "gps"}),
    "file both": (AI, "Tokyo", "Japan", {"city": "Kyoto", "country": "Japan", "source": "file"}),
    "gps country only, AI city": (
        AI,
        "Tokyo",
        "Japan",
        {"country": "Japan", "source": "gps"},
    ),
    "file country only, AI nothing": (
        {},
        "Tokyo",
        "Japan",
        {"country": "Japan", "source": "file"},
    ),
    "ai only": (AI, "Tokyo", "Japan", None),
    "ai city, default country": ({"city": "Osaka"}, "Tokyo", "Japan", None),
    "default only": ({}, "Tokyo", "Japan", None),
    "default country only": ({}, "", "Japan", None),
    "unknown": ({}, "", "", None),
    "region used as city": (
        {},
        "Tokyo",
        "Japan",
        {"region": "Kanagawa", "country": "Japan", "source": "gps"},
    ),
    "prefecture used as city": (
        {},
        "Tokyo",
        "Japan",
        {"prefecture": "Chiba", "country": "Japan", "source": "file"},
    ),
    "foreign known country skips default city": (
        {},
        "Tokyo",
        "Japan",
        {"country": "France", "source": "gps"},
    ),
    "foreign known country, AI city": (
        {"city": "Lyon"},
        "Tokyo",
        "Japan",
        {"country": "France", "source": "gps"},
    ),
    "known country matches default": ({}, "Tokyo", "Japan", {"country": "Japan", "source": "gps"}),
    "region beats default city, AI empty": (
        {"city": ""},
        "Tokyo",
        "Japan",
        {"region": "Saitama", "source": "gps"},
    ),
    "accents and case folded": (
        {"city": "Köln", "country": "Germany"},
        "",
        "",
        None,
    ),
    "unknown source treated as file": (
        {},
        "",
        "",
        {"city": "Nara", "source": "other"},
    ),
    "location as object": (None, "Tokyo", "Japan", None),
}


def test_resolve_place_golden():
    resolve = _seam()["resolve_place"]
    out = {}
    for label, (location, city, country, known) in RESOLVE_CASES.items():
        out[label] = _outcome(lambda a=(location, city, country, known): resolve(*a))
    sources = {r["ok"]["source"] for r in out.values() if "ok" in r}
    assert sources <= set(_seam()["PLACE_SOURCES"])
    check_json("place_resolve.json", out)


# --- build_context merge -----------------------------------------------------------------------

GPS_FULL = {
    "city": "Shibuya",
    "region": "Tokyo",
    "country": "Japan",
    "neighborhood": "Sakuragaokacho",
    "ward": "Shibuya",
    "confidence": "high",
}
GPS_NO_CITY = {"city": None, "region": "Tokyo", "country": "Japan"}
FILE_HINT = {"city": "Yokohama", "region": "Kanagawa", "country": "Japan", "sublocation": "Pier"}
CONTEXT_CASES = {
    "gps only": (None, GPS_FULL),
    "file only": (FILE_HINT, None),
    "gps wins over file": (FILE_HINT, GPS_FULL),
    "gps without city takes the file city": (FILE_HINT, GPS_NO_CITY),
    "gps without city, no file city": ({"country": "Japan"}, GPS_NO_CITY),
    "file in Japanese is dropped": (
        {"city": "渋谷", "country": "日本", "sublocation": "Pier 3"},
        None,
    ),
    "file half usable": ({"city": "Kyoto", "country": "日本"}, None),
    "file with digits is not trusted": ({"city": "Tokyo 2"}, None),
    "nothing": (None, None),
    "empty gps": (None, {"city": None, "region": None, "country": None}),
}


def test_build_context_golden():
    s = _seam()
    out = {}
    for label, (hint, gps) in CONTEXT_CASES.items():
        for flag in (True, False):
            meta = s["PhotoMeta"](place_hint=hint, title="Pier day", keywords=["sea"])
            ctx = s["build_context"](meta, gps, None, True, neighborhood=flag)
            out[f"{label} / neighborhood={flag}"] = {
                "place": ctx["place"],
                "hints": ctx["hints"],
                "place_text": s["place_text"](ctx["place"]),
            }
    check_json("place_context.json", out)


def test_place_text_english_flag():
    s = _seam()
    place = {"city": "渋谷", "region": "Tokyo", "country": "Japan", "sublocation": None}
    assert s["place_text"](place) == "Tokyo, Japan"
    assert s["place_text"](place, english=False) == "渋谷, Tokyo, Japan"
    assert s["place_text"](None) == ""


# --- reading IPTC/XMP text from the file ---------------------------------------------------------


def _iim(record: int, dataset: int, value: bytes) -> bytes:
    return struct.pack(">BBBH", 0x1C, record, dataset, len(value)) + value


def _app13(records: list[tuple[int, int, bytes]]) -> bytes:
    iim = b"".join(_iim(*r) for r in records)
    if len(iim) % 2:
        iim += b"\x00"
    body = b"Photoshop 3.0\x00" + b"8BIM" + struct.pack(">H", 0x0404) + b"\x00\x00"
    body += struct.pack(">I", len(iim)) + iim
    return b"\xff\xed" + struct.pack(">H", len(body) + 2) + body


def _app1_xmp(city: str = "", country: str = "", title: str = "") -> bytes:
    attrs = ""
    if city:
        attrs += f' photoshop:City="{city}"'
    if country:
        attrs += f' photoshop:Country="{country}"'
    if title:
        attrs += f' dc:title="{title}"'
    xml = (
        '<x:xmpmeta xmlns:x="adobe:ns:meta/">'
        '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
        '<rdf:Description rdf:about="" xmlns:photoshop="http://ns.adobe.com/photoshop/1.0/" '
        f'xmlns:dc="http://purl.org/dc/elements/1.1/"{attrs}/></rdf:RDF></x:xmpmeta>'
    ).encode()
    body = b"http://ns.adobe.com/xap/1.0/\x00" + xml
    return b"\xff\xe1" + struct.pack(">H", len(body) + 2) + body


def _jpeg(*segments: bytes) -> bytes:
    buf = BytesIO()
    Image.new("RGB", (16, 16), (120, 120, 120)).save(buf, "JPEG")
    data = buf.getvalue()
    return data[:2] + b"".join(segments) + data[2:]


UTF8, LATIN1 = b"\x1b%G", b"\x1b/A"
DECODE_CASES = {
    "latin-1 Köln undeclared": [(2, 90, "Köln".encode("latin-1")), (2, 101, b"Germany")],
    "latin-1 Köln declared": [(1, 90, LATIN1), (2, 90, "Köln".encode("latin-1"))],
    "utf-8 Köln undeclared": [(2, 90, "Köln".encode()), (2, 101, b"Germany")],
    "utf-8 declared": [(1, 90, UTF8), (2, 90, "Zürich".encode()), (2, 5, "Café".encode())],
    "utf-8 declared but invalid": [(1, 90, UTF8), (2, 90, b"K\xf6ln"), (2, 5, b"Title")],
    "shift-jis 渋谷": [(2, 90, "渋谷".encode("cp932")), (2, 101, "日本".encode("cp932"))],
    "shift-jis title": [(2, 5, "東京の通り".encode("cp932")), (2, 120, "朝の写真".encode("cp932"))],
    "shift-jis half-width kana dropped": [(2, 90, "ｱｲｳ".encode("cp932"))],
    "private use dropped": [(2, 90, bytes([0xF0, 0x40]))],
    "undecodable high bytes": [(2, 90, b"\x81\x20\xfe")],
    "latin-1 with symbols dropped": [(2, 90, b"\xa4\xa4\xa4")],
    "keywords": [(2, 25, b"tokyo"), (2, 25, "渋谷".encode()), (2, 25, b"street")],
    "all place fields": [
        (2, 90, b"Yokohama"),
        (2, 92, b"Pier"),
        (2, 95, b"Kanagawa"),
        (2, 101, b"Japan"),
    ],
}
XMP_CASES = {
    "xmp fills when iptc has nothing": ([], dict(city="Nagoya", country="Japan")),
    "xmp wins over iptc": (
        [(2, 90, b"K\xf6ln"), (2, 101, b"Germany")],
        dict(city="Cologne", country="Germany"),
    ),
    "xmp city only keeps iptc country": (
        [(2, 90, b"Osaka"), (2, 101, b"Japan")],
        dict(city="Kobe"),
    ),
    "xmp title does not replace iptc title": (
        [(2, 5, b"IPTC title")],
        dict(title="XMP title"),
    ),
}


def _meta_view(meta) -> dict:
    return {
        "title": meta.title,
        "caption": meta.caption,
        "keywords": meta.keywords,
        "place_hint": meta.place_hint,
    }


def test_decode_and_xmp_golden():
    read = _seam()["read_photo_meta"]
    out = {}
    for label, records in DECODE_CASES.items():
        out[label] = _meta_view(read(_jpeg(_app13(records))))
    for label, (records, xmp) in XMP_CASES.items():
        segments = ([_app13(records)] if records else []) + [_app1_xmp(**xmp)]
        out[label] = _meta_view(read(_jpeg(*segments)))
    out["not an image"] = _meta_view(read(b"nope"))
    check_json("place_decode.json", out)


# --- place names: validated twice today ---------------------------------------------------

NAME_INPUTS = [
    "Tokyo",
    "  Tokyo  ",
    "New York",
    "St. John's",
    "Hakone-machi",
    "Köln",
    "Zürich",
    "東京",
    "Tokyo 東京",
    "Tokyo2",
    "T",
    "A.",
    "12",
    "",
    None,
    "x" * 60,
    "x" * 61,
    "-Tokyo",
    "Tokyo!",
    "Tokyo\nOsaka",
    "O'Hare",
]


def test_place_name_validators_golden():
    s = _seam()
    out = {}
    for value in NAME_INPUTS:
        out[repr(value)] = {
            "clean_place_part": _outcome(lambda v=value: s["clean_part"](v)),
            "usable_place_part": s["usable_part"](value),
        }
    check_json("place_names.json", out)


def test_report_place_text_and_default_flag():
    s = _seam()
    cases = [
        None,
        "x",
        {},
        {"city": "TOKYO", "country": "JAPAN", "source": "ai", "default": False},
        {"city": "", "country": "JAPAN", "default": True},
        {"city": "TOKYO"},
    ]
    check_json(
        "place_report_text.json",
        [
            {
                "place": c,
                "text": s["report_place_text"](c),
                "default": s["report_default_flag"](c),
            }
            for c in cases
        ],
    )
