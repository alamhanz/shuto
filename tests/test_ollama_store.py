"""Where Ollama keeps its models: env setting per OS, restart, and the safe move. No real
registry, launchctl, processes or Ollama are touched."""

from __future__ import annotations

import os
import plistlib
import sys
import types
from pathlib import Path

import pytest

from shuto import hardware
from shuto import ollama_store as store


class FakeSystem:
    """A pretend Ollama: taskkill/osascript stop it, spawning starts it."""

    def __init__(self, models=()):
        self.up = True
        self.models = list(models)
        self.cmds: list[list[str]] = []
        self.spawned: list[list[str]] = []
        self.start_works = True
        self.hide_models = False
        self.url = "http://127.0.0.1:11434"

    # client API
    def version(self):
        if not self.up:
            raise OSError("down")
        return "0.0-test"

    def tags(self):
        if not self.up:
            raise OSError("down")
        return [] if self.hide_models else [{"name": m} for m in self.models]

    # process API
    def run(self, cmd, timeout=20):
        self.cmds.append(list(cmd))
        if cmd[0] in ("taskkill", "osascript", "pkill"):
            self.up = False
        if cmd[0] == "open":
            self.up = self.start_works
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    def spawn(self, cmd):
        self.spawned.append(list(cmd))
        self.up = self.start_works


@pytest.fixture
def sysfake(monkeypatch, tmp_path):
    fake = FakeSystem(models=["gemma3:4b"])
    monkeypatch.setattr(store, "_run", fake.run)
    monkeypatch.setattr(store, "_spawn", lambda cmd: fake.spawn(cmd))
    monkeypatch.setattr(store, "can_start", lambda os_name=None: True)
    app = tmp_path / "ollama app.exe"
    app.write_text("x")
    monkeypatch.setattr(store, "_tray_app", lambda: app)
    monkeypatch.setattr(store.time, "sleep", lambda s: None)
    clock = {"t": 0.0}

    def tick():
        clock["t"] += 1.0
        return clock["t"]

    monkeypatch.setattr(store.time, "monotonic", tick)
    monkeypatch.delenv("OLLAMA_MODELS", raising=False)
    return fake


@pytest.fixture
def envlog(monkeypatch):
    """Records what would be saved, instead of touching the registry / launchctl."""
    log: list[str | None] = []
    monkeypatch.setattr(store, "_win_write", lambda v: log.append(v))
    monkeypatch.setattr(store, "_mac_write", lambda v: log.append(v))
    return log


def make_models(root: Path, blobs: dict[str, bytes]):
    (root / "blobs").mkdir(parents=True)
    (root / "manifests" / "registry" / "library" / "gemma3").mkdir(parents=True)
    for name, data in blobs.items():
        (root / "blobs" / name).write_bytes(data)
    (root / "manifests" / "registry" / "library" / "gemma3" / "4b").write_text('{"v": 1}')


# ---------------------------------------------------------------- validation


def test_validate_rejects_bad_folders(tmp_path):
    cur = tmp_path / "old"
    cur.mkdir()
    with pytest.raises(store.StoreError, match="Choose a folder"):
        store.validate_target("  ", cur)
    with pytest.raises(store.StoreError, match="full location"):
        store.validate_target("models", cur)
    with pytest.raises(store.StoreError, match="already"):
        store.validate_target(str(cur), cur)
    with pytest.raises(store.StoreError, match="not inside"):
        store.validate_target(str(cur / "sub"), cur)
    with pytest.raises(store.StoreError, match="not inside"):
        store.validate_target(str(tmp_path), cur)
    f = tmp_path / "afile"
    f.write_text("x")
    with pytest.raises(store.StoreError, match="file"):
        store.validate_target(str(f), cur)


def test_validate_creates_folder_and_checks_writable(tmp_path, monkeypatch):
    cur = tmp_path / "old"
    new = tmp_path / "d" / "ollama" / "models"
    check = store.validate_target(f'"{new}"', cur)
    assert check.path == new and new.is_dir() and check.warnings == []
    assert not list(new.iterdir())  # the write test leaves nothing behind

    def boom(self, *a, **k):
        raise PermissionError("no")

    monkeypatch.setattr(Path, "write_bytes", boom)
    with pytest.raises(store.StoreError, match="cannot save files"):
        store.validate_target(str(tmp_path / "other"), cur)


# ---------------------------------------------------------------- saving the setting


class FakeWinreg:
    HKEY_CURRENT_USER = "HKCU"
    KEY_SET_VALUE = 2
    REG_SZ = 1

    def __init__(self):
        self.values: dict[str, str] = {}
        self.opened: list[tuple] = []

    def OpenKey(self, root, sub, res, access):  # noqa: N802
        self.opened.append((root, sub, access))
        outer = self

        class Key:
            def __enter__(self):
                return outer

            def __exit__(self, *a):
                return False

        return Key()

    def SetValueEx(self, key, name, res, kind, value):  # noqa: N802
        assert kind == self.REG_SZ
        self.values[name] = value

    def DeleteValue(self, key, name):  # noqa: N802
        if name not in self.values:
            raise FileNotFoundError(name)
        del self.values[name]


def test_windows_set_and_reset_use_user_environment(monkeypatch):
    reg = FakeWinreg()
    broadcasts = []
    monkeypatch.setattr(store, "_winreg", lambda: reg)
    monkeypatch.setattr(store, "_win_broadcast", lambda: broadcasts.append(1))
    monkeypatch.delenv("OLLAMA_MODELS", raising=False)
    store.set_models_dir(Path("D:/ollama/models"), "windows")
    assert reg.values == {"OLLAMA_MODELS": str(Path("D:/ollama/models"))}
    assert reg.opened == [("HKCU", "Environment", 2)]
    assert os.environ["OLLAMA_MODELS"] == str(Path("D:/ollama/models"))
    assert broadcasts == [1]
    assert store.is_custom()
    store.reset_models_dir("windows")
    assert reg.values == {} and "OLLAMA_MODELS" not in os.environ
    store.reset_models_dir("windows")  # nothing to delete: still fine
    assert len(broadcasts) == 3


def test_windows_broadcast_failure_is_not_fatal(monkeypatch):
    monkeypatch.setattr(store, "_winreg", lambda: FakeWinreg())

    def boom():
        raise OSError("no user32")

    monkeypatch.setattr(store, "_win_broadcast", boom)
    monkeypatch.delenv("OLLAMA_MODELS", raising=False)
    store.set_models_dir("D:\\m", "windows")
    assert os.environ["OLLAMA_MODELS"] == "D:\\m"


def test_mac_set_and_reset_write_login_item(monkeypatch, tmp_path):
    cmds = []
    plist = tmp_path / "LaunchAgents" / "com.shuto.ollama-models.plist"
    monkeypatch.setattr(store, "_plist_path", lambda: plist)
    monkeypatch.setattr(
        store,
        "_run",
        lambda cmd, timeout=20: cmds.append(cmd) or types.SimpleNamespace(returncode=0),
    )
    monkeypatch.delenv("OLLAMA_MODELS", raising=False)
    store.set_models_dir("/Volumes/Big/ollama", "macos")
    assert cmds == [["launchctl", "setenv", "OLLAMA_MODELS", "/Volumes/Big/ollama"]]
    data = plistlib.loads(plist.read_bytes())
    assert data["RunAtLoad"] is True
    assert data["ProgramArguments"] == [
        "/bin/launchctl",
        "setenv",
        "OLLAMA_MODELS",
        "/Volumes/Big/ollama",
    ]
    store.reset_models_dir("macos")
    assert cmds[-1] == ["launchctl", "unsetenv", "OLLAMA_MODELS"]
    assert not plist.exists() and "OLLAMA_MODELS" not in os.environ


def test_mac_launchctl_failure_is_reported(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "_plist_path", lambda: tmp_path / "p.plist")
    monkeypatch.setattr(store, "_run", lambda cmd, timeout=20: types.SimpleNamespace(returncode=1))
    monkeypatch.delenv("OLLAMA_MODELS", raising=False)
    with pytest.raises(store.StoreError, match="macOS would not"):
        store.set_models_dir("/x", "macos")
    assert "OLLAMA_MODELS" not in os.environ


def test_linux_is_not_supported_in_app():
    assert not store.supported("linux") and store.supported("windows") and store.supported("macos")
    with pytest.raises(store.StoreUnsupported, match="Help"):
        store.set_models_dir("/mnt/x", "linux")
    with pytest.raises(store.StoreUnsupported):
        store.reset_models_dir("linux")
    with pytest.raises(store.StoreUnsupported):
        store.change_models_dir(Path("/mnt/x"), move=False, client=None, os_name="linux")


# ---------------------------------------------------------------- restart


def test_restart_windows_polite_then_forced_then_tray_app(sysfake):
    store.restart_ollama(sysfake, "windows")
    names = [c[:3] for c in sysfake.cmds]
    assert names[0] == ["taskkill", "/IM", "ollama app.exe"]
    assert names[1] == ["taskkill", "/IM", "ollama.exe"]
    assert ["taskkill", "/F", "/IM"] in names
    assert sysfake.spawned[0][0].endswith("ollama app.exe") and sysfake.up


def test_restart_windows_falls_back_to_serve(sysfake, monkeypatch):
    monkeypatch.setattr(store, "_tray_app", lambda: Path("nope.exe"))
    monkeypatch.setattr(store.shutil, "which", lambda n: "C:/o/ollama.exe")
    store.restart_ollama(sysfake, "windows")
    assert sysfake.spawned == [["C:/o/ollama.exe", "serve"]]


def test_restart_windows_nothing_to_start(sysfake, monkeypatch):
    monkeypatch.setattr(store, "_tray_app", lambda: Path("nope.exe"))
    monkeypatch.setattr(store.shutil, "which", lambda n: None)
    with pytest.raises(store.StoreError, match="could not find Ollama"):
        store.restart_ollama(sysfake, "windows")


def test_restart_mac_quits_then_opens_app(sysfake):
    store.restart_ollama(sysfake, "macos")
    assert sysfake.cmds[0] == ["osascript", "-e", 'quit app "Ollama"']
    assert ["open", "-a", "Ollama"] in sysfake.cmds


def test_restart_times_out_with_plain_message(sysfake):
    sysfake.start_works = False
    with pytest.raises(store.StoreError, match="did not start again within 30 seconds"):
        store.restart_ollama(sysfake, "windows")


def test_stop_reports_when_ollama_will_not_close(sysfake, monkeypatch):
    monkeypatch.setattr(store, "_run", lambda cmd, timeout=20: types.SimpleNamespace(returncode=1))
    with pytest.raises(store.StoreError, match="would not close"):
        store.stop_ollama(sysfake, "windows")


# ---------------------------------------------------------------- moving


@pytest.fixture
def folders(tmp_path, monkeypatch):
    old, new = tmp_path / "old", tmp_path / "new"
    make_models(old, {"sha256-aaa": b"A" * 2_000_000, "sha256-bbb": b"B" * 1_500_000})
    (old / "blobs" / "sha256-ccc-partial-0").write_bytes(b"half")
    monkeypatch.setenv("OLLAMA_MODELS", str(old))
    new.mkdir()
    return old, new


def test_move_copies_verifies_then_deletes_old(sysfake, envlog, folders):
    old, new = folders
    seen = []
    msg = store.change_models_dir(
        new, move=True, client=sysfake, os_name="windows", progress=lambda *a: seen.append(a)
    )
    assert (new / "blobs" / "sha256-aaa").stat().st_size == 2_000_000
    assert (new / "manifests" / "registry" / "library" / "gemma3" / "4b").is_file()
    assert not (old / "blobs" / "sha256-aaa").exists()
    assert not (old / "blobs" / "sha256-bbb").exists()
    assert not (old / "manifests").exists()  # emptied folders are pruned
    assert (old / "blobs" / "sha256-ccc-partial-0").exists()  # unfinished downloads untouched
    assert not (new / "blobs" / "sha256-ccc-partial-0").exists()
    assert not list(new.rglob("*.shuto-part"))
    assert envlog == [str(new)] and sysfake.up
    assert "moved" in msg and str(new) in msg
    assert seen and seen[-1][0] == seen[-1][1]


def test_identical_files_are_skipped(sysfake, envlog, folders, monkeypatch):
    old, new = folders
    (new / "blobs").mkdir()
    (new / "blobs" / "sha256-aaa").write_bytes(b"A" * 2_000_000)
    copied = []
    real = store._copy_file
    monkeypatch.setattr(
        store, "_copy_file", lambda s, d, *a: copied.append(d.name) or real(s, d, *a)
    )
    plan = store.plan_move(old, new)
    assert plan.todo_bytes == plan.total_bytes - 2_000_000
    store.change_models_dir(new, move=True, client=sysfake, os_name="windows")
    assert "sha256-aaa" not in copied and "sha256-bbb" in copied


def test_a_same_name_file_of_other_size_is_copied_again(folders):
    old, new = folders
    (new / "blobs").mkdir()
    (new / "blobs" / "sha256-aaa").write_bytes(b"A" * 10)
    plan = store.plan_move(old, new)
    assert plan.todo_bytes == plan.total_bytes


def test_not_enough_space_changes_nothing(sysfake, envlog, folders, monkeypatch):
    old, new = folders
    monkeypatch.setattr(hardware, "free_gb", lambda p: 0.5)
    with pytest.raises(store.StoreError, match="not enough room"):
        store.change_models_dir(new, move=True, client=sysfake, os_name="windows")
    assert envlog == [] and sysfake.cmds == [] and (old / "blobs" / "sha256-aaa").exists()


def test_copy_failure_keeps_old_files_and_restores_setting(sysfake, envlog, folders, monkeypatch):
    old, new = folders
    real = store._copy_file
    calls = []

    def flaky(src, dst, *a):
        calls.append(dst.name)
        if len(calls) == 2:
            raise OSError(28, "No space left on device")
        return real(src, dst, *a)

    monkeypatch.setattr(store, "_copy_file", flaky)
    with pytest.raises(store.StoreError, match="models were not changed"):
        store.change_models_dir(new, move=True, client=sysfake, os_name="windows")
    assert (old / "blobs" / "sha256-aaa").exists() and (old / "blobs" / "sha256-bbb").exists()
    assert (old / "manifests").exists()
    assert envlog == [str(old)]  # the previous setting was put back
    assert sysfake.up  # Ollama was started again


def test_corrupt_copy_is_detected(sysfake, envlog, folders, monkeypatch):
    old, new = folders
    real = store._copy_file

    def short(src, dst, on_bytes, cancelled):
        real(src, dst, on_bytes, cancelled)
        if dst.name == "sha256-bbb":
            dst.write_bytes(b"short")

    monkeypatch.setattr(store, "_copy_file", short)
    with pytest.raises(store.StoreError, match="did not match"):
        store.change_models_dir(new, move=True, client=sysfake, os_name="windows")
    assert (old / "blobs" / "sha256-bbb").stat().st_size == 1_500_000


def test_models_missing_after_restart_keeps_old_files(sysfake, envlog, folders):
    old, new = folders
    started = sysfake.spawn

    def spawn_and_hide(cmd):
        started(cmd)
        sysfake.hide_models = True

    sysfake.spawn = spawn_and_hide
    with pytest.raises(store.StoreError, match="cannot see gemma3:4b"):
        store.change_models_dir(new, move=True, client=sysfake, os_name="windows")
    assert (old / "blobs" / "sha256-aaa").exists()
    assert envlog[-1] == str(old)


def test_ollama_not_coming_back_rolls_back(sysfake, envlog, folders):
    old, new = folders
    sysfake.start_works = False
    with pytest.raises(store.StoreError, match="did not start again"):
        store.change_models_dir(new, move=False, client=sysfake, os_name="windows")
    assert envlog[0] == str(new) and envlog[-1] == str(old)
    assert (old / "blobs" / "sha256-aaa").exists()


def test_cancel_during_copy_changes_nothing(sysfake, envlog, folders):
    old, new = folders
    msg = store.change_models_dir(
        new, move=True, client=sysfake, os_name="windows", cancelled=lambda: True
    )
    assert msg.startswith("Stopped") and envlog == [str(old)]
    assert (old / "blobs" / "sha256-aaa").exists() and sysfake.up
    assert not list(new.rglob("*.shuto-part"))


def test_change_without_move_leaves_old_models(sysfake, envlog, folders):
    old, new = folders
    msg = store.change_models_dir(new, move=False, client=sysfake, os_name="windows")
    assert (old / "blobs" / "sha256-aaa").exists() and envlog == [str(new)]
    assert "stay in the old folder" in msg


def test_reset_goes_back_to_default(sysfake, envlog, folders, monkeypatch, tmp_path):
    monkeypatch.setattr(store, "default_models_dir", lambda home=None: tmp_path / "default")
    msg = store.change_models_dir(None, move=False, client=sysfake, os_name="windows")
    assert envlog == [None] and str(tmp_path / "default") in msg
    assert "OLLAMA_MODELS" not in os.environ


def test_reset_when_already_default_says_so(sysfake, envlog, folders, monkeypatch):
    old, _ = folders
    monkeypatch.setattr(store, "default_models_dir", lambda home=None: old)
    with pytest.raises(store.StoreError, match="already"):
        store.change_models_dir(None, move=False, client=sysfake, os_name="windows")


def test_ollama_not_installed_only_saves(sysfake, envlog, folders, monkeypatch):
    _, new = folders
    sysfake.up = False
    monkeypatch.setattr(store, "can_start", lambda os_name=None: False)
    msg = store.change_models_dir(new, move=True, client=sysfake, os_name="macos")
    assert envlog == [str(new)] and "once it is installed" in msg and sysfake.cmds == []


def test_model_files_ignore_symlinks(tmp_path):
    root = tmp_path / "m"
    make_models(root, {"sha256-x": b"1234"})
    target = tmp_path / "secret"
    target.write_text("s")
    try:
        os.symlink(target, root / "blobs" / "sha256-link")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not allowed here")
    names = [Path(rel).name for rel, _ in store._model_files(root)]
    assert "sha256-link" not in names and "sha256-x" in names


def test_wait_stops_at_deadline():
    ticks = iter(range(100))
    assert not store._wait(lambda: False, 3, sleep=lambda s: None, clock=lambda: next(ticks))
    assert store._wait(lambda: True, 3)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
def test_real_winreg_module_has_what_we_use():
    import winreg

    for name in ("OpenKey", "SetValueEx", "DeleteValue", "HKEY_CURRENT_USER", "REG_SZ"):
        assert hasattr(winreg, name)
