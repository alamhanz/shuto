from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INSTALL = ROOT / "install"


def test_cmd_wrapper_runs_install_ps1_and_pauses():
    text = (INSTALL / "install.cmd").read_bytes().decode("ascii")
    assert "\r\n" in text
    assert '"%~dp0install.ps1"' in text and "%*" in text
    assert "pause" in text


def test_command_wrapper_runs_install_sh():
    text = (INSTALL / "install.command").read_text(encoding="utf-8")
    assert text.startswith("#!/bin/sh")
    assert "install.sh" in text and "read " in text


def test_ps1_has_checkout_detection_and_heic_build():
    text = (INSTALL / "install.ps1").read_text(encoding="utf-8")
    assert "$PSScriptRoot" in text
    assert "pyproject.toml" in text
    assert "uv build --wheel" in text.replace("& $uv build", "uv build")
    assert "shuto[heic] @ " in text and "AbsoluteUri" in text


def test_sh_has_checkout_detection_and_heic_build():
    text = (INSTALL / "install.sh").read_text(encoding="utf-8")
    assert "pyproject.toml" in text
    assert "uv build --wheel" in text
    assert "shuto[heic] @ file://" in text


def test_gitattributes_keeps_windows_scripts_crlf():
    attrs = (ROOT / ".gitattributes").read_text(encoding="utf-8")
    for pattern in ("*.ps1", "*.cmd", "*.bat"):
        assert f"{pattern} text eol=crlf" in attrs


def test_sh_uri_encode_escapes_unsafe_bytes():
    import shutil
    import subprocess
    import sys

    sh = shutil.which("sh")
    if sh is None or sys.platform == "win32":
        import pytest

        pytest.skip("needs a POSIX sh")
    text = (INSTALL / "install.sh").read_text(encoding="utf-8")
    start = text.index("uri_encode() {")
    end = text.index("\n}\n", start) + 3
    script = text[start:end] + "uri_encode '/a b/é#%x-1.whl'"
    out = subprocess.run([sh, "-c", script], capture_output=True, text=True, check=True).stdout
    assert out == "/a%20b/%C3%A9%23%25x-1.whl"
