"""Local model management used by the CLI and the web app."""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from shuto.ai import benchmark as ai_benchmark
from shuto.ai import catalog as local_models
from shuto.ai import hardware, machine
from shuto.ai import model_choice as ai_choice
from shuto.ai import rating as ai_rating
from shuto.ai import recommend as ai_recommend
from shuto.ai import selection as ai_selection
from shuto.ai.providers import API_PROVIDERS
from shuto.ai.providers.base import LLMError
from shuto.ai.providers.ollama import OllamaProvider
from shuto.ai.selection import Selection
from shuto.core import paths
from shuto.core.config import Config
from shuto.core.errors import PipelineError
from shuto.ollama import conflict as ollama_conflict_mod
from shuto.ollama import models_dir as ollama_store
from shuto.store import Store
from shuto.workflow import checks as workflow_checks

LOOKUP_TIMEOUT_S = 2  # Ollama questions asked while a page is built: short, never worth a hang
_CAPS: dict[tuple[str, str], list[str]] = {}  # (Ollama address, tag) -> capabilities; per run
_SEEN: dict[tuple[str, str], ai_rating.Observed] = {}  # (address, tag) -> last seen in memory


def clear_caches() -> None:
    """Forget what was learned from Ollama (tests, and a changed Ollama address)."""
    _CAPS.clear()
    _SEEN.clear()


def _param_billions(details: dict) -> float:
    text = str(details.get("parameter_size") or "").strip().upper()
    try:
        number = float(text.rstrip("BMK"))
    except ValueError:
        return 0.0
    return number / 1000 if text.endswith("M") else number


def _capabilities(client: Any, url: str, tag: str) -> list[str]:
    """What Ollama lists for a downloaded model (raises when it cannot be asked). Kept per run."""
    key = (url, tag)
    if key not in _CAPS:
        caps = client.show(tag, timeout=LOOKUP_TIMEOUT_S).get("capabilities") or []
        _CAPS[key] = [str(c) for c in caps]
    return _CAPS[key]


def uncatalogued_model(entry: dict, caps: list[str]) -> local_models.LocalModel:
    """A downloaded model that is not on shuto's list, as a model the rating can handle."""
    name = str(entry.get("name") or entry.get("model") or "")
    return local_models.LocalModel(
        tag=name,
        params_b=_param_billions(entry.get("details") or {}),
        size_gb=round(int(entry.get("size") or 0) / 1e9, 1),
        min_ram_gb=0,
        min_vram_gb=0,
        quality=3,
        json_reliability="medium",
        est_s={},
        notes="Not on shuto's model list.",
        thinks="thinking" in caps,
    )


def model_facts(
    cfg: Config, store: Store | None, client: Any, status: hardware.OllamaStatus
) -> tuple[dict[str, ai_rating.Facts], list[local_models.LocalModel]]:
    """What shuto has seen of the models on this computer, for rating them.

    Returns (facts by normalized tag, installed models that are not on shuto's list and that
    Ollama says can see images). Every question to Ollama is short and optional: when Ollama is
    down or slow the page still builds, with fewer facts. Answers about what is loaded are kept
    for the run, so a model that was seen spilling stays flagged until it is seen fitting again.
    """
    url = getattr(client, "url", cfg.llm.ollama.url)
    measured: dict[str, ai_rating.Measured] = {}
    if store is not None:
        try:
            measured = ai_rating.measured_from_calls(store.recent_llm_calls("ollama"))
        except sqlite3.Error:
            pass
    extras: list[local_models.LocalModel] = []
    if status.running:
        try:
            for tag, seen in ai_rating.spill_from_ps(client.ps(timeout=LOOKUP_TIMEOUT_S)).items():
                _SEEN[(url, tag)] = seen
        except Exception:
            pass  # a hint is never worth breaking the page for
        listed = {hardware.normalize_tag(m.tag) for m in local_models.load_catalog()[0]}
        if any(hardware.normalize_tag(p) not in listed for p in status.pulled):
            try:
                for entry in client.tags():
                    name = str(entry.get("name") or entry.get("model") or "")
                    if not name or hardware.normalize_tag(name) in listed:
                        continue
                    caps = _capabilities(client, url, name)
                    if "vision" in caps:
                        extras.append(uncatalogued_model(entry, caps))
            except Exception:
                pass  # the first failed question ends the asking for this page
    facts: dict[str, ai_rating.Facts] = {}
    tags = {hardware.normalize_tag(m.tag) for m in local_models.load_catalog()[0]}
    tags |= {hardware.normalize_tag(m.tag) for m in extras} | set(measured)
    for tag in tags:
        always = False
        if status.running and hasattr(client, "always_thinks"):
            try:
                always = bool(client.always_thinks(tag.removesuffix(":latest")))
            except Exception:
                always = False
        facts[tag] = ai_rating.Facts(
            always_thinks=always,
            measured=measured.get(tag),
            observed=_SEEN.get((url, tag)),
        )
    return facts, extras


def list_models(
    cfg: Config,
    store: Store | None = None,
    *,
    client: Any = None,
    profile: hardware.HardwareProfile | None = None,
    n: int | None = None,
) -> list[ai_recommend.ModelRow]:
    """Local models for this machine, best first, each with its rating (never picks one)."""
    profile = profile or machine.hardware_profile()
    client = ai_selection._client_or_default(cfg, client)
    status = hardware.ollama_status(client)
    facts, extras = model_facts(cfg, store, client, status)
    return ai_recommend.top_models(
        profile,
        local_models.load_catalog(),
        status.pulled,
        ai_benchmark.load_benchmarks(paths.benchmark_cache_path()),
        hardware.free_gb(hardware.ollama_models_dir()),
        n,
        facts,
        extras,
    )


def ollama_conflict(
    cfg: Config, client: Any = None, chosen: str | None = None
) -> ollama_conflict_mod.Diagnosis | None:
    """Is a different Ollama than the installed one answering? None when not or unsure.

    `chosen` is the local model in use (if any); models on disk that Ollama does not list only
    count when it is one of them."""
    try:
        return ollama_conflict_mod.diagnose(
            ai_selection._client_or_default(cfg, client),
            ollama_store.current_models_dir(),
            chosen=chosen,
        )
    except Exception:
        return None


@dataclass
class DoctorReport:
    hardware: dict[str, Any]
    ollama: hardware.OllamaStatus
    models_dir: str
    disk_free_gb: float | None
    rows: list[ai_recommend.ModelRow]
    selected: str | None
    provider: str
    config_path: str | None
    workspace: str
    selection_unreadable: bool = False
    checks: list[str] = field(default_factory=list)  # theme and rules that new photos get
    default_place: str = ""  # "Tokyo, Japan" or "" when none is set
    conflict: ollama_conflict_mod.Diagnosis | None = None  # another Ollama is answering
    gpu_unknown: bool = False  # a graphics card is there but its memory can't be read


def doctor(
    cfg: Config,
    store: Store | None = None,
    *,
    client: Any = None,
    profile: hardware.HardwareProfile | None = None,
    config_path: str | os.PathLike[str] | None = None,
    with_conflict: bool = True,
) -> DoctorReport:
    profile = profile or machine.hardware_profile()
    client = ai_selection._client_or_default(cfg, client)
    status = hardware.ollama_status(client)
    models_dir = hardware.ollama_models_dir()
    free = hardware.free_gb(models_dir)
    facts, extras = model_facts(cfg, store, client, status)
    rows = ai_recommend.top_models(
        profile,
        local_models.load_catalog(),
        status.pulled,
        ai_benchmark.load_benchmarks(paths.benchmark_cache_path()),
        free,
        None,
        facts,
        extras,
    )
    unreadable = False
    try:
        selection = ai_selection.resolve_selection(cfg, store)
        selected: str | None = f"{selection.provider}:{selection.model}"
        provider = selection.provider
    except ai_choice.ModelChoiceRequired:
        selected, provider = None, cfg.llm.provider
    except (PipelineError, sqlite3.Error):  # what resolve_selection raises; bugs still surface
        selected, provider, unreadable = None, cfg.llm.provider, True
    return DoctorReport(
        profile.to_dict(),
        status,
        str(models_dir),
        free,
        rows,
        selected,
        provider,
        str(config_path) if config_path else None,
        str(cfg.workspace_dir),
        unreadable,
        workflow_checks.active_checks(cfg),
        ", ".join(p for p in (cfg.style.default_place.city, cfg.style.default_place.country) if p),
        ollama_conflict(
            cfg, client, selected[7:] if selected and selected.startswith("ollama:") else None
        )
        if with_conflict and status.running
        else None,
        ai_rating.budget(profile)[2],
    )


def model_disk_advice(
    cfg: Config, tag: str, *, client: Any = None
) -> ai_recommend.DiskAdvice | None:
    """Disk check for a catalog model (None if the tag is not in the catalog)."""
    local, api = local_models.load_catalog()
    model = next((m for m in local if m.tag == tag), None)
    if model is None:
        return None
    pulled = hardware.normalize_tag(tag) in {
        hardware.normalize_tag(p)
        for p in hardware.ollama_status(ai_selection._client_or_default(cfg, client)).pulled
    }
    return ai_recommend.disk_advice(model, hardware.ollama_models_dir(), api, pulled=pulled)


def pull_model(
    cfg: Config,
    tag: str,
    progress: Callable[[dict], None] | None = None,
    *,
    client: Any = None,
) -> None:
    """Download a model with Ollama. Callers must have asked the user first."""
    client = ai_selection._client_or_default(cfg, client)
    try:
        client.pull(tag, progress)
    except LLMError as exc:
        if ollama_conflict_mod.pull_412_message(str(exc), None, None) is None:
            raise
        found = ollama_conflict(cfg, client)
        try:
            server = client.version() or None
        except Exception:
            server = None
        text = ollama_conflict_mod.pull_412_message(str(exc), found, server)
        raise LLMError(text or str(exc), retryable=False) from None


def use_model(cfg: Config, store: Store, spec: str) -> Selection:
    """Persist an explicit choice. `TAG` = local Ollama model; `PROVIDER:MODEL` = API provider
    (`PROVIDER:` alone = that provider's catalog default)."""
    prov, sep, mod = spec.partition(":")
    if sep and prov in API_PROVIDERS:
        entry = ai_selection.api_model(prov, mod or None)
        if entry is None:
            raise PipelineError(f"{spec!r} is not in data/models.yaml for provider {prov}.")
        store.set_setting("llm.provider", prov)
        store.set_setting("llm.model", entry.model)
        return Selection(prov, entry.model, "settings")
    ai_choice.save_choice(paths.model_choice_path(), spec)
    store.set_setting("llm.provider", "ollama")
    store.set_setting("llm.model", spec)  # beats an explicit config model (UI > config)
    return Selection("ollama", spec, "settings")


def bench_model(
    cfg: Config,
    store: Store | None,
    model: str | None = None,
    *,
    client: Any = None,
    profile: hardware.HardwareProfile | None = None,
) -> dict:
    """Two-call benchmark (cold, warm) on the bundled sample; cached in benchmark_cache_path()."""
    profile = profile or machine.hardware_profile()
    tag = model or ai_selection.resolve_selection(cfg, store, provider="ollama").model
    provider = OllamaProvider(
        tag,
        url=cfg.llm.ollama.url,
        keep_alive=cfg.llm.ollama.keep_alive,
        timeout_s=cfg.llm.timeout_s,
        client=ai_selection._client_or_default(cfg, client),
        num_ctx=ai_selection.num_ctx_for(tag),
    )
    return ai_benchmark.benchmark(
        provider,
        ai_benchmark.sample_jpeg_bytes(),
        paths.benchmark_cache_path(),
        profile.fingerprint,
    )
