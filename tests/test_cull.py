from __future__ import annotations

import io

import pytest

from shuto.cull import CullResult, CullThresholds, evaluate, pick_best

from .helpers import make_image, write_jpeg


def test_sharp_passes_and_blurred_fails(tmp_path):
    sharp = evaluate(write_jpeg(tmp_path / "s.jpg", "sharp"))
    blurred = evaluate(write_jpeg(tmp_path / "b.jpg", "blurred"))
    assert sharp.ok, sharp.reasons
    assert sharp.sharpness > blurred.sharpness * 5
    assert not blurred.ok
    assert any(r.startswith("blurry") for r in blurred.reasons)


def test_small_flagged_with_readable_reason(tmp_path):
    res = evaluate(write_jpeg(tmp_path / "t.jpg", "small"))
    assert not res.ok
    assert any("too small" in r and "MP" in r for r in res.reasons)
    assert res.megapixels == pytest.approx(0.48)


def test_dark_and_bright_flagged(tmp_path):
    dark = evaluate(write_jpeg(tmp_path / "d.jpg", "dark"))
    bright = evaluate(write_jpeg(tmp_path / "l.jpg", "bright"))
    assert any("underexposed" in r for r in dark.reasons)
    assert any("overexposed" in r for r in bright.reasons)
    assert dark.mean_luma < 35 < 220 < bright.mean_luma
    assert bright.clipped_highlights_pct > dark.clipped_highlights_pct


def test_accepts_bytes_and_custom_thresholds():
    buf = io.BytesIO()
    make_image("sharp").save(buf, "JPEG")
    assert evaluate(buf.getvalue()).ok
    strict = evaluate(buf.getvalue(), CullThresholds(min_megapixels=10))
    assert not strict.ok


def test_orientation_counts_in_megapixels(tmp_path):
    res = evaluate(write_jpeg(tmp_path / "o.jpg", "sharp", orientation=6))
    assert res.ok


def test_pick_best_prefers_sharpest_then_largest():
    a = CullResult(True, sharpness=100, megapixels=10)
    b = CullResult(True, sharpness=300, megapixels=5)
    c = CullResult(True, sharpness=300, megapixels=8)
    assert pick_best([a, b, c]) == 2
    with pytest.raises(ValueError):
        pick_best([])
