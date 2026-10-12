"""Local vision-model catalog, ranking ("top N"), disk-fit advice, choice and benchmarks.

Core logic only: no prompts, no downloads. Callers (CLI, web) render the structured rows
and call OllamaClient.pull() themselves after explicit user confirmation.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import yaml

from . import estimate as est
from .hardware import (
    HardwareProfile,
    Volume,
    free_gb,
    normalize_tag,
    other_volumes,
    set_env_hint,
)

DATA_DIR = Path(__file__).parent / "data"
SAMPLE_PATH = DATA_DIR / "sample.jpg"
QUALITY_LABELS = {1: "draft", 2: "basic", 3: "good", 4: "high", 5: "best"}
DISK_HEADROOM_GB = 1.0


class ModelChoiceRequired(Exception):
    """`model: auto` was used but the user never explicitly chose a model."""


@dataclass
class LocalModel:
    tag: str
    params_b: float
    size_gb: float
    min_ram_gb: float
    min_vram_gb: float
    quality: int
    json_reliability: str
    est_s: dict[str, float]
    notes: str = ""
    verify: bool = False


@dataclass
class ApiModel:
    provider: str
    model: str
    default: bool
    usd_per_mtok_in: float
    usd_per_mtok_out: float
    image_tokens: int
    updated: str = ""
    verify: bool = False


@dataclass
class Recommendation:
    model: LocalModel
    fits: bool
    reason: str
    pulled: bool
    est_seconds_per_image: float
    basis: Literal["benchmark", "heuristic"]
    mode: str = "cpu"  # "gpu" when the model fits in GPU/unified memory, else "cpu"


# ---------------------------------------------------------------- catalog


def _local(d: dict) -> LocalModel:
    return LocalModel(
        tag=str(d["tag"]),
        params_b=float(d.get("params_b", 0)),
        size_gb=float(d["size_gb"]),
        min_ram_gb=float(d.get("min_ram_gb", 8)),
        min_vram_gb=float(d.get("min_vram_gb", 0)),
        quality=int(d.get("quality", 3)),
        json_reliability=str(d.get("json_reliability", "medium")),
        est_s={str(k): float(v) for k, v in (d.get("est_s") or {}).items()},
        notes=str(d.get("notes", "")),
        verify=bool(d.get("verify", False)),
    )


def _api(d: dict) -> ApiModel:
    return ApiModel(
        provider=str(d["provider"]),
        model=str(d["model"]),
        default=bool(d.get("default", False)),
        usd_per_mtok_in=float(d["usd_per_mtok_in"]),
        usd_per_mtok_out=float(d["usd_per_mtok_out"]),
        image_tokens=int(d.get("image_tokens", 1600)),
        updated=str(d.get("updated", "")),
        verify=bool(d.get("verify", False)),
    )


def load_catalog(extra: Path | None = None) -> tuple[list[LocalModel], list[ApiModel]]:
    """Bundled catalog; entries from `extra` (same schema) replace or add by tag / model."""
    docs = [yaml.safe_load((DATA_DIR / "models.yaml").read_text(encoding="utf-8"))]
    if extra is not None and Path(extra).exists():
        docs.append(yaml.safe_load(Path(extra).read_text(encoding="utf-8")) or {})
    local: dict[str, LocalModel] = {}
    api: dict[tuple[str, str], ApiModel] = {}
    for doc in docs:
        for d in doc.get("local") or []:
            m = _local(d)
            local[m.tag] = m
        for d in doc.get("api") or []:
            a = _api(d)
            api[(a.provider, a.model)] = a
    return list(local.values()), list(api.values())


# ---------------------------------------------------------------- fit + speed

_FALLBACK = {
    "gpu_8": ["gpu_8", "gpu_6"],
    "gpu_24": ["gpu_24", "gpu_12"],
    "apple_64": ["apple_64", "apple_32"],
}


def _heuristic_s(m: LocalModel, tier: str) -> float | None:
    for key in _FALLBACK.get(tier, [tier]):
        if key in m.est_s:
            return m.est_s[key]
    return None


def bench_key(model: str, fingerprint: str) -> str:
    return f"{normalize_tag(model)}@{fingerprint}"


def assess(m: LocalModel, profile: HardwareProfile, benchmarks: dict | None = None):
    """Return (fits, mode, reason, seconds, basis) for one model on this machine."""
    gpu_fit = profile.usable_gpu_gb > 0 and m.size_gb * 1.2 <= profile.usable_gpu_gb
    cpu_fit = m.min_ram_gb <= profile.ram_gb - 4
    if gpu_fit:
        mode, fits = "gpu", True
        reason = f"fits in {profile.usable_gpu_gb:.0f} GB GPU memory"
        secs = _heuristic_s(m, profile.tier)
        if secs is None:
            secs = m.est_s.get("cpu", 120.0) / 8
    elif cpu_fit:
        mode, fits = "cpu", True
        reason = f"runs on CPU ({profile.ram_gb:.0f} GB RAM)"
        secs = m.est_s.get("cpu", 120.0)
    else:
        mode, fits = "cpu", False
        if profile.usable_gpu_gb > 0:
            reason = f"needs {m.size_gb * 1.2:.0f} GB GPU memory or {m.min_ram_gb + 4:.0f} GB RAM"
        else:
            reason = f"needs about {m.min_ram_gb + 4:.0f} GB RAM (have {profile.ram_gb:.0f} GB)"
        secs = m.est_s.get("cpu", 120.0)
    basis: Literal["benchmark", "heuristic"] = "heuristic"
    b = (benchmarks or {}).get(bench_key(m.tag, profile.fingerprint))
    if b and b.get("ok_json") and b.get("warm_s"):
        secs, basis = float(b["warm_s"]), "benchmark"
    return fits, mode, reason, float(secs), basis


def recommend(
    profile: HardwareProfile,
    catalog: tuple[list[LocalModel], list[ApiModel]] | list[LocalModel],
    pulled: list[str],
    benchmarks: dict | None = None,
) -> list[Recommendation]:
    """Fitting models first, then quality descending, then speed ascending."""
    models = catalog[0] if isinstance(catalog, tuple) else catalog
    have = {normalize_tag(p) for p in pulled}
    recs = []
    for m in models:
        fits, mode, reason, secs, basis = assess(m, profile, benchmarks)
        recs.append(
            Recommendation(m, fits, reason, normalize_tag(m.tag) in have, secs, basis, mode)
        )
    recs.sort(key=lambda r: (not r.fits, -r.model.quality, r.est_seconds_per_image))
    return recs


def pick_auto(recs: list[Recommendation]) -> LocalModel | None:
    """Best fitting model that is already pulled (recs are already ordered)."""
    for r in recs:
        if r.fits and r.pulled:
            return r.model
    return None


# ---------------------------------------------------------------- top-N for the user


def speed_label(seconds: float) -> str:
    for limit, label in ((5, "very fast"), (15, "fast"), (45, "moderate"), (120, "slow")):
        if seconds <= limit:
            return label
    return "very slow"


def combined_score(quality: int, seconds: float) -> float:
    """0..1; 60% quality, 40% speed (1/(1+s/20): 0 s -> 1, 20 s -> 0.5, 180 s -> 0.1)."""
    return round(0.6 * quality / 5 + 0.4 / (1 + seconds / 20), 4)


@dataclass
class ModelRow:
    rank: int
    tag: str
    size_gb: float
    est_seconds_per_image: float
    basis: str
    quality: int
    tier_label: str
    score: float
    mode: str
    fits_hardware: bool
    fits_disk: bool
    pulled: bool
    recommended: bool
    reason: str
    notes: str
    verify: bool

    def to_dict(self) -> dict:
        return asdict(self)


def top_models(
    profile: HardwareProfile,
    catalog: tuple[list[LocalModel], list[ApiModel]] | list[LocalModel],
    pulled: list[str],
    benchmarks: dict | None = None,
    disk_free_gb: float | None = None,
    n: int = 8,
) -> list[ModelRow]:
    """Top n models by quality+speed score among those this machine can run.

    If fewer than n can run, the best of the rest are appended with fits_hardware=False.
    disk_free_gb is the free space where Ollama stores models (None = unknown -> assume fits).
    """
    recs = recommend(profile, catalog, pulled, benchmarks)
    scored = sorted(
        recs,
        key=lambda r: (not r.fits, -combined_score(r.model.quality, r.est_seconds_per_image)),
    )[:n]
    rows = []
    for i, r in enumerate(scored, 1):
        m = r.model
        fits_disk = r.pulled or disk_free_gb is None or m.size_gb + DISK_HEADROOM_GB <= disk_free_gb
        rows.append(
            ModelRow(
                rank=i,
                tag=m.tag,
                size_gb=m.size_gb,
                est_seconds_per_image=round(r.est_seconds_per_image, 1),
                basis=r.basis,
                quality=m.quality,
                tier_label=f"{QUALITY_LABELS.get(m.quality, 'good')} quality / "
                f"{speed_label(r.est_seconds_per_image)}",
                score=combined_score(m.quality, r.est_seconds_per_image),
                mode=r.mode,
                fits_hardware=r.fits,
                fits_disk=fits_disk,
                pulled=r.pulled,
                recommended=(i == 1 and r.fits),
                reason=r.reason,
                notes=m.notes,
                verify=m.verify,
            )
        )
    return rows


# ---------------------------------------------------------------- disk advice


@dataclass
class ApiOption:
    provider: str
    model: str
    usd_per_image: float
    updated: str
    text: str


@dataclass
class VolumeOption:
    mount: str
    free_gb: float
    suggested_path: str
    env_hint: str


@dataclass
class DiskAdvice:
    tag: str
    fits: bool
    models_dir: str
    free_gb: float | None
    need_gb: float
    volumes: list[VolumeOption] = field(default_factory=list)
    api_options: list[ApiOption] = field(default_factory=list)
    message: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def disk_advice(
    model: LocalModel,
    models_dir: Path,
    api_models: list[ApiModel],
    *,
    pulled: bool = False,
    os_name: str | None = None,
    free: float | None | Any = "auto",
    volumes: list[Volume] | None = None,
) -> DiskAdvice:
    """Does the model fit where Ollama stores models? If not: other drives, then API options."""
    need = round(model.size_gb + DISK_HEADROOM_GB, 1)
    have = free_gb(models_dir) if free == "auto" else free
    fits = pulled or have is None or have >= need
    adv = DiskAdvice(model.tag, bool(fits), str(models_dir), have, need)
    if fits:
        adv.message = f"{model.tag} fits ({have} GB free at {models_dir})."
        return adv
    vols = volumes if volumes is not None else other_volumes(need, models_dir, os_name)
    adv.volumes = [
        VolumeOption(v.mount, v.free_gb, v.suggested_path, set_env_hint(v.suggested_path, os_name))
        for v in vols
        if v.free_gb >= need
    ]
    for a in sorted(api_models, key=lambda a: est.cost_per_image(a)):
        c = est.cost_per_image(a)
        adv.api_options.append(
            ApiOption(
                a.provider,
                a.model,
                round(c, 5),
                a.updated,
                f"~${c:.4f}/image (pricing {a.updated})",
            )
        )
    adv.message = (
        f"{model.tag} needs about {need} GB but only {have} GB is free at {models_dir}. "
        + (
            "Move model storage to another drive by setting OLLAMA_MODELS, "
            if adv.volumes
            else "No other drive has enough space; "
        )
        + "or use an API provider instead."
    )
    return adv


# ---------------------------------------------------------------- explicit choice


def load_choice(path: Path) -> str | None:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8")).get("model") or None
    except (OSError, ValueError, AttributeError):
        return None


def save_choice(path: Path, tag: str) -> None:
    """Record that the user explicitly chose this model (enables `model: auto`)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"model": tag, "at": datetime.now(UTC).isoformat()}), encoding="utf-8"
    )


def resolve_model(configured: str, choice_path: Path) -> str:
    """An explicit model tag is used as-is; `auto` only works after an explicit choice."""
    if configured and configured != "auto":
        return configured
    chosen = load_choice(choice_path)
    if chosen:
        return chosen
    raise ModelChoiceRequired(
        "No local model chosen yet. Run `shuto models` to see the top 8 for this machine "
        "and pick one with `shuto models use <tag>`."
    )


# ---------------------------------------------------------------- benchmark


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
    from .llm.base import LLMError, LLMRequest, describe_structured

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
