import subprocess
from pathlib import Path
from types import SimpleNamespace

from shuto import hardware as hw


def fake_run(table):
    def run(cmd, **kw):
        v = table.get(cmd[0])
        if v is None:
            raise FileNotFoundError(cmd[0])
        if isinstance(v, Exception):
            raise v
        return SimpleNamespace(returncode=0, stdout=v)

    return run


def files(d):
    return lambda p: d.get(p)


def test_linux_nvidia_12gb():
    run = fake_run({"nvidia-smi": "NVIDIA GeForce RTX 3060, 12288, 11000\n"})
    p = hw.detect(
        run,
        os_name="linux",
        arch="x86_64",
        cores=8,
        read_text=files(
            {
                "/proc/meminfo": "MemTotal:       32768000 kB\n",
                "/proc/cpuinfo": "model name\t: AMD Ryzen 5\n",
            }
        ),
        glob=lambda pat: [],
    )
    assert p.tier == "gpu_12" and p.usable_gpu_gb == 12.0
    assert p.gpus[0].vendor == "nvidia" and abs(p.ram_gb - 31.2) < 0.2
    assert p.cpu == "AMD Ryzen 5"


def mac_run(mem_gb, wired="0\n"):
    def run(cmd, **kw):
        if "iogpu.wired_limit_mb" in cmd:
            return SimpleNamespace(returncode=0, stdout=wired)
        return SimpleNamespace(returncode=0, stdout=f"Apple M2 Pro\n{mem_gb * 1024**3}\n")

    return run


def test_macos_apple_32gb():
    p = hw.detect(mac_run(32), os_name="macos", arch="arm64", cores=10)
    assert p.apple_silicon and p.ram_gb == 32.0
    assert abs(p.usable_gpu_gb - 22.4) < 0.1 and p.tier == "apple_32"


def test_macos_64gb_and_wired_limit():
    p = hw.detect(mac_run(64), os_name="macos", arch="arm64", cores=12)
    assert p.usable_gpu_gb == 48.0 and p.tier == "apple_64"
    q = hw.detect(mac_run(16, wired="14336\n"), os_name="macos", arch="arm64", cores=8)
    assert q.usable_gpu_gb == 14.0 and q.tier == "apple_16"


def test_windows_cpu_only_missing_tools():
    p = hw.detect(fake_run({}), os_name="windows", arch="amd64", cores=4, windows_ram=lambda: 16.0)
    assert p.tier == "cpu" and p.usable_gpu_gb == 0 and p.ram_gb == 16.0 and p.gpus == []


def test_windows_amd_name_only_adds_note():
    run = fake_run({"powershell": "AMD Radeon RX 6700\n"})
    p = hw.detect(run, os_name="windows", arch="amd64", cores=8, windows_ram=lambda: 32.0)
    assert p.gpus[0].vendor == "amd" and p.gpus[0].vram_total_gb is None
    assert p.tier == "cpu" and any("VRAM unknown" in n for n in p.notes)


def test_linux_amd_sysfs():
    d = {
        "/proc/meminfo": "MemTotal: 16777216 kB\n",
        "/sys/class/drm/card0/device/mem_info_vram_total": str(8 * 1024**3),
        "/sys/class/drm/card0/device/vendor": "0x1002\n",
        "/sys/class/drm/card0/device/mem_info_vram_used": str(1024**3),
    }
    p = hw.detect(
        fake_run({}),
        os_name="linux",
        arch="x86_64",
        cores=8,
        read_text=files(d),
        glob=lambda pat: ["/sys/class/drm/card0/device/mem_info_vram_total"],
    )
    assert p.gpus[0].vendor == "amd" and p.tier == "gpu_8"
    assert p.gpus[0].vram_free_gb == 7.0


def test_timeout_is_noted_not_raised():
    run = fake_run({"nvidia-smi": subprocess.TimeoutExpired("nvidia-smi", 5)})
    p = hw.detect(
        run, os_name="linux", arch="x86_64", cores=2, read_text=files({}), glob=lambda x: []
    )
    assert p.tier == "cpu" and any("timed out" in n for n in p.notes)
    assert any("RAM" in n for n in p.notes)


def test_small_nvidia_falls_back_to_cpu():
    run = fake_run({"nvidia-smi": "GT 710, 2048, 2000\n"})
    p = hw.detect(
        run, os_name="linux", arch="x86_64", cores=2, read_text=files({}), glob=lambda x: []
    )
    assert p.tier == "cpu" and p.usable_gpu_gb == 0


def test_fingerprint_stable_and_differs():
    def d(ram):
        return hw.detect(
            fake_run({}), os_name="windows", arch="amd64", cores=4, windows_ram=lambda: ram
        )

    assert d(16.0).fingerprint == d(16.0).fingerprint != d(32.0).fingerprint


class FakeClient:
    url = "http://x:1"

    def __init__(self, ok=True):
        self.ok = ok

    def version(self):
        if not self.ok:
            raise RuntimeError("down")
        return "0.9.0"

    def tags(self):
        return [{"name": "gemma3:4b"}, {"model": "moondream:latest"}]


def test_ollama_status():
    s = hw.ollama_status(FakeClient(), which=lambda n: None)
    assert s.running and s.installed and s.pulled == ["gemma3:4b", "moondream:latest"]
    d = hw.ollama_status(FakeClient(False), which=lambda n: "/usr/bin/ollama")
    assert d.installed and not d.running and d.pulled == []
    assert not hw.ollama_status(FakeClient(False), which=lambda n: None).installed


def test_models_dir_env_and_defaults():
    home = Path("/h")
    assert hw.ollama_models_dir({"OLLAMA_MODELS": "/mnt/x"}, "linux", home) == Path("/mnt/x")
    assert hw.ollama_models_dir({}, "macos", home) == home / ".ollama" / "models"
    svc = Path("/usr/share/ollama/.ollama/models")
    assert hw.ollama_models_dir({}, "linux", home, exists=lambda p: p == svc) == svc
    assert (
        hw.ollama_models_dir({}, "linux", home, exists=lambda p: True)
        == home / ".ollama" / "models"
    )
    assert hw.ollama_models_dir({}, "windows", home) == home / ".ollama" / "models"


def test_free_gb_walks_to_existing_parent():
    seen = []

    def usage(p):
        seen.append(p)
        return SimpleNamespace(free=50 * 1024**3, total=100 * 1024**3)

    existing = Path("/a")
    out = hw.free_gb(Path("/a/b/c"), usage, exists=lambda p: p in (existing, Path("/")))
    assert out == 50.0 and Path(seen[0]) == existing


def test_mount_points_per_os():
    assert hw.mount_points("windows", exists=lambda p: p in ("C:\\", "E:\\")) == ["C:\\", "E:\\"]
    assert hw.mount_points("macos", listdir=lambda p: ["Macintosh HD", "Ext"]) == [
        "/",
        "/Volumes/Ext",
        "/Volumes/Macintosh HD",
    ]
    mounts = (
        "proc /proc proc rw 0 0\n/dev/sda1 / ext4 rw 0 0\n"
        "/dev/sdb1 /mnt/ext\\040hd ext4 rw 0 0\ntmpfs /run tmpfs rw 0 0\n"
    )
    assert hw.mount_points("linux", read_text=lambda p: mounts) == ["/", "/mnt/ext hd"]


def test_other_volumes_filters_and_sorts_windows():
    free = {"C:\\": 5, "D:\\": 500, "E:\\": 100, "F:\\": 2}

    def usage(m):
        return SimpleNamespace(free=free[m] * 1024**3, total=1000 * 1024**3)

    v = hw.other_volumes(
        10, Path("C:/Users/me/.ollama/models"), "windows", ["C:\\", "D:\\", "E:\\", "F:\\"], usage
    )
    assert [x.mount for x in v] == ["D:\\", "E:\\"]
    assert v[0].suggested_path == "D:\\ollama\\models"


def test_other_volumes_linux_excludes_holder():
    def usage(m):
        return SimpleNamespace(free=200 * 1024**3, total=1000 * 1024**3)

    v = hw.other_volumes(10, Path("/home/me/.ollama/models"), "linux", ["/", "/mnt/ext"], usage)
    assert [x.mount for x in v] == ["/mnt/ext"] and v[0].suggested_path == "/mnt/ext/ollama/models"


def test_env_hint_per_os():
    assert hw.set_env_hint("E:\\o", "windows").startswith("setx OLLAMA_MODELS")
    assert "launchctl" in hw.set_env_hint("/Volumes/X/o", "macos")
    assert "systemctl edit ollama" in hw.set_env_hint("/mnt/o", "linux")
