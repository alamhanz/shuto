import json
from pathlib import Path

import pytest

from shuto.ai import catalog as lm
from shuto.ai import hardware as hw
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
    recs = lm.recommend(prof("gpu_12", 12, 32), CAT, [])
    fits = [r.fits for r in recs]
    assert fits == sorted(fits, reverse=True)
    q = [r.model.quality for r in recs if r.fits]
    assert q == sorted(q, reverse=True)
    byt = {r.model.tag: r for r in recs}
    assert byt["gemma3:12b"].mode == "gpu" and byt["gemma3:12b"].fits
    assert byt["qwen3-vl:32b"].fits is False


def test_cpu_8gb_runs_only_small():
    recs = lm.recommend(prof("cpu", 0, 8), CAT, [])
    fitting = {r.model.tag for r in recs if r.fits}
    assert "moondream" in fitting and "gemma3:12b" not in fitting
    assert all(r.mode == "cpu" for r in recs)


def test_gpu_fit_uses_tier_estimate_with_fallback():
    recs = lm.recommend(prof("gpu_8", 8, 16), CAT, [])
    r = {x.model.tag: x for x in recs}["qwen3-vl:8b"]
    assert r.mode == "gpu" and r.est_seconds_per_image == 15  # gpu_8 falls back to gpu_6
    assert r.basis == "heuristic"


def test_benchmark_overrides_heuristic():
    p = prof("gpu_8", 8, 16)
    key = lm.bench_key("qwen3-vl:8b", p.fingerprint)
    b = {key: {"ok_json": True, "warm_s": 3.3}}
    r = {x.model.tag: x for x in lm.recommend(p, CAT, [], b)}["qwen3-vl:8b"]
    assert r.basis == "benchmark" and r.est_seconds_per_image == 3.3
    b[key]["ok_json"] = False
    r = {x.model.tag: x for x in lm.recommend(p, CAT, [], b)}["qwen3-vl:8b"]
    assert r.basis == "heuristic"


def test_top_models_is_eight_and_ranked_by_score():
    rows = lm.top_models(prof("gpu_12", 12, 32), CAT, [], disk_free_gb=100)
    assert len(rows) == 8 and [r.rank for r in rows] == list(range(1, 9))
    assert all(r.fits_hardware for r in rows)
    scores = [r.score for r in rows]
    assert scores == sorted(scores, reverse=True)
    assert rows[0].recommended and not rows[1].recommended
    d = rows[0].to_dict()
    assert {"tag", "size_gb", "est_seconds_per_image", "tier_label", "fits_disk"} <= d.keys()
    assert "quality" in d["tier_label"]


def test_top_models_pads_with_unrunnable_on_weak_machine():
    rows = lm.top_models(prof("cpu", 0, 6), CAT, [])
    assert len(rows) == 8
    flags = [r.fits_hardware for r in rows]
    assert flags == sorted(flags, reverse=True) and not all(flags)


def test_top_models_disk_flag_and_pulled():
    rows = lm.top_models(prof("gpu_12", 12, 32), CAT, ["gemma3:12b"], disk_free_gb=2)
    for r in rows:
        assert r.fits_disk == (r.pulled or r.size_gb + 1 <= 2)
    assert any(not r.fits_disk for r in rows)


def test_disk_advice_fits():
    m = next(x for x in CAT[0] if x.tag == "gemma3:4b")
    a = lm.disk_advice(m, Path("/x"), CAT[1], free=50)
    assert a.fits and not a.volumes and not a.api_options


def test_disk_advice_no_fit_lists_volumes_and_apis():
    m = next(x for x in CAT[0] if x.tag == "gemma3:27b")
    vols = [hw.Volume("E:\\", 400, 1000, "E:\\ollama\\models")]
    a = lm.disk_advice(m, Path("C:/o"), CAT[1], free=5, os_name="windows", volumes=vols)
    assert not a.fits and a.need_gb == 18.0
    assert a.volumes[0].mount == "E:\\" and a.volumes[0].env_hint.startswith("setx")
    costs = [o.usd_per_image for o in a.api_options]
    assert costs == sorted(costs) and {o.provider for o in a.api_options} >= {"anthropic", "gemini"}
    assert "another drive" in a.message
    assert a.to_dict()["api_options"][0]["text"].startswith("~$")


def test_disk_advice_no_volumes_message_and_pulled_ok():
    m = next(x for x in CAT[0] if x.tag == "gemma3:27b")
    a = lm.disk_advice(m, Path("/x"), CAT[1], free=5, volumes=[])
    assert not a.fits and "No other drive" in a.message
    assert lm.disk_advice(m, Path("/x"), CAT[1], free=5, pulled=True).fits


def test_resolve_model_auto_requires_explicit_choice(tmp_path):
    cp = tmp_path / "choice.json"
    assert lm.resolve_model("gemma3:4b", cp) == "gemma3:4b"
    with pytest.raises(lm.ModelChoiceRequired):
        lm.resolve_model("auto", cp)
    lm.save_choice(cp, "qwen3-vl:8b")
    assert lm.resolve_model("auto", cp) == "qwen3-vl:8b"
    cp.write_text("garbage")
    with pytest.raises(lm.ModelChoiceRequired):
        lm.resolve_model("auto", cp)


def test_sample_image_exists_and_small():
    b = lm.sample_jpeg_bytes()
    assert b[:2] == b"\xff\xd8" and len(b) < 200_000
    assert lm.make_synthetic_sample((64, 48))[:2] == b"\xff\xd8"
    assert lm.sample_jpeg_bytes(lm.DATA_DIR / "missing.jpg")[:2] == b"\xff\xd8"


def test_benchmark_roundtrip(tmp_path):
    cache = tmp_path / "sub" / "bench.json"
    prov = FakeProvider("gemma3:4b", response=GOOD)
    rec = lm.benchmark(prov, lm.sample_jpeg_bytes(), cache, "fpA")
    assert len(prov.calls) == 2 and rec["model"] == "gemma3:4b" and rec["fingerprint"] == "fpA"
    assert rec["ok_json"] is True
    assert {"cold_s", "warm_s", "at"} <= rec.keys()
    assert lm.load_benchmarks(cache)[lm.bench_key("gemma3:4b", "fpA")] == rec
    lm.benchmark(FakeProvider("moondream", response=GOOD), b"x", cache, "fpA")
    lm.benchmark(prov, b"x", cache, "fpB")
    assert len(lm.load_benchmarks(cache)) == 3
    assert json.loads(cache.read_text())["version"] == 1


def test_benchmark_bad_json_and_llm_error(tmp_path):
    cache = tmp_path / "b.json"
    rec = lm.benchmark(FakeProvider("m", response="not json at all"), b"x", cache, "f")
    assert rec["ok_json"] is False

    def boom(req):
        raise LLMError("down")

    with pytest.raises(LLMError):
        lm.benchmark(FakeProvider("m", response=boom), b"x", cache, "f")


def test_load_benchmarks_missing(tmp_path):
    assert lm.load_benchmarks(tmp_path / "nope.json") == {}
