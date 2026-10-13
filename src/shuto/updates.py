"""In-app updates: find the newest release, and start the detached updater that installs it.

Only stdlib plus shuto config/pipeline helpers. Downloads come only from the fixed GitHub
release URL below; the version string is validated before it is put in any URL or command.
The install itself is done by `_update_helper.py`, a stdlib-only script that runs outside the
shuto tool folder (Windows locks the files of a running program).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from importlib import metadata
from pathlib import Path
from typing import Any, Literal

from . import hardware, pipeline
from .config import Config

REPO_RELEASES = "https://github.com/alamhanz/shuto/releases"
LATEST_PAGE = REPO_RELEASES + "/latest"
WHEEL_PREFIX = REPO_RELEASES + "/download/v"
VERSION_RE = re.compile(r"\d+\.\d+\.\d+")  # final releases only; always use fullmatch
TAG_URL_RE = re.compile(r"https://github\.com/alamhanz/shuto/releases/tag/v(\d+\.\d+\.\d+)")
SPEC_RE = re.compile(
    r"shuto\[heic\] @ https://github\.com/alamhanz/shuto/releases/download/"
    r"v(?P<v>\d+\.\d+\.\d+)/shuto-(?P=v)-py3-none-any\.whl"
)
REDIRECTS = (301, 302, 303, 307, 308)
CHECK_TTL_S = 24 * 3600
HTTP_TIMEOUT_S = 5
LOOPBACK = {"127.0.0.1", "::1", "localhost"}
HELPER_SCRIPT = Path(__file__).with_name("_update_helper.py")

_FETCH_LOCK = threading.Lock()


class UpdateError(Exception):
    """The update could not be started; the message is plain language."""


@dataclass(frozen=True)
class Latest:
    version: str
    wheel_url: str
    notes_url: str
    checked_at: float


@dataclass(frozen=True)
class Eligibility:
    ok: bool
    reason: str | None


def wheel_url(version: str) -> str:
    if not VERSION_RE.fullmatch(version):
        raise ValueError(f"Not a release version: {version!r}")
    return f"{WHEEL_PREFIX}{version}/shuto-{version}-py3-none-any.whl"


def notes_url(version: str) -> str:
    if not VERSION_RE.fullmatch(version):
        raise ValueError(f"Not a release version: {version!r}")
    return f"{REPO_RELEASES}/tag/v{version}"


def _parts(version: str) -> tuple[int, ...] | None:
    if not VERSION_RE.fullmatch(version):
        return None
    return tuple(int(p) for p in version.split("."))


def is_newer(latest: str, current: str) -> bool:
    a, b = _parts(latest), _parts(current)
    return a is not None and b is not None and a > b


def current_version() -> str:
    try:
        return metadata.version("shuto")
    except metadata.PackageNotFoundError:
        return "dev"


# --------------------------------------------------------------------------- version check


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def fetch_latest(
    opener: Any = None, timeout: float = HTTP_TIMEOUT_S, wheel_opener: Any = None
) -> Latest | None:
    """Ask GitHub which release is the latest. Any problem (offline, odd answer) gives None.

    The wheel is only probed: a redirect from the release URL proves it exists, and it is not
    followed, so nothing is downloaded.
    """
    if opener is None:
        opener = urllib.request.build_opener()
        wheel_opener = wheel_opener or urllib.request.build_opener(_NoRedirect)
    wheel_opener = wheel_opener or opener
    headers = {"User-Agent": f"shuto/{current_version()}"}

    def head(client: Any, url: str) -> tuple[str, int]:
        req = urllib.request.Request(url, method="HEAD", headers=headers)
        try:
            resp = client.open(req, timeout=timeout)
        except urllib.error.HTTPError as err:
            location = err.headers.get("Location", "") if err.headers else ""
            if err.code in REDIRECTS and location.startswith("https://"):
                return url, 200
            raise
        try:
            return resp.geturl(), resp.getcode()
        finally:
            close = getattr(resp, "close", None)
            if close:
                close()

    try:
        final, _ = head(opener, LATEST_PAGE)
        match = TAG_URL_RE.fullmatch(final)
        if not match:
            return None
        version = match.group(1)
        _, code = head(wheel_opener, wheel_url(version))
        if code != 200:
            return None
        return Latest(version, wheel_url(version), notes_url(version), time.time())
    except Exception:
        return None


def _latest_from_cache(cache: Any) -> Latest | None:
    if not isinstance(cache, dict) or not cache.get("ok"):
        return None
    version = cache.get("version")
    if not isinstance(version, str) or not VERSION_RE.fullmatch(version):
        return None
    return Latest(version, wheel_url(version), notes_url(version), float(cache["checked_at"]))


def peek_cached(store) -> Latest | None:
    """The last known latest release, without any network call."""
    return _latest_from_cache(pipeline.get_ui_setting(store, "update_cache"))


def last_check_failed(store) -> bool:
    cache = pipeline.get_ui_setting(store, "update_cache")
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
        cache = pipeline.get_ui_setting(store, "update_cache")
        stamp = cache.get("checked_at") if isinstance(cache, dict) else None
        fresh = isinstance(stamp, int | float) and 0 <= now() - stamp < CHECK_TTL_S
        if fresh and not force:
            return _latest_from_cache(cache)
        found = fetch()
        previous = cache.get("version", "") if isinstance(cache, dict) else ""
        pipeline.set_ui_setting(
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
    spec: str,
    rollback_spec: str | None,
    version: str,
    from_version: str,
    status: Path,
    log: Path,
    relaunch: list[str],
) -> list[str]:
    """Argument list for the updater (every value is its own element; never a shell string)."""
    if not SPEC_RE.fullmatch(spec):
        raise ValueError("Refusing to install from outside the shuto releases page.")
    if rollback_spec is not None and not SPEC_RE.fullmatch(rollback_spec):
        raise ValueError("Refusing to roll back to a file outside the shuto releases page.")
    cmd = [
        *python,
        str(script),
        *[arg for pid in pids for arg in ("--pid", str(pid))],
        "--uv",
        uv,
        "--spec",
        spec,
        "--version",
        version,
        "--from-version",
        from_version,
        "--status",
        str(status),
        "--log",
        str(log),
    ]
    if rollback_spec:
        cmd += ["--rollback-spec", rollback_spec]
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
) -> None:
    """Write the status file, copy the updater to a temp folder and start it detached."""
    spawn = spawn or spawn_detached
    version = latest.version
    current = current_version()
    spec = f"shuto[heic] @ {wheel_url(version)}"
    rollback = f"shuto[heic] @ {wheel_url(current)}" if VERSION_RE.fullmatch(current) else None
    relaunch = relaunch_cmd(hardware._os_name(), uv, ui_args)
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
        spec=spec,
        rollback_spec=rollback,
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
