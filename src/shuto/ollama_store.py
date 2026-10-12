"""Where Ollama keeps its models: choose a folder, restart Ollama, optionally move the models.

Ollama reads OLLAMA_MODELS only when it starts, so changing the folder means (1) saving the
setting where new programs will find it, (2) restarting Ollama, (3) checking that it came back.
Windows and macOS are handled here without administrator rights. On Linux the service is
configured with systemd, which needs administrator rights, so the Help page explains it.

Safety of a move: files are copied under a temporary name, every copy is size-checked, Ollama
is restarted on the new folder and must still list every model it listed before. Only then are
the old `blobs/` and `manifests/` files removed. Any failure leaves the old files untouched.
"""

from __future__ import annotations

import filecmp
import os
import plistlib
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import hardware

ENV_NAME = "OLLAMA_MODELS"
SUBDIRS = ("blobs", "manifests")  # the only parts of a models folder that are copied or deleted
HEADROOM_GB = 1.0  # free space kept on the target on top of the files being copied
START_TIMEOUT_S = 30.0
STOP_TIMEOUT_S = 10.0
CHUNK = 8 * 1024 * 1024
SMALL_FILE = 1024 * 1024  # files under this are compared byte by byte, larger ones by size
PART_SUFFIX = ".shuto-part"
LAUNCH_AGENT = "com.shuto.ollama-models.plist"
LINUX_TEXT = (
    "On Linux, Ollama runs as a system service, so this needs an administrator. "
    "Ask a helper to follow the steps on the Help page, under For helpers."
)


class StoreError(Exception):
    """Something failed. The message is plain language and safe to show."""


class StoreUnsupported(StoreError):
    """Not available on this system (Linux): the Help page has the steps."""


class MoveCancelled(StoreError):
    """The user stopped the move."""


@dataclass
class Check:
    path: Path
    warnings: list[str] = field(default_factory=list)


@dataclass
class MovePlan:
    old: Path
    new: Path
    files: list[tuple[str, int]]  # (path relative to old, size) of every model file
    todo_bytes: int  # bytes still to copy (identical files already in the new folder are skipped)

    @property
    def total_bytes(self) -> int:
        return sum(s for _, s in self.files)


def os_name() -> str:
    return hardware._os_name()


def supported(name: str | None = None) -> bool:
    return (name or hardware._os_name()) in ("windows", "macos")


def current_models_dir() -> Path:
    return hardware.ollama_models_dir()


def default_models_dir(home: Path | None = None) -> Path:
    return (home or Path.home()) / ".ollama" / "models"


def is_custom() -> bool:
    return bool(os.environ.get(ENV_NAME))


def _norm(p: Path | str) -> str:
    return os.path.normcase(os.path.abspath(os.path.expanduser(str(p))))


def _inside(child: str, parent: str) -> bool:
    return child == parent or child.startswith(parent.rstrip(os.sep) + os.sep)


# ---------------------------------------------------------------- the folder you pick


def validate_target(raw: str, current: Path | None = None) -> Check:
    """Check a folder chosen by the user; creates it if needed. Raises StoreError in plain words."""
    raw = (raw or "").strip().strip('"')
    if not raw or "\x00" in raw:
        raise StoreError("Choose a folder first.")
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise StoreError("Use the full location of the folder, for example D:\\ollama\\models.")
    current = current or current_models_dir()
    new, old = _norm(path), _norm(current)
    if new == old:
        raise StoreError("Ollama already keeps its models in that folder.")
    if _inside(new, old) or _inside(old, new):
        raise StoreError(
            "Choose a folder that is not inside the current models folder, and does not contain it."
        )
    if path.exists() and not path.is_dir():
        raise StoreError("That location is a file, not a folder.")
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / f".shuto-write-test{PART_SUFFIX}"
        probe.write_bytes(b"ok")
        probe.unlink()
    except OSError:
        raise StoreError(
            "shuto cannot save files in that folder. Choose another folder or drive."
        ) from None
    warnings = []
    if path.parent == path:
        warnings.append(
            "That is the top of a drive. A folder inside it, such as ollama\\models, is tidier."
        )
    return Check(path, warnings)


# ---------------------------------------------------------------- saving the setting


def _run(cmd: list[str], timeout: float = 20) -> subprocess.CompletedProcess:
    flags = 0x08000000 if sys.platform == "win32" else 0  # CREATE_NO_WINDOW
    try:
        return subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, creationflags=flags
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return subprocess.CompletedProcess(cmd, 127, "", str(exc))


def _winreg() -> Any:
    import winreg

    return winreg


def _win_broadcast() -> None:
    """Tell running programs (Explorer) that environment variables changed."""
    import ctypes

    result = ctypes.c_ulong()
    ctypes.windll.user32.SendMessageTimeoutW(  # type: ignore[attr-defined]
        0xFFFF, 0x001A, 0, "Environment", 0x0002, 5000, ctypes.byref(result)
    )


def _win_write(value: str | None) -> None:
    reg = _winreg()
    try:
        with reg.OpenKey(reg.HKEY_CURRENT_USER, "Environment", 0, reg.KEY_SET_VALUE) as key:
            if value is None:
                try:
                    reg.DeleteValue(key, ENV_NAME)
                except FileNotFoundError:
                    pass
            else:
                reg.SetValueEx(key, ENV_NAME, 0, reg.REG_SZ, value)
    except OSError:
        raise StoreError("Windows would not let shuto save this setting.") from None
    try:
        _win_broadcast()
    except Exception:
        pass  # only affects programs that are already open


def _plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / LAUNCH_AGENT


def _mac_write(value: str | None) -> None:
    plist = _plist_path()
    if value is None:
        _run(["launchctl", "unsetenv", ENV_NAME])
        try:
            plist.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            raise StoreError("shuto could not remove its start-up setting.") from None
        return
    done = _run(["launchctl", "setenv", ENV_NAME, value])
    if done.returncode != 0:
        raise StoreError("macOS would not let shuto save this setting.")
    try:
        plist.parent.mkdir(parents=True, exist_ok=True)
        with plist.open("wb") as fh:
            plistlib.dump(
                {
                    "Label": LAUNCH_AGENT.removesuffix(".plist"),
                    "ProgramArguments": ["/bin/launchctl", "setenv", ENV_NAME, value],
                    "RunAtLoad": True,
                },
                fh,
            )
    except OSError:
        raise StoreError("shuto could not save the start-up setting for this folder.") from None


def _save_env(value: str | None, os_name: str | None) -> None:
    os_ = os_name or hardware._os_name()
    if os_ == "windows":
        _win_write(value)
    elif os_ == "macos":
        _mac_write(value)
    else:
        raise StoreUnsupported(LINUX_TEXT)
    if value is None:
        os.environ.pop(ENV_NAME, None)
    else:
        os.environ[ENV_NAME] = value


def set_models_dir(path: Path | str, os_name: str | None = None) -> None:
    """Save OLLAMA_MODELS for this user (no admin rights) and for this shuto process."""
    _save_env(str(path), os_name)


def reset_models_dir(os_name: str | None = None) -> None:
    """Undo set_models_dir: Ollama goes back to its own default folder."""
    _save_env(None, os_name)


# ---------------------------------------------------------------- stopping and starting Ollama


def _is_up(client: Any) -> bool:
    try:
        client.version()
    except Exception:
        return False
    return True


def _wait(
    until: Callable[[], bool],
    timeout: float,
    sleep: Callable[[float], None] | None = None,
    clock: Callable[[], float] | None = None,
) -> bool:
    sleep, clock = sleep or time.sleep, clock or time.monotonic
    end = clock() + timeout
    while True:
        if until():
            return True
        if clock() >= end:
            return False
        sleep(0.5)


def _tray_app() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama" / "ollama app.exe"


def _mac_app() -> Path:
    return Path("/Applications/Ollama.app")


def can_start(os_name: str | None = None) -> bool:
    """Is there an Ollama on this computer that shuto can start?"""
    os_ = os_name or hardware._os_name()
    if shutil.which("ollama"):
        return True
    return _tray_app().is_file() if os_ == "windows" else _mac_app().exists()


def stop_ollama(client: Any, os_name: str | None = None, timeout: float = STOP_TIMEOUT_S) -> None:
    """Close Ollama politely first, then by force if it is still running. No-op if not running."""
    os_ = os_name or hardware._os_name()
    if os_ == "windows":
        _run(["taskkill", "/IM", "ollama app.exe"])
        _run(["taskkill", "/IM", "ollama.exe"])
        _wait(lambda: not _is_up(client), timeout)
        _run(["taskkill", "/F", "/IM", "ollama app.exe"])
        _run(["taskkill", "/F", "/IM", "ollama.exe"])
    elif os_ == "macos":
        _run(["osascript", "-e", 'quit app "Ollama"'])
        if not _wait(lambda: not _is_up(client), timeout):
            _run(["pkill", "-x", "ollama"])
    else:
        raise StoreUnsupported(LINUX_TEXT)
    if not _wait(lambda: not _is_up(client), 5):
        raise StoreError("Ollama would not close. Quit Ollama yourself, then try again.")


def _spawn(cmd: list[str]) -> None:
    kw: dict[str, Any] = {}
    if sys.platform == "win32":
        kw["creationflags"] = 0x00000008 | 0x00000200 | 0x08000000  # detached, new group, no window
    else:
        kw["start_new_session"] = True
    try:
        subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            **kw,
        )
    except OSError:
        raise StoreError(
            "shuto could not start Ollama. Open Ollama yourself, then try again."
        ) from None


def start_ollama(client: Any, os_name: str | None = None, timeout: float = START_TIMEOUT_S) -> None:
    """Start Ollama (it inherits the new OLLAMA_MODELS) and wait until it answers."""
    os_ = os_name or hardware._os_name()
    exe = shutil.which("ollama")
    if os_ == "windows":
        app = _tray_app()
        if app.is_file():
            _spawn([str(app)])
        elif exe:
            _spawn([exe, "serve"])
        else:
            raise StoreError("shuto could not find Ollama. Open Ollama from the Start menu.")
    elif os_ == "macos":
        if _run(["open", "-a", "Ollama"]).returncode != 0:
            if not exe:
                raise StoreError("shuto could not find Ollama. Open it from Applications.")
            _spawn([exe, "serve"])
    else:
        raise StoreUnsupported(LINUX_TEXT)
    if not _wait(lambda: _is_up(client), timeout):
        raise StoreError(
            "Ollama did not start again within 30 seconds. Open Ollama yourself "
            "(Start menu on Windows, Applications on a Mac), then check this page."
        )


def restart_ollama(
    client: Any, os_name: str | None = None, timeout: float = START_TIMEOUT_S
) -> None:
    stop_ollama(client, os_name)
    start_ollama(client, os_name, timeout)


# ---------------------------------------------------------------- moving the models


def _model_files(root: Path) -> list[tuple[str, int]]:
    out: list[tuple[str, int]] = []
    for sub in SUBDIRS:
        base = root / sub
        if not base.is_dir():
            continue
        for dirpath, _dirs, names in os.walk(base):
            for name in names:
                p = Path(dirpath) / name
                if "-partial" in name or name.endswith(PART_SUFFIX) or p.is_symlink():
                    continue  # unfinished downloads and links are left alone
                try:
                    out.append((str(p.relative_to(root)), p.stat().st_size))
                except OSError:
                    continue
    return out


def models_size_bytes(root: Path) -> int:
    return sum(s for _, s in _model_files(root))


def _same(src: Path, dst: Path, size: int) -> bool:
    try:
        if not dst.is_file() or dst.stat().st_size != size:
            return False
        return size >= SMALL_FILE or filecmp.cmp(src, dst, shallow=False)
    except OSError:
        return False


def plan_move(old: Path, new: Path) -> MovePlan:
    files = _model_files(old)
    todo = sum(s for rel, s in files if not _same(old / rel, new / rel, s))
    return MovePlan(old, new, files, todo)


Progress = Callable[[int, int, str], None]


def _copy_file(
    src: Path, dst: Path, on_bytes: Callable[[int], None], cancelled: Callable[[], bool]
):
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + PART_SUFFIX)
    try:
        with src.open("rb") as fin, tmp.open("wb") as fout:
            while chunk := fin.read(CHUNK):
                fout.write(chunk)
                on_bytes(len(chunk))
                if cancelled():
                    raise MoveCancelled("stopped")
        os.replace(tmp, dst)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def copy_models(
    plan: MovePlan,
    progress: Progress | None = None,
    cancelled: Callable[[], bool] = lambda: False,
) -> int:
    """Copy every model file not already identical in the new folder, then check all sizes.

    Returns the number of files copied. Never touches the old folder.
    """
    total = max(plan.todo_bytes, 1)
    done = 0
    copied = 0

    def on_bytes(n: int) -> None:
        nonlocal done
        done += n
        if progress:
            progress(done // 1_000_000, total // 1_000_000, "Copying models")

    for rel, size in plan.files:
        src, dst = plan.old / rel, plan.new / rel
        if _same(src, dst, size):
            continue
        if cancelled():
            raise MoveCancelled("stopped")
        try:
            _copy_file(src, dst, on_bytes, cancelled)
        except OSError as exc:
            raise StoreError(
                f"Copying the models failed ({exc.strerror or 'disk error'}). "
                "Your models were not changed."
            ) from None
        copied += 1
    bad = [rel for rel, size in plan.files if not _same(plan.old / rel, plan.new / rel, size)]
    if bad:
        raise StoreError(
            f"{len(bad)} copied file(s) did not match the originals. Your models were not changed."
        )
    return copied


def delete_old_models(plan: MovePlan) -> int:
    """Remove the old model files that were copied and verified. Returns bytes freed."""
    freed = 0
    for rel, size in plan.files:
        try:
            (plan.old / rel).unlink()
            freed += size
        except OSError:
            continue
    for sub in SUBDIRS:
        base = plan.old / sub
        if not base.is_dir():
            continue
        for dirpath, _dirs, _names in os.walk(base, topdown=False):
            try:
                Path(dirpath).rmdir()  # only succeeds when empty
            except OSError:
                pass
    return freed


# ---------------------------------------------------------------- the whole change


def _names(client: Any) -> set[str]:
    try:
        return {
            hardware.normalize_tag(m.get("name") or m.get("model", ""))
            for m in client.tags()
            if m.get("name") or m.get("model")
        }
    except Exception:
        return set()


def _gb(n: int) -> str:
    return f"{n / 1024**3:.1f} GB"


def change_models_dir(
    target: Path | None,
    *,
    move: bool,
    client: Any,
    progress: Progress | None = None,
    cancelled: Callable[[], bool] = lambda: False,
    os_name: str | None = None,
) -> str:
    """Point Ollama at `target` (None = back to the default folder) and restart it.

    Returns a plain-language result. Raises StoreError if something went wrong; in that case
    the previous setting is restored and no model file has been deleted.
    """
    os_ = os_name or hardware._os_name()
    if not supported(os_):
        raise StoreUnsupported(LINUX_TEXT)
    old = current_models_dir()
    new = default_models_dir() if target is None else target
    if _norm(old) == _norm(new):
        raise StoreError("Ollama already keeps its models in that folder.")
    prev = os.environ.get(ENV_NAME)
    was_up = _is_up(client)
    installed = was_up or can_start(os_)
    names_before = _names(client) if was_up else set()

    def save() -> None:
        reset_models_dir(os_) if target is None else set_models_dir(new, os_)

    if not installed:
        save()
        return f"Saved. Ollama will keep its models in {new} once it is installed."

    plan = plan_move(old, new) if move else None
    if plan and plan.files:
        free = hardware.free_gb(new)
        if free is not None and free * 1024**3 < plan.todo_bytes + HEADROOM_GB * 1024**3:
            raise StoreError(
                f"There is not enough room: the models need {_gb(plan.todo_bytes)} and "
                f"{free:g} GB is free there. Choose a drive with more space, "
                "or do not move the models."
            )
    else:
        plan = None

    stopped = False
    try:
        if plan:
            stop_ollama(client, os_)
            stopped = True
            copy_models(plan, progress, cancelled)
        elif was_up:
            stop_ollama(client, os_)
            stopped = True
        save()
        start_ollama(client, os_)
        if plan:
            missing = names_before - _names(client)
            if missing:
                raise StoreError(
                    "Ollama restarted but cannot see " + ", ".join(sorted(missing)) + " in "
                    "the new folder."
                )
    except MoveCancelled:
        _recover(client, os_, prev, stopped or was_up)
        return f"Stopped. Nothing was changed. Your models are still in {old}."
    except StoreError as exc:
        _recover(client, os_, prev, stopped or was_up)
        raise StoreError(
            f"{exc} The setting was put back and your models are still in {old}."
        ) from None

    where = f"Ollama now keeps its models in {new}."
    if plan:
        freed = delete_old_models(plan)
        return f"{where} {len(plan.files)} model file(s) were moved; {_gb(freed)} freed in {old}."
    return where + (
        " Models you downloaded before stay in the old folder." if models_size_bytes(old) else ""
    )


def _recover(client: Any, os_: str, prev: str | None, restart: bool) -> None:
    """Best effort: restore the previous setting and bring Ollama back."""
    try:
        if prev:
            set_models_dir(prev, os_)
        else:
            reset_models_dir(os_)
    except StoreError:
        pass
    if restart:
        try:
            if not _is_up(client):
                start_ollama(client, os_)
        except StoreError:
            pass
