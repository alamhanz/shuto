"""Which local model fits this machine: ranking ("top N"), speed labels and disk-fit advice.

Core logic only: no prompts, no downloads. Callers (CLI, web) render the structured rows
and call OllamaClient.pull() themselves after explicit user confirmation.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

from shuto.ai import catalog, rating
from shuto.ai import estimate as est
from shuto.ai.benchmark import bench_key
from shuto.ai.catalog import DISK_HEADROOM_GB, QUALITY_LABELS, ApiModel, LocalModel
from shuto.ai.hardware import (
    HardwareProfile,
    Volume,
    normalize_tag,
    other_volumes,
    set_env_hint,
)

OS_RESERVED_RAM_GB = rating.OS_RESERVED_RAM_GB  # RAM kept for the system on the CPU
UNKNOWN_SECONDS = 60.0  # sort position of a model with no speed figures (never shown as a time)


@dataclass
class Recommendation:
    model: LocalModel
    fits: bool
    reason: str
    pulled: bool
    est_seconds_per_image: float
    basis: Literal["benchmark", "heuristic", "measured", "unknown"]
    mode: str = "cpu"  # "gpu" when the model fits in GPU/unified memory, else "cpu"
    rating: rating.ModelRating | None = None


def gpu_fit(m: LocalModel, profile: HardwareProfile) -> bool:
    """True when the model (plus 20% working room) fits in the usable GPU/unified memory."""
    return profile.usable_gpu_gb > 0 and m.size_gb * 1.2 <= profile.usable_gpu_gb


def ram_spare_gb(m: LocalModel, profile: HardwareProfile) -> float:
    """RAM left over for the model on the CPU after the system's share (negative = too big)."""
    return profile.ram_gb - OS_RESERVED_RAM_GB - m.min_ram_gb


def _heuristic_s(m: LocalModel, tier: str) -> float | None:
    return rating.tier_seconds(m, tier)


def assess(m: LocalModel, profile: HardwareProfile, benchmarks: dict | None = None):
    """Return (fits, mode, reason, seconds, basis) for one model on this machine."""
    if gpu_fit(m, profile):
        mode, fits = "gpu", True
        reason = f"fits in {profile.usable_gpu_gb:.0f} GB GPU memory"
        secs = _heuristic_s(m, profile.tier)
        if secs is None:
            secs = m.est_s.get("cpu", 120.0) / 8
    elif ram_spare_gb(m, profile) >= 0:
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


def _recommendation(
    m: LocalModel,
    profile: HardwareProfile,
    have: set[str],
    benchmarks: dict | None,
    facts: dict[str, rating.Facts] | None,
) -> Recommendation:
    fits, _mode, reason, _secs, _basis = assess(m, profile, benchmarks)
    tag = normalize_tag(m.tag)
    r = rating.rate(m, profile, (facts or {}).get(tag), benchmarks)
    secs = r.seconds if r.seconds is not None else UNKNOWN_SECONDS
    basis = "heuristic" if r.basis == "estimate" else r.basis
    return Recommendation(
        m, fits and r.memory != "no_room", reason, tag in have, secs, basis, r.mode, r
    )


def recommend(
    profile: HardwareProfile,
    catalog: tuple[list[LocalModel], list[ApiModel]] | list[LocalModel],
    pulled: list[str],
    benchmarks: dict | None = None,
    facts: dict[str, rating.Facts] | None = None,
) -> list[Recommendation]:
    """Fitting models first, then quality descending, then speed ascending."""
    models = catalog[0] if isinstance(catalog, tuple) else catalog
    have = {normalize_tag(p) for p in pulled}
    recs = [_recommendation(m, profile, have, benchmarks, facts) for m in models]
    recs.sort(key=lambda r: (not r.fits, -r.model.quality, r.est_seconds_per_image))
    return recs


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
    rating: str = "ok"
    rating_label: str = ""
    rating_reason: str = ""
    thinks: bool = False  # keeps thinking although told not to
    catalogued: bool = True

    def to_dict(self) -> dict:
        return asdict(self)


def top_models(
    profile: HardwareProfile,
    catalog: tuple[list[LocalModel], list[ApiModel]] | list[LocalModel],
    pulled: list[str],
    benchmarks: dict | None = None,
    disk_free_gb: float | None = None,
    n: int | None = 8,
    facts: dict[str, rating.Facts] | None = None,
    extra_models: Sequence[LocalModel] = (),
) -> list[ModelRow]:
    """The best n models, rated Good first, then OK, then Not recommended, best score first.

    `extra_models` are installed models that are not on the model list: they are rated from
    what shuto has seen of them. n=None returns every model. disk_free_gb is the free space
    where Ollama stores models (None = unknown -> assume fits).
    """
    recs = recommend(profile, catalog, pulled, benchmarks, facts)
    have = {normalize_tag(p) for p in pulled}
    recs += [_recommendation(m, profile, have, benchmarks, facts) for m in extra_models]
    extra_tags = {normalize_tag(m.tag) for m in extra_models}
    scored = sorted(
        recs,
        key=lambda r: (
            rating.RATING_ORDER[r.rating.rating if r.rating else "ok"],
            not r.fits,
            -combined_score(r.model.quality, r.est_seconds_per_image),
        ),
    )
    if n is not None:
        scored = scored[:n]
    rows = []
    for i, r in enumerate(scored, 1):
        m = r.model
        rt = r.rating
        assert rt is not None
        fits_disk = r.pulled or disk_free_gb is None or m.size_gb + DISK_HEADROOM_GB <= disk_free_gb
        unknown = rt.basis == "unknown"
        rows.append(
            ModelRow(
                rank=i,
                tag=m.tag,
                size_gb=m.size_gb,
                est_seconds_per_image=round(r.est_seconds_per_image, 1),
                basis=r.basis,
                quality=m.quality,
                tier_label=f"{QUALITY_LABELS.get(m.quality, 'good')} quality / "
                + ("speed not measured" if unknown else speed_label(r.est_seconds_per_image)),
                score=combined_score(m.quality, r.est_seconds_per_image),
                mode=r.mode,
                fits_hardware=r.fits,
                fits_disk=fits_disk,
                pulled=r.pulled,
                recommended=(i == 1 and rt.rating != "not_recommended" and r.fits),
                reason=r.reason,
                notes=m.notes,
                verify=m.verify,
                rating=rt.rating,
                rating_label=rating.RATING_LABELS[rt.rating],
                rating_reason=rt.reason,
                thinks=rt.thinks,
                catalogued=normalize_tag(m.tag) not in extra_tags,
            )
        )
    return rows


OK_SHOWN_WITHOUT_GOOD = 3  # OK models shown when no model is Good


def visible_rows(
    rows: Sequence[ModelRow], selected: str | None, show_all: bool = False
) -> tuple[list[ModelRow], bool]:
    """The rows to show and whether no model is Good for this computer.

    Normally only Good models show. If none is Good, the best few OK ones show instead. The model
    in use (`selected`, a normalized tag) always shows, and so does a downloaded model that is
    not on shuto's list unless it is Not recommended. `show_all` shows everything.
    """
    no_good = not any(r.rating == "good" for r in rows)
    if show_all:
        return list(rows), no_good
    ok_left = OK_SHOWN_WITHOUT_GOOD if no_good else 0
    keep = []
    for r in rows:
        wanted = r.rating == "good"
        if not wanted and r.rating == "ok" and ok_left > 0:
            wanted, ok_left = True, ok_left - 1
        if not wanted and r.pulled and not r.catalogued and r.rating != "not_recommended":
            wanted = True
        if wanted or (selected is not None and normalize_tag(r.tag) == selected):
            keep.append(r)
    return keep, no_good and any(r.rating == "ok" for r in keep)


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
    have = catalog.free_gb(models_dir) if free == "auto" else free
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
            "Move model storage to another drive, "
            if adv.volumes
            else "No other drive has enough space; "
        )
        + "or use an API provider instead."
    )
    return adv
