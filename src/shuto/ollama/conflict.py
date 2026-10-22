"""Spot another Ollama (in WSL, Docker or an old install) answering instead of the one installed.

Everything here is best effort: any doubt or failure means "no conflict found". The checks run
only when a model is missing or on the settings and help screens, never on the normal path, and
every program started has a short timeout.
"""

from __future__ import annotations

import functools
import os
import re
import shutil
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from shuto.ai import hardware
from shuto.core import platform as core_platform
from shuto.ollama import OLLAMA_DOWNLOAD_URL, env

HELP_LINK = "/help#other-ollama"
HELP_SENTENCE = " Open Help for the steps."
MAX_MANIFESTS = 500
LOOPBACK = {"127.0.0.1", "localhost", "::1"}
_VERSION_RE = re.compile(r"(\d+(?:\.\d+)*)")


@dataclass(frozen=True)
class Diagnosis:
    kind: str  # "older_server" | "unlisted_models" (the main reason)
    server_version: str | None
    installed_version: str | None
    unlisted: list[str]  # models on disk that the running Ollama does not list
    hint: str  # "" | "wsl" | "docker" | "no_windows_process"
    message: str  # one plain paragraph
    steps: list[str]


def _normalize_version(text: str) -> str | None:
    m = _VERSION_RE.search(text.strip().lstrip("vV"))
    return m.group(1) if m else None


def parse_cli_version(text: str) -> str | None:
    """Version of the installed program from `ollama --version` output.

    Either `ollama version is X` or two lines (`ollama version is S` of the running server and
    `Warning: client version is C`). The client line wins."""
    server = None
    for line in (text or "").splitlines():
        low = line.lower()
        if "client version is" in low:
            return _normalize_version(low.split("client version is", 1)[1])
        if "version is" in low and server is None:
            server = _normalize_version(low.split("version is", 1)[1])
    return server


def _version_tuple(text: str | None) -> tuple[int, ...] | None:
    v = _normalize_version(text or "")
    return tuple(int(p) for p in v.split(".")) if v else None


def is_newer(a: str | None, b: str | None) -> bool:
    """True only when both versions are known and `a` is newer than `b`."""
    ta, tb = _version_tuple(a), _version_tuple(b)
    return ta is not None and tb is not None and ta > tb


def _windows_exe() -> str | None:
    base = os.environ.get("LOCALAPPDATA")
    if not base:
        return None
    path = Path(base) / "Programs" / "Ollama" / "ollama.exe"
    return str(path) if path.exists() else None


def installed_version(run=env._run, which=shutil.which) -> str | None:
    """Version of the Ollama program installed here (None when missing, slow or unreadable)."""
    exe = which("ollama")
    if exe is None and sys.platform == "win32":
        exe = _windows_exe()
    if exe is None:
        return None
    proc = run([exe, "--version"], 3)
    if proc.returncode != 0:
        return None
    return parse_cli_version((proc.stdout or "") + "\n" + (proc.stderr or ""))


@functools.lru_cache(maxsize=1)
def installed_version_cached() -> str | None:
    """`installed_version()` once per process, so page polls start no new programs."""
    return installed_version()


def disk_models(models_dir: Path) -> set[str]:
    """Tags that have a manifest under `models_dir/manifests` (empty when unreadable)."""
    found: set[str] = set()

    def walk(path: str, parts: list[str]) -> None:
        if len(parts) > 6:
            return
        try:
            with os.scandir(path) as it:
                entries = list(it)
        except OSError:
            return
        for e in entries:
            if len(found) >= MAX_MANIFESTS:
                return
            try:
                is_dir = e.is_dir()
            except OSError:
                continue
            if is_dir:
                walk(e.path, [*parts, e.name])
            elif len(parts) >= 3:
                host, *middle, name = parts
                tag = f"{name}:{e.name}"
                if host == "registry.ollama.ai" and middle == ["library"]:
                    found.add(hardware.normalize_tag(tag))
                else:
                    found.add(hardware.normalize_tag("/".join([host, *middle, tag])))

    walk(str(Path(models_dir) / "manifests"), [])
    return found


def _port_of(url: str) -> int | None:
    try:
        port = urlparse(url).port or 11434
    except ValueError:
        return None
    return port if 0 < port < 65536 else None


def _is_loopback(url: str) -> bool:
    try:
        host = urlparse(url).hostname
    except ValueError:
        return False
    return (host or "") in LOOPBACK


def port_hint(url: str, os_name=None, run=env._run, which=shutil.which) -> str:
    """Windows only: who holds the port. "wsl", "docker", "no_windows_process" or ""."""
    if (os_name or core_platform.os_name()) != "windows":
        return ""
    port = _port_of(url)
    if port is None:
        return ""
    net = run(["netstat", "-ano", "-p", "tcp"], 3)
    if net.returncode != 0:
        return ""
    pid = None
    for line in (net.stdout or "").splitlines():
        cols = line.split()
        if len(cols) >= 5 and cols[3].upper() == "LISTENING" and cols[1].endswith(f":{port}"):
            pid = cols[4]
            break
    if pid is None:
        return "no_windows_process"
    if not pid.isdigit():
        return ""
    task = run(["tasklist", "/FI", f"PID eq {int(pid)}", "/FO", "CSV", "/NH"], 3)
    name = ""
    if task.returncode == 0 and task.stdout.strip().startswith('"'):
        name = task.stdout.strip().split(",")[0].strip('"').lower()
    if name in ("wslrelay.exe", "wslhost.exe"):
        return "wsl"
    if name.startswith(("docker", "com.docker")) or "vpnkit" in name:
        return "docker"
    if not name.startswith("ollama") and which("wsl"):
        return "wsl"
    return ""


def _steps(hint: str) -> list[str]:
    if hint == "wsl":
        first = "Close the other Ollama. It is most likely running inside WSL (Linux on Windows)."
    elif hint == "docker":
        first = "Close the other Ollama. It is most likely running in Docker."
    else:
        first = (
            "Close the other Ollama (for example one in WSL or Docker), "
            "or restart Ollama if you just updated it."
        )
    return [
        first,
        "Open Ollama from the Start menu (Windows) or Applications (Mac).",
        "Come back here and press Try again.",
    ]


def diagnose(
    client,
    models_dir: Path,
    *,
    installed: str | None = None,
    hint_for: Callable[[str], str] | None = None,
    disk: Callable[[Path], set[str]] | None = None,
) -> Diagnosis | None:
    """Is a different Ollama answering than the one installed here? None when not or unsure."""
    url = getattr(client, "url", "http://127.0.0.1:11434")
    if not _is_loopback(url):
        return None  # a remote Ollama is chosen on purpose
    try:
        server = (client.version() or "").strip() or None
        listed = {
            hardware.normalize_tag(m.get("name") or m.get("model", ""))
            for m in client.tags()
            if m.get("name") or m.get("model")
        }
    except Exception:
        return None
    inst = installed or installed_version_cached()
    older = is_newer(inst, server)
    unlisted = sorted((disk or disk_models)(models_dir) - listed)
    if not older and not unlisted:
        return None
    hint = (hint_for or port_hint)(url)
    if older:
        message = (
            f"The Ollama answering on this computer is version {server}, but you installed "
            f"{inst}. Another copy of Ollama (often inside WSL or Docker) is using the same "
            "address, so your own Ollama and its models are not being used."
        )
    else:
        message = (
            f"shuto found downloaded models in {models_dir} that the running Ollama does not "
            "list. Another Ollama may be answering."
        )
    return Diagnosis(
        "older_server" if older else "unlisted_models",
        server,
        inst,
        unlisted,
        hint,
        message + HELP_SENTENCE,
        _steps(hint),
    )


def pull_412_message(err: str, d: Diagnosis | None, server_version: str | None) -> str | None:
    """Plain replacement for Ollama's 'requires a newer version' refusal (else None)."""
    low = (err or "").lower()
    if "412" not in low and "requires a newer version" not in low:
        return None
    if d is not None and d.kind == "older_server":
        steps = " ".join(f"{i}) {s}" for i, s in enumerate(d.steps, 1))
        base = d.message.removesuffix(HELP_SENTENCE)
        return (
            f"{base} The download was refused because that older Ollama cannot pull this "
            f"model. {steps}"
        )
    shown = f" (version {server_version})" if server_version else ""
    return (
        f"The Ollama running now{shown} is too old for this model. "
        f"Update Ollama from {OLLAMA_DOWNLOAD_URL}."
    )
