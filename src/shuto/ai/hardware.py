"""Hardware detection, Ollama status and model-storage disk probing.

Every probe is guarded (timeout 5 s, missing tool -> skipped with a note) and all
OS access is injectable so tests never touch the real machine.
"""

from __future__ import annotations

import hashlib
import os
import platform
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

from shuto.core import platform as core_platform

GPU_TIERS = ("gpu_6", "gpu_8", "gpu_12", "gpu_24")
APPLE_TIERS = ("apple_16", "apple_32", "apple_64")


@dataclass
class GPU:
    vendor: Literal["nvidia", "amd", "apple", "intel", "unknown"]
    name: str
    vram_total_gb: float | None = None
    vram_free_gb: float | None = None


@dataclass
class OllamaStatus:
    installed: bool
    running: bool
    version: str | None
    url: str
    pulled: list[str] = field(default_factory=list)
    entries: list[dict] = field(default_factory=list)  # Ollama's /api/tags rows, as listed


@dataclass
class HardwareProfile:
    os: str
    arch: str
    cpu: str
    cores: int
    ram_gb: float
    gpus: list[GPU]
    apple_silicon: bool
    usable_gpu_gb: float
    tier: str
    fingerprint: str
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------- helpers


def _default_read(path: str) -> str | None:
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _default_glob(pattern: str) -> list[str]:
    import glob

    return sorted(glob.glob(pattern))


def _windows_ram_gb() -> float | None:
    try:
        import ctypes

        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        st = MEMORYSTATUSEX()
        st.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):  # type: ignore[attr-defined]
            return None
        return st.ullTotalPhys / 1024**3
    except Exception:
        return None


def tier_for(usable_gpu_gb: float, apple_silicon: bool) -> str:
    if apple_silicon:
        if usable_gpu_gb < 16.8:
            return "apple_16"
        if usable_gpu_gb < 33.6:
            return "apple_32"
        return "apple_64"
    if usable_gpu_gb < 4:
        return "cpu"
    if usable_gpu_gb < 7:
        return "gpu_6"
    if usable_gpu_gb < 10:
        return "gpu_8"
    if usable_gpu_gb < 18:
        return "gpu_12"
    return "gpu_24"


def _fingerprint(os_: str, arch: str, cpu: str, ram_gb: float, gpus: list[GPU]) -> str:
    key = "|".join([os_, arch, cpu, str(round(ram_gb)), ",".join(sorted(g.name for g in gpus))])
    return hashlib.sha256(key.encode()).hexdigest()[:16]


# ---------------------------------------------------------------- probes


def _nvidia(run, notes) -> list[GPU]:
    out = core_platform.run(
        run,
        [
            "nvidia-smi",
            "--query-gpu=name,memory.total,memory.free",
            "--format=csv,noheader,nounits",
        ],
        notes,
        "nvidia-smi",
    )
    gpus: list[GPU] = []
    for line in (out or "").splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 3:
            continue
        try:
            total, free = float(parts[-2]) / 1024, float(parts[-1]) / 1024
        except ValueError:
            continue
        gpus.append(GPU("nvidia", ",".join(parts[:-2]), round(total, 2), round(free, 2)))
    return gpus


def _amd_linux(run, notes, read_text, glob) -> list[GPU]:
    gpus: list[GPU] = []
    for p in glob("/sys/class/drm/card*/device/mem_info_vram_total"):
        raw = read_text(p)
        dev = p.rsplit("/", 1)[0]
        vendor = (read_text(dev + "/vendor") or "").strip().lower()
        if vendor and vendor != "0x1002":
            continue
        try:
            total = int((raw or "").strip()) / 1024**3
        except ValueError:
            continue
        free = None
        used = read_text(dev + "/mem_info_vram_used")
        try:
            free = round(total - int((used or "").strip()) / 1024**3, 2)
        except ValueError:
            pass
        card = dev.split("/")[-2] if dev.count("/") >= 2 else "card"
        gpus.append(GPU("amd", f"AMD GPU ({card})", round(total, 2), free))
    if gpus:
        return gpus
    out = core_platform.run(run, ["rocm-smi", "--showmeminfo", "vram", "--json"], notes, "rocm-smi")
    if out:
        import json

        try:
            data = json.loads(out)
            for card, info in data.items():
                total = int(info.get("VRAM Total Memory (B)", 0)) / 1024**3
                used = int(info.get("VRAM Total Used Memory (B)", 0)) / 1024**3
                if total:
                    gpus.append(
                        GPU("amd", f"AMD GPU ({card})", round(total, 2), round(total - used, 2))
                    )
        except (ValueError, AttributeError, TypeError):
            notes.append("rocm-smi: unparseable output")
    return gpus


def _windows_gpu_names(run, notes) -> list[GPU]:
    out = core_platform.run(
        run,
        ["powershell", "-NoProfile", "-Command", "(Get-CimInstance Win32_VideoController).Name"],
        notes,
        "gpu name query",
        timeout=15,  # a cold PowerShell start can take several seconds
    )
    gpus = []
    for name in (out or "").splitlines():
        name = name.strip()
        if not name:
            continue
        low = name.lower()
        if "amd" in low or "radeon" in low:
            vendor = "amd"
        elif "intel" in low:
            vendor = "intel"
        elif "nvidia" in low:
            vendor = "nvidia"
        else:
            vendor = "unknown"
        gpus.append(GPU(vendor, name, None, None))  # AdapterRAM is capped at 4 GB: unreliable
    return gpus


def _linux_ram(read_text) -> float | None:
    txt = read_text("/proc/meminfo") or ""
    for line in txt.splitlines():
        if line.startswith("MemTotal:"):
            try:
                return int(line.split()[1]) / 1024**2
            except (IndexError, ValueError):
                return None
    return None


def detect(
    run: Callable = subprocess.run,
    *,
    os_name: str | None = None,
    arch: str | None = None,
    cores: int | None = None,
    read_text: Callable[[str], str | None] = _default_read,
    glob: Callable[[str], list[str]] = _default_glob,
    windows_ram: Callable[[], float | None] = _windows_ram_gb,
) -> HardwareProfile:
    notes: list[str] = []
    os_ = os_name or core_platform.os_name()
    arch = arch or platform.machine().lower() or "unknown"
    cores = cores or os.cpu_count() or 1
    cpu = ""
    ram: float | None = None
    apple = False
    usable = 0.0

    if os_ == "macos":
        out = core_platform.run(
            run, ["sysctl", "-n", "machdep.cpu.brand_string", "hw.memsize"], notes, "sysctl"
        )
        lines = (out or "").strip().splitlines()
        if lines:
            cpu = lines[0].strip()
            try:
                ram = int(lines[1].strip()) / 1024**3 if len(lines) > 1 else None
            except ValueError:
                ram = None
        apple = arch in ("arm64", "aarch64") or cpu.startswith("Apple")
    elif os_ == "linux":
        ram = _linux_ram(read_text)
        for line in (read_text("/proc/cpuinfo") or "").splitlines():
            if line.lower().startswith(("model name", "hardware")) and ":" in line:
                cpu = line.split(":", 1)[1].strip()
                break
    elif os_ == "windows":
        ram = windows_ram()
        cpu = os.environ.get("PROCESSOR_IDENTIFIER", "")
    cpu = cpu or platform.processor() or arch
    if ram is None:
        notes.append("could not detect RAM; assuming 8 GB")
        ram = 8.0
    ram = round(ram, 1)

    if apple:
        out = core_platform.run(
            run, ["sysctl", "-n", "iogpu.wired_limit_mb"], notes, "sysctl iogpu"
        )
        try:
            wired_mb = int((out or "0").strip() or 0)
        except ValueError:
            wired_mb = 0
        usable = wired_mb / 1024 if wired_mb > 0 else ram * (0.75 if ram >= 64 else 0.70)
        gpus = [GPU("apple", cpu or "Apple Silicon", ram, round(usable, 1))]
    else:
        gpus = _nvidia(run, notes)
        if not gpus and os_ == "linux":
            gpus = _amd_linux(run, notes, read_text, glob)
        if not gpus and os_ == "windows":
            gpus = _windows_gpu_names(run, notes)
            if gpus:
                notes.append(
                    "GPU VRAM unknown on Windows for non-NVIDIA cards; assuming CPU tier "
                    "(Ollama may still use the GPU)"
                )
        sized = [g.vram_total_gb for g in gpus if g.vram_total_gb]
        usable = round(max(sized), 2) if sized else 0.0
        if os_ == "macos":
            notes.append("Intel Mac: CPU-only inference")

    tier = tier_for(usable, apple)
    if not apple and 0 < usable < 4:
        notes.append(f"GPU has only {usable:.1f} GB VRAM; using CPU tier")
        usable = 0.0
    return HardwareProfile(
        os=os_,
        arch=arch,
        cpu=cpu,
        cores=cores,
        ram_gb=ram,
        gpus=gpus,
        apple_silicon=apple,
        usable_gpu_gb=round(usable, 2),
        tier=tier,
        fingerprint=_fingerprint(os_, arch, cpu, ram, gpus),
        notes=notes,
    )


# ---------------------------------------------------------------- ollama


def normalize_tag(tag: str) -> str:
    return tag if ":" in tag else f"{tag}:latest"


def ollama_status(client: Any, which: Callable[[str], str | None] = shutil.which) -> OllamaStatus:
    url = getattr(client, "url", "http://127.0.0.1:11434")
    version: str | None = None
    running = False
    pulled: list[str] = []
    entries: list[dict] = []
    try:
        version = client.version() or None
        running = True
        entries = list(client.tags())
        names = [m.get("name") or m.get("model", "") for m in entries]
        pulled = [n for n in names if n]
    except Exception:
        pass
    installed = bool(which("ollama")) or running
    return OllamaStatus(installed, running, version, url, pulled, entries)


# ---------------------------------------------------------------- disk


def ollama_models_dir(
    env: dict[str, str] | None = None,
    os_name: str | None = None,
    home: Path | None = None,
    exists: Callable[[Path], bool] = lambda p: p.exists(),
) -> Path:
    """Where Ollama stores models (OLLAMA_MODELS wins; else per-OS default)."""
    env = os.environ if env is None else env
    os_ = os_name or core_platform.os_name()
    if env.get("OLLAMA_MODELS"):
        return Path(env["OLLAMA_MODELS"]).expanduser()
    home = home or Path.home()
    default = home / ".ollama" / "models"
    if os_ == "linux":
        svc = Path("/usr/share/ollama/.ollama/models")
        if not exists(default) and exists(svc):
            return svc
    return default


def _existing_parent(p: Path, exists: Callable[[Path], bool]) -> Path:
    cur = p
    while not exists(cur) and cur.parent != cur:
        cur = cur.parent
    return cur


def free_gb(
    path: Path,
    usage: Callable = shutil.disk_usage,
    exists: Callable[[Path], bool] = lambda p: p.exists(),
) -> float | None:
    """Free space (GB) on the volume that holds (or would hold) path."""
    try:
        return round(usage(str(_existing_parent(path, exists))).free / 1024**3, 2)
    except OSError:
        return None


@dataclass
class Volume:
    mount: str
    free_gb: float
    total_gb: float
    suggested_path: str

    def to_dict(self) -> dict:
        return asdict(self)


_PSEUDO_FS = {
    "proc", "sysfs", "tmpfs", "devtmpfs", "devpts", "cgroup", "cgroup2", "overlay",
    "squashfs", "autofs", "securityfs", "debugfs", "tracefs", "pstore", "bpf", "configfs",
    "fusectl", "mqueue", "hugetlbfs", "binfmt_misc", "efivarfs", "ramfs", "nsfs",
}  # fmt: skip


def mount_points(
    os_name: str | None = None,
    read_text: Callable[[str], str | None] = _default_read,
    exists: Callable[[str], bool] = os.path.exists,
    listdir: Callable[[str], list[str]] = os.listdir,
) -> list[str]:
    os_ = os_name or core_platform.os_name()
    if os_ == "windows":
        return [f"{c}:\\" for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ" if exists(f"{c}:\\")]
    if os_ == "macos":
        out = ["/"]
        try:
            out += [f"/Volumes/{v}" for v in sorted(listdir("/Volumes"))]
        except OSError:
            pass
        return out
    out = []
    for line in (read_text("/proc/mounts") or "").splitlines():
        parts = line.split()
        if len(parts) < 3 or parts[2] in _PSEUDO_FS:
            continue
        mnt = parts[1].replace("\\040", " ")
        if mnt.startswith(("/proc", "/sys", "/dev", "/run", "/snap", "/boot")):
            continue
        out.append(mnt)
    return out or ["/"]


def other_volumes(
    need_gb: float,
    models_dir: Path,
    os_name: str | None = None,
    mounts: list[str] | None = None,
    usage: Callable = shutil.disk_usage,
) -> list[Volume]:
    """Mounted drives/volumes with at least need_gb free, excluding the current one."""
    os_ = os_name or core_platform.os_name()
    mounts = mounts if mounts is not None else mount_points(os_)
    win = os_ == "windows"
    sep = "\\" if win else "/"

    def norm(s: str) -> str:
        s = s.replace("\\", "/") if win else s
        return (s.lower() if win else s).rstrip("/")

    md = norm(str(models_dir))
    holder = ""
    for m in mounts:
        nm = norm(m)
        if (md == nm or md.startswith(nm + "/")) and len(nm) >= len(holder):
            holder = nm
    vols: list[Volume] = []
    for m in mounts:
        if norm(m) == holder:
            continue
        try:
            u = usage(m)
        except OSError:
            continue
        free = u.free / 1024**3
        if free < need_gb:
            continue
        sugg = m.rstrip("\\/") + sep + "ollama" + sep + "models"
        vols.append(Volume(m, round(free, 1), round(u.total / 1024**3, 1), sugg))
    return sorted(vols, key=lambda v: -v.free_gb)


def set_env_hint(path: str, os_name: str | None = None) -> str:
    os_ = os_name or core_platform.os_name()
    if os_ == "windows":
        return f'setx OLLAMA_MODELS "{path}"   (then restart Ollama)'
    if os_ == "macos":
        return f'launchctl setenv OLLAMA_MODELS "{path}"   (then restart the Ollama app)'
    return (
        f'export OLLAMA_MODELS="{path}" (shell); for the systemd service run '
        f'`systemctl edit ollama` and add Environment="OLLAMA_MODELS={path}"'
    )
