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

import errno
import filecmp
import hashlib
import json
import os
import plistlib
import re
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
    conflicts: list[str] = field(default_factory=list)  # files that exist there but differ

    @property
    def total_bytes(self) -> int:
        return sum(s for _, s in self.files)


def os_name() -> str:
    return hardware._os_name()


def supported(name: str | None = None) -> bool:
    return (name or hardware._os_name()) in ("windows", "macos")


def current_models_dir() -> Path:
    """Where Ollama keeps its models, from the saved setting rather than this process's copy."""
    os_ = hardware._os_name()
    if supported(os_):
        value = saved_value(os_) or machine_any_value(os_)
        if value:
            return Path(value).expanduser()
        return hardware.ollama_models_dir(env={})
    return hardware.ollama_models_dir()


def default_models_dir(home: Path | None = None) -> Path:
    return (home or Path.home()) / ".ollama" / "models"


def is_custom() -> bool:
    os_ = hardware._os_name()
    if supported(os_):
        return bool(saved_value(os_) or machine_any_value(os_))
    return bool(os.environ.get(ENV_NAME))


MACHINE_KEY = r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"
MACHINE_TEXT = (
    "The models folder is set for the whole computer by an administrator, so shuto cannot "
    "change it. Ask someone with administrator rights, or a helper, to change it "
    "(see the Help page, under For helpers)."
)
MACHINE_RESET_TEXT = (
    "The whole computer is set up by an administrator to keep models in another folder, so "
    "going back to the standard folder would not work: Ollama would use the administrator's "
    "folder again after the next sign-in. Ask someone with administrator rights, or a helper, "
    "to change it (see the Help page, under For helpers)."
)


def _win_read(machine: bool) -> str | None:
    """OLLAMA_MODELS as stored in Windows for this user (machine=False) or for everyone."""
    reg = _winreg()
    root, sub = (
        (reg.HKEY_LOCAL_MACHINE, MACHINE_KEY) if machine else (reg.HKEY_CURRENT_USER, "Environment")
    )
    try:
        with reg.OpenKey(root, sub, 0, reg.KEY_READ) as key:
            value, _kind = reg.QueryValueEx(key, ENV_NAME)
    except OSError:
        return None
    return str(value) if value else None


def machine_any_value(os_name: str | None = None) -> str | None:
    """The whole-computer OLLAMA_MODELS on Windows, whether or not this user also has one."""
    if (os_name or hardware._os_name()) != "windows":
        return None
    try:
        return _win_read(True)
    except Exception:
        return None


def machine_wide_value(os_name: str | None = None) -> str | None:
    """The whole-computer OLLAMA_MODELS on Windows when this user has no value of their own
    (a user value would win over it). shuto cannot change it without administrator rights."""
    if (os_name or hardware._os_name()) != "windows":
        return None
    try:
        if _win_read(False):
            return None
    except Exception:
        return None
    return machine_any_value(os_name)


def saved_value(os_name: str | None = None) -> str | None:
    """OLLAMA_MODELS as saved for this user (registry / launchctl), not as this process
    happened to inherit it. Where nothing is saved (or on Linux) None, or the process value."""
    os_ = os_name or hardware._os_name()
    if os_ == "windows":
        try:
            return _win_read(False)
        except Exception:
            return None
    if os_ == "macos":
        value = str(getattr(_run(["launchctl", "getenv", ENV_NAME]), "stdout", "") or "").strip()
        if value:
            return value
        try:
            args = plistlib.loads(_plist_path().read_bytes()).get("ProgramArguments", [])
            return str(args[3]) if len(args) > 3 else None
        except Exception:
            return None
    return os.environ.get(ENV_NAME) or None


def _real(p: Path | str) -> Path:
    """The folder as the file system sees it: links, junctions, subst drives and short names
    resolved (as far as the folder exists)."""
    return Path(os.path.abspath(os.path.expanduser(str(p)))).resolve(strict=False)


def same_dir(a: Path | str, b: Path | str) -> bool:
    """Are these two spellings the same folder? Compares real identity, not text."""
    ra, rb = _real(a), _real(b)
    if os.path.normcase(str(ra)) == os.path.normcase(str(rb)):
        return True
    try:
        return os.path.samefile(ra, rb)
    except OSError:
        return False  # one of them does not exist, so they cannot be the same folder


def is_inside(child: Path | str, parent: Path | str) -> bool:
    """Is `child` the folder `parent` or somewhere below it (by real identity)?"""
    cur = _real(child)
    while True:
        if same_dir(cur, parent):
            return True
        if cur.parent == cur:
            return False
        cur = cur.parent


def check_distinct(old: Path | str, new: Path | str) -> None:
    """Raise a plain-language StoreError unless the two folders are really different places."""
    if same_dir(new, old):
        raise StoreError("Ollama already keeps its models in that folder.")
    if is_inside(new, old) or is_inside(old, new):
        raise StoreError(
            "Choose a folder that is not inside the current models folder, and does not contain it."
        )


# ---------------------------------------------------------------- the folder you pick


def validate_target(raw: str, current: Path | None = None) -> Check:
    """Check a folder chosen by the user. Raises StoreError in plain words.

    Nothing is created here; the folder is made (and tested) when the change runs.
    """
    raw = (raw or "").strip().strip('"')
    if not raw or "\x00" in raw:
        raise StoreError("Choose a folder first.")
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise StoreError("Use the full location of the folder, for example D:\\ollama\\models.")
    path = Path(os.path.abspath(path))
    check_distinct(current or current_models_dir(), path)
    if path.exists() and not path.is_dir():
        raise StoreError("That location is a file, not a folder.")
    warnings = []
    if path.parent == path:
        warnings.append(
            "That is the top of a drive. A folder inside it, such as ollama\\models, is tidier."
        )
    return Check(path, warnings)


@dataclass
class Created:
    """What one run created in the new folder, so a failed or stopped run can undo exactly that."""

    files: list[Path] = field(default_factory=list)
    dirs: list[Path] = field(default_factory=list)

    def remove(self) -> int:
        """Delete only what this run created. Returns how many items could not be removed."""
        left = 0
        for f in reversed(self.files):
            try:
                f.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                left += 1
        for d in reversed(self.dirs):  # deepest first (they were recorded parent first)
            try:
                d.rmdir()  # only succeeds when empty
            except FileNotFoundError:
                pass
            except OSError:
                left += 1
        return left


def _missing_dirs(path: Path) -> list[Path]:
    """The folders in path (parent first) that do not exist yet."""
    out: list[Path] = []
    cur = path
    while not cur.exists() and cur.parent != cur:
        out.append(cur)
        cur = cur.parent
    return out[::-1]


def prepare_folder(path: Path, created: Created) -> None:
    """Make the chosen folder and prove shuto can write there. Records what it made."""
    try:
        created.dirs.extend(_missing_dirs(path))
        path.mkdir(parents=True, exist_ok=True)
        probe = path / f".shuto-write-test{PART_SUFFIX}"
        probe.write_bytes(b"ok")
        probe.unlink()
    except OSError:
        raise StoreError(
            "shuto cannot save files in that folder. Choose another folder or drive."
        ) from None


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


def _manifest_digests(path: Path) -> set[str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    items = [data.get("config") or {}, *(data.get("layers") or [])]
    return {str(i["digest"]).replace(":", "-") for i in items if i.get("digest")}


def freeable_bytes(root: Path, name: str) -> int | None:
    """Bytes Ollama would really free by deleting model `name`: only the blobs that no other
    model uses (models often share layers). None when the manifests cannot be read."""
    name = hardware.normalize_tag(name)
    model, _, tag = name.rpartition(":")
    parts = model.split("/")
    if len(parts) == 1:
        parts = ["library", model]
    wanted = Path("manifests", "registry.ollama.ai", *parts, tag)
    try:
        mine: set[str] = set()
        others: set[str] = set()
        found = False
        for rel, _size in _model_files(root):
            if not rel.startswith("manifests"):
                continue
            digests = _manifest_digests(root / rel)
            if Path(rel) == wanted:
                mine, found = digests, True
            else:
                others |= digests
        if not found:
            return None
        return sum(
            (root / "blobs" / d).stat().st_size
            for d in mine - others
            if (root / "blobs" / d).is_file()
        )
    except (OSError, ValueError, AttributeError, TypeError):
        return None


def _same(src: Path, dst: Path, size: int) -> bool:
    try:
        if not dst.is_file() or dst.stat().st_size != size:
            return False
        return size >= SMALL_FILE or filecmp.cmp(src, dst, shallow=False)
    except OSError:
        return False


_BLOB_NAME = re.compile(r"^sha256-([0-9a-f]{64})$")
_hash_cache: dict[tuple[str, int, int], bool] = {}


def _blob_matches_name(path: Path) -> bool:
    """Does a model blob hold the data its name promises (blobs are named by their sha256)?
    Files that are not named that way are trusted."""
    m = _BLOB_NAME.match(path.name)
    if not m:
        return True
    st = path.stat()
    key = (str(path), st.st_size, st.st_mtime_ns)
    if key not in _hash_cache:
        digest = hashlib.sha256()
        with path.open("rb") as fh:
            while chunk := fh.read(CHUNK):
                digest.update(chunk)
        _hash_cache[key] = digest.hexdigest() == m.group(1)
    return _hash_cache[key]


def plan_move(old: Path, new: Path) -> MovePlan:
    files = _model_files(old)
    todo = 0
    conflicts: list[str] = []
    for rel, size in files:
        dst = new / rel
        if _same(old / rel, dst, size):
            # a big file already there was not copied by us: prove it is intact before the
            # original can be deleted; a damaged one is never overwritten, so it is a conflict
            try:
                intact = size < SMALL_FILE or _blob_matches_name(dst)
            except OSError:
                intact = False
            if not intact:
                conflicts.append(rel)
            continue
        if dst.exists():
            conflicts.append(rel)  # same name, different content: never overwritten
        else:
            todo += size
    return MovePlan(old, new, files, todo, conflicts)


def conflict_message(plan: MovePlan) -> str:
    n = len(plan.conflicts)
    return (
        f"That folder already holds models, and {n} of its files have the same name as yours "
        "but are different. shuto will not overwrite them, so nothing was changed. "
        "Choose an empty folder, or choose the folder without moving the models."
    )


def manifest_names(files: list[tuple[str, int]]) -> set[str]:
    """Model names (as Ollama lists them) for the manifest files of a models folder."""
    names: set[str] = set()
    for rel, _size in files:
        parts = Path(rel).parts
        if len(parts) < 5 or parts[0] != "manifests":
            continue
        host, ns, tail = parts[1], parts[2], parts[3:]
        model, tag = "/".join(tail[:-1]), tail[-1]
        name = f"{model}:{tag}"
        if ns != "library":
            name = f"{ns}/{name}"
        if host not in ("registry.ollama.ai", "registry"):
            name = f"{host}/{name}"
        names.add(hardware.normalize_tag(name))
    return names


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
        if dst.exists():
            raise FileExistsError(errno.EEXIST, "already exists")  # never overwrite
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
    created: Created | None = None,
) -> int:
    """Copy every model file not already identical in the new folder, then check all sizes.

    Returns the number of files copied. Never touches the old folder and never overwrites a
    file in the new one; what it creates is recorded in `created`.
    """
    created = created if created is not None else Created()
    if plan.conflicts:
        raise StoreError(conflict_message(plan))
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
        created.dirs.extend(_missing_dirs(dst.parent))
        try:
            _copy_file(src, dst, on_bytes, cancelled)
        except OSError as exc:
            raise StoreError(
                f"Copying the models failed ({exc.strerror or 'disk error'}). "
                "Your models were not changed."
            ) from None
        created.files.append(dst)
        copied += 1
    bad = [rel for rel, size in plan.files if not _same(plan.old / rel, plan.new / rel, size)]
    if bad:
        raise StoreError(
            f"{len(bad)} copied file(s) did not match the originals. Your models were not changed."
        )
    return copied


def delete_old_models(plan: MovePlan) -> int:
    """Remove the old model files that were copied and verified. Returns bytes freed.

    Refuses (and deletes nothing) unless every copy exists, has the same size and is a
    different file from the original. Manifests go first, so an interrupted delete never
    leaves a model that points at blobs that are gone.
    """
    if same_dir(plan.old, plan.new):
        raise StoreError("The old and the new folder are the same place.")
    for rel, size in plan.files:
        src, dst = plan.old / rel, plan.new / rel
        try:
            if not dst.is_file() or dst.stat().st_size != size:
                raise StoreError(f"The copy of {rel} is missing or incomplete.")
            if src.exists() and os.path.samefile(src, dst):
                raise StoreError(f"{rel} in the old folder is the same file as its copy.")
        except OSError:
            raise StoreError(f"shuto could not check the copy of {rel}.") from None
    freed = 0
    for rel, size in sorted(plan.files, key=lambda f: not f[0].startswith("manifests")):
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


def _names(client: Any) -> set[str] | None:
    """Models Ollama lists right now; None when it could not be asked."""
    try:
        return {
            hardware.normalize_tag(m.get("name") or m.get("model", ""))
            for m in client.tags()
            if m.get("name") or m.get("model")
        }
    except Exception:
        return None


def login_order_warning(client: Any, os_name: str | None = None) -> str | None:
    """macOS only: after a restart Ollama can start before the folder setting is applied, and
    then it looks in the default folder and lists nothing. Say so when that seems to be the case."""
    if (os_name or hardware._os_name()) != "macos" or not is_custom():
        return None
    folder = current_models_dir()
    names = _names(client)
    if (
        names
        or names is None
        or not any(n.startswith("manifests") for n, _ in _model_files(folder))
    ):
        return None
    return (
        f"Ollama is running but shows no models, although {folder} has some. On a Mac, Ollama "
        "can start at login before the setting is applied. Choose Quit Ollama in the menu bar, "
        "open it again from Applications, and your models will be back."
    )


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
    if machine_wide_value(os_):
        raise StoreError(MACHINE_TEXT)
    if target is None and machine_any_value(os_):
        raise StoreError(MACHINE_RESET_TEXT)
    old = current_models_dir()
    new = default_models_dir() if target is None else Path(os.path.abspath(target))
    check_distinct(old, new)
    prev = saved_value(os_)
    was_up = _is_up(client)
    installed = was_up or can_start(os_)

    def save() -> None:
        reset_models_dir(os_) if target is None else set_models_dir(new, os_)

    if not installed:
        save()
        return f"Saved. Ollama will keep its models in {new} once it is installed."

    if move:  # a first look, while Ollama is still running: stop early if the move cannot work
        _check_room(plan_move(old, new), new)

    created = Created()
    restart = False
    plan: MovePlan | None = None
    keep_old = False
    try:
        prepare_folder(new, created)
        if was_up:
            restart = True
            stop_ollama(client, os_)
        if move:  # planned after Ollama stopped, so a finishing download is not missed
            plan = plan_move(old, new)
            if plan.conflicts:
                raise StoreError(conflict_message(plan))
            if not plan.files:
                plan = None
            else:
                _check_room(plan, new)
                copy_models(plan, progress, cancelled, created)
        save()
        restart = True
        start_ollama(client, os_)
        if plan:
            expected = manifest_names(plan.files)
            seen = _names(client)
            if seen is None:
                raise StoreError("Ollama restarted but shuto could not ask it for its models.")
            missing = expected - seen
            if missing:
                raise StoreError(
                    "Ollama restarted but cannot see " + ", ".join(sorted(missing)) + " in "
                    "the new folder."
                )
            keep_old = not expected  # nothing to check the copy against: do not delete
    except MoveCancelled:
        stopped_text = f"Stopped. Nothing was changed. Your models are still in {old}."
        return _undo(client, os_, prev, restart, created, new, stopped_text)
    except StoreError as exc:
        _undo(client, os_, prev, restart, created, new, "")
        raise StoreError(
            f"{exc} The setting was put back and your models are still in {old}."
        ) from None
    except Exception as exc:
        _undo(client, os_, prev, restart, created, new, "")
        raise StoreError(
            f"Something unexpected went wrong ({type(exc).__name__}). "
            f"The setting was put back and your models are still in {old}."
        ) from None

    where = f"Ollama now keeps its models in {new}."
    if plan and keep_old:
        return f"{where} The models were copied; the originals in {old} were left in place."
    if plan:
        try:
            freed = delete_old_models(plan)
        except StoreError as exc:
            return f"{where} The models were copied, but the originals in {old} were kept: {exc}"
        return f"{where} {len(plan.files)} model file(s) were moved; {_gb(freed)} freed in {old}."
    return where + (
        " Models you downloaded before stay in the old folder." if models_size_bytes(old) else ""
    )


def _check_room(plan: MovePlan, new: Path) -> None:
    if plan.conflicts:
        raise StoreError(conflict_message(plan))
    if not plan.files:
        return
    free = hardware.free_gb(new)
    if free is not None and free * 1024**3 < plan.todo_bytes + HEADROOM_GB * 1024**3:
        raise StoreError(
            f"There is not enough room: the models need {_gb(plan.todo_bytes)} and "
            f"{free:g} GB is free there. Choose a drive with more space, "
            "or do not move the models."
        )


def _undo(
    client: Any,
    os_: str,
    prev: str | None,
    restart: bool,
    created: Created,
    new: Path,
    message: str,
) -> str:
    """Put the old setting back, bring Ollama back, then remove only what this run created."""
    _recover(client, os_, prev, restart)
    left = created.remove()
    if left:
        message += f" Some copied files could not be removed from {new}."
    return message


def _recover(client: Any, os_: str, prev: str | None, restart: bool) -> None:
    """Best effort: restore the previous setting and bring Ollama back on it.

    Ollama may already be running on the new folder, so it is always stopped and started again.
    """
    try:
        if prev:
            set_models_dir(prev, os_)
        else:
            reset_models_dir(os_)
    except StoreError:
        pass
    if restart:
        try:
            stop_ollama(client, os_)
        except StoreError:
            pass
        try:
            start_ollama(client, os_)
        except StoreError:
            pass
