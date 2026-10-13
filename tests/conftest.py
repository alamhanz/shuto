"""Shared fixtures: isolated config dirs, fake hardware/Ollama, and a small photo folder."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from shuto import hardware, pipeline
from shuto.config import Config

from .helpers import write_jpeg


@pytest.fixture(autouse=True)
def _no_real_registry(monkeypatch):
    """Tests never read the real Windows registry: the 'saved' user value is the test's env."""
    import os

    from shuto import ollama_store

    monkeypatch.setattr(
        ollama_store,
        "_win_read",
        lambda machine: None if machine else os.environ.get("OLLAMA_MODELS") or None,
    )


class FakeOllama:
    url = "http://127.0.0.1:11434"

    def __init__(self, pulled=(), running=True):
        self.pulled = list(pulled)
        self.running = running
        self.pull_calls: list[str] = []
        self.delete_calls: list[str] = []
        self.sizes: dict[str, int] = {}  # bytes, as Ollama reports them

    def version(self):
        if not self.running:
            raise OSError("down")
        return "0.0-test"

    def tags(self):
        return [{"name": n, "size": self.sizes.get(n, 0)} for n in self.pulled]

    def delete(self, tag):
        self.delete_calls.append(tag)
        if tag not in self.pulled:
            raise OSError("model not found")
        self.pulled.remove(tag)

    def pull(self, tag, on_progress=None):
        self.pull_calls.append(tag)
        if on_progress:
            on_progress({"status": "pulling", "total": 100, "completed": 100})

    def chat(self, *a, **k):  # pragma: no cover - benchmark uses a fake provider instead
        raise AssertionError("no network in tests")


def fake_profile(ram_gb=32.0, tier="cpu", gpu_gb=0.0):
    return hardware.HardwareProfile(
        os="linux",
        arch="x86_64",
        cpu="Test CPU",
        cores=8,
        ram_gb=ram_gb,
        gpus=[],
        apple_silicon=False,
        usable_gpu_gb=gpu_gb,
        tier=tier,
        fingerprint="test-fp",
    )


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    """No test may touch the user's config, cache, keyring-dependent state or hardware."""
    monkeypatch.setattr(pipeline, "model_choice_path", lambda: tmp_path / "cfgdir" / "choice.json")
    monkeypatch.setattr(pipeline, "benchmark_cache_path", lambda: tmp_path / "cache" / "bench.json")
    monkeypatch.setattr(pipeline, "hardware_profile", lambda refresh=False: fake_profile())
    monkeypatch.setattr(hardware, "ollama_models_dir", lambda *a, **k: tmp_path / "ollama-models")
    monkeypatch.chdir(tmp_path)
    for name in ("SHUTO_CONFIG", "SHUTO_FTPS_USER", "SHUTO_FTPS_PASSWORD"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("shuto.secrets._keyring", lambda: None)
    monkeypatch.setattr("shuto.secrets.default_config_dir", lambda: tmp_path / "cfgdir")


@pytest.fixture
def cfg(tmp_path, isolated) -> Config:
    return Config.model_validate({"workspace": str(tmp_path / "ws"), "llm": {"provider": "fake"}})


@pytest.fixture
def store(cfg):
    return pipeline.open_store(cfg)


@pytest.fixture
def photos_dir(tmp_path) -> Path:
    d = tmp_path / "photos"
    when = datetime(2025, 3, 4, 10, 0, 0)
    write_jpeg(d / "a.jpg", "sharp", seed=1, captured=when)
    write_jpeg(d / "b.jpg", "sharp", seed=2, captured=when)
    write_jpeg(d / "c.jpg", "sharp", seed=3, captured=when)
    write_jpeg(d / "blurry.jpg", "blurred", seed=4, captured=when)
    write_jpeg(d / "tiny.jpg", "small", seed=5, captured=when)
    return d
