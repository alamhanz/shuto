import json
from pathlib import Path

import pytest

from shuto.ai import benchmark as ai_benchmark
from shuto.ai import catalog as lm
from shuto.ai import hardware as hw
from shuto.ai import model_choice as ai_choice
from shuto.ai import rating as ai_rating
from shuto.ai import recommend as ai_recommend
from shuto.ai.providers.base import LLMError
from shuto.ai.providers.fake import FakeProvider


def prof(tier, usable, ram, fp="fp1", apple=False):
    return hw.HardwareProfile("linux", "x86_64", "cpu", 8, ram, [], apple, usable, tier, fp)


CAT = lm.load_catalog()
GOOD = {
    "title": "A street",
    "description": "A quiet street with buildings.",
    "keywords": [f"k{i}" for i in range(30)],
    "categories": ["Buildings/Landmarks", "Transportation"],
}


def test_catalog_loads_and_is_wide():
    local, api = CAT
    assert len(local) >= 12
    assert {a.provider for a in api} == {"anthropic", "gemini", "bedrock"}
    assert {"claude-sonnet-5-5", "claude-haiku-5-5"} <= {a.model for a in api}
    assert all(a.updated for a in api)
    for m in local:
        assert m.est_s.get("cpu") and 1 <= m.quality <= 5 and m.size_gb > 0


def test_extra_catalog_overrides(tmp_path):
    p = tmp_path / "x.yaml"
    p.write_text(
        "local:\n  - {tag: moondream, size_gb: 9, quality: 2, est_s: {cpu: 1}}\n"
        "  - {tag: mine:1b, size_gb: 1, quality: 2, est_s: {cpu: 5}}\n"
    )
    local, _ = lm.load_catalog(p)
    d = {m.tag: m for m in local}
    assert d["moondream"].size_gb == 9 and "mine:1b" in d


def test_recommend_order_and_fit():
    recs = ai_recommend.recommend(prof("gpu_12", 12, 32), CAT, [])
    fits = [r.fits for r in recs]
    assert fits == sorted(fits, reverse=True)
    q = [r.model.quality for r in recs if r.fits]
    assert q == sorted(q, reverse=True)
    byt = {r.model.tag: r for r in recs}
    assert byt["gemma3:12b"].mode == "gpu" and byt["gemma3:12b"].fits
    assert byt["qwen3-vl:32b"].fits is False


def test_cpu_8gb_runs_only_small():
    recs = ai_recommend.recommend(prof("cpu", 0, 8), CAT, [])
    fitting = {r.model.tag for r in recs if r.fits}
    assert "moondream" in fitting and "gemma3:12b" not in fitting
    assert all(r.mode == "cpu" for r in recs)


def test_gpu_fit_uses_tier_estimate_with_fallback():
    recs = ai_recommend.recommend(prof("gpu_8", 8, 16), CAT, [])
    r = {x.model.tag: x for x in recs}["qwen2.5vl:7b"]
    assert r.mode == "gpu" and r.est_seconds_per_image == 14  # gpu_8 falls back to gpu_6
    assert r.basis == "heuristic"
    # a model that thinks anyway: the estimate is multiplied (15 s x 4)
    slow = {x.model.tag: x for x in recs}["qwen3-vl:8b"]
    assert slow.est_seconds_per_image == 60


def test_benchmark_overrides_heuristic():
    p = prof("gpu_8", 8, 16)
    key = ai_benchmark.bench_key("qwen3-vl:8b", p.fingerprint)
    b = {key: {"ok_json": True, "warm_s": 3.3}}
    r = {x.model.tag: x for x in ai_recommend.recommend(p, CAT, [], b)}["qwen3-vl:8b"]
    assert r.basis == "benchmark" and r.est_seconds_per_image == 3.3
    b[key]["ok_json"] = False
    r = {x.model.tag: x for x in ai_recommend.recommend(p, CAT, [], b)}["qwen3-vl:8b"]
    assert r.basis == "heuristic"


def test_top_models_is_eight_ranked_by_rating_then_score():
    rows = ai_recommend.top_models(prof("gpu_12", 12, 32), CAT, [], disk_free_gb=100)
    assert len(rows) == 8 and [r.rank for r in rows] == list(range(1, 9))
    assert all(r.fits_hardware for r in rows)
    order = [ai_rating.RATING_ORDER[r.rating] for r in rows]
    assert order == sorted(order)
    for first, second in zip(rows, rows[1:], strict=False):
        if first.rating == second.rating:
            assert first.score >= second.score
    assert rows[0].rating == "good" and rows[0].recommended and not rows[1].recommended
    assert len(ai_recommend.top_models(prof("gpu_12", 12, 32), CAT, [], n=None)) == len(CAT[0])
    d = rows[0].to_dict()
    assert {"tag", "size_gb", "est_seconds_per_image", "tier_label", "fits_disk"} <= d.keys()
    assert "quality" in d["tier_label"]


def test_top_models_pads_with_unrunnable_on_weak_machine():
    rows = ai_recommend.top_models(prof("cpu", 0, 6), CAT, [])
    assert len(rows) == 8
    flags = [r.fits_hardware for r in rows]
    assert flags == sorted(flags, reverse=True) and not all(flags)


def test_top_models_disk_flag_and_pulled():
    rows = ai_recommend.top_models(prof("gpu_12", 12, 32), CAT, ["gemma3:12b"], disk_free_gb=2)
    for r in rows:
        assert r.fits_disk == (r.pulled or r.size_gb + 1 <= 2)
    assert any(not r.fits_disk for r in rows)


def test_disk_advice_fits():
    m = next(x for x in CAT[0] if x.tag == "gemma3:4b")
    a = ai_recommend.disk_advice(m, Path("/x"), CAT[1], free=50)
    assert a.fits and not a.volumes and not a.api_options


def test_disk_advice_no_fit_lists_volumes_and_apis():
    m = next(x for x in CAT[0] if x.tag == "gemma3:27b")
    vols = [hw.Volume("E:\\", 400, 1000, "E:\\ollama\\models")]
    a = ai_recommend.disk_advice(m, Path("C:/o"), CAT[1], free=5, os_name="windows", volumes=vols)
    assert not a.fits and a.need_gb == 18.0
    assert a.volumes[0].mount == "E:\\" and a.volumes[0].env_hint.startswith("setx")
    costs = [o.usd_per_image for o in a.api_options]
    assert costs == sorted(costs) and {o.provider for o in a.api_options} >= {"anthropic", "gemini"}
    assert "another drive" in a.message
    assert a.to_dict()["api_options"][0]["text"].startswith("~$")


def test_disk_advice_no_volumes_message_and_pulled_ok():
    m = next(x for x in CAT[0] if x.tag == "gemma3:27b")
    a = ai_recommend.disk_advice(m, Path("/x"), CAT[1], free=5, volumes=[])
    assert not a.fits and "No other drive" in a.message
    assert ai_recommend.disk_advice(m, Path("/x"), CAT[1], free=5, pulled=True).fits


def test_resolve_model_auto_requires_explicit_choice(tmp_path):
    cp = tmp_path / "choice.json"
    assert ai_choice.resolve_model("gemma3:4b", cp) == "gemma3:4b"
    with pytest.raises(ai_choice.ModelChoiceRequired):
        ai_choice.resolve_model("auto", cp)
    ai_choice.save_choice(cp, "qwen3-vl:8b")
    assert ai_choice.resolve_model("auto", cp) == "qwen3-vl:8b"
    cp.write_text("garbage")
    with pytest.raises(ai_choice.ModelChoiceRequired):
        ai_choice.resolve_model("auto", cp)


def test_sample_image_exists_and_small():
    b = ai_benchmark.sample_jpeg_bytes()
    assert b[:2] == b"\xff\xd8" and len(b) < 200_000
    assert ai_benchmark.make_synthetic_sample((64, 48))[:2] == b"\xff\xd8"
    assert ai_benchmark.sample_jpeg_bytes(lm.DATA_DIR / "missing.jpg")[:2] == b"\xff\xd8"


def test_benchmark_roundtrip(tmp_path):
    cache = tmp_path / "sub" / "bench.json"
    prov = FakeProvider("gemma3:4b", response=GOOD)
    rec = ai_benchmark.benchmark(prov, ai_benchmark.sample_jpeg_bytes(), cache, "fpA")
    assert len(prov.calls) == 2 and rec["model"] == "gemma3:4b" and rec["fingerprint"] == "fpA"
    assert rec["ok_json"] is True
    assert {"cold_s", "warm_s", "at"} <= rec.keys()
    assert ai_benchmark.load_benchmarks(cache)[ai_benchmark.bench_key("gemma3:4b", "fpA")] == rec
    ai_benchmark.benchmark(FakeProvider("moondream", response=GOOD), b"x", cache, "fpA")
    ai_benchmark.benchmark(prov, b"x", cache, "fpB")
    assert len(ai_benchmark.load_benchmarks(cache)) == 3
    assert json.loads(cache.read_text())["version"] == 1


def test_benchmark_bad_json_and_llm_error(tmp_path):
    cache = tmp_path / "b.json"
    rec = ai_benchmark.benchmark(FakeProvider("m", response="not json at all"), b"x", cache, "f")
    assert rec["ok_json"] is False

    def boom(req):
        raise LLMError("down")

    with pytest.raises(LLMError):
        ai_benchmark.benchmark(FakeProvider("m", response=boom), b"x", cache, "f")


def test_load_benchmarks_missing(tmp_path):
    assert ai_benchmark.load_benchmarks(tmp_path / "nope.json") == {}
