"""Windows Ollama fixes (shuto-155) and the `ollama serve` fallback. Fakes only: no real
registry, processes or Ollama."""

from __future__ import annotations

import subprocess
import types
from pathlib import Path

import pytest

from shuto.ollama import env, errors, service

# the autouse guard in conftest replaces env._win_read; keep the real one (its winreg is faked)
REAL_WIN_READ = env._win_read


class FakeReg:
    HKEY_CURRENT_USER = "HKCU"
    HKEY_LOCAL_MACHINE = "HKLM"
    KEY_READ = 1
    REG_SZ = 1
    REG_EXPAND_SZ = 2

    def __init__(self, value, kind):
        self.item = (value, kind)

    def OpenKey(self, root, sub, res, access):  # noqa: N802
        class Key:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        return Key()

    def QueryValueEx(self, key, name):  # noqa: N802
        return self.item


class Client:
    def __init__(self, up=False):
        self.up = up

    def version(self):
        if not self.up:
            raise OSError("down")
        return "x"


@pytest.fixture
def no_wait(monkeypatch):
    monkeypatch.setattr(service.time, "sleep", lambda s: None)
    ticks = iter(range(10_000))
    monkeypatch.setattr(service.time, "monotonic", lambda: float(next(ticks)))


def test_expand_sz_registry_value_is_expanded(monkeypatch, tmp_path):
    monkeypatch.setenv("SHUTO_TEST_HOME", str(tmp_path))
    monkeypatch.setattr(
        env, "_winreg", lambda: FakeReg("%SHUTO_TEST_HOME%/models", FakeReg.REG_EXPAND_SZ)
    )
    assert REAL_WIN_READ(False) == str(tmp_path) + "/models"


def test_plain_sz_registry_value_is_not_expanded(monkeypatch):
    monkeypatch.setattr(env, "_winreg", lambda: FakeReg("D:/100%x%/m", FakeReg.REG_SZ))
    assert REAL_WIN_READ(False) == "D:/100%x%/m"


def test_stop_only_targets_this_users_processes(monkeypatch, no_wait):
    cmds = []
    monkeypatch.setattr(env, "_run", lambda cmd, timeout=20: cmds.append(cmd) or None)
    monkeypatch.setenv("USERNAME", "amy")
    monkeypatch.setenv("USERDOMAIN", "PC1")
    service.stop_ollama(Client(up=False), "windows")
    assert len(cmds) == 4
    assert all(
        c[0] == "taskkill" and c[-2:] == ["/FI", "USERNAME eq PC1" + chr(92) + "amy"] for c in cmds
    )
    assert [("/F" in c) for c in cmds] == [False, False, True, True]  # polite first, then force


def test_stop_without_a_known_user_kills_nothing(monkeypatch, no_wait):
    cmds = []
    monkeypatch.setattr(env, "_run", lambda cmd, timeout=20: cmds.append(cmd) or None)
    monkeypatch.delenv("USERNAME", raising=False)
    monkeypatch.setattr(service.getpass, "getuser", lambda: (_ for _ in ()).throw(OSError()))
    service.stop_ollama(Client(up=False), "windows")
    assert cmds == []


def test_relaunch_env_is_trimmed(monkeypatch, tmp_path):
    venv = tmp_path / "venv"
    monkeypatch.setattr(service.sys, "prefix", str(venv))
    monkeypatch.setattr(service.sys, "base_prefix", str(tmp_path / "base"))
    monkeypatch.setenv("VIRTUAL_ENV", str(venv))
    monkeypatch.setenv("PATH", service.os.pathsep.join([str(venv / "Scripts"), "/usr/bin"]))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "secret")
    monkeypatch.setenv("GEMINI_API_KEY", "secret")
    monkeypatch.setenv("OLLAMA_MODELS", "D:/m")
    monkeypatch.setenv("SYSTEMROOT", "C:/Windows")
    out = service._clean_env()
    assert "ANTHROPIC_API_KEY" not in out and "GEMINI_API_KEY" not in out
    assert "VIRTUAL_ENV" not in out
    assert out["OLLAMA_MODELS"] == "D:/m"
    assert out["PATH"] == "/usr/bin"
    assert any(k.upper() == "SYSTEMROOT" for k in out)


def test_relaunch_env_keeps_proxy_and_ca_settings(monkeypatch):
    names = ["HTTPS_PROXY", "NO_PROXY", "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE"]
    for name in names:
        monkeypatch.setenv(name, "kept")
    monkeypatch.setenv("http_proxy", "kept")  # lower case too
    out = service._clean_env()
    assert {k.upper() for k in out} >= {*names, "HTTP_PROXY"}
    assert all(v == "kept" for k, v in out.items() if k.upper() in {*names, "HTTP_PROXY"})


def test_spawn_passes_the_trimmed_env(monkeypatch):
    seen = {}
    monkeypatch.setattr(service, "_clean_env", lambda: {"A": "1"})
    monkeypatch.setattr(subprocess, "Popen", lambda cmd, **kw: seen.update(kw, cmd=cmd))
    service._spawn(["ollama", "serve"])
    assert seen["env"] == {"A": "1"} and seen["cmd"] == ["ollama", "serve"]


def test_spawn_failure_is_a_plain_error(monkeypatch):
    def boom(cmd, **kw):
        raise OSError("nope")

    monkeypatch.setattr(subprocess, "Popen", boom)
    with pytest.raises(errors.StoreError, match="could not start"):
        service._spawn(["ollama", "serve"])


# ---------------------------------------------------------------- ollama serve fallback


@pytest.fixture
def spawned(monkeypatch, no_wait):
    calls: list[list[str]] = []
    client = Client()
    monkeypatch.setattr(
        service, "_spawn", lambda cmd: (calls.append(cmd), setattr(client, "up", True))
    )
    return calls, client


def test_windows_without_tray_app_falls_back_to_ollama_serve(monkeypatch, spawned, tmp_path):
    calls, client = spawned
    monkeypatch.setattr(service, "_tray_app", lambda: tmp_path / "missing.exe")
    monkeypatch.setattr(service.shutil, "which", lambda n: "C:/bin/ollama.exe")
    service.start_ollama(client, "windows")
    assert calls == [["C:/bin/ollama.exe", "serve"]]


def test_windows_prefers_the_tray_app(monkeypatch, spawned, tmp_path):
    calls, client = spawned
    app = tmp_path / "ollama app.exe"
    app.write_text("x")
    monkeypatch.setattr(service, "_tray_app", lambda: app)
    monkeypatch.setattr(service.shutil, "which", lambda n: "C:/bin/ollama.exe")
    service.start_ollama(client, "windows")
    assert calls == [[str(app)]]


def test_windows_with_no_ollama_at_all_says_so(monkeypatch, spawned, tmp_path):
    calls, client = spawned
    monkeypatch.setattr(service, "_tray_app", lambda: tmp_path / "missing.exe")
    monkeypatch.setattr(service.shutil, "which", lambda n: None)
    with pytest.raises(errors.StoreError, match="could not find Ollama"):
        service.start_ollama(client, "windows")
    assert calls == []


def test_mac_falls_back_to_ollama_serve_when_open_fails(monkeypatch, spawned):
    calls, client = spawned
    monkeypatch.setattr(env, "_run", lambda cmd, timeout=20: types.SimpleNamespace(returncode=1))
    monkeypatch.setattr(service.shutil, "which", lambda n: "/usr/local/bin/ollama")
    service.start_ollama(client, "macos")
    assert calls == [["/usr/local/bin/ollama", "serve"]]


def test_mac_open_ok_does_not_spawn(monkeypatch, spawned):
    calls, client = spawned
    monkeypatch.setattr(
        env,
        "_run",
        lambda cmd, timeout=20: (setattr(client, "up", True), types.SimpleNamespace(returncode=0))[
            1
        ],
    )
    service.start_ollama(client, "macos")
    assert calls == []


def test_mac_open_fails_and_no_binary(monkeypatch, spawned):
    calls, client = spawned
    monkeypatch.setattr(env, "_run", lambda cmd, timeout=20: types.SimpleNamespace(returncode=1))
    monkeypatch.setattr(service.shutil, "which", lambda n: None)
    with pytest.raises(errors.StoreError, match="could not find Ollama"):
        service.start_ollama(client, "macos")


def test_start_that_never_answers_is_reported(monkeypatch, no_wait, tmp_path):
    monkeypatch.setattr(service, "_tray_app", lambda: Path(tmp_path / "missing.exe"))
    monkeypatch.setattr(service.shutil, "which", lambda n: "ollama")
    monkeypatch.setattr(service, "_spawn", lambda cmd: None)
    with pytest.raises(errors.StoreError, match="did not start"):
        service.start_ollama(Client(), "windows", timeout=3)
