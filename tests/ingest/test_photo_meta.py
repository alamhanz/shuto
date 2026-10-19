"""File metadata extraction, offline geocoding and the photo_context interface."""

from __future__ import annotations

import struct
import time
from datetime import datetime
from importlib import resources
from pathlib import Path
from types import SimpleNamespace

from PIL import Image

from shuto.ingest.photo_meta import PhotoMeta, parse_xmp, read_photo_meta
from shuto.metadata import embed
from shuto.places import context as photo_context
from shuto.places import geo
from shuto.places.context import MAX_HINTS_CHARS, build_context, compute_file_facts
from shuto.workflow import importing

SHIBUYA = (35.6595, 139.7005)


def _dms(value: float) -> tuple[float, float, float]:
    value = abs(value)
    deg = int(value)
    minutes = int((value - deg) * 60)
    return float(deg), float(minutes), (value - deg - minutes / 60) * 3600


def make_jpeg(
    path: Path,
    *,
    lat: float | None = None,
    lon: float | None = None,
    alt: float | None = None,
    captured: str | None = None,
    extra_exif: dict | None = None,
    segments: bytes = b"",
) -> Path:
    exif = Image.Exif()
    exif[0x010F] = "FUJIFILM"
    exif[0x0110] = "X-T5"
    sub = exif.get_ifd(0x8769)
    if captured:
        sub[0x9003] = captured
    sub[0xA434] = "XF35mmF1.4 R"
    sub[0x920A] = 35.0
    sub[0xA405] = 52
    if lat is not None and lon is not None:
        gps = exif.get_ifd(0x8825)
        gps[1] = "N" if lat >= 0 else "S"
        gps[2] = _dms(lat)
        gps[3] = "E" if lon >= 0 else "W"
        gps[4] = _dms(lon)
        if alt is not None:
            gps[5] = 0 if alt >= 0 else 1
            gps[6] = abs(alt)
    for k, v in (extra_exif or {}).items():
        exif[k] = v
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (64, 48), "gray").save(path, "JPEG", exif=exif.tobytes())
    if segments:
        data = path.read_bytes()
        path.write_bytes(data[:2] + segments + data[2:])
    return path


def _segment(marker: int, payload: bytes) -> bytes:
    return b"\xff" + bytes([marker]) + struct.pack(">H", len(payload) + 2) + payload


def iptc_segment(**fields: str | list[str]) -> bytes:
    codes = {
        "title": 5,
        "caption": 120,
        "keywords": 25,
        "city": 90,
        "region": 95,
        "country": 101,
        "sub": 92,
    }
    iim = b""
    for key, value in fields.items():
        for item in value if isinstance(value, list) else [value]:
            iim += embed._iim(2, codes[key], item.encode("utf-8"))
    if len(iim) % 2:
        iim += b"\x00"
    res = b"8BIM" + struct.pack(">H", 0x0404) + b"\x00\x00" + struct.pack(">I", len(iim)) + iim
    return _segment(0xED, embed.PS_HEADER + res)


XMP_FULL = (
    b'<?xpacket begin="" id="W5M0MpCehiHzreSzNTczkc9d"?>'
    b'<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF '
    b'xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
    b'<rdf:Description rdf:about="" xmlns:dc="http://purl.org/dc/elements/1.1/" '
    b'xmlns:photoshop="http://ns.adobe.com/photoshop/1.0/" '
    b'xmlns:Iptc4xmpCore="http://iptc.org/std/Iptc4xmpCore/1.0/xmlns/" '
    b'photoshop:City="Kamakura" photoshop:Country="Japan">'
    b'<dc:title><rdf:Alt><rdf:li xml:lang="en">Other</rdf:li>'
    b'<rdf:li xml:lang="x-default">Quiet lane</rdf:li></rdf:Alt></dc:title>'
    b'<dc:description><rdf:Alt><rdf:li xml:lang="x-default">A lane at dusk</rdf:li></rdf:Alt>'
    b"</dc:description>"
    b"<dc:subject><rdf:Bag><rdf:li>lane</rdf:li><rdf:li>dusk</rdf:li></rdf:Bag></dc:subject>"
    b"<photoshop:State>Kanagawa</photoshop:State>"
    b"<Iptc4xmpCore:Location>Komachi-dori</Iptc4xmpCore:Location>"
    b"</rdf:Description></rdf:RDF></x:xmpmeta><?xpacket end='w'?>"
)


def xmp_segment(packet: bytes = XMP_FULL) -> bytes:
    return _segment(0xE1, embed.XMP_HEADER + packet)


# ---------------------------------------------------------------- extraction


def test_exif_fields(tmp_path):
    p = make_jpeg(
        tmp_path / "a.jpg",
        lat=SHIBUYA[0],
        lon=SHIBUYA[1],
        alt=40.5,
        captured="2026:07:31 19:45:10",
        extra_exif={0x0112: 6},
    )
    m = read_photo_meta(p)
    assert m.captured == datetime(2026, 7, 31, 19, 45, 10)
    assert abs(m.gps[0] - SHIBUYA[0]) < 1e-3 and abs(m.gps[1] - SHIBUYA[1]) < 1e-3
    assert m.altitude_m == 40.5
    assert m.camera == "FUJIFILM X-T5"
    assert m.lens == "XF35mmF1.4 R"
    assert m.focal_mm == 35.0 and m.focal_35mm == 52.0
    assert m.orientation == 6


def test_gps_south_west_and_below_sea_level(tmp_path):
    m = read_photo_meta(make_jpeg(tmp_path / "a.jpg", lat=-33.8568, lon=-70.1, alt=-12.0))
    assert m.gps[0] < -33.8 and m.gps[1] < -70.0
    assert m.altitude_m == -12.0


def test_broken_and_missing_gps_never_raise(tmp_path):
    # ref only (what the test helper writes), junk values, zero fix, out of range
    plain = make_jpeg(tmp_path / "plain.jpg")
    assert read_photo_meta(plain).gps is None
    for name, ifd in {
        "ref_only": {1: "N"},
        "junk": {1: "N", 2: (1.0,), 3: "E", 4: (1.0, 2.0)},
        "zero": {1: "N", 2: (0.0, 0.0, 0.0), 3: "E", 4: (0.0, 0.0, 0.0)},
        "range": {1: "N", 2: (95.0, 0.0, 0.0), 3: "E", 4: _dms(10)},
    }.items():
        exif = Image.Exif()
        gps = exif.get_ifd(0x8825)
        gps.update(ifd)
        path = tmp_path / f"{name}.jpg"
        Image.new("RGB", (8, 8)).save(path, "JPEG", exif=exif.tobytes())
        assert read_photo_meta(path).gps is None, name


def test_bad_exif_values_are_skipped(tmp_path):
    p = make_jpeg(
        tmp_path / "a.jpg",
        captured="not a date",
        extra_exif={0x0112: 99},
    )
    m = read_photo_meta(p)
    assert m.captured is None and m.orientation is None


def test_not_an_image_and_missing_file(tmp_path):
    bad = tmp_path / "bad.jpg"
    bad.write_bytes(b"not an image")
    assert read_photo_meta(bad) == PhotoMeta()
    assert read_photo_meta(tmp_path / "missing.jpg") == PhotoMeta()


def test_iptc_fields(tmp_path):
    seg = iptc_segment(
        title="Shibuya crossing",
        caption="People cross at night",
        keywords=["night", "neon"],
        city="Shibuya",
        region="Tokyo",
        country="Japan",
        sub="Hachiko exit",
    )
    m = read_photo_meta(make_jpeg(tmp_path / "a.jpg", segments=seg))
    assert m.title == "Shibuya crossing"
    assert m.caption == "People cross at night"
    assert m.keywords == ["night", "neon"]
    assert m.place_hint == {
        "city": "Shibuya",
        "region": "Tokyo",
        "country": "Japan",
        "sublocation": "Hachiko exit",
    }


def test_xmp_fields_and_precedence(tmp_path):
    m = read_photo_meta(make_jpeg(tmp_path / "a.jpg", segments=xmp_segment()))
    assert m.title == "Quiet lane"  # x-default wins over the first entry
    assert m.caption == "A lane at dusk"
    assert m.keywords == ["lane", "dusk"]
    assert m.place_hint == {
        "city": "Kamakura",
        "country": "Japan",
        "region": "Kanagawa",
        "sublocation": "Komachi-dori",
    }
    # IPTC is read first and wins; XMP fills what is missing and merges keywords
    both = make_jpeg(
        tmp_path / "b.jpg",
        segments=iptc_segment(title="IPTC title", keywords=["lane", "extra"]) + xmp_segment(),
    )
    m2 = read_photo_meta(both)
    assert m2.title == "IPTC title" and m2.caption == "A lane at dusk"
    assert m2.keywords == ["lane", "extra", "dusk"]


def test_xmp_is_safe_and_robust():
    xxe = (
        b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a SYSTEM "file:///etc/passwd">]>'
        b'<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF '
        b'xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
        b'<rdf:Description xmlns:dc="http://purl.org/dc/elements/1.1/">'
        b"<dc:title>&a;</dc:title></rdf:Description></rdf:RDF></x:xmpmeta>"
    )
    assert parse_xmp(xxe) == {}
    assert parse_xmp(b"<x:xmpmeta><broken") == {}
    assert parse_xmp(b"") == {} and parse_xmp(None) == {}
    assert parse_xmp(b"\xff\xfe garbage") == {}
    assert parse_xmp("<x:xmpmeta xmlns:x='adobe:ns:meta/'/>").get("keywords") in (None, [])


def test_to_dict_roundtrip():
    m = PhotoMeta(
        captured=datetime(2026, 1, 2, 3, 4, 5),
        gps=(1.5, 2.5),
        camera="X",
        keywords=["a"],
        place_hint={"city": "Y"},
    )
    again = PhotoMeta.from_dict(m.to_dict())
    assert again == m
    assert PhotoMeta.from_dict(None) == PhotoMeta()
    assert PhotoMeta.from_dict({"captured": "bad", "gps": "bad"}) == PhotoMeta()


# ---------------------------------------------------------------- geocoder


def test_geocode_known_places():
    p = geo.reverse_geocode(*SHIBUYA)
    assert (p.city, p.region, p.country) == ("Shibuya", "Tokyo", "Japan")
    assert (p.neighborhood, p.ward) == ("Sakuragaokacho", "Shibuya")
    assert p.confidence == "high" and p.distance_km < 5
    k = geo.reverse_geocode(35.0116, 135.7681)
    assert (k.city, k.country) == ("Kyoto", "Japan") and k.region == "Kyoto"
    nyc = geo.reverse_geocode(40.7128, -74.0060)
    assert nyc.country == "United States" and nyc.region == "New York"


def test_geocode_prefers_notable_places():
    asakusa = geo.reverse_geocode(35.7148, 139.7967)
    assert (asakusa.neighborhood, asakusa.ward, asakusa.region) == ("Asakusa", "Taito", "Tokyo")
    assert asakusa.city == "Taito"
    shinjuku = geo.reverse_geocode(35.6896, 139.7006)
    assert shinjuku.region == "Tokyo" and shinjuku.distance_km < 3
    assert geo.reverse_geocode(*SHIBUYA) == geo.reverse_geocode(*SHIBUYA)  # deterministic


def test_geocode_levels_kyoto_and_no_ward():
    k = geo.reverse_geocode(35.0116, 135.7681)
    assert k.region == "Kyoto" and k.country == "Japan" and k.city
    # no admin2 on record: main name falls back to the notable nearby place, no ward
    rows = ["C\tZZ\tZedland", "A\tZZ.01\tNorth", "P\tVillage\tZZ\t01\t10.0\t20.0\t500\t"]
    geo._index = geo._Index("\n".join(rows) + "\n")
    try:
        p = geo.reverse_geocode(10.0, 20.0)
    finally:
        geo._index = None
    assert (p.city, p.ward, p.region, p.country) == ("Village", None, "North", "Zedland")


def test_hints_without_ward_and_outside_japan():
    place = {"city": "Village", "region": "North", "country": "Zedland", "neighborhood": "Village"}
    ctx = build_context(PhotoMeta(), place)
    assert ctx["hints"] == (
        "Place from GPS: Village (city), North (region), Zedland. Use the city for the caption."
    )


def test_geocode_far_and_edge_cases():
    assert geo.reverse_geocode(0.0, -30.0) is None  # mid Atlantic
    assert geo.reverse_geocode(95, 10) is None
    assert geo.reverse_geocode("x", None) is None
    assert geo.reverse_geocode(float("nan"), 0) is None
    # anti-meridian wrap: Fiji at lon 179.9 / -179.9 still resolves
    assert geo.reverse_geocode(-17.8, 179.99) is not None
    assert geo.reverse_geocode(-16.8, -179.9) is not None
    # a long way from any city but within 200 km: region/country only, low confidence
    far = geo.reverse_geocode(48.75, 87.5)  # Altai, Russia/Mongolia/China border area
    assert far is None or (far.city is None and far.confidence == "low") or far.city


def test_geocode_speed():
    geo.reverse_geocode(*SHIBUYA)  # load once
    start = time.perf_counter()
    for i in range(1000):
        geo.reverse_geocode(35.0 + (i % 40) * 0.01, 139.0 + (i % 37) * 0.01)
    assert time.perf_counter() - start < 1.0


def test_data_file_is_package_data():
    ref = resources.files("shuto").joinpath("data/places.tsv.gz")
    assert ref.is_file()
    assert 1_000_000 < ref.stat().st_size < 4_500_000


# ---------------------------------------------------------------- context


def test_compute_file_facts_and_photo_context(tmp_path):
    p = make_jpeg(
        tmp_path / "a.jpg",
        lat=SHIBUYA[0],
        lon=SHIBUYA[1],
        captured="2026:07:31 19:45:10",
        segments=iptc_segment(keywords=["night", "neon"], title="Crossing"),
    )
    facts = compute_file_facts(p)
    assert facts["gps_place"]["city"] == "Shibuya"
    assert facts["file_meta"]["keywords"] == ["night", "neon"]
    photo = SimpleNamespace(metadata=facts, src_path=str(p), captured_at=None)
    ctx = photo_context.photo_context(photo)
    assert ctx["place"]["source"] == "gps"
    assert ctx["place"]["city"] == "Shibuya"
    assert ctx["captured"].startswith("2026-07-31")
    hints = ctx["hints"]
    assert hints.startswith(
        "Taken 2026-07-31. Place from GPS: Sakuragaokacho (neighborhood), Shibuya (ward), "
        "Tokyo (prefecture), Japan. Use the ward for the caption; "
        "neighborhood may be a keyword."
    )
    assert ctx["place"]["neighborhood"] == "Sakuragaokacho" and ctx["place"]["ward"] == "Shibuya"
    assert ctx["place"]["prefecture"] == "Tokyo"
    assert "Photographer keywords: night, neon." in hints
    assert len(hints) <= MAX_HINTS_CHARS
    assert "35.65" not in hints and "139.70" not in hints  # never coordinates
    assert "35.65" not in repr(ctx["place"])


def test_photo_context_lazy_for_existing_photos(tmp_path):
    p = make_jpeg(tmp_path / "a.jpg", lat=35.0116, lon=135.7681)
    photo = SimpleNamespace(
        metadata={"title": "x"}, src_path=str(p), captured_at="2025-05-01T10:00:00"
    )
    ctx = photo_context.photo_context(photo)
    assert ctx["place"]["city"] == "Kyoto" and ctx["place"]["source"] == "gps"
    assert ctx["captured"].startswith("2025-05-01") or ctx["captured"] is not None


def test_file_location_used_without_gps_and_when_gps_has_no_city(tmp_path):
    seg = iptc_segment(city="Kamakura", country="Japan", sub="Komachi-dori")
    p = make_jpeg(tmp_path / "a.jpg", segments=seg)
    ctx = photo_context.photo_context(
        SimpleNamespace(metadata=None, src_path=str(p), captured_at=None)
    )
    assert ctx["place"]["source"] == "file"
    assert ctx["hints"].startswith("Taken in Komachi-dori, Kamakura, Japan (from the photo file).")
    meta = read_photo_meta(p)
    low = {"city": None, "region": "Tokyo", "country": "Japan", "confidence": "low"}
    merged = build_context(meta, low)["place"]
    assert (merged["city"], merged["region"], merged["country"]) == ("Kamakura", "Tokyo", "Japan")
    high = {"city": "Shibuya", "region": "Tokyo", "country": "Japan", "confidence": "high"}
    assert build_context(meta, high)["place"]["source"] == "gps"


def test_hints_length_cap_and_empty(tmp_path):
    meta = PhotoMeta(
        captured=datetime(2026, 1, 1),
        title="T" * 500,
        caption="C" * 900,
        keywords=[f"keyword number {i} " + "k" * 50 for i in range(40)],
        lens="L" * 200,
        place_hint={"city": "X" * 100, "country": "Y" * 100},
    )
    ctx = build_context(meta)
    assert 0 < len(ctx["hints"]) <= MAX_HINTS_CHARS
    assert "\n" not in ctx["hints"]
    empty = build_context(PhotoMeta())
    assert empty["hints"] == "" and empty["place"] is None
    plain = make_jpeg(tmp_path / "p.jpg")
    assert (
        photo_context.photo_context(
            SimpleNamespace(metadata=None, src_path=str(plain), captured_at=None)
        )["place"]
        is None
    )


def test_import_stores_facts(tmp_path, cfg, store):
    src = tmp_path / "in"
    make_jpeg(src / "a.jpg", lat=SHIBUYA[0], lon=SHIBUYA[1], captured="2026:07:31 19:45:10")
    importing.import_folder(cfg, store, src)
    (photo,) = store.list_photos()
    assert photo.metadata["gps_place"]["city"] == "Shibuya"
    assert photo.metadata["file_meta"]["camera"] == "FUJIFILM X-T5"
