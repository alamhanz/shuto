"""Local model management used by the CLI and the web app."""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from shuto.ai import catalog as local_models
from shuto.ai import hardware, machine
from shuto.ai import selection as ai_selection
from shuto.ai.providers.ollama import OllamaProvider
from shuto.ai.selection import API_KEY_SECRETS, Selection
from shuto.core import paths
from shuto.core.config import Config
from shuto.core.errors import PipelineError
from shuto.store import Store
from shuto.workflow import checks as workflow_checks


def list_models(
    cfg: Config,
    *,
    client: Any = None,
    profile: hardware.HardwareProfile | None = None,
    n: int = 8,
) -> list[local_models.ModelRow]:
    """Top-n runnable local models for this machine (never picks one for the user)."""
    profile = profile or machine.hardware_profile()
    status = hardware.ollama_status(ai_selection._client_or_default(cfg, client))
    return local_models.top_models(
        profile,
        local_models.load_catalog(),
        status.pulled,
        local_models.load_benchmarks(paths.benchmark_cache_path()),
        hardware.free_gb(hardware.ollama_models_dir()),
        n,
    )


@dataclass
class DoctorReport:
    hardware: dict[str, Any]
    ollama: hardware.OllamaStatus
    models_dir: str
    disk_free_gb: float | None
    rows: list[local_models.ModelRow]
    selected: str | None
    provider: str
    config_path: str | None
    workspace: str
    selection_unreadable: bool = False
    checks: list[str] = field(default_factory=list)  # theme and rules that new photos get
    default_place: str = ""  # "Tokyo, Japan" or "" when none is set


def doctor(
    cfg: Config,
    store: Store | None = None,
    *,
    client: Any = None,
    profile: hardware.HardwareProfile | None = None,
    config_path: str | os.PathLike[str] | None = None,
) -> DoctorReport:
    profile = profile or machine.hardware_profile()
    client = ai_selection._client_or_default(cfg, client)
    status = hardware.ollama_status(client)
    models_dir = hardware.ollama_models_dir()
    free = hardware.free_gb(models_dir)
    rows = local_models.top_models(
        profile,
        local_models.load_catalog(),
        status.pulled,
        local_models.load_benchmarks(paths.benchmark_cache_path()),
        free,
    )
    unreadable = False
    try:
        selection = ai_selection.resolve_selection(cfg, store)
        selected: str | None = f"{selection.provider}:{selection.model}"
        provider = selection.provider
    except local_models.ModelChoiceRequired:
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
    )


def model_disk_advice(
    cfg: Config, tag: str, *, client: Any = None
) -> local_models.DiskAdvice | None:
    """Disk check for a catalog model (None if the tag is not in the catalog)."""
    local, api = local_models.load_catalog()
    model = next((m for m in local if m.tag == tag), None)
    if model is None:
        return None
    pulled = hardware.normalize_tag(tag) in {
        hardware.normalize_tag(p)
        for p in hardware.ollama_status(ai_selection._client_or_default(cfg, client)).pulled
    }
    return local_models.disk_advice(model, hardware.ollama_models_dir(), api, pulled=pulled)


def pull_model(
    cfg: Config,
    tag: str,
    progress: Callable[[dict], None] | None = None,
    *,
    client: Any = None,
) -> None:
    """Download a model with Ollama. Callers must have asked the user first."""
    ai_selection._client_or_default(cfg, client).pull(tag, progress)


def use_model(cfg: Config, store: Store, spec: str) -> Selection:
    """Persist an explicit choice. `TAG` = local Ollama model; `PROVIDER:MODEL` = API provider
    (`PROVIDER:` alone = that provider's catalog default)."""
    prov, sep, mod = spec.partition(":")
    if sep and prov in (*API_KEY_SECRETS, "bedrock"):
        entry = ai_selection.api_model(prov, mod or None)
        if entry is None:
            raise PipelineError(f"{spec!r} is not in data/models.yaml for provider {prov}.")
        store.set_setting("llm.provider", prov)
        store.set_setting("llm.model", entry.model)
        return Selection(prov, entry.model, "settings")
    local_models.save_choice(paths.model_choice_path(), spec)
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
        num_ctx=local_models.effective_num_ctx(tag, profile),
    )
    return local_models.benchmark(
        provider,
        local_models.sample_jpeg_bytes(),
        paths.benchmark_cache_path(),
        profile.fingerprint,
    )
