"""Synthetic test images (no real photos are committed)."""

from __future__ import annotations

import random
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageEnhance, ImageFilter

from shuto.ai import hardware

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


class FakeOllama:
    url = "http://127.0.0.1:11434"

    def __init__(self, pulled=(), running=True):
        self.pulled = list(pulled)
        self.running = running
        self.pull_calls: list[str] = []
        self.delete_calls: list[str] = []
        self.sizes: dict[str, int] = {}  # bytes, as Ollama reports them

    def version(self):
        if not self.running:
            raise OSError("down")
        return "0.0-test"

    def tags(self):
        return [{"name": n, "size": self.sizes.get(n, 0)} for n in self.pulled]

    def delete(self, tag):
        self.delete_calls.append(tag)
        if tag not in self.pulled:
            raise OSError("model not found")
        self.pulled.remove(tag)

    def pull(self, tag, on_progress=None):
        self.pull_calls.append(tag)
        if on_progress:
            on_progress({"status": "pulling", "total": 100, "completed": 100})

    def chat(self, *a, **k):  # pragma: no cover - benchmark uses a fake provider instead
        raise AssertionError("no network in tests")


def fake_profile(ram_gb=32.0, tier="cpu", gpu_gb=0.0):
    return hardware.HardwareProfile(
        os="linux",
        arch="x86_64",
        cpu="Test CPU",
        cores=8,
        ram_gb=ram_gb,
        gpus=[],
        apple_silicon=False,
        usable_gpu_gb=gpu_gb,
        tier=tier,
        fingerprint="test-fp",
    )
