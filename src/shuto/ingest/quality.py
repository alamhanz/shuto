"""Full-resolution quality metrics (Shutterstock reviewers look at 100% zoom). Pure, Pillow only.

Phase 1 is warn-only: the numbers are stored and shown, nothing is culled because of them.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from typing import Any

from PIL import Image, ImageChops, ImageFilter, ImageOps, ImageStat

from shuto.core.exif_tags import EXIF_IFD

QUALITY_VERSION = 1  # bump when a metric changes; callers re-measure older results

_TAG_DIGITAL_ZOOM = 0xA404
_LAPLACIAN = ImageFilter.Kernel((3, 3), [0, 1, 0, 1, -4, 1, 0, 1, 0], scale=1, offset=128)
_IMMERKAER = ImageFilter.Kernel((3, 3), [1, -2, 1, -2, 4, -2, 1, -2, 1], scale=1, offset=128)
_DETAIL_TILE = 256
_NOISE_TILE = 64
_FLAT_SHARE = 0.25  # lowest share of tiles (by Laplacian variance) counted as flat
_FLAT_LUMA = (20.0, 235.0)
_DARK_LUMA = (20.0, 70.0)
_NEUTRAL_LUMA = (60, 230)
_NEUTRAL_CHROMA = 14  # per-axis box inside the radius-20 circle around grey
_CAST_SIZE = 1024
_CAST_MIN_SHIFT = 6.0  # |mean Cb/Cr - 128| above this counts as a cast
_CAST_MIN_SHARE = 0.05


@dataclass(frozen=True)
class QualityThresholds:
    min_detail: float = 400.0  # p90 tile Laplacian variance at full res (provisional)
    min_detail_ratio: float = 0.25  # detail / old 1024-px sharpness (provisional)
    max_dark_noise: float = 6.0  # record-only in v1 (provisional)
    max_digital_zoom: float = 1.05


@dataclass(frozen=True)
class Problem:
    code: str  # "soft_full_size" | "digital_zoom" | "noisy_shadows" | "colour_cast"
    text: str  # plain words for a non-technical owner
    shown: bool  # False = recorded for calibration only, not shown to the owner
    fixable: bool  # a phase-2 fix exists (shrink/brighten/tint/straighten)


@dataclass(frozen=True)
class QualityReport:
    version: int
    detail_p50: float
    detail_p90: float
    detail_ratio: float
    best_tile: tuple[float, float, float, float]  # x, y, w, h as fractions of the upright image
    flat_noise: float
    dark_noise: float | None
    chroma_noise: float
    colour_cast: tuple[float, float]
    neutral_share: float
    digital_zoom: float | None
    megapixels: float
    problems: list[Problem] = field(default_factory=list)


def _percentile(values: list[float], pct: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    pos = (len(ordered) - 1) * pct
    lo = math.floor(pos)
    hi = min(lo + 1, len(ordered) - 1)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (pos - lo)


def _tiles(width: int, height: int, size: int) -> list[tuple[int, int, int, int]]:
    """Full tiles only; an image smaller than one tile is a single tile."""
    if width < size or height < size:
        return [(0, 0, width, height)]
    return [
        (x, y, x + size, y + size)
        for y in range(0, height - size + 1, size)
        for x in range(0, width - size + 1, size)
    ]


def _noise(
    gray: Image.Image, lap_gray: Image.Image, noise_img: Image.Image
) -> tuple[float, float | None]:
    rows = []
    for box in _tiles(*gray.size, _NOISE_TILE):
        luma = ImageStat.Stat(gray.crop(box)).mean[0]
        lap_var = ImageStat.Stat(lap_gray.crop(box)).var[0]
        sigma = ImageStat.Stat(noise_img.crop(box)).stddev[0] / 6.0
        rows.append((lap_var, luma, sigma))
    candidates = [r for r in rows if _FLAT_LUMA[0] <= r[1] <= _FLAT_LUMA[1]]
    candidates.sort(key=lambda r: r[0])
    flat = candidates[: max(1, math.ceil(len(candidates) * _FLAT_SHARE))] if candidates else []
    flat_noise = statistics.median(r[2] for r in flat) if flat else 0.0
    dark = [r[2] for r in rows if _DARK_LUMA[0] <= r[1] <= _DARK_LUMA[1]]
    return flat_noise, (statistics.median(dark) if dark else None)


def _chroma_noise(cb: Image.Image) -> float:
    noise_img = cb.filter(_IMMERKAER)
    lap = cb.filter(_LAPLACIAN)
    rows = []
    for box in _tiles(*cb.size, _NOISE_TILE):
        rows.append(
            (ImageStat.Stat(lap.crop(box)).var[0], ImageStat.Stat(noise_img.crop(box)).stddev[0])
        )
    rows.sort(key=lambda r: r[0])
    flat = rows[: max(1, math.ceil(len(rows) * _FLAT_SHARE))]
    return statistics.median(r[1] for r in flat) / 6.0


def _cast(img: Image.Image) -> tuple[tuple[float, float], float]:
    """Mean Cb/Cr shift of near-neutral pixels and the share of such pixels."""
    small = img.convert("RGB")
    if max(small.size) > _CAST_SIZE:
        small.thumbnail((_CAST_SIZE, _CAST_SIZE), Image.Resampling.BILINEAR)
    y, cb, cr = small.convert("YCbCr").split()
    lo, hi = _NEUTRAL_LUMA
    near = [255 if abs(v - 128) <= _NEUTRAL_CHROMA else 0 for v in range(256)]
    mid = [255 if lo <= v <= hi else 0 for v in range(256)]
    mask = ImageChops.multiply(ImageChops.multiply(cb.point(near), cr.point(near)), y.point(mid))
    count = mask.histogram()[255]
    if count == 0:
        return (0.0, 0.0), 0.0
    cb_shift = ImageStat.Stat(cb, mask=mask).mean[0] - 128.0
    cr_shift = ImageStat.Stat(cr, mask=mask).mean[0] - 128.0
    return (cb_shift, cr_shift), count / (small.width * small.height)


def _digital_zoom(exif: Image.Exif | None) -> float | None:
    if exif is None:
        return None
    try:
        raw = exif.get_ifd(EXIF_IFD).get(_TAG_DIGITAL_ZOOM)
        value = float(raw) if raw is not None else None
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    if value is None or not math.isfinite(value) or value <= 0:
        return None
    return value


def measure(
    img: Image.Image,
    small_sharpness: float,
    exif: Image.Exif | None,
    t: QualityThresholds | None = None,
) -> QualityReport:
    """Measure `img` at full resolution. `small_sharpness` is the old 1024-px Laplacian score."""
    t = t or QualityThresholds()
    img = ImageOps.exif_transpose(img)
    width, height = img.size
    gray = img.convert("L")

    lap = gray.filter(_LAPLACIAN)
    boxes = _tiles(width, height, _DETAIL_TILE)
    variances = [ImageStat.Stat(lap.crop(box)).var[0] for box in boxes]
    best = boxes[max(range(len(variances)), key=variances.__getitem__)]
    best_tile = (
        best[0] / width,
        best[1] / height,
        (best[2] - best[0]) / width,
        (best[3] - best[1]) / height,
    )
    p50 = _percentile(variances, 0.5)
    p90 = _percentile(variances, 0.9)
    ratio = p90 / small_sharpness if small_sharpness > 0 else 0.0

    flat_noise, dark_noise = _noise(gray, lap, gray.filter(_IMMERKAER))
    del lap
    chroma_noise = _chroma_noise(img.convert("YCbCr").getchannel("Cb"))
    cast, neutral_share = _cast(img)
    zoom = _digital_zoom(exif)
    megapixels = width * height / 1_000_000

    problems: list[Problem] = []
    if p90 < t.min_detail or ratio < t.min_detail_ratio:
        problems.append(
            Problem(
                "soft_full_size",
                "Looks soft when zoomed to 100% (Shutterstock checks at 100%).",
                shown=True,
                fixable=True,
            )
        )
    if zoom is not None and zoom > t.max_digital_zoom:
        problems.append(
            Problem(
                "digital_zoom",
                f"Taken with digital zoom ({zoom:.1f}x), so fine detail is often smeared.",
                shown=True,
                fixable=True,
            )
        )
    if dark_noise is not None and dark_noise > t.max_dark_noise:
        problems.append(
            Problem(
                "noisy_shadows",
                "The dark areas look grainy when zoomed in.",
                shown=False,
                fixable=False,
            )
        )
    if neutral_share >= _CAST_MIN_SHARE and max(abs(cast[0]), abs(cast[1])) > _CAST_MIN_SHIFT:
        problems.append(
            Problem(
                "colour_cast",
                "The whites and greys have a colour tint.",
                shown=False,
                fixable=True,
            )
        )

    return QualityReport(
        version=QUALITY_VERSION,
        detail_p50=p50,
        detail_p90=p90,
        detail_ratio=ratio,
        best_tile=best_tile,
        flat_noise=flat_noise,
        dark_noise=dark_noise,
        chroma_noise=chroma_noise,
        colour_cast=cast,
        neutral_share=neutral_share,
        digital_zoom=zoom,
        megapixels=megapixels,
        problems=problems,
    )


def to_dict(r: QualityReport) -> dict[str, Any]:
    """JSON-safe form, stored under cull_json["quality"]."""
    return {
        "version": r.version,
        "detail_p50": r.detail_p50,
        "detail_p90": r.detail_p90,
        "detail_ratio": r.detail_ratio,
        "best_tile": list(r.best_tile),
        "flat_noise": r.flat_noise,
        "dark_noise": r.dark_noise,
        "chroma_noise": r.chroma_noise,
        "colour_cast": list(r.colour_cast),
        "neutral_share": r.neutral_share,
        "digital_zoom": r.digital_zoom,
        "megapixels": r.megapixels,
        "problems": [
            {"code": p.code, "text": p.text, "shown": p.shown, "fixable": p.fixable}
            for p in r.problems
        ],
    }
