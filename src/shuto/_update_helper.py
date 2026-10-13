"""Standalone shuto updater. Standard library only: it runs outside the shuto package.

shuto copies this file to a temporary folder and starts it detached. It waits for shuto to
exit, installs the new release with uv, goes back to the old version if that fails, writes
the result to a status file and starts shuto again. It never stops any program by name.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

SPEC_RE = re.compile(
    r"shuto\[heic\] @ https://github\.com/alamhanz/shuto/releases/download/"
    r"v(?P<v>\d+\.\d+\.\d+)/shuto-(?P=v)-py3-none-any\.whl#sha256=[0-9a-f]{64}"
)
VERSION_RE = re.compile(r"\d+\.\d+\.\d+")
LOCK_RE = re.compile(
    r"Access is denied|os error 5|os error 32|being used by another process", re.IGNORECASE
)
WAIT_PID_S = 60
INSTALL_TIMEOUT_S = 15 * 60
LOCK_RETRIES = 4
LOCK_RETRY_WAIT_S = 3
MAX_LOG_BYTES = 200 * 1024
WIN = sys.platform == "win32"
CREATE_NEW_CONSOLE = 0x00000010
CREATE_NO_WINDOW = 0x08000000

MSG_STILL_RUNNING = (
    "shuto did not close in time, so the update was not started. Nothing was changed."
)
MSG_LAUNCHER_STUCK = (
    "shuto closed, but its start window did not, so the update was not started. "
    "shuto was opened again and nothing was changed."
)
MSG_LOCKED = (
    "A shuto file was in use, so the update could not finish. "
    "Close any other shuto windows and try again."
)


def log(path: Path, text: str) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_file() and path.stat().st_size > MAX_LOG_BYTES:
            tail = path.read_bytes()[-MAX_LOG_BYTES // 2 :]
            path.write_bytes(tail)
        with path.open("a", encoding="utf-8", errors="replace") as fh:
            fh.write(f"[{datetime.now().isoformat(timespec='seconds')}] {text}\n")
    except OSError:
        pass


def write_status(path: Path, state: str, message: str, from_version: str, to: str) -> None:
    data = {
        "state": state,
        "from": from_version,
        "to": to,
        "message": message,
        "time": datetime.now().isoformat(timespec="seconds"),
        "seen": False,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


def wait_for_pid(pid: int, timeout: float = WAIT_PID_S) -> bool:
    """True once the process is gone (or never existed); False if it is still alive."""
    if WIN:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel32.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
        if not handle:
            return True
        try:
            return kernel32.WaitForSingleObject(handle, int(timeout * 1000)) == 0
        finally:
            kernel32.CloseHandle(handle)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            pass
        time.sleep(0.25)
    return False


def spawn_relaunch(cmd: list[str]) -> None:
    kw: dict = {}
    if WIN:
        kw["creationflags"] = CREATE_NEW_CONSOLE
    else:
        kw.update(
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    subprocess.Popen(cmd, close_fds=True, **kw)


def install(run, uv: str, spec: str, logfile: Path, sleep) -> tuple[bool, bool]:
    """Run `uv tool install`. Returns (ok, failed_because_a_file_was_locked)."""
    cmd = [uv, "tool", "install", "--force", "--python", "3.12", spec]
    locked = False
    for attempt in range(LOCK_RETRIES + 1):
        kw = {"creationflags": CREATE_NO_WINDOW} if WIN else {}
        try:
            proc = run(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                errors="replace",
                timeout=INSTALL_TIMEOUT_S,
                **kw,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            log(logfile, f"Could not run uv: {type(exc).__name__}: {exc}")
            return False, False
        output = proc.stdout or ""
        log(logfile, f"uv exit code {proc.returncode}\n{output.strip()}")
        if proc.returncode == 0:
            return True, False
        locked = bool(LOCK_RE.search(output))
        if not locked or attempt == LOCK_RETRIES:
            break
        log(logfile, f"A file was in use; trying again in {LOCK_RETRY_WAIT_S} s.")
        sleep(LOCK_RETRY_WAIT_S)
    return False, locked


def works(run, relaunch: list[str], logfile: Path) -> bool:
    """True when the installed shuto answers `--version` (the old version survived)."""
    if not relaunch:
        return False
    try:
        proc = run(
            [relaunch[0], "--version"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            timeout=60,
            **({"creationflags": CREATE_NO_WINDOW} if WIN else {}),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log(logfile, f"shuto --version failed: {type(exc).__name__}: {exc}")
        return False
    log(logfile, f"shuto --version: exit {proc.returncode} {(proc.stdout or '').strip()}")
    return proc.returncode == 0


def main(
    argv: list[str] | None = None,
    run=subprocess.run,
    wait_pid=wait_for_pid,
    spawn=spawn_relaunch,
    sleep=time.sleep,
) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--pid", type=int, action="append", required=True)
    p.add_argument("--uv", required=True)
    p.add_argument("--spec", required=True)
    p.add_argument("--rollback-spec")
    p.add_argument("--version", required=True)
    p.add_argument("--from-version", default="")
    p.add_argument("--status", type=Path, required=True)
    p.add_argument("--log", type=Path, required=True)
    p.add_argument("--relaunch", nargs=argparse.REMAINDER, default=[])
    a = p.parse_args(argv)

    def status(state: str, message: str) -> None:
        write_status(a.status, state, message, a.from_version, a.version)

    if (
        not SPEC_RE.fullmatch(a.spec)
        or (a.rollback_spec and not SPEC_RE.fullmatch(a.rollback_spec))
        or not VERSION_RE.fullmatch(a.version)
    ):
        log(a.log, "Refused: the install source is not a shuto release.")
        status("failed", "The update was refused because its source was not a shuto release.")
        return 2

    log(a.log, f"Updating shuto {a.from_version} -> {a.version}")
    status("waiting", "Waiting for shuto to close...")
    gone = [wait_pid(pid) for pid in a.pid]
    if not all(gone):
        log(a.log, "shuto did not close in time; not installing.")
        if gone[0] and a.relaunch:
            # the server itself is gone, only its launcher lingers: bring shuto back
            status("failed", MSG_LAUNCHER_STUCK)
            try:
                spawn(a.relaunch)
            except OSError as exc:
                log(a.log, f"Could not start shuto again: {exc}")
        else:
            status("failed", MSG_STILL_RUNNING)
        return 3
    status("installing", f"Installing shuto {a.version}...")
    ok, locked = install(run, a.uv, a.spec, a.log, sleep)
    if ok:
        status("done", f"shuto was updated to {a.version}.")
    else:
        failure = MSG_LOCKED if locked else f"The update to {a.version} did not finish."
        still_works = works(run, a.relaunch, a.log)
        if not still_works and a.rollback_spec:
            log(a.log, f"shuto does not start; going back to shuto {a.from_version}.")
            restored, _ = install(run, a.uv, a.rollback_spec, a.log, sleep)
            still_works = restored and works(run, a.relaunch, a.log)
        if still_works:
            status(
                "rolled_back",
                f"{failure} shuto is still on {a.from_version}. You can try again later.",
            )
        else:
            status(
                "failed",
                f"{failure} shuto may not start. Run the install line from the shuto "
                "website again to repair it.",
            )
    if a.relaunch:
        try:
            spawn(a.relaunch)
        except OSError as exc:
            log(a.log, f"Could not start shuto again: {exc}")
    folder = Path(__file__).resolve().parent
    if folder.name.startswith("shuto-update-"):
        shutil.rmtree(folder, ignore_errors=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
