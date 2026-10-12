"""Technical culling: resolution, sharpness and exposure checks. Pure, no I/O besides reading."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from PIL import ImageFilter, ImageOps, ImageStat

from .imaging import open_image, small_gray

_LAPLACIAN = ImageFilter.Kernel((3, 3), [0, 1, 0, 1, -4, 1, 0, 1, 0], scale=1, offset=128)
_SHADOW_MAX = 5  # luma <= this counts as clipped shadow
_HIGHLIGHT_MIN = 250  # luma >= this counts as clipped highlight


@dataclass(frozen=True)
class CullThresholds:
    min_megapixels: float = 4.0
    min_sharpness: float = 80.0
    mean_luma_range: tuple[float, float] = (35.0, 220.0)
    max_clipped_shadows_pct: float = 8.0
    max_clipped_highlights_pct: float = 5.0


@dataclass(frozen=True)
class CullResult:
    ok: bool
    reasons: list[str] = field(default_factory=list)
    megapixels: float = 0.0
    sharpness: float = 0.0
    mean_luma: float = 0.0
    clipped_shadows_pct: float = 0.0
    clipped_highlights_pct: float = 0.0


def evaluate(path_or_bytes: str | Path | bytes, t: CullThresholds | None = None) -> CullResult:
    t = t or CullThresholds()
    with open_image(path_or_bytes) as im:
        im = ImageOps.exif_transpose(im)
        megapixels = im.width * im.height / 1_000_000
        gray = small_gray(im, 1024)

    sharpness = float(ImageStat.Stat(gray.filter(_LAPLACIAN)).var[0])
    mean_luma = float(ImageStat.Stat(gray).mean[0])
    hist = gray.histogram()
    total = max(sum(hist), 1)
    shadows = 100.0 * sum(hist[: _SHADOW_MAX + 1]) / total
    highlights = 100.0 * sum(hist[_HIGHLIGHT_MIN:]) / total

    reasons: list[str] = []
    if megapixels < t.min_megapixels:
        reasons.append(f"too small: {megapixels:.1f} MP (minimum {t.min_megapixels:g} MP)")
    if sharpness < t.min_sharpness:
        reasons.append(f"blurry: sharpness {sharpness:.0f} (minimum {t.min_sharpness:g})")
    lo, hi = t.mean_luma_range
    if mean_luma < lo:
        reasons.append(f"underexposed: mean brightness {mean_luma:.0f} (minimum {lo:g})")
    elif mean_luma > hi:
        reasons.append(f"overexposed: mean brightness {mean_luma:.0f} (maximum {hi:g})")
    if shadows > t.max_clipped_shadows_pct:
        reasons.append(
            f"clipped shadows: {shadows:.1f}% of pixels (maximum {t.max_clipped_shadows_pct:g}%)"
        )
    if highlights > t.max_clipped_highlights_pct:
        reasons.append(
            f"clipped highlights: {highlights:.1f}% of pixels "
            f"(maximum {t.max_clipped_highlights_pct:g}%)"
        )
    return CullResult(
        ok=not reasons,
        reasons=reasons,
        megapixels=megapixels,
        sharpness=sharpness,
        mean_luma=mean_luma,
        clipped_shadows_pct=shadows,
        clipped_highlights_pct=highlights,
    )


def pick_best(results: list[CullResult]) -> int:
    """Index of the best candidate: sharpest, then largest. Raises on an empty list."""
    if not results:
        raise ValueError("pick_best needs at least one result")
    return max(range(len(results)), key=lambda i: (results[i].sharpness, results[i].megapixels))
