"""In-app updates: can we update, and start the detached updater that installs the release.

Only stdlib plus shuto config and settings helpers. `release` finds the newest release and
`download` fetches the wheel; this module keeps the rest. Downloads come only from the fixed
GitHub release URL (see `release`); the version string is validated before it is put in any
URL or command. shuto downloads the wheel itself, checks its sha256 against the digest GitHub
publishes for the release asset, and keeps the verified file in the workspace; the helper
re-checks the hash right before installing from that file. The install itself is done by
`_helper.py`, a stdlib-only script that runs outside the shuto tool folder (Windows locks the
files of a running program).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from importlib import metadata
from pathlib import Path
from typing import Any, Literal

from shuto.core import platform as core_platform
from shuto.core.config import Config
from shuto.settings import ui as settings_ui
from shuto.update.download import (  # noqa: F401  (re-exported: other packages use update.core.X)
    DOWNLOAD_TIMEOUT_S,
    MAX_WHEEL_BYTES,
    WHEEL_HOSTS,
    _GithubOnlyRedirect,
    _is_wheel_host,
    download_wheel,
    prune_wheels,
    wheels_dir,
)
from shuto.update.release import (  # noqa: F401  (re-exported: other packages use update.core.X)
    DIGEST_RE,
    HTTP_TIMEOUT_S,
    LATEST_PAGE,
    MAX_API_BYTES,
    MSG_DOWNLOAD,
    MSG_MISMATCH,
    MSG_NO_DIGEST,
    RELEASE_API,
    REPO_RELEASES,
    SHA256_RE,
    TAG_URL_RE,
    VERSION_RE,
    WHEEL_PREFIX,
    Latest,
    UpdateError,
    _latest_from_cache,
    _NoRedirect,
    _parts,
    current_version,
    fetch_latest,
    fetch_wheel_digest,
    is_newer,
    notes_url,
    wheel_name,
    wheel_url,
)

CHECK_TTL_S = 24 * 3600
LOOPBACK = {"127.0.0.1", "::1", "localhost"}
HELPER_SCRIPT = Path(__file__).with_name("_helper.py")

_FETCH_LOCK = threading.Lock()


@dataclass(frozen=True)
class Eligibility:
    ok: bool
    reason: str | None


def peek_cached(store) -> Latest | None:
    """The last known latest release, without any network call."""
    return _latest_from_cache(settings_ui.get_ui_setting(store, "update_cache"))


def last_check_failed(store) -> bool:
    cache = settings_ui.get_ui_setting(store, "update_cache")
    return isinstance(cache, dict) and cache.get("ok") is False


def cached_latest(
    store,
    *,
    force: bool = False,
    now: Callable[[], float] = time.time,
    fetch: Callable[[], Latest | None] | None = None,
) -> Latest | None:
    """The latest release, checked at most once a day (a failed check counts as a check)."""
    fetch = fetch or fetch_latest
    with _FETCH_LOCK:
        cache = settings_ui.get_ui_setting(store, "update_cache")
        stamp = cache.get("checked_at") if isinstance(cache, dict) else None
        fresh = isinstance(stamp, int | float) and 0 <= now() - stamp < CHECK_TTL_S
        if fresh and not force:
            return _latest_from_cache(cache)
        found = fetch()
        previous = cache.get("version", "") if isinstance(cache, dict) else ""
        settings_ui.set_ui_setting(
            store,
            "update_cache",
            {
                "version": found.version if found else str(previous),
                "checked_at": now(),
                "ok": found is not None,
            },
        )
        return found


# --------------------------------------------------------------------------- can we update?


def install_kind() -> Literal["uv-tool", "editable", "dev", "other"]:
    try:
        dist = metadata.distribution("shuto")
    except metadata.PackageNotFoundError:
        return "dev"
    try:
        direct = json.loads(dist.read_text("direct_url.json") or "{}")
        if direct.get("dir_info", {}).get("editable"):
            return "editable"
    except (ValueError, AttributeError):
        pass
    if Path(sys.prefix, "uv-receipt.toml").is_file():
        return "uv-tool"
    return "other"


def find_uv() -> str | None:
    """Same places the installers look, plus scoop and Homebrew."""
    found = shutil.which("uv")
    if found:
        return found
    home = Path.home()
    exe = "uv.exe" if sys.platform == "win32" else "uv"
    dirs = [home / ".local" / "bin", home / ".cargo" / "bin"]
    if sys.platform == "win32":
        dirs.append(home / "scoop" / "shims")
    elif sys.platform == "darwin":
        dirs += [Path("/opt/homebrew/bin"), Path("/usr/local/bin")]
    for d in dirs:
        candidate = d / exe
        if candidate.is_file():
            return str(candidate)
    return None


def eligibility(
    runner, *, client_host: str, kind: str | None = None, uv: str | None = None
) -> Eligibility:
    kind = kind or install_kind()
    if kind == "dev":
        return Eligibility(False, "Running from source; update with git.")
    if kind != "uv-tool":
        return Eligibility(
            False,
            "This copy of shuto was not installed with the installer, so it cannot update itself.",
        )
    if not (uv or find_uv()):
        return Eligibility(
            False,
            "shuto could not find its installer helper (uv). Run the install line again.",
        )
    if client_host not in LOOPBACK:
        return Eligibility(False, "Updates can only be started on this computer.")
    if runner.busy():
        return Eligibility(False, "Wait until the current task finishes, then update.")
    return Eligibility(True, None)


# --------------------------------------------------------------------------- the updater


def own_pids() -> list[int]:
    """This server and, on Windows, its launcher parent (shuto.exe / venv python), which must
    also be gone before the install can replace their files."""
    pids = [os.getpid()]
    if sys.platform == "win32" and os.getppid() > 0:
        pids.append(os.getppid())
    return pids


def build_helper_cmd(
    *,
    python: list[str],
    script: Path,
    pids: list[int],
    uv: str,
    wheels: Path,
    sha256: str,
    rollback_sha256: str | None,
    version: str,
    from_version: str,
    status: Path,
    log: Path,
    relaunch: list[str],
) -> list[str]:
    """Argument list for the updater (every value is its own element; never a shell string)."""
    if not VERSION_RE.fullmatch(version) or not SHA256_RE.fullmatch(sha256):
        raise ValueError("Refusing to install something that is not a verified release.")
    if tuple(wheels.parts[-2:]) != ("updates", "wheels"):
        raise ValueError("Refusing to install from outside the shuto updates folder.")
    if rollback_sha256 is not None and (
        not SHA256_RE.fullmatch(rollback_sha256) or not VERSION_RE.fullmatch(from_version)
    ):
        raise ValueError("Refusing to roll back to something that is not a verified release.")
    cmd = [
        *python,
        str(script),
        *[arg for pid in pids for arg in ("--pid", str(pid))],
        "--uv",
        uv,
        "--wheels",
        str(wheels),
        "--sha256",
        sha256,
        "--version",
        version,
        "--from-version",
        from_version,
        "--status",
        str(status),
        "--log",
        str(log),
    ]
    if rollback_sha256:
        cmd += ["--rollback-sha256", rollback_sha256]
    return [*cmd, "--relaunch", *relaunch]


def helper_python(uv: str) -> list[str]:
    """An interpreter outside the tool folder (it is locked and replaced during the install)."""
    base = getattr(sys, "_base_executable", None)
    if base and Path(base).is_file():
        try:
            Path(base).resolve().relative_to(Path(sys.prefix).resolve())
        except ValueError:
            return [base, "-I"]
    return [uv, "run", "--no-project", "--python", "3.12", "--", "python", "-I"]


def relaunch_cmd(
    os_name: str, uv: str | None = None, ui_args: list[str] | None = None
) -> list[str]:
    """How the updater starts shuto again: the same `ui` options as now, headless.

    No new browser tab opens: the page that is already open reloads itself.
    """
    tail = [*(ui_args or ["ui"]), "--no-browser"]
    exe = "shuto.exe" if os_name == "windows" else "shuto"
    argv0 = Path(sys.argv[0]) if sys.argv and sys.argv[0] else None
    if argv0 is not None and argv0.stem == "shuto" and argv0.is_file():
        return [str(argv0.resolve()), *tail]
    if uv:
        try:
            out = subprocess.run(
                [uv, "tool", "dir", "--bin"],
                capture_output=True,
                text=True,
                timeout=15,
                check=True,
            ).stdout.strip()
            candidate = Path(out) / exe
            if out and candidate.is_file():
                return [str(candidate), *tail]
        except (OSError, subprocess.SubprocessError):
            pass
    found = shutil.which("shuto")
    if found:
        return [found, *tail]
    raise UpdateError("shuto could not find its own start command. Run the install line again.")


def spawn_detached(cmd: list[str]) -> None:
    """Start a process that outlives this one (no shell, no console, own process group)."""
    kw: dict[str, Any] = {}
    flags = 0
    if sys.platform == "win32":
        # new process group and a hidden console (so uv's own children never flash a window)
        flags = 0x00000200 | 0x08000000
        kw["creationflags"] = flags | 0x01000000  # breakaway from the parent's job object
    else:
        kw["start_new_session"] = True

    def run() -> None:
        subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            **kw,
        )

    try:
        try:
            run()
        except OSError:
            if sys.platform != "win32":
                raise
            kw["creationflags"] = flags  # the job forbids breakaway; try without it
            run()
    except OSError:
        raise UpdateError(
            "shuto could not start the updater. Run the install line again."
        ) from None


def status_path(cfg: Config) -> Path:
    return cfg.workspace_path("updates", "status.json")


def log_path(cfg: Config) -> Path:
    return cfg.workspace_path("updates", "update.log")


def read_status(cfg: Config) -> dict | None:
    try:
        data = json.loads(status_path(cfg).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and isinstance(data.get("state"), str) else None


def write_status(cfg: Config, **fields: Any) -> None:
    path = status_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {"time": datetime.now().isoformat(timespec="seconds"), "seen": False, **fields}
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    os.replace(tmp, path)


def mark_status_seen(cfg: Config) -> None:
    data = read_status(cfg)
    if data and not data.get("seen"):
        write_status(cfg, **{**data, "seen": True})


def start_update(
    cfg: Config,
    latest: Latest,
    *,
    uv: str,
    pids: list[int] | None = None,
    ui_args: list[str] | None = None,
    spawn: Callable[[list[str]], None] | None = None,
    fetch_digest: Callable[[str], str] | None = None,
    download: Callable[[str, str], Path] | None = None,
) -> None:
    """Download and verify the new wheel, then start the detached updater.

    The wheel's sha256 is looked up on GitHub and compared with the downloaded bytes first;
    without a match nothing is started (never unverified). The current version's wheel is
    kept too (best effort) so the updater can roll back to a verified file.
    """
    spawn = spawn or spawn_detached
    fetch_digest = fetch_digest or fetch_wheel_digest
    wheels = wheels_dir(cfg)
    download = download or (lambda v, h: download_wheel(v, h, wheels))
    version = latest.version
    current = current_version()
    sha256 = fetch_digest(version)
    download(version, sha256)
    rollback_sha256 = None
    keep = {wheel_name(version)}
    if VERSION_RE.fullmatch(current):
        keep.add(wheel_name(current))  # the installed tool's receipt may point at this file
        try:  # no verified old wheel means no rollback, never an unverified one
            old = fetch_digest(current)
            download(current, old)
            rollback_sha256 = old
        except UpdateError:
            rollback_sha256 = None
    prune_wheels(wheels, keep)
    relaunch = relaunch_cmd(core_platform.os_name(), uv, ui_args)
    python = helper_python(uv)
    log_path(cfg).parent.mkdir(parents=True, exist_ok=True)
    write_status(cfg, state="started", **{"from": current}, to=version, message="")
    try:
        folder = Path(tempfile.mkdtemp(prefix="shuto-update-"))
        script = folder / HELPER_SCRIPT.name
        shutil.copyfile(HELPER_SCRIPT, script)
    except OSError:
        raise UpdateError("shuto could not prepare the update. Try again.") from None
    cmd = build_helper_cmd(
        python=python,
        script=script,
        pids=pids if pids is not None else own_pids(),
        uv=uv,
        wheels=wheels,
        sha256=sha256,
        rollback_sha256=rollback_sha256,
        version=version,
        from_version=current,
        status=status_path(cfg),
        log=log_path(cfg),
        relaunch=relaunch,
    )
    spawn(cmd)


def wait_for_helper(
    cfg: Config,
    *,
    timeout: float = 10.0,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> bool:
    """True once the updater is running (status "waiting" or "installing"); False on timeout.

    Raises UpdateError with the updater's own message if it already gave up.
    """
    deadline = clock() + timeout
    while True:
        status = read_status(cfg)
        state = status.get("state") if status else "started"
        if state in ("waiting", "installing"):
            return True
        if state != "started":
            raise UpdateError(
                str(status.get("message") or "")
                or "The updater stopped before it could start. Nothing was changed."
            )
        if clock() >= deadline:
            return False
        sleep(0.25)
