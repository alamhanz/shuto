"""Synthetic test images (no real photos are committed)."""

from __future__ import annotations

import random
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageEnhance, ImageFilter

DEFAULT_SIZE = (2400, 1800)  # 4.32 MP


def texture(size: tuple[int, int] = DEFAULT_SIZE, seed: int = 1) -> Image.Image:
    """Mid-grey RGB image with a coarse random layout plus fine random detail."""
    rng = random.Random(seed)
    coarse = Image.new("RGB", (12, 9))
    coarse.putdata([tuple(rng.randrange(40, 216) for _ in range(3)) for _ in range(12 * 9)])
    base = coarse.resize(size, Image.Resampling.BICUBIC)
    detail = Image.effect_noise(size, 40).convert("RGB")
    return Image.blend(base, detail, 0.35)


def make_image(kind: str = "sharp", size: tuple[int, int] = DEFAULT_SIZE, seed: int = 1):
    img = texture(size, seed)
    if kind == "sharp":
        return img
    if kind == "blurred":
        return img.filter(ImageFilter.GaussianBlur(10))
    if kind == "dark":
        return ImageEnhance.Brightness(img).enhance(0.12)
    if kind == "bright":
        return ImageEnhance.Brightness(img).enhance(2.6)
    if kind == "small":
        return texture((800, 600), seed)
    raise ValueError(kind)


def exif_bytes(captured: datetime | None = None, camera: str | None = None, gps: bool = False):
    exif = Image.Exif()
    if camera:
        make, _, model = camera.partition(" ")
        exif[0x010F] = make
        exif[0x0110] = model or make
    if captured is not None:
        exif.get_ifd(0x8769)[0x9003] = captured.strftime("%Y:%m:%d %H:%M:%S")
    if gps:
        exif.get_ifd(0x8825)[1] = "N"
    return exif.tobytes()


def write_jpeg(
    path: Path,
    kind: str = "sharp",
    *,
    size: tuple[int, int] = DEFAULT_SIZE,
    seed: int = 1,
    captured: datetime | None = None,
    camera: str | None = None,
    gps: bool = False,
    quality: int = 92,
    orientation: int | None = None,
) -> Path:
    img = make_image(kind, size, seed)
    exif = Image.Exif()
    if captured is not None or camera or gps:
        exif = Image.Exif()
        exif.load(exif_bytes(captured, camera, gps))
    if orientation:
        exif[0x0112] = orientation
    path.parent.mkdir(parents=True, exist_ok=True)
    kwargs = {"exif": exif.tobytes()} if len(exif) else {}
    img.save(path, "JPEG", quality=quality, **kwargs)
    return path
