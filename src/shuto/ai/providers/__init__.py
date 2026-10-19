"""Vision LLM providers. Providers are imported lazily so optional SDKs stay optional.

``REGISTRY`` is the one table of provider facts: where the class lives, the label, and for the
online ones the API key name and the SDK module. Everything else (``PROVIDERS``, key and SDK
maps, class lookup) is derived from it.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass

from shuto.ai.providers.base import LLMError, LLMRequest, LLMResult, VisionLLM, describe_structured
from shuto.core import secrets


@dataclass(frozen=True)
class ProviderInfo:
    name: str
    cls_path: str  # "module:Class"
    label: str
    key_secret: str = ""  # secret name of the API key ("" = none needed)
    sdk_module: str = ""  # optional SDK to import ("" = none needed)
    is_api: bool = False  # an online AI (needs an SDK, and usually a key)


REGISTRY: dict[str, ProviderInfo] = {
    info.name: info
    for info in (
        ProviderInfo("ollama", "shuto.ai.providers.ollama:OllamaProvider", "Ollama (local)"),
        ProviderInfo(
            "anthropic",
            "shuto.ai.providers.anthropic_api:AnthropicProvider",
            "Anthropic",
            key_secret=secrets.ANTHROPIC_API_KEY,
            sdk_module="anthropic",
            is_api=True,
        ),
        ProviderInfo(
            "gemini",
            "shuto.ai.providers.gemini:GeminiProvider",
            "Gemini",
            key_secret=secrets.GEMINI_API_KEY,
            sdk_module="google.genai",
            is_api=True,
        ),
        ProviderInfo(
            "bedrock",
            "shuto.ai.providers.bedrock:BedrockProvider",
            "AWS Bedrock",
            sdk_module="boto3",
            is_api=True,
        ),
        ProviderInfo("fake", "shuto.ai.providers.fake:FakeProvider", "Fake (tests)"),
    )
}

PROVIDERS = {name: info.cls_path for name, info in REGISTRY.items()}
API_PROVIDERS = tuple(name for name, info in REGISTRY.items() if info.is_api)
API_KEY_SECRETS = {n: i.key_secret for n, i in REGISTRY.items() if i.key_secret}
API_SDK_MODULES = {n: i.sdk_module for n, i in REGISTRY.items() if i.sdk_module}


class _Defaults:
    """Stand-in when a provider class cannot be imported (SDK not installed)."""

    default_image_side = 1024
    default_concurrency = 1


def provider_class(name: str) -> type:
    """Provider class without instantiating it (for default_image_side / default_concurrency)."""
    mod_name, cls_name = REGISTRY[name].cls_path.split(":")
    try:
        return getattr(importlib.import_module(mod_name), cls_name)
    except ImportError:
        return _Defaults


def get_provider(name: str, model: str, **settings) -> VisionLLM:
    if name not in PROVIDERS:
        raise LLMError(f"Unknown LLM provider {name!r}; choose one of: {', '.join(PROVIDERS)}")
    mod_name, cls_name = PROVIDERS[name].split(":")
    try:
        mod = importlib.import_module(mod_name)
    except ModuleNotFoundError as e:
        if e.name and e.name.split(".")[0] == "shuto":
            raise LLMError(f"Provider {name!r} is not available in this install: {e}") from e
        raise LLMError(f"Provider {name!r} needs an extra: pip install 'shuto[{name}]'") from e
    return getattr(mod, cls_name)(model=model, **settings)


__all__ = [
    "API_KEY_SECRETS",
    "API_PROVIDERS",
    "API_SDK_MODULES",
    "PROVIDERS",
    "REGISTRY",
    "LLMError",
    "LLMRequest",
    "LLMResult",
    "ProviderInfo",
    "VisionLLM",
    "describe_structured",
    "get_provider",
    "provider_class",
]
