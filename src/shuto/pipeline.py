"""Service layer: the only place that maps Config to the pure modules and talks to the Store.

Runners (CLI, web) call these functions. Every long operation takes an optional
``progress(done, total, message)`` callback and, where it makes sense, ``cancel() -> bool``.
"""

from __future__ import annotations

import csv
import ftplib
import importlib
import importlib.util
import os
import re
import threading
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

import platformdirs

from . import cull as cull_mod
from . import deliver, describe, estimate, ftps, guardrails, hardware, imaging, local_models
from . import ledger as ledger_mod
from .collect import _safe_batch_name, batch_id_for, group_near_duplicates, scan_folder
from .config import APP_NAME, Config, default_config_dir
from .csv_export import split_by_kind
from .imaging import hamming
from .llm import get_provider
from .llm.base import LLMError, VisionLLM
from .llm.fake import FakeProvider
from .llm.ollama import OllamaClient, OllamaProvider
from .metadata import PhotoMetadata, normalize
from .models import REASON_CODES, UPLOADED_STATES, LLMCall, Photo, Status
from .prepare import prepare_file, release_check, safe_filename
from .secrets import get_secret, keyring_available
from .store import InvalidTransition, Store

Progress = Callable[[int, int, str], None]
Cancel = Callable[[], bool]

PRE_UPLOAD_EDITABLE = (
    Status.DESCRIBED,
    Status.NEEDS_REVIEW,
    Status.BLOCKED,
    Status.APPROVED,
    Status.PREPARED,
)
API_KEY_SECRETS = {
    "anthropic": "ANTHROPIC_API_KEY",
    "gemini": "GEMINI_API_KEY",
}


API_SDK_MODULES = {"anthropic": "anthropic", "gemini": "google.genai", "bedrock": "boto3"}


class PipelineError(Exception):
    """A problem the user can fix (bad input, missing key, refused action). Message is friendly."""


class UploadRefused(PipelineError):
    pass


# --------------------------------------------------------------------------- paths


def model_choice_path() -> Path:
    """Explicitly chosen local model: <user_config_dir>/model_choice.json."""
    return default_config_dir() / "model_choice.json"


def benchmark_cache_path() -> Path:
    """Cached benchmark results: <user_cache_dir>/benchmarks.json."""
    return Path(platformdirs.user_cache_dir(APP_NAME)) / "benchmarks.json"


def open_store(cfg: Config) -> Store:
    cfg.workspace_dir.mkdir(parents=True, exist_ok=True)
    return Store(cfg.workspace_path("shuto.db"))


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _noop(done: int, total: int, message: str = "") -> None:
    return None


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


# --------------------------------------------------------------------------- import / cull


@dataclass
class AlreadyUploaded:
    """A photo skipped because shuto remembers sending it (exactly or as a look-alike)."""

    name: str
    uploaded_at: str | None
    submit_name: str | None
    exact: bool = False
    rejected: bool = False

    def text(self) -> str:
        day = (self.uploaded_at or "")[:10] or "an earlier date"
        what = "previously submitted and rejected" if self.rejected else "already uploaded"
        kind = what if self.exact else f"looks like a photo {what}"
        return f"{self.name}: {kind} on {day} as {self.submit_name or 'an unknown name'}"


@dataclass
class ImportSummary:
    imported: int = 0
    duplicates: int = 0
    already_known: int = 0
    near_duplicates: int = 0
    marked_uploaded: int = 0
    already_uploaded: list[AlreadyUploaded] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    batches: dict[str, int] = field(default_factory=dict)
    photo_ids: list[int] = field(default_factory=list)


def import_folder(
    cfg: Config,
    store: Store,
    path: str | Path | None = None,
    *,
    recursive: bool = True,
    notes: str | None = None,
    progress: Progress | None = None,
    force: bool = False,
    mark_uploaded: bool = False,
    batch_name: str | None = None,
) -> ImportSummary:
    """Scan a folder (or cfg.sources), register new photos, thumbnails, exact and near dupes.

    Originals are only read. Exact duplicates (same sha256 as a known photo) are counted and not
    stored again; near duplicates keep the sharpest member and mark the rest `near_duplicate`.
    Photos that are (or look like, by dHash distance) ones already uploaded are recorded as
    `already_uploaded` and not processed again unless `force`. With `mark_uploaded` the folder is
    registered as already uploaded instead (no thumbnails; nothing else runs on it). `force` also
    sends look-alikes stored as `already_uploaded` by an earlier import back to `imported`.
    With `batch_name`, every new photo goes into that one batch (instead of `import.batch_by`).
    """
    if force and mark_uploaded:
        raise PipelineError("--force and --mark-uploaded cannot be combined.")
    progress = progress or _noop
    roots: list[tuple[Path, bool]] = (
        [(Path(path).expanduser(), recursive)]
        if path is not None
        else [(s.path, s.recursive) for s in cfg.sources]
    )
    if not roots:
        raise PipelineError("No folder given and no `sources` in the config.")
    for root, _ in roots:
        if not root.is_dir():
            raise PipelineError(f"Not a folder: {root}")
    summary = ImportSummary()
    errors = summary.errors

    def on_error(p: Path, exc: Exception) -> None:
        errors.append(f"{p}: {exc}")

    new_items: dict[str, list[tuple[int, Any]]] = {}
    seen: set[str] = set()
    check_history = not (force or mark_uploaded)
    history = (
        [(p.dhash, p) for p in store.list_photos(statuses=UPLOADED_STATES) if p.dhash is not None]
        if check_history
        else []
    )
    distance = cfg.import_.near_duplicate_distance
    for root, rec in roots:
        progress(0, 0, f"scanning {root}")
        items = scan_folder(root, cfg.import_.extensions, rec, on_error=on_error)
        for n, item in enumerate(items, 1):
            progress(n, len(items), item.path.name)
            known = store.find_by_sha(item.sha256)
            if item.sha256 in seen:
                summary.duplicates += 1
                continue
            if (
                known
                and check_history
                and known.status in UPLOADED_STATES
                and known.status != Status.REJECTED
            ):
                seen.add(item.sha256)
                summary.already_uploaded.append(
                    AlreadyUploaded(
                        item.path.name, known.uploaded_at, known.submit_name, exact=True
                    )
                )
                continue
            if known and force and known.status == Status.ALREADY_UPLOADED:
                assert known.id is not None and known.batch_id is not None
                seen.add(item.sha256)
                store.set_status(known.id, Status.IMPORTED, force=True, detail={"forced": True})
                summary.imported += 1
                summary.photo_ids.append(known.id)
                summary.batches[known.batch_id] = summary.batches.get(known.batch_id, 0) + 1
                continue
            if known and mark_uploaded and known.status not in UPLOADED_STATES:
                assert known.id is not None
                store.update_photo(known.id, uploaded_at=_now_iso())
                store.set_status(known.id, Status.UPLOADED, force=True, detail={"marked": True})
                summary.marked_uploaded += 1
                continue
            if known and known.src_path != str(item.path):
                if Path(known.src_path).exists():
                    summary.duplicates += 1
                    continue
                # The original was moved: follow it so prepare finds the file.
                assert known.id is not None
                store.update_photo(known.id, src_path=str(item.path))
                summary.already_known += 1
                continue
            if known:
                summary.already_known += 1
                continue
            seen.add(item.sha256)
            batch = (
                _safe_batch_name(batch_name)
                if batch_name
                else batch_id_for(item, cfg.import_.batch_by, root)
            )
            if notes is not None:
                store.upsert_batch(batch, notes=notes, source=str(root))
            match = None
            if history:
                best = min(history, key=lambda h: hamming(h[0], item.dhash))
                if hamming(best[0], item.dhash) <= distance:
                    match = best[1]
            status = (
                Status.UPLOADED
                if mark_uploaded
                else Status.ALREADY_UPLOADED
                if match
                else Status.IMPORTED
            )
            pid = store.add_photo(
                status=status,
                uploaded_at=_now_iso() if mark_uploaded else None,
                sha256=item.sha256,
                src_path=str(item.path),
                orig_name=item.path.name,
                batch_id=batch,
                dhash=item.dhash,
                width=item.info.width,
                height=item.info.height,
                captured_at=item.info.captured.isoformat() if item.info.captured else None,
                camera=item.info.camera,
                has_gps=item.info.has_gps,
            )
            if mark_uploaded:
                summary.marked_uploaded += 1
                continue
            try:
                imaging.make_thumbnail(
                    item.path, cfg.workspace_path("thumbs", f"{item.sha256}.jpg")
                )
            except Exception as exc:
                errors.append(f"{item.path}: thumbnail failed: {exc}")
            if match:
                store.log_event(
                    "already_uploaded",
                    photo_id=pid,
                    batch_id=batch,
                    detail={"look_alike_of": match.id, "submit_name": match.submit_name},
                )
                summary.already_uploaded.append(
                    AlreadyUploaded(
                        item.path.name,
                        match.uploaded_at,
                        match.submit_name,
                        rejected=match.status == Status.REJECTED,
                    )
                )
                continue
            new_items.setdefault(batch, []).append((pid, item))
            summary.imported += 1
            summary.photo_ids.append(pid)
            summary.batches[batch] = summary.batches.get(batch, 0) + 1

    if summary.marked_uploaded:
        refresh_ledger(cfg, store)
    for entries in new_items.values():
        scan = [item for _, item in entries]
        for group in group_near_duplicates(scan, distance):
            scores = []
            for i in group:
                try:
                    scores.append(cull_mod.evaluate(scan[i].path))
                except Exception:
                    scores.append(cull_mod.CullResult(ok=False))
            best = cull_mod.pick_best(scores)
            group_id = entries[group[0]][0]
            for pos, i in enumerate(group):
                pid = entries[i][0]
                store.update_photo(pid, near_dup_group=group_id)
                if pos != best:
                    store.set_status(pid, Status.NEAR_DUPLICATE)
                    summary.near_duplicates += 1
    return summary


@dataclass
class CullSummary:
    total: int = 0
    ready: int = 0
    culled: int = 0
    details: list[dict[str, Any]] = field(default_factory=list)  # culled photos with reasons


def cull_batch(
    cfg: Config,
    store: Store,
    batch_id: str,
    *,
    explain: bool = False,
    progress: Progress | None = None,
    cancel: Cancel | None = None,
) -> CullSummary:
    """Technical check of `imported` photos: -> `ready` or `culled` (reasons stored in cull)."""
    progress = progress or _noop
    thresholds = cull_mod.CullThresholds(**cfg.cull.model_dump())
    photos = store.list_photos(batch_id, [Status.IMPORTED])
    summary = CullSummary(total=len(photos))
    for n, photo in enumerate(photos, 1):
        if cancel and cancel():
            break
        progress(n, len(photos), photo.orig_name)
        try:
            res = cull_mod.evaluate(photo.src_path, thresholds)
            data = asdict(res)
        except Exception as exc:
            data = {"ok": False, "reasons": [f"unreadable: {exc}"]}
        assert photo.id is not None
        store.update_photo(photo.id, cull=data)
        if data["ok"]:
            store.set_status(photo.id, Status.READY)
            summary.ready += 1
        else:
            store.set_status(photo.id, Status.CULLED)
            summary.culled += 1
            summary.details.append(
                {"id": photo.id, "name": photo.orig_name, "reasons": list(data["reasons"])}
            )
    return summary


# --------------------------------------------------------------------------- LLM selection


@dataclass
class Selection:
    provider: str
    model: str
    source: str  # flag | settings | config


def api_catalog() -> list[local_models.ApiModel]:
    return local_models.load_catalog()[1]


def api_model(provider: str, model: str | None = None) -> local_models.ApiModel | None:
    """Catalog entry for provider/model; model None or `auto` gives the provider default."""
    entries = [a for a in api_catalog() if a.provider == provider]
    if model and model != "auto":
        return next((a for a in entries if a.model == model), None)
    return next((a for a in entries if a.default), entries[0] if entries else None)


def resolve_selection(
    cfg: Config, store: Store | None, provider: str | None = None, model: str | None = None
) -> Selection:
    """flag > saved choice (settings table / model_choice.json) > config > auto.

    Raises local_models.ModelChoiceRequired for ollama with `auto` and no saved choice.
    """
    saved_provider = store.get_setting("llm.provider") if store else None
    saved_model = store.get_setting("llm.model") if store else None
    prov = provider or saved_provider or cfg.llm.provider
    source = "flag" if provider else "settings" if saved_provider else "config"
    if model:
        chosen, source = model, "flag"
    elif saved_model and prov == saved_provider:
        chosen, source = saved_model, "settings"
    elif prov == cfg.llm.provider:
        chosen = cfg.llm.model
    else:
        chosen = "auto"
    if prov == "ollama":
        return Selection(prov, local_models.resolve_model(chosen, model_choice_path()), source)
    if prov == "fake":
        return Selection(prov, "fake", source)
    if chosen == "auto":
        entry = api_model(prov)
        if entry is None:
            raise PipelineError(f"No default model for provider {prov!r} in data/models.yaml.")
        chosen = entry.model
    return Selection(prov, chosen, source)


def ollama_client(cfg: Config) -> OllamaClient:
    return OllamaClient(cfg.llm.ollama.url, cfg.llm.timeout_s)


def resolve_llm(
    cfg: Config, store: Store | None, provider: str | None = None, model: str | None = None
) -> VisionLLM:
    """Build the provider. Model ids for API providers come from data/models.yaml (catalog),
    so the constructor fallbacks inside the provider classes are never used."""
    sel = resolve_selection(cfg, store, provider, model)
    if sel.provider == "fake":
        return FakeProvider(sel.model)
    if sel.provider == "ollama":
        return OllamaProvider(
            sel.model,
            url=cfg.llm.ollama.url,
            keep_alive=cfg.llm.ollama.keep_alive,
            timeout_s=cfg.llm.timeout_s,
        )
    if sel.provider == "bedrock":
        return get_provider(
            "bedrock",
            sel.model,
            region=cfg.llm.bedrock.region,
            profile=cfg.llm.bedrock.profile,
            timeout_s=cfg.llm.timeout_s,
        )
    secret = API_KEY_SECRETS[sel.provider]
    key = get_secret(secret)
    if not key:
        raise PipelineError(
            f"{sel.provider} needs an API key: set {secret} (environment, .env file or "
            f"`shuto secrets set {secret}`)."
        )
    return get_provider(sel.provider, sel.model, api_key=key)


# --------------------------------------------------------------------------- hardware / models

_hw_lock = threading.Lock()
_hw_cache: list[hardware.HardwareProfile] = []


def hardware_profile(refresh: bool = False) -> hardware.HardwareProfile:
    with _hw_lock:
        if refresh or not _hw_cache:
            _hw_cache[:] = [hardware.detect()]
        return _hw_cache[0]


def _client_or_default(cfg: Config, client: Any) -> Any:
    return client if client is not None else ollama_client(cfg)


def list_models(
    cfg: Config,
    *,
    client: Any = None,
    profile: hardware.HardwareProfile | None = None,
    n: int = 8,
) -> list[local_models.ModelRow]:
    """Top-n runnable local models for this machine (never picks one for the user)."""
    profile = profile or hardware_profile()
    status = hardware.ollama_status(_client_or_default(cfg, client))
    return local_models.top_models(
        profile,
        local_models.load_catalog(),
        status.pulled,
        local_models.load_benchmarks(benchmark_cache_path()),
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


def doctor(
    cfg: Config,
    store: Store | None = None,
    *,
    client: Any = None,
    profile: hardware.HardwareProfile | None = None,
    config_path: str | os.PathLike[str] | None = None,
) -> DoctorReport:
    profile = profile or hardware_profile()
    client = _client_or_default(cfg, client)
    status = hardware.ollama_status(client)
    models_dir = hardware.ollama_models_dir()
    free = hardware.free_gb(models_dir)
    rows = local_models.top_models(
        profile,
        local_models.load_catalog(),
        status.pulled,
        local_models.load_benchmarks(benchmark_cache_path()),
        free,
    )
    unreadable = False
    try:
        selection = resolve_selection(cfg, store)
        selected: str | None = f"{selection.provider}:{selection.model}"
        provider = selection.provider
    except local_models.ModelChoiceRequired:
        selected, provider = None, cfg.llm.provider
    except Exception:
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
        for p in hardware.ollama_status(_client_or_default(cfg, client)).pulled
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
    _client_or_default(cfg, client).pull(tag, progress)


def use_model(cfg: Config, store: Store, spec: str) -> Selection:
    """Persist an explicit choice. `TAG` = local Ollama model; `PROVIDER:MODEL` = API provider
    (`PROVIDER:` alone = that provider's catalog default)."""
    prov, sep, mod = spec.partition(":")
    if sep and prov in (*API_KEY_SECRETS, "bedrock"):
        entry = api_model(prov, mod or None)
        if entry is None:
            raise PipelineError(f"{spec!r} is not in data/models.yaml for provider {prov}.")
        store.set_setting("llm.provider", prov)
        store.set_setting("llm.model", entry.model)
        return Selection(prov, entry.model, "settings")
    local_models.save_choice(model_choice_path(), spec)
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
    profile = profile or hardware_profile()
    tag = model or resolve_selection(cfg, store, provider="ollama").model
    provider = OllamaProvider(
        tag,
        url=cfg.llm.ollama.url,
        keep_alive=cfg.llm.ollama.keep_alive,
        timeout_s=cfg.llm.timeout_s,
        client=_client_or_default(cfg, client),
    )
    return local_models.benchmark(
        provider, local_models.sample_jpeg_bytes(), benchmark_cache_path(), profile.fingerprint
    )


def _sdk_installed(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


def _api_preflight(cfg: Config, sel: Selection) -> list[str]:
    problems: list[str] = []
    prov = sel.provider
    if not _sdk_installed(API_SDK_MODULES[prov]):
        problems.append(f"The {prov} SDK is not installed. Run: pip install 'shuto[{prov}]'.")
    if prov in API_KEY_SECRETS:
        secret = API_KEY_SECRETS[prov]
        if not get_secret(secret):
            problems.append(
                f"{secret} is not set (environment, .env file or `shuto secrets set {secret}`)."
            )
    elif prov == "bedrock" and not problems:
        try:
            import boto3

            session = boto3.Session(profile_name=cfg.llm.bedrock.profile or None)
            if session.get_credentials() is None:
                problems.append(
                    "No AWS credentials found for Bedrock (set the profile in the config or "
                    "run `aws configure`)."
                )
        except Exception as exc:
            problems.append(f"Bedrock credentials could not be loaded: {exc}")
    return problems


def api_quick_problems(sel: Selection) -> list[str]:
    """Cheap check for an online AI: SDK importable and access key saved. No network, no
    credential lookups (used for the app-wide banner; jobs use `describe_preflight`)."""
    if sel.provider not in API_SDK_MODULES:
        return []
    problems: list[str] = []
    if not _sdk_installed(API_SDK_MODULES[sel.provider]):
        problems.append("sdk")
    secret = API_KEY_SECRETS.get(sel.provider)
    if secret and not get_secret(secret):
        problems.append("key")
    return problems


def describe_preflight(cfg: Config, sel: Selection, *, client: Any = None) -> list[str]:
    """Problems that would make every describe call fail (Ollama down, model not pulled,
    API key or SDK missing)."""
    if sel.provider in API_SDK_MODULES:
        return _api_preflight(cfg, sel)
    if sel.provider != "ollama":
        return []
    status = hardware.ollama_status(_client_or_default(cfg, client))
    if not status.running:
        return [f"Ollama is not running at {status.url}. Start it, or run `shuto doctor`."]
    have = {hardware.normalize_tag(p) for p in status.pulled}
    if hardware.normalize_tag(sel.model) not in have:
        return [f"Model {sel.model} is not downloaded. Run `shuto models pull {sel.model}`."]
    return []


# --------------------------------------------------------------------------- describe


def _count(store: Store, batch_id: str, only_ids: Iterable[int] | None) -> int:
    return len(_describe_targets(store, batch_id, only_ids))


def _describe_targets(store: Store, batch_id: str, only_ids: Iterable[int] | None) -> list[Photo]:
    if only_ids is None:
        return store.list_photos(batch_id, [Status.READY])
    wanted = set(only_ids)
    return [p for p in store.list_photos(batch_id) if p.id in wanted and p.status in _REDESCRIBE]


_REDESCRIBE = {
    Status.READY,
    Status.CULLED,
    Status.NEAR_DUPLICATE,
    Status.ALREADY_UPLOADED,
    Status.DUPLICATE,
    Status.DESCRIBED,
    Status.NEEDS_REVIEW,
    Status.BLOCKED,
    Status.APPROVED,
    Status.PREPARED,
}


def estimate_describe(
    cfg: Config,
    store: Store,
    batch_id: str,
    *,
    only_ids: Iterable[int] | None = None,
    provider: str | None = None,
    model: str | None = None,
    profile: hardware.HardwareProfile | None = None,
) -> estimate.Estimate:
    """Time (local) or cost (API) for the photos describe would process."""
    sel = resolve_selection(cfg, store, provider, model)
    n = _count(store, batch_id, only_ids)
    llm_cls = get_provider_class(sel.provider)
    side = cfg.llm.image_max_side or llm_cls.default_image_side
    conc = cfg.llm.concurrency or llm_cls.default_concurrency
    if sel.provider == "ollama":
        profile = profile or hardware_profile()
        local_catalog = local_models.load_catalog()
        recs = local_models.recommend(
            profile,
            local_catalog,
            [],
            local_models.load_benchmarks(benchmark_cache_path()),
        )
        rec = next((r for r in recs if r.model.tag == sel.model), None)
        return estimate.for_batch(n, "ollama", sel.model, recommendation=rec, concurrency=conc)
    return estimate.for_batch(
        n,
        sel.provider,
        sel.model,
        api_model=api_model(sel.provider, sel.model),
        concurrency=conc,
        image_side=side,
    )


def get_provider_class(provider: str):
    """Provider class without instantiating it (for default_image_side / default_concurrency)."""

    from .llm import PROVIDERS

    mod_name, cls_name = PROVIDERS[provider].split(":")
    try:
        return getattr(importlib.import_module(mod_name), cls_name)
    except ImportError:
        return _Defaults


class _Defaults:
    default_image_side = 1024
    default_concurrency = 1


@lru_cache(maxsize=8)
def _restrictions(
    builtin: bool, extra: tuple[str, ...], disabled: tuple[str, ...], house: bool
) -> guardrails.Restrictions:
    return guardrails.load_restrictions(builtin, extra, disabled, house_rules=house)


def load_restrictions(cfg: Config) -> guardrails.Restrictions:
    r = cfg.restrictions
    return _restrictions(
        r.builtin,
        tuple(str(p) for p in r.extra_files),
        tuple(r.disabled_rules),
        cfg.style.region_focus == "japan",
    )


def _release_flags(release_ids: list[str]) -> tuple[bool, bool]:
    has_property = any(r.startswith("property:") for r in release_ids)
    has_model = any(not r.startswith("property:") for r in release_ids)
    return has_model, has_property


def _status_for(meta: PhotoMetadata, gr: guardrails.GuardrailResult) -> Status:
    if gr.action == "block":
        return Status.BLOCKED
    extra = [w for w in meta.warnings if w not in gr.messages]
    if gr.action == "review" or extra:
        return Status.NEEDS_REVIEW
    return Status.DESCRIBED


def _evaluate(
    cfg: Config, store: Store, photo: Photo, data: dict[str, Any]
) -> tuple[PhotoMetadata, guardrails.GuardrailResult, Status]:
    """Normalize hand-edited metadata and re-run the deterministic guardrails."""
    batch = store.get_batch(photo.batch_id) if photo.batch_id else None
    has_model, has_property = _release_flags(photo.release_ids)
    meta = normalize(photo.orig_name, data)
    meta, gr = guardrails.check(
        meta,
        photo.detected,
        load_restrictions(cfg),
        notes=batch.notes if batch else "",
        has_model_release=has_model,
        has_property_release=has_property,
    )
    meta.warnings.extend(m for m in gr.messages if m not in meta.warnings)
    return meta, gr, _status_for(meta, gr)


@dataclass
class DescribeSummary:
    described: int = 0
    needs_review: int = 0
    blocked: int = 0
    failed: int = 0
    approved: int = 0
    errors: list[tuple[int, str]] = field(default_factory=list)
    aborted: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0


def _drop_prepared(store: Store, photo: Photo) -> None:
    if photo.prepared_path:
        try:
            Path(photo.prepared_path).unlink(missing_ok=True)
        except OSError:
            pass
        assert photo.id is not None
        store.update_photo(photo.id, prepared_path=None, submit_name=None)


def describe_batch(
    cfg: Config,
    store: Store,
    batch_id: str,
    only_ids: Iterable[int] | None = None,
    llm: VisionLLM | None = None,
    *,
    progress: Progress | None = None,
    cancel: Cancel | None = None,
) -> DescribeSummary:
    """Describe `ready` photos (or the given ids, which are sent back to `ready` first).

    Culled photos never reach the model. Result per photo: described / needs_review / blocked.
    Stops early when the provider is unreachable or several calls in a row fail.
    """
    progress = progress or _noop
    llm = llm or resolve_llm(cfg, store)
    photos = _describe_targets(store, batch_id, only_ids)
    summary = DescribeSummary()
    if not photos:
        return summary
    batch = store.get_batch(batch_id)
    notes = batch.notes if batch else ""
    restrictions = load_restrictions(cfg)
    style = describe.Style.from_config(cfg.style)
    feedback = describe.feedback_lines(store.rejection_stats(), store.recent_rejection_examples())
    side = cfg.llm.image_max_side or llm.default_image_side
    conc = max(1, cfg.llm.concurrency or llm.default_concurrency)
    lock = threading.Lock()
    stop = threading.Event()
    consecutive = [0]

    def work(photo: Photo) -> tuple[Photo, Status | None, str | None]:
        if stop.is_set() or (cancel and cancel()):
            return photo, None, None
        pid = photo.id
        assert pid is not None
        try:
            image = imaging.preview_jpeg(photo.src_path, side)
            has_model, has_property = _release_flags(photo.release_ids)
            res = describe.describe_photo(
                llm,
                image,
                photo.orig_name,
                style=style,
                restrictions=restrictions,
                notes=notes,
                captured=_parse_dt(photo.captured_at),
                feedback=feedback,
                max_tokens=cfg.llm.max_tokens,
                temperature=cfg.llm.temperature,
                retries=cfg.llm.json_retries,
                timeout_s=cfg.llm.timeout_s,
                has_model_release=has_model,
                has_property_release=has_property,
            )
        except LLMError as exc:
            store.log_llm_call(
                LLMCall(llm.name, llm.model, photo_id=pid, ok=False, error=str(exc)[:500])
            )
            with lock:
                consecutive[0] += 1
                if "is not running" in str(exc) or consecutive[0] >= 3:
                    stop.set()
                    summary.aborted = str(exc)
            return photo, None, str(exc)
        except Exception as exc:
            return photo, None, f"{type(exc).__name__}: {exc}"
        with lock:
            consecutive[0] = 0
        r = res.llm
        store.log_llm_call(
            LLMCall(
                llm.name,
                llm.model,
                photo_id=pid,
                input_tokens=r.input_tokens,
                output_tokens=r.output_tokens,
                seconds=r.seconds,
                repaired=r.repaired,
                retries=r.retries,
            )
        )
        meta = res.metadata.to_dict()
        meta["location"] = res.location
        _drop_prepared(store, photo)
        if photo.status != Status.READY:
            store.set_status(pid, Status.READY)
        store.update_photo(
            pid,
            metadata=meta,
            metadata_source=f"llm:{llm.name}/{llm.model}",
            detected=res.detected,
            guardrail=res.guardrails.to_dict(),
        )
        status = _status_for(res.metadata, res.guardrails)
        store.set_status(pid, status)
        if status == Status.DESCRIBED and cfg.review.auto_approve_clean:
            store.set_status(pid, Status.APPROVED, detail={"auto": True})
            with lock:
                summary.approved += 1
        with lock:
            summary.input_tokens += r.input_tokens
            summary.output_tokens += r.output_tokens
        return photo, status, None

    done = 0
    with ThreadPoolExecutor(max_workers=conc) as pool:
        futures = [pool.submit(work, p) for p in photos]
        for fut in as_completed(futures):
            photo, status, err = fut.result()
            done += 1
            progress(done, len(photos), photo.orig_name)
            if status == Status.BLOCKED:
                summary.blocked += 1
            elif status == Status.NEEDS_REVIEW:
                summary.needs_review += 1
            elif status == Status.DESCRIBED:
                summary.described += 1
            elif err:
                summary.failed += 1
                assert photo.id is not None
                summary.errors.append((photo.id, err))
    return summary


# --------------------------------------------------------------------------- review


def get_photo(store: Store, photo_id: int) -> Photo:
    photo = store.get_photo(photo_id)
    if photo is None:
        raise PipelineError(f"No photo with id {photo_id}.")
    return photo


def find_photo(store: Store, ref: str | int) -> Photo:
    """Photo by id, prepared (submit) name or original file name (must be unambiguous)."""
    text = str(ref).strip()
    if text.isdigit():
        return get_photo(store, int(text))
    by_name = store.find_by_submit_name(text)
    if by_name:
        return by_name
    matches = [p for p in store.list_photos() if p.orig_name.lower() == text.lower()]
    if not matches:
        raise PipelineError(f"No photo matches {text!r}.")
    if len(matches) > 1:
        ids = ", ".join(str(p.id) for p in matches)
        raise PipelineError(f"{text!r} is ambiguous (photo ids: {ids}); use an id.")
    return matches[0]


def update_metadata(
    cfg: Config,
    store: Store,
    photo_id: int,
    *,
    description: str | None = None,
    title: str | None = None,
    keywords: list[str] | None = None,
    categories: list[str] | None = None,
    editorial: bool | None = None,
    mature: bool | None = None,
    release_ids: list[str] | None = None,
) -> Photo:
    """Edit metadata; guardrails re-run on every save and decide the new status.

    A prepared photo goes back to approved (its stale copy is deleted); an approved photo that
    now violates a guardrail drops to needs_review/blocked.
    """
    photo = get_photo(store, photo_id)
    if photo.status not in PRE_UPLOAD_EDITABLE:
        raise PipelineError(f"Photo {photo_id} is {photo.status.value}; it cannot be edited now.")
    if release_ids is not None:
        store.update_photo(photo_id, release_ids=list(release_ids))
        photo = get_photo(store, photo_id)
    data = dict(photo.metadata or {})
    for key, value in (
        ("description", description),
        ("title", title),
        ("keywords", keywords),
        ("categories", categories),
        ("editorial", editorial),
        ("mature", mature),
    ):
        if value is not None:
            data[key] = value
    meta, gr, status = _evaluate(cfg, store, photo, data)
    stored = meta.to_dict()
    for extra in ("title", "location"):
        if extra in data:
            stored[extra] = data[extra]
    was = photo.status
    if was == Status.PREPARED:
        _drop_prepared(store, photo)
        store.set_status(photo_id, Status.APPROVED)
        was = Status.APPROVED
    store.update_photo(photo_id, metadata=stored, guardrail=gr.to_dict(), metadata_source="manual")
    if was == Status.APPROVED:
        if status in (Status.NEEDS_REVIEW, Status.BLOCKED):
            store.set_status(photo_id, status, force=True, detail={"reason": "edited"})
    elif status != was:
        store.set_status(photo_id, status)
    return get_photo(store, photo_id)


@dataclass
class ApproveSummary:
    approved: list[int] = field(default_factory=list)
    refused: dict[int, str] = field(default_factory=dict)


def approve(
    cfg: Config,
    store: Store,
    *,
    batch_id: str | None = None,
    photo_ids: Iterable[int] | None = None,
    include_needs_review: bool = False,
) -> ApproveSummary:
    """Manual approval. Guardrails are re-checked; blocked photos are always refused."""
    summary = ApproveSummary()
    if photo_ids is not None:
        photos = [get_photo(store, i) for i in photo_ids]
    else:
        statuses = [Status.DESCRIBED] + ([Status.NEEDS_REVIEW] if include_needs_review else [])
        photos = store.list_photos(batch_id, statuses)
    for photo in photos:
        pid = photo.id
        assert pid is not None
        if photo.status == Status.APPROVED:
            continue
        if photo.status not in (Status.DESCRIBED, Status.NEEDS_REVIEW):
            summary.refused[pid] = f"status is {photo.status.value}"
            continue
        meta, gr, _ = _evaluate(cfg, store, photo, photo.metadata or {})
        if gr.action == "block":
            store.update_photo(pid, guardrail=gr.to_dict())
            store.set_status(pid, Status.BLOCKED)
            summary.refused[pid] = "blocked by guardrails: " + "; ".join(gr.messages[:2])
            continue
        store.set_status(pid, Status.APPROVED)
        summary.approved.append(pid)
    return summary


_KEEPS_PREPARED = {
    Status.PREPARED,
    Status.UPLOADED,
    Status.EXPORTED,
    Status.SUBMITTED,
    Status.ACCEPTED,
    Status.REJECTED,
}


def override_status(store: Store, photo_id: int, status: Status | str) -> Photo:
    """Manual status change that follows models.ALLOWED (e.g. culled -> ready, any -> ready)."""
    before = get_photo(store, photo_id)
    try:
        store.set_status(photo_id, Status(status))
    except InvalidTransition as exc:
        raise PipelineError(str(exc)) from exc
    if Status(status) not in _KEEPS_PREPARED:
        _drop_prepared(store, before)
    return get_photo(store, photo_id)


# --------------------------------------------------------------------------- deliver


@dataclass
class PrepareSummary:
    prepared: list[str] = field(default_factory=list)
    problems: dict[int, list[str]] = field(default_factory=dict)


def prepare_batch(
    cfg: Config,
    store: Store,
    batch_id: str,
    *,
    only_ids: Iterable[int] | None = None,
    progress: Progress | None = None,
    cancel: Cancel | None = None,
) -> PrepareSummary:
    """Approved photos -> safe-named JPEG copies in <workspace>/prepared/<batch> with IPTC/XMP."""
    progress = progress or _noop
    photos = store.list_photos(batch_id, [Status.APPROVED])
    if only_ids is not None:
        wanted = set(only_ids)
        photos = [p for p in photos if p.id in wanted]
    taken = {p.submit_name.lower() for p in store.list_photos() if p.submit_name}
    dest_dir = cfg.workspace_path("prepared", _safe_dir(batch_id))
    if dest_dir.is_dir():
        taken |= {f.name.lower() for f in dest_dir.iterdir()}
    n = sum(1 for p in store.list_photos(batch_id) if p.submit_name)
    summary = PrepareSummary()
    for i, photo in enumerate(photos, 1):
        if cancel and cancel():
            break
        pid = photo.id
        assert pid is not None
        progress(i, len(photos), photo.orig_name)
        meta = dict(photo.metadata or {})
        gr = (photo.guardrail or {}).get("action")
        problems = release_check(meta, photo.detected, photo.release_ids)
        if gr == "block":
            problems.append("Blocked by guardrails.")
        if problems:
            summary.problems[pid] = problems
            continue
        n += 1
        title = str(meta.get("title") or meta.get("description") or photo.orig_name)
        name = safe_filename(
            _parse_dt(photo.captured_at),
            title,
            n,
            cfg.prepare.filename_pattern,
            cfg.prepare.slug_words,
            taken,
        )
        try:
            dest = prepare_file(
                Path(photo.src_path), dest_dir, name, meta, cfg.prepare.jpeg_quality
            )
        except Exception as exc:
            summary.problems[pid] = [f"prepare failed: {exc}"]
            continue
        store.update_photo(pid, prepared_path=str(dest), submit_name=name)
        store.set_status(pid, Status.PREPARED)
        summary.prepared.append(name)
    return summary


def _safe_dir(batch_id: str) -> str:
    return "".join(c if c.isalnum() or c in "._-" else "-" for c in batch_id) or "batch"


@dataclass
class UploadSummary:
    uploaded: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)
    skipped_blocked: list[str] = field(default_factory=list)


def upload_batch(
    cfg: Config,
    store: Store,
    batch_id: str,
    *,
    confirm: bool = False,
    only_ids: Iterable[int] | None = None,
    progress: Progress | None = None,
    creds: deliver.FtpsCreds | None = None,
    connect: Callable[..., object] | None = None,
) -> UploadSummary:
    """FTPS-upload prepared photos. Refused unless deliver.upload_enabled or confirm=True."""
    if not (cfg.deliver.upload_enabled or confirm):
        raise UploadRefused(
            "Uploading is disabled. Set deliver.upload_enabled: true in the config, or confirm "
            "this upload explicitly (CLI: --yes)."
        )
    progress = progress or _noop
    photos = store.list_photos(batch_id, [Status.PREPARED])
    if only_ids is not None:
        wanted = set(only_ids)
        photos = [p for p in photos if p.id in wanted]
    summary = UploadSummary()
    by_name: dict[str, Photo] = {}
    files: list[Path] = []
    for photo in photos:
        if (photo.guardrail or {}).get("action") == "block" or not photo.prepared_path:
            summary.skipped_blocked.append(photo.orig_name)
            continue
        by_name[Path(photo.prepared_path).name] = photo
        files.append(Path(photo.prepared_path))
    if not files:
        return summary
    creds = creds or deliver.creds_from_secrets(cfg.deliver.ftps_host)
    done = [0]

    def on_result(name: str, exc: Exception | None) -> None:
        done[0] += 1
        progress(done[0], len(files), name)
        photo = by_name[name]
        assert photo.id is not None
        if exc is None:
            store.update_photo(photo.id, uploaded_at=_now_iso())
            store.set_status(photo.id, Status.UPLOADED)
            summary.uploaded.append(name)
        else:
            summary.failed[name] = str(exc) or type(exc).__name__

    kwargs = {"connect": connect} if connect is not None else {}
    deliver.upload_files(creds, files, on_result, **kwargs)
    if summary.uploaded:
        refresh_ledger(cfg, store)
    return summary


# --------------------------------------------------------------------------- ledger


def ledger_path(cfg: Config) -> Path:
    return cfg.workspace_path("uploaded.csv")


def _ledger_rows(store: Store) -> list[ledger_mod.LedgerRow]:
    return [
        ledger_mod.LedgerRow(
            p.sha256,
            p.dhash,
            p.orig_name,
            p.submit_name or "",
            p.uploaded_at or "",
            p.batch_id or "",
        )
        for p in store.list_photos(statuses=UPLOADED_STATES)
    ]


def export_ledger(store: Store, path: str | Path, *, keep_existing: bool = False) -> int:
    """Write the uploaded photos to a ledger CSV; returns the number of rows written.

    With `keep_existing`, rows already in the file but not in the database are kept (so a ledger
    restored from backup is never shrunk by a workspace that is missing those photos).
    """
    rows = _ledger_rows(store)
    if keep_existing:
        try:
            old = ledger_mod.read_ledger(path)
        except (UnicodeDecodeError, csv.Error):
            ledger_mod.back_up_corrupt(path)
            old = []
        rows = ledger_mod.merge(old, rows)
    ledger_mod.write_ledger(path, rows)
    return len(rows)


def refresh_ledger(cfg: Config, store: Store) -> None:
    """Rewrite <workspace>/uploaded.csv after an upload. A file problem never fails the upload."""
    try:
        export_ledger(store, ledger_path(cfg), keep_existing=True)
    except (OSError, ValueError, csv.Error) as exc:
        store.log_event("ledger_failed", detail={"error": str(exc)})


@dataclass
class LedgerImportSummary:
    added: int = 0
    already_known: int = 0
    marked: int = 0


def import_ledger(cfg: Config, store: Store, path: str | Path) -> LedgerImportSummary:
    """Merge a ledger CSV into the database as uploaded photos (idempotent, keyed by sha256)."""
    p = Path(path).expanduser()
    if not p.is_file():
        raise PipelineError(f"Ledger file not found: {p}")
    try:
        rows = ledger_mod.read_ledger(p)
    except (OSError, UnicodeDecodeError, csv.Error) as exc:
        raise PipelineError(f"Could not read the ledger {p}: {exc}") from exc
    summary = LedgerImportSummary()
    for row in rows:
        known = store.find_by_sha(row.sha256)
        if known and known.status in UPLOADED_STATES:
            summary.already_known += 1
            continue
        if known:
            assert known.id is not None
            store.update_photo(known.id, uploaded_at=row.uploaded_at or _now_iso())
            store.set_status(known.id, Status.UPLOADED, force=True, detail={"ledger": True})
            summary.marked += 1
            continue
        fields: dict[str, Any] = {
            "sha256": row.sha256,
            "src_path": "(from ledger)",
            "orig_name": row.orig_name or row.sha256[:12],
            "batch_id": row.batch_id or "uploaded-before",
            "status": Status.UPLOADED,
            "uploaded_at": row.uploaded_at or _now_iso(),
        }
        if row.dhash is not None:
            fields["dhash"] = row.dhash
        if row.submit_name and not store.find_by_submit_name(row.submit_name):
            fields["submit_name"] = row.submit_name
        store.add_photo(**fields)
        summary.added += 1
    if summary.added or summary.marked:
        refresh_ledger(cfg, store)
    return summary


@dataclass
class ExportSummary:
    files: list[Path] = field(default_factory=list)
    rows: list[dict[str, Any]] = field(default_factory=list)
    dry_run: bool = False


def export_rows(photos: Iterable[Photo]) -> list[dict[str, Any]]:
    rows = []
    for p in photos:
        m = p.metadata or {}
        rows.append(
            {
                "filename": p.submit_name,
                "description": m.get("description", ""),
                "keywords": list(m.get("keywords") or []),
                "categories": list(m.get("categories") or []),
                "illustration": bool(m.get("illustration")),
                "mature": bool(m.get("mature")),
                "editorial": bool(m.get("editorial")),
            }
        )
    return rows


def export_batch(
    cfg: Config,
    store: Store,
    batch_id: str,
    *,
    dry_run: bool = False,
    now: datetime | None = None,
) -> ExportSummary:
    """Write the Shutterstock bulk CSVs for uploaded photos (filenames = uploaded names)."""
    photos = [p for p in store.list_photos(batch_id, [Status.UPLOADED, Status.EXPORTED])]
    photos = [p for p in photos if p.submit_name]
    rows = export_rows(photos)
    summary = ExportSummary(rows=rows, dry_run=dry_run)
    if dry_run or not rows:
        return summary
    now = now or datetime.now()
    files = deliver.write_csvs(batch_id, rows, cfg.workspace_path("exports"), now)
    summary.files = files
    by_kind = {kind: f for kind, f in zip(split_by_kind(rows), files, strict=True)}
    for photo, row in zip(photos, rows, strict=True):
        assert photo.id is not None
        kind = "editorial" if row["editorial"] else "commercial"
        store.update_photo(photo.id, exported_at=_now_iso(), export_file=str(by_kind[kind]))
        if photo.status == Status.UPLOADED:
            store.set_status(photo.id, Status.EXPORTED)
    return summary


def mark_submitted(store: Store, batch_id: str) -> list[int]:
    """After the manual Submit on the Shutterstock portal: exported -> submitted."""
    ids = []
    for photo in store.list_photos(batch_id, [Status.EXPORTED]):
        assert photo.id is not None
        store.update_photo(photo.id, submitted_at=_now_iso())
        store.set_status(photo.id, Status.SUBMITTED)
        ids.append(photo.id)
    return ids


# --------------------------------------------------------------------------- tracking


def record_result(
    store: Store,
    photo_id: int,
    accepted: bool,
    reason_code: str | None = None,
    note: str | None = None,
) -> Photo:
    """Record Shutterstock's verdict. Rejection reasons feed later describe prompts."""
    photo = get_photo(store, photo_id)
    if not accepted:
        if reason_code not in REASON_CODES:
            raise PipelineError("Reason must be one of: " + ", ".join(REASON_CODES))
    if photo.status == Status.EXPORTED:
        store.update_photo(photo_id, submitted_at=_now_iso())
        store.set_status(photo_id, Status.SUBMITTED)
    try:
        store.set_status(photo_id, Status.ACCEPTED if accepted else Status.REJECTED)
    except InvalidTransition as exc:
        raise PipelineError(
            f"Photo {photo_id} is {photo.status.value}; only submitted photos get a result."
        ) from exc
    if not accepted and reason_code:
        store.add_rejection(photo_id, reason_code, note)
    return get_photo(store, photo_id)


def batch_report(store: Store, batch_id: str | None = None) -> list[dict[str, Any]]:
    """Per-batch status counts and LLM usage (all batches when batch_id is None)."""
    out = []
    for batch, counts in store.list_batches():
        if batch_id is not None and batch.id != batch_id:
            continue
        out.append(
            {
                "id": batch.id,
                "notes": batch.notes,
                "created_at": batch.created_at,
                "counts": {s.value: n for s, n in counts.items()},
                "total": sum(counts.values()),
                "usage": store.usage_by_batch(batch.id),
            }
        )
    return out


# --------------------------------------------------------------------------- batch services (web)


@dataclass
class CheckSummary:
    cull: CullSummary
    describe: DescribeSummary | None = None


def check_batch(
    cfg: Config,
    store: Store,
    batch_id: str,
    *,
    progress: Progress | None = None,
    cancel: Cancel | None = None,
    llm: VisionLLM | None = None,
) -> CheckSummary:
    """Quality check, then titles and keywords, with one combined progress.

    Raises ModelChoiceRequired / PipelineError (friendly text) before doing any work when the
    AI cannot run. `describe` is None when cancelled before the second step.
    """
    progress = progress or _noop
    if llm is None:
        problems = describe_preflight(cfg, resolve_selection(cfg, store))
        if problems:
            raise PipelineError(" ".join(problems))
        llm = resolve_llm(cfg, store)
    to_cull = len(store.list_photos(batch_id, [Status.IMPORTED]))
    total = to_cull + len(store.list_photos(batch_id, [Status.READY]))

    def cull_progress(done: int, _total: int, _message: str = "") -> None:
        progress(done, total, "Quality check")

    def describe_progress(done: int, _total: int, _message: str = "") -> None:
        progress(min(to_cull + done, total), total, "Writing titles & keywords")

    culled = cull_batch(cfg, store, batch_id, progress=cull_progress, cancel=cancel)
    if cancel and cancel():
        return CheckSummary(culled, None)
    described = describe_batch(
        cfg, store, batch_id, llm=llm, progress=describe_progress, cancel=cancel
    )
    return CheckSummary(culled, described)


def estimate_check(cfg: Config, store: Store, batch_id: str) -> estimate.Estimate:
    """Like estimate_describe for imported + ready photos (an upper bound: some get culled)."""
    sel = resolve_selection(cfg, store)
    n = len(store.list_photos(batch_id, [Status.IMPORTED, Status.READY]))
    llm_cls = get_provider_class(sel.provider)
    side = cfg.llm.image_max_side or llm_cls.default_image_side
    conc = cfg.llm.concurrency or llm_cls.default_concurrency
    if sel.provider == "ollama":
        recs = local_models.recommend(
            hardware_profile(),
            local_models.load_catalog(),
            [],
            local_models.load_benchmarks(benchmark_cache_path()),
        )
        rec = next((r for r in recs if r.model.tag == sel.model), None)
        est = estimate.for_batch(n, "ollama", sel.model, recommendation=rec, concurrency=conc)
    else:
        est = estimate.for_batch(
            n,
            sel.provider,
            sel.model,
            api_model=api_model(sel.provider, sel.model),
            concurrency=conc,
            image_side=side,
        )
    return replace(est, text=f"up to {est.text}")


@dataclass
class DeliverSummary:
    prepare: PrepareSummary
    upload: UploadSummary | None = None
    export: ExportSummary | None = None


def deliver_batch(
    cfg: Config,
    store: Store,
    batch_id: str,
    *,
    confirm: bool,
    progress: Progress | None = None,
    cancel: Cancel | None = None,
    creds: deliver.FtpsCreds | None = None,
    connect: Callable[..., object] | None = None,
) -> DeliverSummary:
    """Prepare approved photos, upload them (needs `confirm`), then write the CSV files.

    One progress range of 2n+1 (n = approved + prepared photos). Stops after upload failures
    or a cancel and returns what was done so far; uploaded photos can be exported later.
    """
    if not (cfg.deliver.upload_enabled or confirm):
        raise UploadRefused(
            "Uploading is disabled. Set deliver.upload_enabled: true in the config, or confirm "
            "this upload explicitly."
        )
    progress = progress or _noop
    n = len(store.list_photos(batch_id, [Status.APPROVED, Status.PREPARED]))
    total = 2 * n + 1

    def prepare_progress(done: int, _total: int, message: str = "") -> None:
        progress(min(done, n), total, message)

    def upload_progress(done: int, _total: int, message: str = "") -> None:
        progress(min(n + done, 2 * n), total, message)

    summary = DeliverSummary(
        prepare_batch(cfg, store, batch_id, progress=prepare_progress, cancel=cancel)
    )
    if cancel and cancel():
        return summary
    summary.upload = upload_batch(
        cfg,
        store,
        batch_id,
        confirm=confirm,
        progress=upload_progress,
        creds=creds,
        connect=connect,
    )
    if summary.upload.failed or (cancel and cancel()):
        return summary
    summary.export = export_batch(cfg, store, batch_id)
    progress(total, total, "Writing the CSV files")
    return summary


@dataclass
class BulkEditSummary:
    changed: list[int] = field(default_factory=list)
    refused: dict[int, str] = field(default_factory=dict)


def _fold(words: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for w in words:
        w = str(w).strip()
        if w and w.lower() not in seen:
            seen.add(w.lower())
            out.append(w)
    return out


def bulk_edit(
    cfg: Config,
    store: Store,
    photo_ids: Iterable[int],
    *,
    add_keywords: Iterable[str] = (),
    remove_keywords: Iterable[str] = (),
    categories: list[str] | None = None,
    editorial: bool | None = None,
) -> BulkEditSummary:
    """Apply one keyword/category/editorial change to many photos (guardrails re-run on each)."""
    if categories is not None and len(categories) > 2:
        raise PipelineError("Pick at most 2 categories.")
    add = _fold(add_keywords)
    drop = {w.lower() for w in _fold(remove_keywords)}
    from .web import ux  # plain-words status names; imported late (web imports this module)

    summary = BulkEditSummary()
    for pid in photo_ids:
        photo = store.get_photo(pid)
        if photo is None:
            summary.refused[pid] = "photo not found"
            continue
        if photo.status not in PRE_UPLOAD_EDITABLE:
            summary.refused[pid] = (
                f'it is marked "{ux.label(photo.status)}" and cannot be edited now'
            )
            continue
        current = [str(k) for k in (photo.metadata or {}).get("keywords") or []]
        keywords = _fold([k for k in current if k.lower() not in drop] + add)
        try:
            update_metadata(
                cfg,
                store,
                pid,
                keywords=keywords if (add or drop) else None,
                categories=list(categories) if categories is not None else None,
                editorial=editorial,
            )
        except PipelineError as exc:
            summary.refused[pid] = str(exc)
            continue
        summary.changed.append(pid)
    return summary


def batch_summary(store: Store, batch_id: str) -> dict[str, Any]:
    """Status counts, photo total and bytes that an upload would send (approved + prepared)."""
    counts: dict[str, int] = {}
    upload_bytes = 0
    photos = store.list_photos(batch_id)
    for p in photos:
        counts[p.status.value] = counts.get(p.status.value, 0) + 1
        if p.status in (Status.APPROVED, Status.PREPARED):
            try:
                upload_bytes += Path(p.src_path).stat().st_size
            except OSError:
                continue
    return {"counts": counts, "total": len(photos), "upload_bytes": upload_bytes}


# --------------------------------------------------------------------------- UI settings

UI_SETTING_KEYS = (
    "onboarded",
    "last_folder",
    "style",
    "bedrock_profile",
    "update_check",
    "update_cache",
)
PROFILE_RE = re.compile(r"^[A-Za-z0-9_.@:+=,-]{1,100}$")
MAX_STYLE_TEXT = 200
MAX_EXTRA_INSTRUCTIONS = 2000
MAX_ALWAYS_INCLUDE = 20


def _ui_key(name: str) -> str:
    short = name.removeprefix("ui.")
    if short not in UI_SETTING_KEYS:
        raise PipelineError(f"Unknown setting: {name!r}.")
    return f"ui.{short}"


def get_ui_setting(store: Store, name: str, default: Any = None) -> Any:
    return store.get_setting(_ui_key(name), default)


REGION_FOCUS = ("", "japan")


def _clean_style(value: Any) -> dict[str, Any]:
    """Validate the style settings. A field that was submitted (even empty) is stored, so
    clearing it overrides the config default; a field that was not submitted stays unset."""
    if not isinstance(value, dict):
        raise PipelineError("Style settings must be a set of named values.")
    out: dict[str, Any] = {}
    for key in ("theme", "tone"):
        if key not in value:
            continue
        text = str(value.get(key) or "").strip()
        if len(text) > MAX_STYLE_TEXT:
            raise PipelineError(f"Keep the {key} under {MAX_STYLE_TEXT} characters.")
        out[key] = text
    if "extra_instructions" in value:
        extra = str(value.get("extra_instructions") or "").strip()
        if len(extra) > MAX_EXTRA_INSTRUCTIONS:
            raise PipelineError(f"Keep the extra notes under {MAX_EXTRA_INSTRUCTIONS} characters.")
        out["extra_instructions"] = extra
    if "always_include" in value:
        words = value.get("always_include") or []
        if isinstance(words, str):
            words = words.replace("\n", ",").split(",")
        words = _fold(words)
        if len(words) > MAX_ALWAYS_INCLUDE:
            raise PipelineError(f"Use at most {MAX_ALWAYS_INCLUDE} always-include keywords.")
        out["always_include"] = words
    if "region_focus" in value:
        focus = str(value.get("region_focus") or "").strip().casefold()
        if focus not in REGION_FOCUS:
            raise PipelineError("Region focus must be None or Japan.")
        out["region_focus"] = focus
    return out


def _clean_update_cache(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PipelineError("The update cache must be a mapping.")
    version, checked_at, ok = value.get("version"), value.get("checked_at"), value.get("ok")
    if not isinstance(version, str) or not isinstance(ok, bool):
        raise PipelineError("The update cache is not valid.")
    if isinstance(checked_at, bool) or not isinstance(checked_at, int | float):
        raise PipelineError("The update cache is not valid.")
    return {"version": version, "checked_at": float(checked_at), "ok": ok}


def set_ui_setting(store: Store, name: str, value: Any) -> Any:
    """Save a UI setting (validated for `style`); returns the stored value."""
    key = _ui_key(name)
    if key == "ui.style":
        value = _clean_style(value)
    elif key == "ui.bedrock_profile":
        value = str(value or "").strip()
        if value and not PROFILE_RE.match(value):
            raise PipelineError("That AWS profile name is not valid.")
    elif key == "ui.update_check":
        value = bool(value)
    elif key == "ui.update_cache":
        value = _clean_update_cache(value)
    store.set_setting(key, value)
    return value


def apply_style_overrides(cfg: Config, store: Store) -> Config:
    """Config copy with the style set in the web app (theme, tone, always-include, extras,
    region focus). A key that is present wins over the config file, even when empty."""
    profile = store.get_setting("ui.bedrock_profile")
    if isinstance(profile, str):
        cfg = cfg.model_copy(
            update={
                "llm": cfg.llm.model_copy(
                    update={"bedrock": cfg.llm.bedrock.model_copy(update={"profile": profile})}
                )
            }
        )
    style = store.get_setting("ui.style") or {}
    if not style:
        return cfg
    update: dict[str, Any] = {
        k: style[k]
        for k in ("theme", "tone", "extra_instructions", "region_focus")
        if isinstance(style.get(k), str)
    }
    if isinstance(style.get("always_include"), list):
        update["keywords"] = cfg.style.keywords.model_copy(
            update={"always_include": list(style["always_include"])}
        )
    return cfg.model_copy(update={"style": cfg.style.model_copy(update=update)})


# --------------------------------------------------------------------------- FTPS login


def ftps_login_status(cfg: Config) -> dict[str, bool]:
    """Which parts of the Shutterstock login are saved (never the values)."""
    return {
        "user_set": bool(get_secret("SHUTO_FTPS_USER")),
        "password_set": bool(get_secret("SHUTO_FTPS_PASSWORD")),
        "keyring_ok": keyring_available(),
    }


def test_ftps_login(cfg: Config, *, connect: Callable[..., object] | None = None) -> None:
    """Log in to the upload server and log out again; PipelineError with plain-words advice."""
    try:
        creds = deliver.creds_from_secrets(cfg.deliver.ftps_host)
    except RuntimeError as exc:
        raise PipelineError(
            "Your Shutterstock upload login is not saved yet. Enter it in Settings."
        ) from exc
    try:
        ftp = (connect or ftps.connect)(creds.host, creds.username, creds.password)
    except ftplib.error_perm as exc:
        raise PipelineError(
            "The upload server refused this email or password. Check them and try again."
        ) from exc
    except Exception as exc:
        raise PipelineError(
            f"Could not reach {creds.host}. Check your internet connection and try again "
            f"({type(exc).__name__})."
        ) from exc
    ftps.close(ftp)
