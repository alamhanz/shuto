"""Install shuto from a built wheel into a throw-away uv tool folder and check that it works.

    python scripts/smoke_install.py                      build the checkout, install, serve pages
    python scripts/smoke_install.py --from-wheel X.whl   same, with a wheel you already have
    python scripts/smoke_install.py --upgrade-from 0.5.0 simulate an update: install that release
                                                         (GitHub asset), make a workspace with
                                                         saved settings, install the new wheel over
                                                         it, and check nothing was lost
    python scripts/smoke_install.py --keep               keep the temp folder (path is printed)

Everything lives in a temp folder outside the repo: uv tool dir, its bin dir, the config, the
workspace and the user config/cache dirs. Needs `uv` (and `gh`, logged in, for --upgrade-from) and
network access to PyPI. Standard library only. Exit code 0 means every check passed.

The upgrade simulation covers the install and start part of the in-app update. The in-app button
itself (download, verify, helper, relaunch) is checked once by hand on a real install.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import textwrap
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
GITHUB_REPO = "alamhanz/shuto"
WINDOWS = os.name == "nt"
SETTINGS_TO_SAVE = {"max_tries": 3, "language": {"english_only": False, "language": "Japanese"}}
PAGES = ("/healthz", "/", "/settings")

# Run with the OLD install's python: writes the settings an upgrading user would already have.
STATE_SCRIPT = textwrap.dedent(
    """
    import json, sys
    from shuto import pipeline
    from shuto.config import load_config

    cfg = load_config(sys.argv[1])
    store = pipeline.open_store(cfg)
    wanted = {"onboarded": True, **json.loads(sys.argv[2])}
    saved, skipped = {}, []
    for key, value in wanted.items():
        if key in pipeline.UI_SETTING_KEYS:
            saved[key] = pipeline.set_ui_setting(store, key, value)
        else:
            skipped.append(key)
    batches = sorted({p.batch_id for p in store.list_photos() if p.batch_id})
    print(json.dumps({"saved": saved, "skipped": skipped, "batches": batches}))
    """
)
# Run with the OLD install's python: a few synthetic photos (nothing real is needed).
PHOTOS_SCRIPT = textwrap.dedent(
    """
    import random, sys
    from datetime import datetime
    from pathlib import Path
    from PIL import Image

    out = Path(sys.argv[1])
    out.mkdir(parents=True, exist_ok=True)
    for n in range(1, 4):
        rng = random.Random(n)
        coarse = Image.new("RGB", (12, 9))
        coarse.putdata([tuple(rng.randrange(40, 216) for _ in range(3)) for _ in range(108)])
        size = (2400, 1800)
        img = Image.blend(
            coarse.resize(size, Image.Resampling.BICUBIC),
            Image.effect_noise(size, 40).convert("RGB"),
            0.35,
        )
        exif = Image.Exif()
        exif.get_ifd(0x8769)[0x9003] = datetime(2025, 3, 3 + n, 10).strftime("%Y:%m:%d %H:%M:%S")
        img.save(out / f"photo{n}.jpg", "JPEG", quality=92, exif=exif.tobytes())
    """
)


class SmokeError(Exception):
    pass


def step(text: str) -> None:
    print(f"== {text}", flush=True)


def ok(text: str) -> None:
    print(f"   ok: {text}", flush=True)


def run(cmd, *, env=None, cwd=None, check=True, timeout=900) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        [str(c) for c in cmd],
        env=env,
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    if check and proc.returncode != 0:
        raise SmokeError(
            f"command failed ({proc.returncode}): {' '.join(map(str, cmd))}\n"
            f"{proc.stdout[-2000:]}\n{proc.stderr[-2000:]}"
        )
    return proc


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Sandbox:
    """The temp folder and the two environments (uv's tool folders; shuto's user folders)."""

    def __init__(self, root: Path):
        self.root = root
        self.tool_dir = root / "uv-tools"
        self.bin_dir = root / "uv-bin"
        home = root / "home"
        self.config = root / "shuto.yaml"
        self.workspace = root / "workspace"
        self.uv_env = {
            **os.environ,
            "UV_TOOL_DIR": str(self.tool_dir),
            "UV_TOOL_BIN_DIR": str(self.bin_dir),
        }
        self.app_env = {
            **os.environ,
            "SHUTO_CONFIG": str(self.config),
            "PYTHONIOENCODING": "utf-8",
            "HOME": str(home),
            "USERPROFILE": str(home),
            "APPDATA": str(home / "AppData" / "Roaming"),
            "LOCALAPPDATA": str(home / "AppData" / "Local"),
            "XDG_CONFIG_HOME": str(home / ".config"),
            "XDG_DATA_HOME": str(home / ".local" / "share"),
            "XDG_CACHE_HOME": str(home / ".cache"),
            "PATH": str(self.bin_dir) + os.pathsep + os.environ.get("PATH", ""),
        }
        for name in ("SHUTO_FTPS_USER", "SHUTO_FTPS_PASSWORD", "OLLAMA_MODELS"):
            self.app_env.pop(name, None)
        home.mkdir(parents=True, exist_ok=True)
        self.config.write_text(
            f"workspace: {json.dumps(str(self.workspace))}\nllm:\n  provider: fake\n",
            encoding="utf-8",
        )

    @property
    def shuto(self) -> Path:
        return self.bin_dir / ("shuto.exe" if WINDOWS else "shuto")

    @property
    def tool_python(self) -> Path:
        sub = "Scripts/python.exe" if WINDOWS else "bin/python"
        return self.tool_dir / "shuto" / sub

    def cli(self, *args, check=True) -> subprocess.CompletedProcess:
        return run([self.shuto, *args], env=self.app_env, check=check, timeout=300)


def build_wheel(out_dir: Path) -> Path:
    step("build the wheel from the checkout")
    run(["uv", "build", "--wheel", "--out-dir", out_dir, REPO])
    wheels = sorted(out_dir.glob("shuto-*.whl"))
    if not wheels:
        raise SmokeError(f"no wheel was built in {out_dir}")
    ok(wheels[-1].name)
    return wheels[-1]


def check_wheel_contents(wheel: Path) -> None:
    step("wheel contents")
    names = zipfile.ZipFile(wheel).namelist()

    def has(suffix: str) -> bool:
        return any(n.endswith(suffix) for n in names)

    for needed in ("shuto/data/models.yaml", "shuto/web/templates/settings.html"):
        if not has(needed):
            raise SmokeError(f"{needed} is missing from {wheel.name}")
    if not any(n.startswith("shuto/web/static/") for n in names):
        raise SmokeError("shuto/web/static/ is missing from the wheel")
    helper = [
        n for n in names if n.endswith(("shuto/_update_helper.py", "shuto/update/_helper.py"))
    ]
    if not helper:
        raise SmokeError("the update helper script is missing from the wheel")
    ok(f"data, templates, static and {helper[0]} are in the wheel")


def install(box: Sandbox, wheel: Path, *, force: bool = False) -> None:
    cmd = ["uv", "tool", "install", *(["--force"] if force else []), wheel]
    run(cmd, env=box.uv_env)
    if not box.shuto.is_file():
        raise SmokeError(f"{box.shuto} was not created")
    check_installed_files(box, wheel)


def check_installed_files(box: Sandbox, wheel: Path) -> None:
    """The installed package is this wheel (not a cached build of the same version number)."""
    out = run(
        [box.tool_python, "-I", "-c", "import shuto; print(shuto.__file__)"], env=box.app_env
    ).stdout.strip()
    package = Path(out).parent
    archive = zipfile.ZipFile(wheel)
    checked = 0
    for name in archive.namelist():
        if name.startswith("shuto/") and name.endswith(".py"):
            installed = package / name.removeprefix("shuto/")
            if not installed.is_file() or installed.read_bytes() != archive.read(name):
                raise SmokeError(f"installed {name} differs from {wheel.name}")
            checked += 1
    ok(f"{checked} installed modules match {wheel.name}")


def check_version(box: Sandbox, expect: str | None = None) -> str:
    out = box.cli("--version").stdout.strip()
    if not re.fullmatch(r"shuto \d+\.\d+\.\d+\S*", out):
        raise SmokeError(f"unexpected --version output: {out!r}")
    if expect and out != f"shuto {expect}":
        raise SmokeError(f"--version says {out!r}, expected shuto {expect}")
    ok(out)
    return out


def get(port: int, path: str) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=20) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")


def stop(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    if WINDOWS:
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True)
    else:
        proc.terminate()
    try:
        proc.wait(timeout=20)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=20)


class Server:
    """`shuto ui --no-browser` on a free port, stopped on exit."""

    def __init__(self, box: Sandbox):
        self.box = box
        self.port = free_port()
        self.log = box.root / f"ui-{self.port}.log"

    def __enter__(self) -> Server:
        self.fh = self.log.open("w", encoding="utf-8")
        self.proc = subprocess.Popen(
            [str(self.box.shuto), "ui", "--no-browser", "--port", str(self.port)],
            env=self.box.app_env,
            stdout=self.fh,
            stderr=subprocess.STDOUT,
        )
        deadline = time.time() + 90
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise SmokeError(f"shuto ui exited early:\n{self.log.read_text(errors='replace')}")
            try:
                if get(self.port, "/healthz")[0] == 200:
                    return self
            except OSError:
                time.sleep(0.5)
        raise SmokeError(
            f"shuto ui did not answer in time:\n{self.log.read_text(errors='replace')}"
        )

    def __exit__(self, *exc) -> None:
        stop(self.proc)
        self.fh.close()

    def expect_ok(self, path: str) -> str:
        status, body = get(self.port, path)
        if status != 200:
            raise SmokeError(f"GET {path} -> {status}\n{body[:500]}")
        ok(f"GET {path} -> 200")
        return body


def serve_pages(box: Sandbox, extra: tuple[str, ...] = ()) -> dict[str, str]:
    step("start the web app and fetch pages")
    bodies = {}
    with Server(box) as server:
        for path in (*PAGES, *extra):
            bodies[path] = server.expect_ok(path)
    ok("server stopped")
    return bodies


# --- upgrade simulation ----------------------------------------------------------------------


def download_release(version: str, out_dir: Path) -> Path:
    step(f"download shuto {version} from GitHub")
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = ["gh", "release", "download", f"v{version}", "-R", GITHUB_REPO, "-p", "*.whl",
           "-D", out_dir, "--clobber"]  # fmt: skip
    for attempt in (1, 2, 3):
        try:
            run(cmd)
            break
        except SmokeError:
            if attempt == 3:
                raise
            time.sleep(3 * attempt)
    wheels = sorted(out_dir.glob("shuto-*.whl"))
    if not wheels:
        raise SmokeError(f"release v{version} has no wheel asset")
    ok(wheels[0].name)
    return wheels[0]


def report_rows(box: Sandbox, batch: str, name: str) -> tuple[list[str], list[dict[str, str]]]:
    path = box.root / name
    box.cli("report", batch, "--out", path)
    with path.open(encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(io.StringIO(fh.read()))
        return list(reader.fieldnames or []), list(reader)


def stable(header: list[str]) -> list[str]:
    """Report columns that must not change over an upgrade (times are not stable)."""
    return [h for h in header if not h.endswith("_at") and h != "ai_seconds"]


def upgrade(box: Sandbox, old_version: str, new_wheel: Path, new_version: str | None) -> None:
    old_wheel = download_release(old_version, box.root / "old-wheel")
    step(f"install shuto {old_version}")
    install(box, old_wheel)
    check_version(box, old_version)

    step("make a workspace with the old version")
    photos_script = box.root / "make_photos.py"
    photos_script.write_text(PHOTOS_SCRIPT, encoding="utf-8")
    run([box.tool_python, photos_script, box.root / "photos"], env=box.app_env)
    box.cli("run", box.root / "photos", "--yes")
    state_script = box.root / "make_state.py"
    state_script.write_text(STATE_SCRIPT, encoding="utf-8")
    proc = run(
        [box.tool_python, state_script, box.config, json.dumps(SETTINGS_TO_SAVE)],
        env=box.app_env,
    )
    state = json.loads(proc.stdout.strip().splitlines()[-1])
    if not state["batches"]:
        raise SmokeError("the old version imported no photos")
    batch = state["batches"][0]
    ok(f"batch {batch}; settings saved {sorted(state['saved'])}; not in this version "
       f"{state['skipped']}")  # fmt: skip
    box.cli("approve", batch)
    before_header, before = report_rows(box, batch, "report-before.csv")
    if not before:
        raise SmokeError("the report of the old workspace is empty")
    ok(f"report before: {len(before)} rows, {len(before_header)} columns")

    step(f"install the new wheel over shuto {old_version} (uv tool install --force)")
    install(box, new_wheel, force=True)
    check_version(box, new_version)

    bodies = serve_pages(box, (f"/batch/{batch}/review",))
    settings = bodies["/settings"]
    if "max_tries" in state["saved"] and not re.search(r'<option value="3"\s+selected', settings):
        raise SmokeError("/settings does not show the saved max tries (3)")
    if "language" in state["saved"]:
        if 'value="Japanese"' not in settings:
            raise SmokeError("/settings does not show the saved language (Japanese)")
        if not re.search(r'name="english_only" value="no"\s+checked', settings):
            raise SmokeError("/settings does not show 'Another language' as chosen")
    ok("/settings shows the saved tries and language")

    step("compare the report before and after")
    after_header, after = report_rows(box, batch, "report-after.csv")
    common = [h for h in stable(before_header) if h in after_header]
    for column in ("file", "batch", "sha256", "title", "status"):
        if column not in common:
            raise SmokeError(f"report column {column!r} is gone after the upgrade")
    if len(before) != len(after):
        raise SmokeError(f"report rows: {len(before)} before, {len(after)} after")
    for old_row, new_row in zip(before, after, strict=True):
        diff = {c: (old_row[c], new_row[c]) for c in common if old_row[c] != new_row[c]}
        if diff:
            raise SmokeError(f"report row {old_row['file']} changed: {diff}")
    added = [h for h in after_header if h not in before_header]
    ok(f"{len(common)} stable columns identical; new columns {added}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--from-wheel", type=Path, help="use this wheel instead of building")
    parser.add_argument("--upgrade-from", metavar="VERSION", help="e.g. 0.5.0 (a GitHub release)")
    parser.add_argument("--keep", action="store_true", help="keep the temp folder")
    args = parser.parse_args(argv)

    root = Path(tempfile.mkdtemp(prefix="shuto-smoke-"))
    print(f"temp folder: {root}")
    box = Sandbox(root)
    try:
        if args.from_wheel:
            wheel = args.from_wheel.resolve()
        else:
            wheel = build_wheel(root / "dist")
        check_wheel_contents(wheel)
        match = re.match(r"shuto-(\d[^-]*)-", wheel.name)
        version = match.group(1) if match else None
        if args.upgrade_from:
            upgrade(box, args.upgrade_from, wheel, version)
        else:
            step("install the wheel")
            install(box, wheel)
            check_version(box, version)
            serve_pages(box)
    except (SmokeError, subprocess.TimeoutExpired, OSError) as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 1
    finally:
        if args.keep:
            print(f"kept: {root}")
        else:
            shutil.rmtree(root, ignore_errors=True)
    print("SMOKE OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
