"""Desktop launchers and the "is shuto already running?" probe.

Shortcuts only ever start ``shuto ui``. Nothing here needs admin rights.
"""

from __future__ import annotations

import http.client
import os
import shutil
import subprocess
import sys
from importlib import resources
from pathlib import Path

APP_NAME = "shuto"
COMMENT = "Prepare and upload photos for Shutterstock"


def is_running(host: str, port: int, timeout: float = 0.5) -> bool:
    """True when a shuto web UI answers on host:port (GET /healthz starts with "shuto")."""
    try:
        conn = http.client.HTTPConnection(host, port, timeout=timeout)
        try:
            conn.request("GET", "/healthz", headers={"Host": f"{host}:{port}"})
            resp = conn.getresponse()
            body = resp.read(64)
        finally:
            conn.close()
    except (OSError, http.client.HTTPException):
        return False
    return resp.status == 200 and body.lstrip().lower().startswith(b"shuto")


def shuto_command() -> list[str]:
    """Absolute command that starts shuto."""
    exe = shutil.which("shuto")
    if exe:
        return [str(Path(exe).resolve())]
    return [sys.executable, "-m", "shuto"]


def icon_path(suffix: str) -> Path | None:
    try:
        path = Path(str(resources.files("shuto.data").joinpath(f"shuto.{suffix}")))
    except (ModuleNotFoundError, TypeError):
        return None
    return path if path.is_file() else None


def _windows_known_folder(csidl: int) -> Path | None:
    """The real folder for a CSIDL (Desktop may be redirected to OneDrive), or None."""
    try:
        import ctypes

        buf = ctypes.create_unicode_buffer(260)
        # flag 0 = current location (not the default), so redirected folders are honoured
        if ctypes.windll.shell32.SHGetFolderPathW(None, csidl, None, 0, buf) == 0 and buf.value:
            return Path(buf.value)
    except (AttributeError, OSError):
        pass
    return None


_CSIDL_PROGRAMS = 0x02
_CSIDL_DESKTOPDIRECTORY = 0x10


def _desktop_dir() -> Path:
    if sys.platform == "win32":
        found = _windows_known_folder(_CSIDL_DESKTOPDIRECTORY)
        if found:
            return found
    return Path.home() / "Desktop"


def _windows_dirs() -> tuple[Path, Path]:
    menu = _windows_known_folder(_CSIDL_PROGRAMS)
    if menu is None:
        appdata = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
        menu = appdata / "Microsoft" / "Windows" / "Start Menu" / "Programs"
    return _desktop_dir(), menu


def _linux_menu_dir() -> Path:
    return Path.home() / ".local" / "share" / "applications"


def _quote_desktop(arg: str) -> str:
    escaped = arg.replace("\\", "\\\\").replace('"', '\\"')
    escaped = escaped.replace("`", "\\`").replace("$", "\\$")
    return '"' + escaped + '"'


# Constant script; the values arrive as SHUTO_* environment variables, never as script text.
_LNK_SCRIPT = (
    "$s = (New-Object -ComObject WScript.Shell).CreateShortcut($env:SHUTO_LNK);"
    "$s.TargetPath = $env:SHUTO_TARGET; $s.Arguments = $env:SHUTO_ARGS;"
    "$s.IconLocation = $env:SHUTO_ICON; $s.Description = $env:SHUTO_DESC;"
    "$s.WorkingDirectory = $env:SHUTO_WD; $s.Save()"
)


def windows_lnk_command(
    lnk: Path, target_cmd: list[str], icon: Path | None
) -> tuple[list[str], dict[str, str]]:
    """PowerShell command plus the environment that carries every value as data."""
    env = {
        **os.environ,
        "SHUTO_LNK": str(lnk),
        "SHUTO_TARGET": target_cmd[0],
        "SHUTO_ARGS": subprocess.list2cmdline(target_cmd[1:]),
        "SHUTO_ICON": str(icon) if icon else target_cmd[0],
        "SHUTO_DESC": COMMENT,
        "SHUTO_WD": str(Path.home()),
    }
    cmd = ["powershell", "-NoProfile", "-NonInteractive", "-Command", _LNK_SCRIPT]
    return cmd, env


def _create_windows(cmd: list[str], desktop: bool, menu: bool) -> list[Path]:
    desk_dir, menu_dir = _windows_dirs()
    icon = icon_path("ico")
    made: list[Path] = []
    for wanted, folder in ((desktop, desk_dir), (menu, menu_dir)):
        if not wanted:
            continue
        folder.mkdir(parents=True, exist_ok=True)
        lnk = folder / f"{APP_NAME}.lnk"
        ps_cmd, env = windows_lnk_command(lnk, [*cmd, "ui"], icon)
        subprocess.run(ps_cmd, check=True, capture_output=True, timeout=30, env=env)
        made.append(lnk)
    return made


def _create_macos(cmd: list[str], desktop: bool, menu: bool) -> list[Path]:
    folders = []
    if desktop:
        folders.append(_desktop_dir())
    if menu:
        folders.append(Path.home() / "Applications")
    quoted = " ".join(_quote_desktop(c) for c in cmd)
    made: list[Path] = []
    for folder in folders:
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{APP_NAME}.command"
        path.write_text(f"#!/bin/sh\nexec {quoted} ui\n", encoding="utf-8")
        path.chmod(0o755)
        made.append(path)
    return made


def _create_linux(cmd: list[str], desktop: bool, menu: bool) -> list[Path]:
    icon = icon_path("png")
    lines = [
        "[Desktop Entry]",
        "Type=Application",
        f"Name={APP_NAME}",
        f"Comment={COMMENT}",
        "Exec=" + " ".join(_quote_desktop(c) for c in [*cmd, "ui"]),
        "Terminal=false",
        "Categories=Graphics;",
    ]
    if icon:
        lines.append(f"Icon={icon}")
    text = "\n".join(lines) + "\n"
    folders = []
    if menu:
        folders.append(_linux_menu_dir())
    if desktop:
        folders.append(_desktop_dir())
    made: list[Path] = []
    for folder in folders:
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{APP_NAME}.desktop"
        path.write_text(text, encoding="utf-8")
        path.chmod(0o755)
        made.append(path)
    return made


def create_shortcuts(
    target_cmd: list[str] | None = None, *, desktop: bool = True, menu: bool = True
) -> list[Path]:
    """Create launchers that run ``<target_cmd> ui``. Returns the files written."""
    cmd = target_cmd or shuto_command()
    if sys.platform == "win32":
        return _create_windows(cmd, desktop, menu)
    if sys.platform == "darwin":
        return _create_macos(cmd, desktop, menu)
    return _create_linux(cmd, desktop, menu)


def _shortcut_paths() -> list[Path]:
    if sys.platform == "win32":
        desk, menu = _windows_dirs()
        return [desk / f"{APP_NAME}.lnk", menu / f"{APP_NAME}.lnk"]
    if sys.platform == "darwin":
        return [
            _desktop_dir() / f"{APP_NAME}.command",
            Path.home() / "Applications" / f"{APP_NAME}.command",
        ]
    return [
        _linux_menu_dir() / f"{APP_NAME}.desktop",
        _desktop_dir() / f"{APP_NAME}.desktop",
    ]


def list_shortcuts() -> list[Path]:
    """Launchers that currently exist."""
    return [p for p in _shortcut_paths() if p.is_file()]


def remove_shortcuts() -> list[Path]:
    """Delete launchers created by create_shortcuts. Returns the files removed."""
    removed: list[Path] = []
    for path in _shortcut_paths():
        if path.is_file():
            path.unlink()
            removed.append(path)
    return removed
