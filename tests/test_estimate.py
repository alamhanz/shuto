from shuto import estimate as est
from shuto.local_models import ApiModel


def api(**kw):
    d = dict(
        provider="anthropic", model="m", default=True, usd_per_mtok_in=3.0,
        usd_per_mtok_out=15.0, image_tokens=1600, updated="2026-10-12",
    )  # fmt: skip
    d.update(kw)
    return ApiModel(**d)


class Rec:
    def __init__(self, s, basis="benchmark"):
        self.est_seconds_per_image = s
        self.basis = basis


def test_time_text():
    e = est.for_batch(42, "ollama", "gemma3:4b", recommendation=Rec(20))
    assert e.kind == "time" and e.seconds == 840
    assert e.text == "~14 min for 42 photos (benchmark, 20 s/photo)"


def test_time_concurrency_and_single():
    e = est.for_batch(1, "ollama", "m", recommendation=Rec(30, "heuristic"), concurrency=2)
    assert e.seconds == 15 and e.text == "~15 s for 1 photo (heuristic, 30 s/photo)"


def test_time_unknown():
    e = est.for_batch(5, "ollama", "m")
    assert e.seconds is None and "unknown" in e.text


def test_cost_text():
    e = est.for_batch(42, "anthropic", "m", api_model=api())
    per = (2800 * 3 + 600 * 15) / 1e6
    assert e.kind == "cost" and abs(e.usd - per * 42) < 1e-9
    assert e.text == f"~${per * 42:.2f} for 42 photos (pricing table 2026-10-12)"


def test_cost_tiny_and_unknown_and_fake():
    t = est.for_batch(1, "gemini", "m", api_model=api(usd_per_mtok_in=0.01, usd_per_mtok_out=0.01))
    assert t.text.startswith("<$0.01")
    assert "unknown" in est.for_batch(3, "nonesuch", "x").text
    assert est.for_batch(3, "fake", "x").seconds == 0


def test_smaller_image_costs_less():
    a = api()
    big = est.for_batch(10, "anthropic", "m", api_model=a, image_side=1568).usd
    small = est.for_batch(10, "anthropic", "m", api_model=a, image_side=784).usd
    assert small < big


def test_duration_format():
    assert est.format_duration(30) == "30 s"
    assert est.format_duration(600) == "10 min"
    assert est.format_duration(7200) == "2.0 h"
