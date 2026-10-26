import json
import time

from PIL import Image, ImageFilter, ImageStat

from shuto.ingest import cull, quality
from shuto.ingest.imaging import small_gray
from tests.helpers import make_image


def _measure(img, sharp=1000.0, exif=None, t=None):
    return quality.measure(img, sharp, exif, t)


def _codes(report):
    return {p.code for p in report.problems}


def _checker(size):
    w, h = size
    img = Image.new("L", size)
    img.putdata([255 if ((x // 2) + (y // 2)) % 2 else 0 for y in range(h) for x in range(w)])
    return img.convert("RGB")


def test_sharp_image_passes():
    r = _measure(_checker((1200, 900)))
    assert r.detail_p90 > 400
    assert "soft_full_size" not in _codes(r)
    assert r.version == quality.QUALITY_VERSION


def test_blurred_image_fails_soft():
    r = _measure(_checker((1200, 900)).filter(ImageFilter.GaussianBlur(2)))
    assert "soft_full_size" in _codes(r)
    assert next(p for p in r.problems if p.code == "soft_full_size").shown


def test_upscaled_image_fails_on_ratio():
    small = make_image("sharp", (700, 500))
    big = small.resize((2100, 1500), Image.Resampling.BICUBIC)
    lap = small_gray(big, 1024).filter(cull._LAPLACIAN)
    old = float(ImageStat.Stat(lap).var[0])
    r = _measure(big, old, t=quality.QualityThresholds(min_detail=0.0))
    assert r.detail_ratio < 0.25
    assert "soft_full_size" in _codes(r)


def test_noise_rises_with_added_noise():
    base = Image.new("RGB", (800, 600), (120, 120, 120))
    levels = []
    for sigma in (0, 4, 10):
        noise = Image.effect_noise(base.size, sigma).convert("RGB") if sigma else base
        img = Image.blend(base, noise, 1.0) if sigma else base
        levels.append(_measure(img).flat_noise)
    assert levels[0] < levels[1] < levels[2]


def test_blue_tint_gives_cast():
    grey = Image.new("RGB", (600, 400), (128, 128, 128))
    r_, g_, b_ = grey.split()
    tinted = Image.merge("RGB", (r_, g_, b_.point(lambda v: v + 20)))
    r = _measure(tinted)
    assert r.colour_cast[0] > 6
    assert "colour_cast" in _codes(r)
    assert not next(p for p in r.problems if p.code == "colour_cast").shown
    neutral = _measure(grey)
    assert "colour_cast" not in _codes(neutral)


def test_digital_zoom_flags_and_invalid_values():
    img = _checker((800, 600))
    exif = Image.Exif()
    exif.get_ifd(0x8769)[0xA404] = 1.4
    r = _measure(img, exif=exif)
    assert r.digital_zoom == 1.4
    assert "digital_zoom" in _codes(r)
    for bad in (0, float("nan")):
        e = Image.Exif()
        e.get_ifd(0x8769)[0xA404] = bad
        assert _measure(img, exif=e).digital_zoom is None
    assert _measure(img, exif=None).digital_zoom is None


def test_best_tile_uses_upright_fractions():
    img = Image.new("RGB", (1024, 512), (128, 128, 128))
    img.paste(_checker((256, 256)), (768, 0))
    r = _measure(img)
    assert r.best_tile == (0.75, 0.0, 0.25, 0.5)


def test_tiny_image_is_one_tile():
    r = _measure(_checker((100, 80)))
    assert r.best_tile == (0.0, 0.0, 1.0, 1.0)
    assert r.dark_noise is None or r.dark_noise >= 0


def test_to_dict_round_trips_json():
    r = _measure(_checker((600, 400)))
    data = quality.to_dict(r)
    assert json.loads(json.dumps(data)) == data


def test_twelve_megapixel_is_fast():
    img = make_image("sharp", (4000, 3000))
    start = time.perf_counter()
    _measure(img)
    assert time.perf_counter() - start < 1.5  # loose bound for slow CI


def test_evaluate_adds_quality_without_changing_ok(tmp_path):
    from tests.helpers import write_jpeg

    p = write_jpeg(tmp_path / "a.jpg")
    res = cull.evaluate(p)
    assert res.ok
    assert res.quality["version"] == quality.QUALITY_VERSION
    assert isinstance(res.warnings, list)


def test_grid_stats_match_imagestat():
    img = make_image("sharp", (700, 530)).convert("L")
    means, variances = quality._grid_stats(img, 64)
    boxes = quality._tiles(*img.size, 64)
    assert len(means) == len(variances) == len(boxes) == (700 // 64) * (530 // 64)
    for box, m, v in zip(boxes, means, variances, strict=True):
        stat = ImageStat.Stat(img.crop(box))
        assert abs(m - stat.mean[0]) < 0.01
        assert abs(v - stat.var[0]) < 0.01 * max(stat.var[0], 1.0)
