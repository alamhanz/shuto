"""Rating local models for a computer: Good, OK or Not recommended."""

from __future__ import annotations

import dataclasses

import pytest

from shuto.ai import catalog as lm
from shuto.ai import rating
from shuto.ai.benchmark import bench_key
from shuto.ai.hardware import GPU, HardwareProfile
from shuto.ai.rating import Facts, Measured, Observed

LOCAL = {m.tag: m for m in lm.load_catalog()[0]}


def machine(ram, usable, tier, gpus=(), apple=False, os_="linux"):
    return HardwareProfile(
        os_, "x86_64", "cpu", 8, ram, list(gpus), apple, usable, tier, f"fp-{tier}-{ram}"
    )


# the owner's laptop: 8 GB RTX 5060, an AMD iGPU that Windows lists by name only, 31 GB RAM
OWNER = machine(
    31.1,
    7.96,
    "gpu_8",
    [GPU("nvidia", "RTX 5060 Laptop", 7.96, 7.0), GPU("amd", "Radeon 890M")],
    os_="windows",
)
GPU_4 = machine(16, 4.0, "gpu_6", [GPU("nvidia", "GTX", 4.0, 3.8)])
CPU_16 = machine(16, 0.0, "cpu")
APPLE_16 = machine(16, 11.2, "apple_16", [GPU("apple", "M2", 16, 11.2)], apple=True)
UNKNOWN_GPU = machine(32, 0.0, "cpu", [GPU("amd", "Radeon RX 7800")], os_="windows")


def rate(tag, profile, facts=None, bench=None):
    return rating.rate(LOCAL[tag], profile, facts, bench)


def ratings(profile, facts=None):
    return {t: rate(t, profile, (facts or {}).get(t)).rating for t in LOCAL}


def calls(n, seconds, tokens=300, ok=True, error=None, model="m"):
    return [
        {"model": model, "ok": ok, "seconds": seconds, "output_tokens": tokens, "error": error}
        for _ in range(n)
    ]


# ------------------------------------------------------------------ the owner's computer


def test_owner_machine_by_estimate_alone():
    got = ratings(OWNER)
    assert got["minicpm-v:8b"] == "good"
    assert got["gemma3:4b"] == "good"
    assert got["qwen2.5vl:3b"] == "good"
    assert got["qwen3-vl:8b"] == "not_recommended"  # thinks anyway and only just fits
    assert got["qwen3.5:4b"] == "ok"  # numbers not confirmed until it is measured
    assert got["qwen2.5vl:7b"] == "ok"  # only just fits
    assert got["qwen3-vl:4b"] == "ok"  # thinks anyway: never Good
    assert got["llama3.2-vision:11b"] == got["gemma3:12b"] == "not_recommended"
    assert got["moondream"] == "ok"  # fast but basic


def test_owner_numbers():
    minicpm = rate("minicpm-v:8b", OWNER)
    assert (minicpm.memory, minicpm.mode, minicpm.seconds) == ("fits", "gpu", 13)
    assert minicpm.need_gb == pytest.approx(5.87, abs=0.01)
    big = rate("qwen3-vl:8b", OWNER)
    assert big.memory == "tight" and big.thinks and big.seconds == 60  # 15 s x 4
    assert "only just fits" in big.reason


def test_owner_calls_replayed():
    """minicpm measured about 8 s; qwen3-vl failed 5 of 7; qwen3.5:4b answered fast."""
    rows = (
        calls(6, 8.4, 300, model="minicpm-v:8b")
        + calls(2, 80.0, 2295, model="qwen3-vl:8b")
        + calls(5, None, None, ok=False, model="qwen3-vl:8b")
        + calls(3, 7.0, 250, model="qwen3.5:4b")
    )
    measured = rating.measured_from_calls(rows)
    facts = {t: Facts(measured=measured[rating.normalize_tag(t)]) for t in LOCAL if t in measured}
    got = {t: rate(t, OWNER, f) for t, f in facts.items()}
    assert got["minicpm-v:8b"].rating == "good" and got["minicpm-v:8b"].basis == "measured"
    assert got["qwen3-vl:8b"].rating == "not_recommended"
    assert "stopped without an answer" in got["qwen3-vl:8b"].reason
    assert got["qwen3.5:4b"].rating == "good" and not got["qwen3.5:4b"].thinks


def test_qwen35_is_ok_until_three_fast_calls():
    def with_calls(n):
        m = Measured(n, 0, 7.0, 250)
        return rate("qwen3.5:4b", OWNER, Facts(measured=m)).rating

    assert [with_calls(n) for n in (0, 1, 2, 3)] == ["ok", "ok", "ok", "good"]


def test_unlisted_model_is_ok_until_measured():
    m = dataclasses.replace(LOCAL["gemma3:4b"], tag="mine:4b", est_s={}, mem_gb=0.0)
    unmeasured = rating.rate(m, OWNER)
    assert (
        unmeasured.rating == "ok" and unmeasured.basis == "unknown" and unmeasured.seconds is None
    )
    assert "no speed figures" in unmeasured.reason
    measured = rating.rate(m, OWNER, Facts(measured=Measured(3, 0, 9.0, 200)))
    assert measured.rating == "good"


# ------------------------------------------------------------------ other computers


def test_four_gb_graphics_card_has_no_good_model():
    got = ratings(GPU_4)
    assert "good" not in got.values()
    assert got["gemma3:4b"] == "ok" and got["qwen2.5vl:3b"] == "ok"  # too big for the card
    assert got["moondream"] == "ok"
    assert rate("gemma3:4b", GPU_4).mode == "cpu"


def test_cpu_only_16gb():
    got = ratings(CPU_16)
    assert "good" not in got.values()
    assert got["gemma3:4b"] == "ok" and got["qwen2.5vl:3b"] == "ok"
    assert got["minicpm-v:8b"] == "not_recommended"
    assert got["qwen3-vl:4b"] == "not_recommended"


def test_apple_16gb():
    got = ratings(APPLE_16)
    assert got["minicpm-v:8b"] == got["gemma3:4b"] == got["qwen2.5vl:7b"] == "good"
    assert got["llama3.2-vision:11b"] == got["gemma3:12b"] == "ok"
    assert got["qwen3-vl:8b"] == "not_recommended"


def test_unknown_graphics_card_is_rated_as_the_processor():
    assert rating.budget(UNKNOWN_GPU) == ("cpu", 28.0, True)
    assert rating.budget(CPU_16)[2] is False  # no card at all is not "unknown"
    assert rate("gemma3:4b", UNKNOWN_GPU).mode == "cpu"


def test_cards_are_never_added_together():
    two = machine(64, 8.0, "gpu_8", [GPU("nvidia", "A", 8.0, 7.0), GPU("nvidia", "B", 8.0, 7.0)])
    assert rating.budget(two) == ("gpu", 7.5, False)
    assert rating.budget(OWNER) == ("gpu", pytest.approx(7.46), False)
    assert rate("gemma3:12b", two).memory == "too_big"  # 16 GB of cards would have fitted it


def test_model_larger_than_the_ram_is_not_recommended():
    r = rate("gemma3:27b", OWNER)
    assert (r.memory, r.rating) == ("no_room", "not_recommended")
    assert "more memory" in r.reason
    assert rate("gemma3:4b", machine(8, 0.0, "cpu")).memory == "no_room"  # RAM guess of 8 GB


# ------------------------------------------------------------------ memory need


def test_memory_need_order_loaded_then_list_then_estimate():
    m = LOCAL["minicpm-v:8b"]
    loaded = Observed(False, 5.0, 8192)
    assert rating.memory_need_gb(m, 8192, loaded) == 5.0
    assert rating.memory_need_gb(m, 8192) == pytest.approx(6.3 / rating.GIB_PER_GB)
    unlisted = dataclasses.replace(m, mem_gb=0.0)
    expected = (m.size_gb + 0.12 * m.params_b + rating.WORK_GB) / rating.GIB_PER_GB
    assert rating.memory_need_gb(unlisted, 8192) == pytest.approx(expected)
    assert rating.memory_need_gb(m, 12288, loaded) > 5.0  # more room asked: more memory
    assert rating.memory_need_gb(m, 12288) > rating.memory_need_gb(m, 8192)


def test_a_model_that_thinks_anyway_is_sized_with_extra_room():
    plain = dataclasses.replace(LOCAL["qwen3-vl:8b"], always_thinks=False)
    thinking = LOCAL["qwen3-vl:8b"]
    assert rating.rate(thinking, OWNER).need_gb > rating.rate(plain, OWNER).need_gb


# ------------------------------------------------------------------ speed bands


def custom(est):
    return dataclasses.replace(
        LOCAL["gemma3:4b"], tag="speedy", est_s={"gpu_6": est, "cpu": 60}, verify=False
    )


def band(est, measured_s):
    m = custom(est)
    facts = Facts(measured=Measured(5, 0, measured_s, 300)) if measured_s is not None else None
    return rating.rate(m, OWNER, facts).rating


def test_speed_limits():
    assert band(20, None) == "good" and band(21, None) == "ok"
    assert band(60, None) == "ok" and band(61, None) == "not_recommended"


def test_a_measured_speed_must_pass_a_limit_by_20_percent():
    # estimated Good (limit 20 s): stays Good up to 24 s
    assert band(15, 24.0) == "good"
    assert band(15, 24.5) == "ok"
    # estimated OK: becomes Good only below 16 s
    assert band(30, 16.0) == "ok"
    assert band(30, 15.5) == "good"
    # estimated OK (limit 60 s): stays OK up to 72 s
    assert band(30, 72.0) == "ok"
    assert band(30, 73.0) == "not_recommended"


def test_thinks_anyway_has_a_stricter_limit_and_is_never_good():
    m = dataclasses.replace(custom(5), always_thinks=True)
    est = rating.rate(m, OWNER)  # 5 s x 4 = 20 s
    assert (est.rating, est.seconds) == ("ok", 20)
    slow = rating.rate(dataclasses.replace(custom(11), always_thinks=True), OWNER)
    assert slow.seconds == 44 and slow.rating == "not_recommended"  # above 40 s
    fast = rating.rate(m, OWNER, Facts(measured=Measured(5, 0, 4.0, 3000)))
    assert fast.rating == "ok"  # even measured fast


def test_measured_beats_benchmark_beats_estimate():
    m = custom(10)
    key = bench_key(m.tag, OWNER.fingerprint)
    bench = {key: {"ok_json": True, "warm_s": 22.0}}
    assert rating.rate(m, OWNER, bench=bench).basis == "benchmark"
    r = rating.rate(m, OWNER, Facts(measured=Measured(3, 0, 12.0, 200)), bench)
    assert (r.basis, r.seconds) == ("measured", 12.0)
    bench[key]["ok_json"] = False
    assert rating.rate(m, OWNER, bench=bench).basis == "estimate"


def test_a_missing_tier_estimate_falls_back_to_cpu_over_eight():
    m = dataclasses.replace(custom(5), est_s={"cpu": 80})
    r = rating.rate(m, OWNER)
    assert r.seconds == 10 and r.rating == "good"


# ------------------------------------------------------------------ failures, spill, tight


def test_three_failures_in_the_window_is_not_recommended():
    m = Measured(7, 3, 8.0, 300)
    r = rate("minicpm-v:8b", OWNER, Facts(measured=m))
    assert r.rating == "not_recommended" and "stopped without an answer" in r.reason
    assert rate("minicpm-v:8b", OWNER, Facts(measured=Measured(8, 2, 8.0, 300))).rating == "good"


def test_only_failures_the_model_causes_count():
    caused = ["[model] Ollama timed out", "[model] thinks before it answers and ran out of room"]
    not_caused = [
        "[connection] Ollama is not running at http://x",
        "[missing] Ollama HTTP 404: model not found",
        "[cancelled] Stopped by you",
        "[other] Ollama HTTP 400: bad request",
    ]
    for err in caused:
        assert rating.caused_by_model(err)
    for err in not_caused:
        assert not rating.caused_by_model(err)
    rows = [c for e in not_caused for c in calls(1, None, None, ok=False, error=e)] + calls(4, 8.0)
    (m,) = rating.measured_from_calls(rows).values()
    assert (m.failures, m.ok_calls) == (0, 4)
    rows = [c for e in caused for c in calls(1, None, None, ok=False, error=e)] + calls(4, 8.0)
    (m,) = rating.measured_from_calls(rows).values()
    assert (m.failures, m.ok_calls) == (2, 4)


def test_ollama_stopped_does_not_make_the_model_not_recommended():
    rows = calls(3, None, None, ok=False, error="[connection] Ollama is not running") + calls(
        4, 8.0, model="minicpm-v:8b"
    )
    rows = [{**r, "model": "minicpm-v:8b"} for r in rows]
    measured = rating.measured_from_calls(rows)
    assert rate("minicpm-v:8b", OWNER, Facts(measured=measured["minicpm-v:8b"])).rating == "good"


def test_failures_logged_before_error_kinds_are_judged_by_their_text():
    old_stop = "Ollama is not running at http://127.0.0.1:11434; start it"
    assert not rating.caused_by_model(old_stop)
    assert not rating.caused_by_model("Ollama HTTP 404: model not found (model not pulled?)")
    assert not rating.caused_by_model("The AI connection was lost (ConnectionResetError)")
    assert rating.caused_by_model("Ollama timed out at http://127.0.0.1:11434")
    assert rating.caused_by_model(None)


def test_output_tokens_are_counted_per_try():
    # a normal model (300 tokens an answer) whose photos often needed 2 more tries
    rows = [
        {
            "model": "m",
            "ok": True,
            "seconds": 9.0,
            "output_tokens": 900,
            "retries": 2,
            "error": None,
        }
        for _ in range(4)
    ]
    (m,) = rating.measured_from_calls(rows).values()
    assert m.median_out == 300
    r = rate("minicpm-v:8b", OWNER, Facts(measured=Measured(4, 0, 9.0, m.median_out)))
    assert not r.thinks and r.rating == "good"
    # a real thinker still shows: 2400 tokens in a single try
    rows = [{**rows[0], "output_tokens": 2400, "retries": 0}] * 4
    (m,) = rating.measured_from_calls(rows).values()
    assert m.median_out == 2400


def test_only_the_last_ten_calls_count_and_the_median_is_used():
    rows = calls(7, 8.0) + calls(3, 60.0) + calls(5, None, None, ok=False)  # newest first
    (m,) = rating.measured_from_calls(rows).values()
    assert m.ok_calls + m.failures == 10 and m.failures == 0
    assert m.median_s == 8.0 and m.median_out == 300
    rows = calls(3, 8.0, model="a:1b") + calls(3, 9.0, model="B:2b")
    assert set(rating.measured_from_calls(rows)) == {"a:1b", "B:2b"}


def test_a_cold_first_load_does_not_move_the_median():
    rows = calls(4, 8.0) + calls(1, 62.0)
    (m,) = rating.measured_from_calls(rows).values()
    assert m.median_s == 8.0


def test_fewer_than_three_ok_calls_are_not_used():
    r = rate("minicpm-v:8b", OWNER, Facts(measured=Measured(2, 0, 200.0, 300)))
    assert r.basis == "estimate" and r.rating == "good"


def test_tight_memory_caps_at_ok_unless_measured_fast():
    assert rate("qwen2.5vl:7b", OWNER).rating == "ok"
    fast = Facts(measured=Measured(5, 0, 12.0, 300))
    assert rate("qwen2.5vl:7b", OWNER, fast).rating == "good"
    slow = Facts(measured=Measured(5, 0, 40.0, 300))
    assert rate("qwen2.5vl:7b", OWNER, slow).rating == "ok"


def test_spilling_out_of_graphics_card_memory_is_not_recommended():
    seen = Facts(observed=Observed(True, 5.9, 8192))
    r = rate("minicpm-v:8b", OWNER, seen)
    assert r.rating == "not_recommended" and "part of it runs on the processor" in r.reason
    assert rate("minicpm-v:8b", CPU_16, seen).rating != "good"  # no card: not a spill
    assert "part of it runs" not in rate("minicpm-v:8b", CPU_16, seen).reason


def test_ps_rows_tell_spill_from_fit():
    gib = 1024**3
    rows = [
        {"name": "a:1b", "size": 10 * gib, "size_vram": 7.8 * gib, "context_length": 4096},
        {"name": "b:1b", "size": 10 * gib, "size_vram": 9.7 * gib},
        {"name": "c", "size": 0, "size_vram": 0},
    ]
    seen = rating.spill_from_ps(rows)
    assert seen["a:1b"].spills and seen["a:1b"].context == 4096
    assert not seen["b:1b"].spills
    assert set(seen) == {"a:1b", "b:1b"}


# ------------------------------------------------------------------ thinks anyway


def test_a_model_that_obeys_think_false_is_rated_like_any_other():
    r = rate("qwen3.5:4b", OWNER, Facts(measured=Measured(3, 0, 7.0, 250)))
    assert not r.thinks and r.rating == "good"


def test_long_answers_mean_it_thinks_anyway():
    for tokens, thinks in ((800, False), (801, True)):
        r = rate("qwen3.5:4b", OWNER, Facts(measured=Measured(3, 0, 7.0, tokens)))
        assert r.thinks is thinks
        assert r.rating == ("ok" if thinks else "good")


def test_measured_normal_answers_clear_the_list_flag():
    flagged = rate("qwen3-vl:8b", APPLE_16)
    assert flagged.thinks
    cleared = rate("qwen3-vl:8b", APPLE_16, Facts(measured=Measured(3, 0, 15.0, 400)))
    assert not cleared.thinks and cleared.rating == "good"
    few = rate("qwen3-vl:8b", APPLE_16, Facts(measured=Measured(2, 0, 15.0, 400)))
    assert few.thinks


def test_the_client_seeing_it_think_is_enough():
    r = rate("gemma3:4b", OWNER, Facts(always_thinks=True))
    assert r.thinks and r.rating == "ok"


def test_a_speed_hovering_around_a_limit_does_not_flip_the_rating():
    memory: dict[str, int] = {}

    def shown(seconds, tag="minicpm-v:8b"):
        f = Facts(measured=Measured(5, 0, seconds, 300), band_memory=memory)
        return rate(tag, OWNER, f).rating

    assert shown(19.0) == "good"
    assert [shown(s) for s in (23.9, 22.0, 24.0)] == ["good"] * 3
    assert shown(24.1) == "ok"  # 20% past the 20 s limit: it really is slower now
    assert [shown(s) for s in (23.9, 17.0, 23.9, 16.1)] == ["ok"] * 4
    assert shown(15.9) == "good"  # 20% below the limit
    assert [shown(s) for s in (19.9, 23.9, 19.0)] == ["good"] * 3


def test_flap_protection_also_works_without_an_estimate():
    memory: dict[str, int] = {}
    unlisted = dataclasses.replace(custom(5), est_s={})  # no speed figures in the model list

    def shown(seconds):
        f = Facts(measured=Measured(5, 0, seconds, 300), band_memory=memory)
        return rating.rate(unlisted, OWNER, f).rating

    assert [shown(s) for s in (10.0, 23.0, 10.0, 23.9)] == ["good"] * 4
    assert [shown(s) for s in (24.1, 23.0, 17.0)] == ["ok"] * 3
    assert shown(15.0) == "good"
    # no figure at all: it still rates, and remembers nothing new
    nothing = rating.rate(unlisted, OWNER, Facts(band_memory={}))
    assert nothing.basis == "unknown" and nothing.rating == "ok"


def test_each_model_has_its_own_remembered_band():
    memory: dict[str, int] = {}
    slow = Facts(measured=Measured(5, 0, 25.0, 300), band_memory=memory)
    assert rate("minicpm-v:8b", OWNER, slow).rating == "ok"  # past the estimate's dead-band
    assert (
        rate(
            "gemma3:4b", OWNER, Facts(measured=Measured(5, 0, 5.0, 300), band_memory=memory)
        ).rating
        == "good"
    )
    assert set(memory) == {"minicpm-v:8b", "gemma3:4b"}


def test_best_good_is_the_first_good_row():
    rows = [
        dataclasses.make_dataclass("R", ["tag", "rating"])(t, g)
        for t, g in (("a", "ok"), ("b", "good"), ("c", "good"))
    ]
    assert rating.best_good(rows) == "b" and rating.best_good(rows[:1]) is None
