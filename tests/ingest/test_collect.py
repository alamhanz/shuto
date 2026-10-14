from __future__ import annotations

import io
from datetime import datetime
from pathlib import Path

import pytest
from PIL import Image

from shuto.core.files import sha256_file
from shuto.ingest.collect import batch_id_for, group_near_duplicates, scan_folder
from shuto.ingest.imaging import (
    dhash,
    hamming,
    make_thumbnail,
    open_image,
    preview_jpeg,
    read_info,
    small_gray,
    to_jpeg_bytes,
)

from ..helpers import make_image, write_jpeg

SMALL = (900, 600)


def test_read_info_exif(tmp_path):
    p = write_jpeg(
        tmp_path / "a.jpg",
        size=SMALL,
        captured=datetime(2024, 12, 30, 8, 15, 0),
        camera="Canon EOS R6",
        gps=True,
    )
    info = read_info(p)
    assert (info.width, info.height) == SMALL
    assert info.captured == datetime(2024, 12, 30, 8, 15, 0)
    assert info.camera == "Canon EOS R6"
    assert info.has_gps and info.format == "JPEG"


def test_read_info_without_exif_and_orientation(tmp_path):
    info = read_info(write_jpeg(tmp_path / "a.jpg", size=SMALL, orientation=6))
    assert (info.width, info.height) == (600, 900)
    assert info.captured is None and info.camera is None and not info.has_gps


def test_sha256_and_hamming(tmp_path):
    p = write_jpeg(tmp_path / "a.jpg", size=SMALL)
    q = tmp_path / "b.jpg"
    q.write_bytes(p.read_bytes())
    assert sha256_file(p) == sha256_file(q)
    assert hamming(0b1011, 0b0001) == 2


def test_dhash_recompressed_close_and_different_far(tmp_path):
    original = make_image("sharp", (1600, 1200), seed=1)
    a = tmp_path / "a.jpg"
    original.save(a, "JPEG", quality=95)
    copy = tmp_path / "copy.jpg"
    original.resize((800, 600)).save(copy, "JPEG", quality=60)
    other = tmp_path / "other.jpg"
    make_image("sharp", (1600, 1200), seed=2).save(other, "JPEG", quality=95)

    def h(p):
        with open_image(p) as im:
            return dhash(small_gray(im, 256))

    assert hamming(h(a), h(copy)) <= 6
    assert hamming(h(a), h(other)) > 20
    assert 0 <= h(a) < 2**63


def test_thumbnail_and_previews(tmp_path):
    p = write_jpeg(tmp_path / "a.jpg", size=(1600, 800), orientation=6)
    thumb = make_thumbnail(p, tmp_path / "thumbs" / "x.jpg", 400)
    with Image.open(thumb) as im:
        assert im.size == (200, 400)
    assert not list((tmp_path / "thumbs").glob("*.tmp"))
    with Image.open(io.BytesIO(preview_jpeg(p, 300))) as im:
        assert max(im.size) == 300


def test_to_jpeg_bytes_keeps_exif(tmp_path):
    p = write_jpeg(
        tmp_path / "a.jpg", size=SMALL, captured=datetime(2025, 3, 4, 5, 6, 7), camera="Sony A7"
    )
    out = tmp_path / "out.jpg"
    out.write_bytes(to_jpeg_bytes(p, 90))
    info = read_info(out)
    assert info.captured == datetime(2025, 3, 4, 5, 6, 7)
    assert info.camera == "Sony A7"


def test_scan_folder_filters_sorts_and_skips_hidden(tmp_path):
    write_jpeg(tmp_path / "b.JPG", size=SMALL, seed=2)
    write_jpeg(tmp_path / "a.jpg", size=SMALL, seed=3)
    write_jpeg(tmp_path / "sub" / "c.jpeg", size=SMALL, seed=4)
    write_jpeg(tmp_path / ".hidden" / "d.jpg", size=SMALL, seed=5)
    write_jpeg(tmp_path / ".e.jpg", size=SMALL, seed=6)
    (tmp_path / "notes.txt").write_text("x")
    (tmp_path / "broken.jpg").write_bytes(b"not an image")

    errors: list[Path] = []
    items = scan_folder(tmp_path, [".jpg", "JPEG"], True, on_error=lambda p, e: errors.append(p))
    assert [i.path.name for i in items] == ["a.jpg", "b.JPG", "c.jpeg"]
    assert [p.name for p in errors] == ["broken.jpg"]
    assert all(len(i.sha256) == 64 for i in items)
    flat = scan_folder(tmp_path, [".jpg", ".jpeg"], recursive=False)
    assert [i.path.name for i in flat] == ["a.jpg", "b.JPG"]


def test_batch_id_iso_week_across_year_boundary(tmp_path):
    def item(day: datetime):
        folder = tmp_path / f"{day:%Y%m%d}"
        write_jpeg(folder / "x.jpg", size=(200, 200), captured=day)
        return scan_folder(folder, [".jpg"])[0]

    dec = item(datetime(2024, 12, 30, 12))
    jan = item(datetime(2025, 1, 5, 12))
    late = item(datetime(2021, 1, 3, 12))  # ISO week 53 of 2020
    assert batch_id_for(dec, "iso_week") == "2025-W01"
    assert batch_id_for(jan, "iso_week") == "2025-W01"
    assert batch_id_for(late, "iso_week") == "2020-W53"
    assert batch_id_for(dec, "month") == "2024-12"
    assert batch_id_for(dec, "day") == "2024-12-30"
    with pytest.raises(ValueError):
        batch_id_for(dec, "nope")


def test_batch_id_folder_and_mtime_fallback(tmp_path):
    p = write_jpeg(tmp_path / "trip" / "kyoto" / "a.jpg", size=(200, 200))
    (it,) = scan_folder(tmp_path, [".jpg"])
    assert batch_id_for(it, "folder", tmp_path) == "trip-kyoto"
    assert batch_id_for(it, "folder") == "kyoto"
    assert batch_id_for(it, "day") == datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d")


def test_group_near_duplicates(tmp_path):
    base = make_image("sharp", (1200, 900), seed=1)
    base.save(tmp_path / "a.jpg", "JPEG", quality=95)
    base.resize((600, 450)).save(tmp_path / "b.jpg", "JPEG", quality=50)
    make_image("sharp", (1200, 900), seed=7).save(tmp_path / "c.jpg", "JPEG", quality=95)
    items = scan_folder(tmp_path, [".jpg"])
    assert group_near_duplicates(items, 6) == [[0, 1]]


def test_heic_roundtrip_needs_extra(tmp_path):
    pillow_heif = pytest.importorskip("pillow_heif")
    pillow_heif.register_heif_opener()
    p = tmp_path / "a.heic"
    make_image("sharp", (400, 300)).save(p, "HEIF")
    (item,) = scan_folder(tmp_path, [".heic"])
    assert (item.info.width, item.info.height) == (400, 300)
