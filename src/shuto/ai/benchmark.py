"""Benchmark of one model on this machine: a sample photo, two calls, cached per model+machine."""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from shuto.ai.catalog import SAMPLE_PATH
from shuto.ai.hardware import normalize_tag


def bench_key(model: str, fingerprint: str) -> str:
    return f"{normalize_tag(model)}@{fingerprint}"


def sample_jpeg_bytes(path: Path | None = None) -> bytes:
    """data/sample.jpg if present (swap it for a public-domain photo), else a synthetic one."""
    p = Path(path) if path else SAMPLE_PATH
    if p.exists():
        return p.read_bytes()
    return make_synthetic_sample()


def make_synthetic_sample(size: tuple[int, int] = (1024, 683)) -> bytes:
    import io
    import random

    from PIL import Image, ImageDraw

    w, h = size
    rnd = random.Random(7)
    img = Image.new("RGB", size)
    d = ImageDraw.Draw(img)
    horizon = int(h * 0.62)
    for y in range(horizon):  # sky gradient
        t = y / horizon
        d.line([(0, y), (w, y)], fill=(int(120 + 100 * t), int(170 + 60 * t), int(235 - 20 * t)))
    x = 0
    while x < w:  # buildings with windows
        bw, bh = rnd.randint(70, 150), rnd.randint(150, 330)
        col = tuple(rnd.randint(90, 190) for _ in range(3))
        d.rectangle([x, horizon - bh, x + bw, horizon], fill=col)
        for wy in range(horizon - bh + 15, horizon - 20, 34):
            for wx in range(x + 10, x + bw - 18, 26):
                d.rectangle([wx, wy, wx + 12, wy + 18], fill=(235, 225, 150))
        x += bw + rnd.randint(0, 8)
    d.rectangle([0, horizon, w, h], fill=(70, 72, 78))  # road
    for rx in range(20, w, 120):
        d.rectangle([rx, int(h * 0.82), rx + 60, int(h * 0.835)], fill=(230, 230, 230))
    d.ellipse([w - 160, 40, w - 90, 110], fill=(255, 245, 190))  # sun
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=82)
    return buf.getvalue()


def load_benchmarks(cache_path: Path) -> dict:
    try:
        data = json.loads(Path(cache_path).read_text(encoding="utf-8"))
        return dict(data.get("entries", {}))
    except (OSError, ValueError, AttributeError):
        return {}


def _save_benchmarks(cache_path: Path, entries: dict) -> None:
    cache_path = Path(cache_path)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = cache_path.with_suffix(cache_path.suffix + ".tmp")
    tmp.write_text(json.dumps({"version": 1, "entries": entries}, indent=2), encoding="utf-8")
    tmp.replace(cache_path)


def benchmark(provider: Any, sample_jpeg: bytes, cache_path: Path, fingerprint: str) -> dict:
    """Two calls on one image: cold (includes model load) then warm. Cached per model+machine.

    Provider connection errors (LLMError) propagate; bad JSON just sets ok_json False.
    """
    from shuto.ai.providers.base import LLMError, LLMRequest, describe_structured

    req = LLMRequest(
        image_jpeg=sample_jpeg,
        system="You are a stock photo metadata assistant.",
        prompt="Describe this image for a stock photo site: title, description, keywords.",
    )
    timings, ok = [], True
    for _ in range(2):
        t0 = time.monotonic()
        try:
            describe_structured(provider, req, retries=0)
        except LLMError as e:
            if "invalid output" not in str(
                e
            ):  # connection / auth / timeout: not a model quality issue
                raise
            ok = False
        except Exception:
            ok = False
        timings.append(time.monotonic() - t0)
    rec = {
        "model": provider.model,
        "fingerprint": fingerprint,
        "cold_s": round(timings[0], 2),
        "warm_s": round(timings[1], 2),
        "ok_json": ok,
        "at": datetime.now(UTC).isoformat(),
    }
    entries = load_benchmarks(cache_path)
    entries[bench_key(provider.model, fingerprint)] = rec
    _save_benchmarks(cache_path, entries)
    return rec
