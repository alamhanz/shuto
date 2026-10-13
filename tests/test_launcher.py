"""Launcher: running-instance probe, shortcut files, and `shuto ui` single-instance."""

from __future__ import annotations

import http.server
import os
import socket
import subprocess
import sys
import threading
from pathlib import Path

import pytest
import yaml

from shuto import launcher
from shuto.cli import main


@pytest.fixture
def stub_server():
    servers = []

    def start(body: bytes, status: int = 200):
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        srv = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=lambda: srv.serve_forever(poll_interval=0.01), daemon=True).start()
        servers.append(srv)
        return srv.server_address[1]

    yield start
    for srv in servers:
        srv.shutdown()
        srv.server_close()


def test_is_running_true_for_shuto(stub_server):
    assert launcher.is_running("127.0.0.1", stub_server(b"shuto ok"))


def test_is_running_false_for_other_service(stub_server):
    assert not launcher.is_running("127.0.0.1", stub_server(b"hello"))
    assert not launcher.is_running("127.0.0.1", stub_server(b"shuto", status=404))


def test_is_running_false_when_nothing_listens(stub_server):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        free = s.getsockname()[1]
    assert not launcher.is_running("127.0.0.1", free)


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setenv("APPDATA", str(tmp_path / "AppData"))
    return tmp_path


def test_linux_desktop_files(fake_home, monkeypatch):
    monkeypatch.setattr(launcher.sys, "platform", "linux")
    made = launcher.create_shortcuts(["/opt/my tools/shuto"])
    names = {p.parent.name for p in made}
    assert names == {"applications", "Desktop"}
    text = made[0].read_text(encoding="utf-8")
    assert text.startswith("[Desktop Entry]")
    assert 'Exec="/opt/my tools/shuto" "ui"' in text
    assert "Icon=" in text and text.count("shuto.png") == 1
    assert set(launcher.remove_shortcuts()) == set(made)
    assert not any(p.exists() for p in made)


def test_macos_command_files(fake_home, monkeypatch):
    monkeypatch.setattr(launcher.sys, "platform", "darwin")
    made = launcher.create_shortcuts(["/usr/local/bin/shuto"], menu=False)
    assert [p.name for p in made] == ["shuto.command"]
    assert made[0].parent == fake_home / "Desktop"
    text = made[0].read_text(encoding="utf-8")
    assert text == '#!/bin/sh\nexec "/usr/local/bin/shuto" ui\n'
    if os.name != "nt":
        assert made[0].stat().st_mode & 0o111
    assert launcher.remove_shortcuts() == made


def test_no_desktop_and_no_menu_flags(fake_home, monkeypatch):
    monkeypatch.setattr(launcher.sys, "platform", "linux")
    made = launcher.create_shortcuts(["shuto"], desktop=False)
    assert [p.parent.name for p in made] == ["applications"]


def test_windows_lnk_passes_values_as_environment(fake_home, monkeypatch):
    monkeypatch.setattr(launcher.sys, "platform", "win32")
    monkeypatch.setattr(launcher, "_windows_known_folder", lambda csidl: None)
    calls = []
    monkeypatch.setattr(
        launcher.subprocess, "run", lambda cmd, **kw: calls.append((cmd, kw["env"]))
    )
    made = launcher.create_shortcuts([r"C:\Users\A B\shuto.exe"])
    assert [p.suffix for p in made] == [".lnk", ".lnk"]
    assert len(calls) == 2
    for (cmd, env), lnk in zip(calls, made, strict=True):
        assert cmd[0] == "powershell" and cmd[-1] == cmd[cmd.index("-Command") + 1]
        script = cmd[-1]
        assert "WScript.Shell" in script
        assert str(lnk) not in script and "shuto.exe" not in script
        assert not any(str(lnk) in part for part in cmd)
        assert env["SHUTO_LNK"] == str(lnk)
        assert env["SHUTO_TARGET"] == r"C:\Users\A B\shuto.exe"
        assert env["SHUTO_ARGS"] == "ui"


def test_windows_uses_real_desktop_and_programs_folders(fake_home, monkeypatch):
    monkeypatch.setattr(launcher.sys, "platform", "win32")
    desk = fake_home / "OneDrive" / "Desktop"
    programs = fake_home / "Roaming" / "Programs"
    folders = {launcher._CSIDL_DESKTOPDIRECTORY: desk, launcher._CSIDL_PROGRAMS: programs}
    monkeypatch.setattr(launcher, "_windows_known_folder", folders.get)
    monkeypatch.setattr(launcher.subprocess, "run", lambda cmd, **kw: None)
    made = launcher.create_shortcuts([r"C:\x\shuto.exe"])
    assert made == [desk / "shuto.lnk", programs / "shuto.lnk"]
    assert not (fake_home / "Desktop").exists()
    for path in made:
        path.write_text("x")
    assert launcher.list_shortcuts() == made
    assert launcher.remove_shortcuts() == made


def test_desktop_quote_escapes_dollar_and_backtick():
    assert launcher._quote_desktop("/a/$HOME/`x`") == r'"/a/\$HOME/\`x\`"'


@pytest.mark.skipif(sys.platform != "win32", reason="needs Windows PowerShell and COM")
def test_windows_lnk_really_created(tmp_path):
    target = tmp_path / "my tools" / "shuto.exe"
    target.parent.mkdir()
    target.write_bytes(b"")
    lnk = tmp_path / "out" / "shuto.lnk"
    lnk.parent.mkdir()
    cmd, env = launcher.windows_lnk_command(lnk, [str(target), "ui"], None)
    subprocess.run(cmd, check=True, capture_output=True, timeout=60, env=env)
    read = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-Command",
            "$s=(New-Object -ComObject WScript.Shell).CreateShortcut($env:SHUTO_LNK);"
            "$s.TargetPath; $s.Arguments",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ, "SHUTO_LNK": str(lnk)},
    )
    lines = read.stdout.splitlines()
    assert lines[0].strip().lower() == str(target).lower()
    assert lines[1].strip() == "ui"


def test_shuto_command_fallback(monkeypatch):
    monkeypatch.setattr(launcher.shutil, "which", lambda name: None)
    cmd = launcher.shuto_command()
    assert cmd[1:] == ["-m", "shuto"]


def test_ui_already_running_returns_0_without_serving(tmp_path, monkeypatch, capsys):
    cfg = tmp_path / "shuto.yaml"
    cfg.write_text(
        yaml.safe_dump({"workspace": str(tmp_path / "ws"), "llm": {"provider": "fake"}}),
        encoding="utf-8",
    )
    opened = []
    monkeypatch.setattr("shuto.cli.launcher.is_running", lambda host, port: True)
    monkeypatch.setattr("shuto.cli.webbrowser.open", lambda url: opened.append(url))

    def boom(*a, **kw):
        raise AssertionError("serve must not run")

    monkeypatch.setattr("shuto.web.serve", boom)
    assert main(["--config", str(cfg), "ui", "--port", "9123"]) == 0
    assert "already running" in capsys.readouterr().out
    assert opened == ["http://127.0.0.1:9123/"]

    opened.clear()
    assert main(["--config", str(cfg), "ui", "--port", "9123", "--no-browser"]) == 0
    assert opened == []


def test_shortcut_command_parses(tmp_path, monkeypatch, capsys):
    cfg = tmp_path / "shuto.yaml"
    cfg.write_text(
        yaml.safe_dump({"workspace": str(tmp_path / "ws"), "llm": {"provider": "fake"}}),
        encoding="utf-8",
    )
    seen = {}
    monkeypatch.setattr(
        "shuto.cli.launcher.create_shortcuts",
        lambda **kw: seen.update(kw) or [tmp_path / "shuto.lnk"],
    )
    assert main(["--config", str(cfg), "shortcut", "--no-menu"]) == 0
    assert seen == {"desktop": True, "menu": False}
    monkeypatch.setattr("shuto.cli.launcher.remove_shortcuts", lambda: [])
    assert main(["--config", str(cfg), "shortcut", "--remove"]) == 0
    assert "No shuto shortcuts" in capsys.readouterr().out
