"""The provider registry, the num_ctx sizing helpers and the hardware cache."""

from __future__ import annotations

from dataclasses import replace

import pytest

from shuto.ai import catalog, hardware, machine, recommend, selection
from shuto.ai.providers import (
    API_KEY_SECRETS,
    API_PROVIDERS,
    API_SDK_MODULES,
    PROVIDERS,
    REGISTRY,
    provider_class,
)
from shuto.ai.providers.ollama import DEFAULT_NUM_CTX

from ..helpers import fake_profile


def test_registry_derives_the_provider_maps():
    assert list(PROVIDERS) == ["ollama", "anthropic", "gemini", "bedrock", "fake"]
    assert API_PROVIDERS == ("anthropic", "gemini", "bedrock")
    assert API_KEY_SECRETS == {"anthropic": "ANTHROPIC_API_KEY", "gemini": "GEMINI_API_KEY"}
    assert API_SDK_MODULES == {
        "anthropic": "anthropic",
        "gemini": "google.genai",
        "bedrock": "boto3",
    }
    assert selection.API_KEY_SECRETS is API_KEY_SECRETS
    assert all(info.label for info in REGISTRY.values())


def test_provider_class_falls_back_to_defaults(monkeypatch):
    assert provider_class("fake").default_image_side == 512
    assert selection.get_provider_class("ollama").default_concurrency == 1

    def broken(name, *a, **k):
        raise ImportError(name)

    monkeypatch.setattr("importlib.import_module", broken)
    assert provider_class("gemini").default_image_side == 1024


def test_ollama_default_num_ctx_is_the_catalog_target():
    assert DEFAULT_NUM_CTX == catalog.NUM_CTX_TARGET


def profile(gpu_gb: float, ram_gb: float):
    return replace(fake_profile(), usable_gpu_gb=gpu_gb, ram_gb=ram_gb)


def test_gpu_fit_and_ram_spare_drive_the_fit_decisions():
    m = catalog.LocalModel("t:1", 4, 5.0, 8.0, 0, 3, "high", {})
    assert recommend.gpu_fit(m, profile(6.0, 8)) and not recommend.gpu_fit(m, profile(5.9, 8))
    assert recommend.ram_spare_gb(m, profile(0, 16)) == 4.0
    tight = catalog.effective_num_ctx("t:1", profile(0, 12.5), [m])
    roomy = catalog.effective_num_ctx("t:1", profile(0, 16), [m])
    assert tight == catalog.SMALL_CONTEXT_TOKENS and roomy == catalog.NUM_CTX_TARGET


def test_moved_names_stay_reachable_on_catalog():
    assert catalog.recommend is recommend.recommend
    assert catalog.ModelChoiceRequired.__module__ == "shuto.ai.model_choice"
    with pytest.raises(AttributeError):
        catalog.no_such_name  # noqa: B018


def test_num_ctx_for_alias():
    assert selection._num_ctx_for is selection.num_ctx_for


def test_hardware_cache_clear(monkeypatch):
    calls = []
    monkeypatch.setattr(hardware, "detect", lambda: calls.append(1) or profile(0, 8))
    machine.clear_cache()
    first = machine._cache.get()
    assert machine._cache.get() is first and calls == [1]
    machine.clear_cache()
    machine._cache.get()
    assert calls == [1, 1]
    machine.clear_cache()
