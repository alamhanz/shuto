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


def _ps1(name: str) -> str:
    return (INSTALL / name).read_text(encoding="utf-8")


def test_install_ps1_closes_running_shuto_before_install():
    text = _ps1("install.ps1")
    assert "function Stop-RunningShuto" in text
    assert "Win32_Process" in text and "OrdinalIgnoreCase" in text
    assert "tool dir" in text and "Close it now? [Y/n]" in text
    assert "run this line again" in text
    assert text.index("Stop-RunningShuto $uv") < text.index("tool install --force")


def test_install_ps1_reports_file_in_use_instead_of_internet():
    text = _ps1("install.ps1")
    for marker in (
        "Access is denied",
        "os error 5",
        "os error 32",
        "being used by another process",
    ):
        assert marker in text
    assert "A shuto file is in use" in text
    assert "dropped internet connection" in text


def test_uninstall_ps1_closes_running_shuto_before_removing():
    text = _ps1("uninstall.ps1")
    assert "function Stop-RunningShuto" in text
    assert text.index("Stop-RunningShuto $uv") < text.index("tool uninstall shuto")


def test_ps1_stops_descendants_and_waits_until_gone():
    for name in ("install.ps1", "uninstall.ps1"):
        text = _ps1(name)
        assert "ParentProcessId" in text
        assert "Get-ShutoProcesses" in text
        assert "Start-Sleep -Seconds 1" in text and "$i -lt 10" in text


def test_ps1_spares_ollama_guards_pid_reuse_and_own_ancestors():
    for name in ("install.ps1", "uninstall.ps1"):
        text = _ps1(name)
        assert "-like 'ollama*'" in text
        assert "CreationDate -lt $parent.CreationDate" in text
        assert "$protected" in text and "$byId[$cur].ParentProcessId" in text


def test_ps1_installs_latest_release_wheel_without_fallback():
    text = _ps1("install.ps1")
    assert "releases/latest" in text and "api.github.com" not in text and "main.zip" not in text
    assert "py3-none-any" in text and "Tls12" in text
    assert "'shuto[heic] @ ' + $url" in text and "-Method Head" in text
    assert "Installing shuto $($latest.Version)..." in text
    assert "Could not find the latest shuto release on GitHub" in text
    assert "https://github.com/alamhanz/shuto/releases" in text and "shared network" in text


def test_ps1_looks_up_release_before_uv_and_install():
    text = _ps1("install.ps1")
    lookup = text.index("$latest = Get-LatestRelease")
    assert text.index("Tls12") < text.index("Invoke-RestMethod $UvInstaller")
    assert lookup < text.index("Invoke-RestMethod $UvInstaller")
    assert lookup < text.index("tool install --force")


def test_sh_installs_latest_release_wheel_without_fallback():
    text = (INSTALL / "install.sh").read_text(encoding="utf-8")
    assert "releases/latest" in text and "api.github.com" not in text and "main.zip" not in text
    assert "py3-none-any" in text and "%{url_effective}" in text
    assert 'SOURCE="shuto[heic] @ ${LATEST#* }"' in text
    assert "Could not find the latest shuto release on GitHub" in text
    assert "https://github.com/alamhanz/shuto/releases" in text and "shared network" in text


def test_sh_looks_up_release_before_uv_and_install():
    text = (INSTALL / "install.sh").read_text(encoding="utf-8")
    lookup = text.index('LATEST="$(latest_release)"')
    assert lookup < text.index("command -v uv")
    assert lookup < text.index("uv tool install --force")


def test_uninstallers_no_longer_mention_private_repo():
    for name in ("uninstall.ps1", "uninstall.sh"):
        assert "once the repository is public" not in _ps1(name)


_TAG_URL = "https://github.com/alamhanz/shuto/releases/tag/v0.2.0"
_WHEEL_URL = (
    "https://github.com/alamhanz/shuto/releases/download/v0.2.0/shuto-0.2.0-py3-none-any.whl"
)


def _run_latest_release(tmp_path: Path, final_url: str, redirect_rc: int, head_rc: int):
    import os
    import shutil
    import subprocess
    import sys

    import pytest

    sh = shutil.which("sh")
    if sh is None or sys.platform == "win32":
        pytest.skip("needs a POSIX sh")
    # Fake curl: with -w it plays the "latest" redirect, otherwise it plays the HEAD check.
    fake = tmp_path / "curl"
    fake.write_text(
        "#!/bin/sh\n"
        'for a in "$@"; do\n'
        f'  if [ "$a" = "-w" ]; then printf %s "{final_url}"; exit {redirect_rc}; fi\n'
        "done\n"
        f"exit {head_rc}\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    text = (INSTALL / "install.sh").read_text(encoding="utf-8")
    start = text.index("latest_release() {")
    end = text.index("\n}\n", start) + 3
    script = (
        'LATEST_RELEASE_PAGE="https://example.invalid/latest"\n'
        'RELEASE_DOWNLOAD="https://github.com/alamhanz/shuto/releases/download"\n'
        + text[start:end]
        + "latest_release\n"
    )
    env = {**os.environ, "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}"}
    done = subprocess.run([sh, "-c", script], capture_output=True, text=True, env=env)
    return done.returncode, done.stdout.strip()


def test_sh_latest_release_builds_wheel_url_from_redirect(tmp_path):
    code, out = _run_latest_release(tmp_path, _TAG_URL, 0, 0)
    assert code == 0
    assert out == f"0.2.0 {_WHEEL_URL}"


def test_sh_latest_release_accepts_prerelease_version(tmp_path):
    tag = "https://github.com/alamhanz/shuto/releases/tag/v1.0.0rc1"
    code, out = _run_latest_release(tmp_path, tag, 0, 0)
    assert code == 0 and out.startswith("1.0.0rc1 ")


def test_sh_latest_release_fails_when_redirect_fails(tmp_path):
    code, out = _run_latest_release(tmp_path, "", 22, 0)
    assert code != 0 and out == ""


def test_sh_latest_release_rejects_bad_or_missing_tag(tmp_path):
    for final in (
        "https://github.com/alamhanz/shuto/releases",
        "https://github.com/alamhanz/shuto/releases/tag/main",
        "https://github.com/alamhanz/shuto/releases/tag/v1.0;rm",
    ):
        code, out = _run_latest_release(tmp_path, final, 0, 0)
        assert code != 0 and out == ""


def test_sh_latest_release_fails_when_wheel_is_missing(tmp_path):
    code, out = _run_latest_release(tmp_path, _TAG_URL, 0, 22)
    assert code != 0 and out == ""
