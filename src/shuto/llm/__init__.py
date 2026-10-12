"""Vision LLM providers. Providers are imported lazily so optional SDKs stay optional."""

from __future__ import annotations

import importlib

from .base import LLMError, LLMRequest, LLMResult, VisionLLM, describe_structured

PROVIDERS = {
    "ollama": "shuto.llm.ollama:OllamaProvider",
    "anthropic": "shuto.llm.anthropic_api:AnthropicProvider",
    "gemini": "shuto.llm.gemini:GeminiProvider",
    "bedrock": "shuto.llm.bedrock:BedrockProvider",
    "fake": "shuto.llm.fake:FakeProvider",
}


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
    "PROVIDERS",
    "LLMError",
    "LLMRequest",
    "LLMResult",
    "VisionLLM",
    "describe_structured",
    "get_provider",
]
