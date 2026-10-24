"""Rate a local model for this computer: Good, OK or Not recommended.

Pure logic (no network, no database). The inputs are the model's numbers, the hardware profile
and what shuto has seen on this computer (past calls, what Ollama reports about loaded models).

The rules, strictest result wins:

* Memory. The model's need (GiB) is compared with the usable memory of the graphics card (or
  of the computer when there is no usable card): within 85% is "fits", up to 100% "tight".
  A model too big for the card runs on the processor and is rated by CPU speed. A model that
  does not even fit in RAM is Not recommended.
* Speed (seconds per photo). Measured median of the last calls if there are at least three good
  ones, else the speed test, else the estimate from the model list. Good: 20 s or less. OK: 60 s
  or less. Slower: Not recommended. A measured speed only moves a model out of the band its
  estimate puts it in when it is past the limit by 20% (so ratings do not flap).
* Thinks anyway. A model that keeps thinking although shuto sends ``think: false`` is slow and
  often stops without an answer. It is never Good, its OK limit is 40 s, its estimate is
  multiplied by THINK_FACTOR, and with tight memory it is Not recommended. A model that obeys
  the switch is rated like any other model. It counts as "thinks anyway" when the model list
  says so, or Ollama showed shuto it ignored the switch, or its measured answers are far longer
  than a normal answer (median above THINK_OUT_TOKENS). Enough measured answers (MIN_OK_CALLS)
  with a normal length clear the list's flag.
* Quality below 3 or a small context window: at most OK.
* Numbers not confirmed (``verify`` in the model list) or unknown (a model not on the list):
  at most OK until shuto has measured it.
* Three or more failed calls in the last ten: Not recommended. A model seen spilling out of
  graphics card memory right now: Not recommended.
"""

from __future__ import annotations

import statistics
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Literal

from shuto.ai import catalog, estimate
from shuto.ai.benchmark import bench_key
from shuto.ai.catalog import LocalModel
from shuto.ai.hardware import HardwareProfile, normalize_tag

Rating = Literal["good", "ok", "not_recommended"]
RATING_LABELS: dict[str, str] = {
    "good": "Good for this computer",
    "ok": "OK for this computer",
    "not_recommended": "Not recommended for this computer",
}
RATING_ORDER: dict[str, int] = {"good": 0, "ok": 1, "not_recommended": 2}

GIB_PER_GB = 1.0737  # the model list is in GB (10^9 bytes); memory here is in GiB
DRIVER_RESERVE_GB = 0.5  # kept free in a graphics card for the driver
OS_RESERVED_RAM_GB = 4  # RAM kept for the system when a model runs on the processor
HEADROOM = 0.15  # "fits" leaves at least this share of the memory free
GOOD_S = 20.0  # Good: this many seconds per photo or less
OK_S = 60.0  # OK: this many seconds per photo or less
THINK_OK_S = 40.0  # OK limit for a model that thinks anyway
THINK_FACTOR = 4  # estimates are multiplied by this for a model that thinks anyway
MIN_QUALITY_GOOD = 3
WINDOW = 10  # the last calls per model that count
WINDOW_DAYS = 90
MIN_OK_CALLS = 3  # successful calls needed before measured numbers are used
FAIL_CAP = 3  # failures in the window that make a model Not recommended
HYSTERESIS = 0.2  # a measured speed must pass a limit by this much to change the band
THINK_OUT_TOKENS = 800  # a median answer longer than this means the model thinks anyway
SPILL_TOLERANCE = 0.95  # in graphics card memory below this share of its size = spilling
KV_GB_PER_B_8K = 0.12  # working-memory growth per billion parameters at 8192 context
WORK_GB = 0.4
THINKING_HEADROOM = 4096  # the same extra room the Ollama provider asks for thinking models

_TIER_FALLBACK = {
    "gpu_8": ["gpu_8", "gpu_6"],
    "gpu_24": ["gpu_24", "gpu_12"],
    "apple_64": ["apple_64", "apple_32"],
}
_BAND_NAMES: tuple[Rating, ...] = ("good", "ok", "not_recommended")


@dataclass(frozen=True)
class Measured:
    ok_calls: int
    failures: int
    median_s: float | None
    median_out: int | None


@dataclass(frozen=True)
class Observed:
    spills: bool
    size_gb: float  # GiB, as Ollama reports the loaded model
    context: int  # the context size it was loaded with (0 = not reported)


@dataclass(frozen=True)
class Facts:
    always_thinks: bool = False  # Ollama client saw it think although told not to
    measured: Measured | None = None
    observed: Observed | None = None


@dataclass(frozen=True)
class ModelRating:
    rating: Rating
    reason: str
    seconds: float | None
    basis: Literal["estimate", "benchmark", "measured", "unknown"]
    mode: Literal["gpu", "cpu"]
    memory: Literal["fits", "tight", "too_big", "no_room"]
    thinks: bool  # keeps thinking although told not to
    need_gb: float


# ---------------------------------------------------------------- memory


def budget(profile: HardwareProfile) -> tuple[Literal["gpu", "apple", "cpu"], float, bool]:
    """(kind, usable GiB, graphics card unknown) for this computer.

    Only the largest single card counts (cards are never added together). A card whose memory
    shuto cannot read is treated as no card, and the third value says so.
    """
    cpu_usable = max(profile.ram_gb - OS_RESERVED_RAM_GB, 0.0)
    if profile.apple_silicon and profile.usable_gpu_gb > 0:
        return "apple", profile.usable_gpu_gb, False
    if profile.usable_gpu_gb > 0:
        return "gpu", max(profile.usable_gpu_gb - DRIVER_RESERVE_GB, 0.0), False
    unknown = bool(profile.gpus) and not any(g.vram_total_gb for g in profile.gpus)
    return "cpu", cpu_usable, unknown


def tier_seconds(m: LocalModel, tier: str) -> float | None:
    for key in _TIER_FALLBACK.get(tier, [tier]):
        if key in m.est_s:
            return m.est_s[key]
    return None


def _kv_gb(m: LocalModel, ctx_delta: int) -> float:
    return KV_GB_PER_B_8K * m.params_b * ctx_delta / catalog.NUM_CTX_TARGET


def memory_need_gb(m: LocalModel, num_ctx: int, observed: Observed | None = None) -> float:
    """GiB the model needs at `num_ctx`: the loaded size reported by Ollama, else the model list,
    else an estimate from its size and parameters."""
    if observed is not None and observed.size_gb > 0:
        if observed.context > 0 and observed.context != num_ctx:
            return max(observed.size_gb + _kv_gb(m, num_ctx - observed.context) / GIB_PER_GB, 0.0)
        return observed.size_gb
    if m.mem_gb > 0:
        need = m.mem_gb + _kv_gb(m, num_ctx - catalog.NUM_CTX_TARGET)
        return max(need, m.size_gb) / GIB_PER_GB
    return (m.size_gb + _kv_gb(m, num_ctx) + WORK_GB) / GIB_PER_GB


def _thinks_anyway(m: LocalModel, facts: Facts) -> bool:
    if facts.always_thinks:
        return True
    me = facts.measured
    if me is not None and me.ok_calls >= MIN_OK_CALLS and me.median_out is not None:
        return me.median_out > THINK_OUT_TOKENS
    return m.always_thinks


# ---------------------------------------------------------------- speed


def _band(seconds: float, good_s: float, ok_s: float) -> int:
    return 0 if seconds <= good_s else 1 if seconds <= ok_s else 2


def _band_with_hysteresis(measured: float, est: float | None, good_s: float, ok_s: float) -> int:
    """The measured speed's band, but staying in the estimate's band unless it is 20% past a
    limit. Without an estimate the plain band is used."""
    if est is None:
        return _band(measured, good_s, ok_s)
    limits = (good_s, ok_s)
    cur = _band(est, good_s, ok_s)
    while cur < 2 and measured > limits[cur] * (1 + HYSTERESIS):
        cur += 1
    while cur > 0 and measured < limits[cur - 1] * (1 - HYSTERESIS):
        cur -= 1
    return cur


def _words(seconds: float) -> str:
    text = estimate.format_duration(seconds)
    n, _, unit = text.partition(" ")
    if unit == "s":
        return f"{n} second" + ("" if n == "1" else "s")
    if unit == "min":
        return f"{n} minute" + ("" if n == "1" else "s")
    return text


# ---------------------------------------------------------------- rating


def rate(
    m: LocalModel,
    profile: HardwareProfile,
    facts: Facts | None = None,
    bench: dict | None = None,
) -> ModelRating:
    facts = facts or Facts()
    kind, usable, _unknown = budget(profile)
    thinks = _thinks_anyway(m, facts)
    cpu_usable = max(profile.ram_gb - OS_RESERVED_RAM_GB, 0.0)

    num_ctx = catalog.effective_num_ctx(m.tag, profile, [m])
    if thinks and num_ctx > catalog.SMALL_CONTEXT_TOKENS:
        num_ctx += THINKING_HEADROOM
        if m.context_tokens:
            num_ctx = min(num_ctx, m.context_tokens)
    need = memory_need_gb(m, num_ctx, facts.observed)

    # memory class
    if need <= usable * (1 - HEADROOM):
        memory = "fits"
    elif need <= usable:
        memory = "tight"
    else:
        memory = "too_big"
    mode: Literal["gpu", "cpu"] = "cpu" if kind == "cpu" else "gpu"
    if memory == "too_big":
        mode = "cpu"
        if kind == "cpu" or need > cpu_usable or m.min_ram_gb > cpu_usable:
            memory = "no_room"
    elif kind == "cpu" and m.min_ram_gb > cpu_usable:
        memory = "no_room"

    # speed: measured, then the speed test, then the estimate from the model list
    me = facts.measured
    good_s, ok_s = GOOD_S, (THINK_OK_S if thinks else OK_S)
    est = tier_seconds(m, profile.tier) if mode == "gpu" else None
    if mode == "gpu" and est is None and "cpu" in m.est_s:
        est = m.est_s["cpu"] / 8
    if mode == "cpu":
        est = m.est_s.get("cpu")
    if est is not None and thinks:
        est *= THINK_FACTOR
    seconds: float | None = est
    basis: Literal["estimate", "benchmark", "measured", "unknown"] = (
        "estimate" if est is not None else "unknown"
    )
    band: int | None = _band(est, good_s, ok_s) if est is not None else None
    b = (bench or {}).get(bench_key(m.tag, profile.fingerprint))
    if b and b.get("ok_json") and b.get("warm_s"):
        seconds, basis = float(b["warm_s"]), "benchmark"
        band = _band(seconds, good_s, ok_s)
    if me is not None and me.ok_calls >= MIN_OK_CALLS and me.median_s is not None:
        seconds, basis = me.median_s, "measured"
        band = _band_with_hysteresis(me.median_s, est, good_s, ok_s)

    # candidates: (level, priority, reason); the worst level wins, the lowest priority breaks ties
    level_of = {name: i for i, name in enumerate(_BAND_NAMES)}
    cands: list[tuple[int, int, str]] = []
    spills = bool(facts.observed and facts.observed.spills) and kind != "cpu"
    if seconds is not None:
        base = (
            f"Fits in your graphics card with room to spare. About {_words(seconds)} per photo."
            if memory == "fits" and mode == "gpu"
            else f"Works well here. About {_words(seconds)} per photo."
        )
    else:
        base = "Fits this computer."
    cands.append((0, 9, base))
    too_big_text = "Too big for your graphics card, so it runs mostly on the processor" + (
        f": about {_words(seconds)} per photo." if seconds is not None else "."
    )

    if memory == "no_room":
        cands.append((2, 0, "Needs more memory than this computer has."))
    elif memory == "too_big":
        cands.append((level_of["ok"], 0, too_big_text))
    elif memory == "tight":
        shown_fast = basis == "measured" and band == 0 and not spills and not thinks
        if not shown_fast:
            cands.append(
                (
                    1,
                    0,
                    "Only just fits in your "
                    + ("graphics card" if mode == "gpu" else "computer's memory")
                    + ", so it can slow down when other apps are open.",
                )
            )
    if spills:
        cands.append(
            (
                2,
                0,
                "Right now it does not fit in your graphics card memory, so part of it runs "
                "on the processor.",
            )
        )
    if me is not None and me.failures >= FAIL_CAP:
        cands.append((2, 1, "On this computer it often stopped without an answer."))
    if thinks:
        if memory == "tight":
            cands.append(
                (
                    2,
                    2,
                    "It keeps thinking before it answers and only just fits in your graphics "
                    "card, so it gets very slow.",
                )
            )
        cands.append(
            (
                1,
                2,
                "It keeps thinking before it answers, so it is slower and sometimes stops "
                "without an answer.",
            )
        )
    if band is not None and band > 0:
        if basis == "measured":
            when = f"On this computer it took about {_words(seconds or 0)} per photo."
        elif memory == "too_big":
            when = too_big_text
        elif mode == "cpu":
            when = f"Runs on the processor: about {_words(seconds or 0)} per photo."
        else:
            when = f"Works, but slow on this computer: about {_words(seconds or 0)} per photo."
        cands.append((band, 3, when))
    if m.quality < MIN_QUALITY_GOOD or 0 < m.context_tokens <= catalog.SMALL_CONTEXT_TOKENS:
        cands.append((1, 4, "Fast, but its titles and keywords are basic."))
    if basis == "unknown":
        cands.append(
            (
                1,
                5,
                "shuto has no speed figures for this model yet. It is rated after a few photos.",
            )
        )
    elif m.verify and basis == "estimate":
        cands.append(
            (
                1,
                5,
                "shuto's speed figures for this model are not confirmed yet. "
                "It is rated after a few photos.",
            )
        )

    level, _, reason = max(cands, key=lambda c: (c[0], -c[1]))
    return ModelRating(
        rating=_BAND_NAMES[level],
        reason=reason,
        seconds=seconds,
        basis=basis,
        mode=mode,
        memory=memory,  # type: ignore[arg-type]
        thinks=thinks,
        need_gb=round(need, 2),
    )


# ---------------------------------------------------------------- what shuto has seen


def _is_cancel(error: object) -> bool:
    return isinstance(error, str) and "cancel" in error.lower()


def measured_from_calls(rows: Iterable[Mapping]) -> dict[str, Measured]:
    """Per model (normalized tag) from call rows `{model, ok, seconds, output_tokens, error}`,
    newest first. Only the last WINDOW calls per model count; cancelled calls are skipped."""
    grouped: dict[str, list[Mapping]] = {}
    for r in rows:
        if _is_cancel(r.get("error")):
            continue
        calls = grouped.setdefault(normalize_tag(str(r.get("model") or "")), [])
        if len(calls) < WINDOW:
            calls.append(r)
    out: dict[str, Measured] = {}
    for tag, calls in grouped.items():
        good = [c for c in calls if c.get("ok") and c.get("seconds") is not None]
        outs = [int(c["output_tokens"]) for c in good if c.get("output_tokens") is not None]
        out[tag] = Measured(
            ok_calls=len(good),
            failures=sum(1 for c in calls if not c.get("ok")),
            median_s=round(statistics.median(float(c["seconds"]) for c in good), 1)
            if good
            else None,
            median_out=int(statistics.median(outs)) if outs else None,
        )
    return out


def spill_from_ps(rows: list[dict]) -> dict[str, Observed]:
    """Per loaded model (normalized tag): is part of it outside graphics card memory?"""
    out: dict[str, Observed] = {}
    for r in rows:
        name = str(r.get("name") or r.get("model") or "")
        size = int(r.get("size") or 0)
        if not name or size <= 0:
            continue
        vram = int(r.get("size_vram") or 0)
        out[normalize_tag(name)] = Observed(
            spills=vram < size * SPILL_TOLERANCE,
            size_gb=round(size / 1024**3, 2),
            context=int(r.get("context_length") or 0),
        )
    return out


def best_good(rows: Iterable) -> str | None:
    """The tag of the first Good row (rows are sorted best first)."""
    return next((r.tag for r in rows if r.rating == "good"), None)
