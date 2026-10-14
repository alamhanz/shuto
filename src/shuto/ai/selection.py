"""Which AI provider and model to use, and whether it is ready."""

from __future__ import annotations

import importlib
import importlib.util
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from shuto.ai import catalog as local_models
from shuto.ai import hardware, machine
from shuto.ai.providers import get_provider
from shuto.ai.providers.base import VisionLLM
from shuto.ai.providers.fake import FakeProvider
from shuto.ai.providers.ollama import OllamaClient, OllamaProvider
from shuto.core import paths
from shuto.core.config import Config
from shuto.core.errors import PipelineError
from shuto.core.secrets import get_secret

if TYPE_CHECKING:
    from shuto.store import Store

API_KEY_SECRETS = {
    "anthropic": "ANTHROPIC_API_KEY",
    "gemini": "GEMINI_API_KEY",
}


API_SDK_MODULES = {"anthropic": "anthropic", "gemini": "google.genai", "bedrock": "boto3"}


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
        return Selection(
            prov, local_models.resolve_model(chosen, paths.model_choice_path()), source
        )
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


def _num_ctx_for(model: str) -> int:
    """The Ollama context size for `model` on this machine (see local_models)."""
    try:
        profile = machine.hardware_profile()
    except Exception:
        profile = None
    return local_models.effective_num_ctx(model, profile)


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
            num_ctx=_num_ctx_for(sel.model),
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


def _client_or_default(cfg: Config, client: Any) -> Any:
    return client if client is not None else ollama_client(cfg)


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


def get_provider_class(provider: str):
    """Provider class without instantiating it (for default_image_side / default_concurrency)."""

    from shuto.ai.providers import PROVIDERS

    mod_name, cls_name = PROVIDERS[provider].split(":")
    try:
        return getattr(importlib.import_module(mod_name), cls_name)
    except ImportError:
        return _Defaults


class _Defaults:
    default_image_side = 1024
    default_concurrency = 1


def use_condensed_prompt(llm: VisionLLM) -> bool:
    """The short prompt is for local models whose context window (the num_ctx actually sent to
    Ollama) is small (see local_models)."""
    if llm.name != "ollama":
        return False
    num_ctx = getattr(llm, "num_ctx", None)
    if isinstance(num_ctx, int) and num_ctx > 0:
        return num_ctx <= local_models.SMALL_CONTEXT_TOKENS
    return local_models.is_small_context(llm.model)
