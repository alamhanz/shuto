"""Image I/O helpers: metadata, hashing, thumbnails and JPEG conversion.

Originals are only ever read. Everything that produces bytes or files works on copies.
"""

from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageOps

_EXIF_IFD = 0x8769
_GPS_IFD = 0x8825
_TAG_DATETIME_ORIGINAL = 0x9003
_TAG_DATETIME = 0x0132
_TAG_MAKE = 0x010F
_TAG_MODEL = 0x0110
_TAG_ORIENTATION = 0x0112

_HASH_W, _HASH_H = 9, 7  # (9-1) * 7 = 56 bits: always fits a signed 64-bit SQLite integer
_heif_registered = False


@dataclass(frozen=True)
class ImageInfo:
    width: int
    height: int
    captured: datetime | None
    camera: str | None
    format: str | None
    has_gps: bool


def _register_heif() -> None:
    global _heif_registered
    if _heif_registered:
        return
    _heif_registered = True
    try:
        import pillow_heif
    except ImportError:
        return
    pillow_heif.register_heif_opener()


HEIC_SUFFIXES = (".heic", ".heif")
HEIC_NO_SUPPORT = "HEIC photo could not be read - install HEIC support"
HEIC_DAMAGED = "HEIC photo could not be read - the file may be damaged"


def heic_support() -> bool:
    """True when the optional pillow-heif package is installed."""
    try:
        import pillow_heif  # noqa: F401
    except ImportError:
        return False
    return True


def unreadable_message(path: str | Path, exc: BaseException | None = None) -> str | None:
    """The plain-words reason for a HEIC/HEIF file that cannot be opened; None for other files."""
    if Path(path).suffix.lower() not in HEIC_SUFFIXES:
        return None
    return HEIC_DAMAGED if heic_support() else HEIC_NO_SUPPORT


def open_image(path: str | Path | bytes) -> Image.Image:
    """Open lazily (no pixel decode yet). Accepts a path or encoded bytes."""
    _register_heif()
    if isinstance(path, (bytes, bytearray)):
        return Image.open(io.BytesIO(path))
    return Image.open(Path(path))


def _parse_exif_date(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    text = value.strip().strip("\x00")
    for fmt, size in (("%Y:%m:%d %H:%M:%S", 19), ("%Y-%m-%d %H:%M:%S", 19), ("%Y:%m:%d", 10)):
        try:
            return datetime.strptime(text[:size], fmt)
        except ValueError:
            continue
    return None


def read_info(path: str | Path | bytes) -> ImageInfo:
    """Dimensions (after EXIF orientation), capture time, camera and GPS presence."""
    with open_image(path) as im:
        width, height = im.size
        fmt = im.format
        try:
            exif = im.getexif()
        except Exception:
            exif = Image.Exif()
        if exif.get(_TAG_ORIENTATION) in (5, 6, 7, 8):
            width, height = height, width
        captured = None
        try:
            captured = _parse_exif_date(exif.get_ifd(_EXIF_IFD).get(_TAG_DATETIME_ORIGINAL))
        except Exception:
            captured = None
        if captured is None:
            captured = _parse_exif_date(exif.get(_TAG_DATETIME))
        make = str(exif.get(_TAG_MAKE) or "").strip().strip("\x00")
        model = str(exif.get(_TAG_MODEL) or "").strip().strip("\x00")
        if make and model and model.lower().startswith(make.lower()):
            camera = model
        else:
            camera = " ".join(p for p in (make, model) if p) or None
        try:
            has_gps = bool(exif.get_ifd(_GPS_IFD))
        except Exception:
            has_gps = False
    return ImageInfo(width, height, captured, camera, fmt, has_gps)


def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _oriented_rgb(path: str | Path | bytes) -> Image.Image:
    with open_image(path) as im:
        return ImageOps.exif_transpose(im).convert("RGB")


def small_gray(img: Image.Image, max_side: int = 1024) -> Image.Image:
    """Grayscale copy whose longest side is at most `max_side` (never upscaled)."""
    gray = img.convert("L")
    if max(gray.size) > max_side:
        gray.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
    return gray


def dhash(img: Image.Image) -> int:
    """Difference hash (56 bits, non-negative, fits SQLite INTEGER)."""
    g = img.convert("L").resize((_HASH_W, _HASH_H), Image.Resampling.LANCZOS)
    px = g.tobytes()
    bits = 0
    for y in range(_HASH_H):
        row = px[y * _HASH_W : (y + 1) * _HASH_W]
        for x in range(_HASH_W - 1):
            bits = (bits << 1) | (1 if row[x] > row[x + 1] else 0)
    return bits


def hamming(a: int, b: int) -> int:
    return (a ^ b).bit_count()


def make_thumbnail(path: str | Path, dest: str | Path, max_side: int = 400) -> Path:
    """Write a JPEG thumbnail (atomically) and return its path."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    im = _oriented_rgb(path)
    im.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
    tmp = dest.with_name(dest.name + ".tmp")
    with open(tmp, "wb") as fh:
        im.save(fh, "JPEG", quality=85)
    tmp.replace(dest)
    return dest


def to_jpeg_bytes(path: str | Path, quality: int = 95) -> bytes:
    """Re-encode as upright RGB JPEG, keeping EXIF (orientation reset) and ICC profile."""
    with open_image(path) as src:
        exif = src.getexif()
        icc = src.info.get("icc_profile")
        im = ImageOps.exif_transpose(src).convert("RGB")
    kwargs: dict[str, object] = {}
    if len(exif):
        exif[_TAG_ORIENTATION] = 1
        kwargs["exif"] = exif.tobytes()
    if icc:
        kwargs["icc_profile"] = icc
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=quality, **kwargs)
    return buf.getvalue()


def preview_jpeg(path_or_bytes: str | Path | bytes, max_side: int = 1024) -> bytes:
    """Downscaled upright JPEG (no metadata) for sending to a vision model."""
    im = _oriented_rgb(path_or_bytes)
    if max(im.size) > max_side:
        im.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=88)
    return buf.getvalue()
