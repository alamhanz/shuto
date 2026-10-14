"""Speed check of a running describe job."""

from __future__ import annotations

from shuto.ai import catalog as local_models
from shuto.ai import hardware, machine, selection, speedcheck
from shuto.ai.providers.base import VisionLLM
from shuto.core import paths
from shuto.core.config import Config
from shuto.store import Store


def expected_seconds_per_photo(cfg: Config, model: str) -> float | None:
    """The seconds per photo shown before a local run (estimate_check uses the same figure)."""
    recs = local_models.recommend(
        machine.hardware_profile(),
        local_models.load_catalog(),
        [],
        local_models.load_benchmarks(paths.benchmark_cache_path()),
    )
    rec = next((r for r in recs if r.model.tag == model), None)
    return float(rec.est_seconds_per_image) if rec else None


def faster_model_options(cfg: Config, model: str) -> tuple[float, list[speedcheck.ModelOption]]:
    """(expected seconds for `model`, models that fit this machine and are faster than it).

    Installed ones come with `installed=True`; the others must be downloaded in Settings first.
    """
    try:
        pulled = hardware.ollama_status(selection.ollama_client(cfg)).pulled
    except Exception:
        pulled = []
    recs = local_models.recommend(
        machine.hardware_profile(),
        local_models.load_catalog(),
        pulled,
        local_models.load_benchmarks(paths.benchmark_cache_path()),
    )
    here = next((r for r in recs if r.model.tag == model), None)
    current = float(here.est_seconds_per_image) if here else 0.0
    faster = [
        r for r in recs if r.fits and r.model.tag != model and r.est_seconds_per_image < current
    ]
    faster.sort(key=lambda r: (not r.pulled, -r.model.quality, r.est_seconds_per_image))
    options = [
        speedcheck.ModelOption(r.model.tag, float(r.est_seconds_per_image), r.pulled)
        for r in faster
    ]
    return current, options


def make_live_run(
    cfg: Config, store: Store | None, llm: VisionLLM | None = None
) -> speedcheck.LiveRun | None:
    """The live speed check for a local run; None for online AI (no check there) or when no
    expected time is known for the model."""
    if llm is not None:
        provider, model = llm.name, llm.model
    else:
        try:
            sel = selection.resolve_selection(cfg, store)
        except Exception:
            return None
        provider, model = sel.provider, sel.model
    if provider != "ollama":
        return None
    expected = expected_seconds_per_photo(cfg, model)
    if not expected:
        return None
    return speedcheck.LiveRun(
        model=model,
        max_tries=cfg.llm.max_tries,
        expected_s=expected,
        options=lambda tag: faster_model_options(cfg, tag),
        llm_factory=lambda tag: selection.resolve_llm(cfg, store, "ollama", tag),
    )
