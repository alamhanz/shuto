"""Local vision-model catalog: the bundled model list, context sizes and the num_ctx we ask for.

Ranking and disk advice live in ``recommend``, the saved choice in ``model_choice`` and the
speed test in ``benchmark``. Their public names stay reachable here (loaded on first use), so
``catalog.recommend`` and ``catalog.benchmark`` keep working for callers and tests that patch
this module.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

from shuto.ai.hardware import HardwareProfile, normalize_tag
from shuto.ai.hardware import free_gb as free_gb  # seam: recommend.disk_advice reads it here

if TYPE_CHECKING:
    from shuto.ai.recommend import ModelRow

DATA_DIR = Path(str(resources.files("shuto.data")))
SAMPLE_PATH = DATA_DIR / "sample.jpg"
QUALITY_LABELS = {1: "draft", 2: "basic", 3: "good", 4: "high", 5: "best"}
DISK_HEADROOM_GB = 1.0
SMALL_CONTEXT_TOKENS = 4096  # a model with this much room or less gets the short prompt
NUM_CTX_TARGET = 8192  # room asked of Ollama: prompt + image + answer (+ a repair round)
STALE_AFTER_DAYS = 183  # about six months: the model list is then worth a second look


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
    context_tokens: int = 0  # the model's context window (0 = not known)
    mem_gb: float = 0.0  # memory needed at NUM_CTX_TARGET (0 = not known, the rating estimates it)
    thinks: bool = False  # can think before it answers
    always_thinks: bool = False  # keeps thinking although told not to (slow)


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


# ---------------------------------------------------------------- catalog


def context_tokens(tag: str, catalog: list[LocalModel] | None = None) -> int:
    """The context window of a catalogue model in tokens; 0 when the tag is not listed."""
    models = catalog if catalog is not None else load_catalog()[0]
    wanted = normalize_tag(tag)
    return next((m.context_tokens for m in models if normalize_tag(m.tag) == wanted), 0)


def is_small_context(tag: str, catalog: list[LocalModel] | None = None) -> bool:
    """True when the catalogue says the model's window is SMALL_CONTEXT_TOKENS or less.

    Unknown models and models without a listed window are not small (the full prompt is used).
    """
    return 0 < context_tokens(tag, catalog) <= SMALL_CONTEXT_TOKENS


def effective_num_ctx(
    tag: str,
    profile: HardwareProfile | None = None,
    catalog: list[LocalModel] | None = None,
) -> int:
    """The context size (``num_ctx``) shuto asks Ollama for, in tokens.

    NUM_CTX_TARGET, capped at the model's catalogue window; capped at SMALL_CONTEXT_TOKENS
    when the machine is tight (the model has under 1 GB of memory to spare, so a big window
    could push it out of memory). Ollama's own default (2048 or 4096) is too small for the
    full prompt, the image and the answer, and it silently cuts the start of the prompt.
    """
    models = catalog if catalog is not None else load_catalog()[0]
    wanted = normalize_tag(tag)
    model = next((m for m in models if normalize_tag(m.tag) == wanted), None)
    num_ctx = NUM_CTX_TARGET
    if model is not None and 0 < model.context_tokens < num_ctx:
        num_ctx = model.context_tokens
    if model is not None and profile is not None:
        from shuto.ai.recommend import gpu_fit, ram_spare_gb  # recommend imports this module

        if not gpu_fit(model, profile) and ram_spare_gb(model, profile) < 1.0:
            num_ctx = min(num_ctx, SMALL_CONTEXT_TOKENS)
    return num_ctx


def small_model_warning(tag: str, rows: list[ModelRow], catalog: list[LocalModel] | None = None):
    """Plain-words warning for a small-context model, naming the best bigger one that is not
    rated Not recommended (rows are best first); else None."""
    if not is_small_context(tag, catalog):
        return None
    pick = next(
        (
            r.tag
            for r in rows
            if r.rating != "not_recommended"
            and r.tag != tag
            and not is_small_context(r.tag, catalog)
        ),
        None,
    )
    text = (
        f"{tag} is a small model with little memory, so shuto gives it shorter instructions "
        "and its descriptions and keywords will usually be weaker."
    )
    if pick:
        text += f" For better results choose a bigger model, for example {pick}."
    else:
        text += " For better results choose a bigger model or an online AI."
    return text


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
        context_tokens=int(d.get("context_tokens", 0) or 0),
        mem_gb=float(d.get("mem_gb", 0) or 0),
        thinks=bool(d.get("thinks", False)),
        always_thinks=bool(d.get("always_thinks", False)),
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


def catalog_updated() -> date | None:
    """When the bundled model list was last checked (the `updated:` date in models.yaml)."""
    try:
        doc = yaml.safe_load((DATA_DIR / "models.yaml").read_text(encoding="utf-8")) or {}
        value = doc.get("updated")
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        return date.fromisoformat(str(value))
    except (OSError, ValueError, yaml.YAMLError, AttributeError):
        return None


def catalog_staleness(today: date | None = None) -> tuple[str, bool]:
    """('12 October 2026', False): the list date in plain words, and whether it is old
    (more than STALE_AFTER_DAYS days). ('', False) when the date is unknown."""
    updated = catalog_updated()
    if updated is None:
        return "", False
    age = ((today or date.today()) - updated).days
    return f"{updated.day} {updated:%B %Y}", age > STALE_AFTER_DAYS
