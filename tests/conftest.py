"""Shared fixtures: isolated config dirs, fake hardware/Ollama, and a small photo folder."""

from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path

import pytest

from shuto import store as store_pkg
from shuto.ai import hardware
from shuto.ai import machine as ai_machine
from shuto.core import paths
from shuto.core.config import Config

from .helpers import fake_profile, write_jpeg


@pytest.fixture(autouse=True)
def _fresh_caches():
    """Every process-wide cache starts empty, so no test depends on what an earlier one loaded."""
    from shuto.places import geo
    from shuto.workflow import checks

    ai_machine.clear_cache()
    geo.clear()
    checks.clear_cache()


@pytest.fixture(autouse=True)
def _no_real_ollama_conflict(monkeypatch):
    """Tests never run the real `ollama` program or read the real models folder."""
    from shuto.ollama import conflict

    conflict.clear_installed_cache()
    monkeypatch.setattr(conflict, "installed_version", lambda *a, **k: None)
    monkeypatch.setattr(conflict, "disk_models", lambda _dir: set())
    monkeypatch.setattr(conflict, "port_hint", lambda _url, *a, **k: "")


@pytest.fixture(autouse=True)
def _plenty_of_disk(monkeypatch):
    """Tests see 500 GB free unless they say otherwise, whatever the CI runner has left."""
    from collections import namedtuple

    from shuto.ai import catalog, hardware

    real = hardware.free_gb
    usage = namedtuple("usage", "total used free")(1000 * 1024**3, 500 * 1024**3, 500 * 1024**3)

    def free_gb(path, usage_fn=lambda _p: usage, **kw):
        return real(path, usage_fn, **kw)

    monkeypatch.setattr(hardware, "free_gb", free_gb)
    monkeypatch.setattr(catalog, "free_gb", free_gb)


@pytest.fixture(autouse=True)
def _no_real_registry(monkeypatch):
    """Tests never read the real Windows registry: the 'saved' user value is the test's env."""
    import os

    from shuto.ollama import env as ollama_env

    monkeypatch.setattr(
        ollama_env,
        "_win_read",
        lambda machine: None if machine else os.environ.get("OLLAMA_MODELS") or None,
    )


@pytest.fixture(autouse=True)
def _no_real_update_check(monkeypatch):
    """Tests never ask GitHub for the latest release."""
    from shuto.update import core as updates

    monkeypatch.setattr(updates, "fetch_latest", lambda *a, **k: None)

    def no_digest(*a, **k):
        raise updates.UpdateError(updates.MSG_NO_DIGEST)

    monkeypatch.setattr(updates, "fetch_wheel_digest", no_digest)


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    """No test may touch the user's config, cache, keyring-dependent state or hardware."""
    monkeypatch.setattr(paths, "model_choice_path", lambda: tmp_path / "cfgdir" / "choice.json")
    monkeypatch.setattr(paths, "benchmark_cache_path", lambda: tmp_path / "cache" / "bench.json")
    monkeypatch.setattr(ai_machine, "hardware_profile", lambda refresh=False: fake_profile())
    monkeypatch.setattr(hardware, "ollama_models_dir", lambda *a, **k: tmp_path / "ollama-models")
    monkeypatch.chdir(tmp_path)
    for name in ("SHUTO_CONFIG", "SHUTO_FTPS_USER", "SHUTO_FTPS_PASSWORD"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("shuto.core.secrets._keyring", lambda: None)
    monkeypatch.setattr("shuto.core.secrets.default_config_dir", lambda: tmp_path / "cfgdir")


@pytest.fixture
def cfg(tmp_path, isolated) -> Config:
    return Config.model_validate({"workspace": str(tmp_path / "ws"), "llm": {"provider": "fake"}})


@pytest.fixture
def store(cfg):
    return store_pkg.open_store(cfg)


@pytest.fixture(scope="session")
def _photos_template(tmp_path_factory) -> Path:
    """The five synthetic photos, made once per test process (making them is the slow part)."""
    d = tmp_path_factory.mktemp("photos-template")
    when = datetime(2025, 3, 4, 10, 0, 0)
    write_jpeg(d / "a.jpg", "sharp", seed=1, captured=when)
    write_jpeg(d / "b.jpg", "sharp", seed=2, captured=when)
    write_jpeg(d / "c.jpg", "sharp", seed=3, captured=when)
    write_jpeg(d / "blurry.jpg", "blurred", seed=4, captured=when)
    write_jpeg(d / "tiny.jpg", "small", seed=5, captured=when)
    return d


@pytest.fixture
def photos_dir(tmp_path, _photos_template) -> Path:
    d = tmp_path / "photos"
    shutil.copytree(_photos_template, d)
    return d
