"""Another Ollama answering than the installed one: detection and plain-language text."""

from __future__ import annotations

import subprocess

from shuto.ollama import conflict
from shuto.ollama.conflict import disk_models as real_disk_models
from shuto.ollama.conflict import installed_version as real_installed_version
from shuto.ollama.conflict import port_hint as real_port_hint


def proc(rc=0, out="", err=""):
    return subprocess.CompletedProcess([], rc, out, err)


class Server:
    def __init__(self, version="0.22.0", tags=(), url="http://127.0.0.1:11434", down=False):
        self.url, self._v, self._t, self._down = url, version, tags, down

    def version(self):
        if self._down:
            raise OSError("down")
        return self._v

    def tags(self):
        return [{"name": t} for t in self._t]


def no_disk(_path):
    return set()


def test_parse_cli_version():
    assert conflict.parse_cli_version("ollama version is 0.40.8\n") == "0.40.8"
    two = "ollama version is 0.22.0\nWarning: client version is 0.40.8\n"
    assert conflict.parse_cli_version(two) == "0.40.8"
    assert conflict.parse_cli_version("ollama version is v0.9.1-rc2") == "0.9.1"
    assert conflict.parse_cli_version("garbage") is None
    assert conflict.parse_cli_version("") is None


def test_version_comparison():
    assert conflict.is_newer("0.40.8", "0.22.0")
    assert conflict.is_newer("0.40.10", "0.40.9")
    assert not conflict.is_newer("0.9", "0.40.8")
    assert not conflict.is_newer("0.22.0", "0.22.0")
    assert not conflict.is_newer(None, "0.1") and not conflict.is_newer("0.1", "x")


def test_installed_version_cases():
    ok = proc(out="ollama version is 0.40.8")
    seen = []

    def run(cmd, timeout):
        seen.append(timeout)
        return ok

    assert real_installed_version(run, lambda n: "/bin/ollama") == "0.40.8"
    assert seen == [3]
    assert real_installed_version(lambda c, t: proc(127), lambda n: "/bin/ollama") is None


def test_installed_version_missing_cli(monkeypatch):
    monkeypatch.setattr(conflict, "_windows_exe", lambda: None)

    def never(cmd, timeout):
        raise AssertionError("must not run")

    assert real_installed_version(never, lambda n: None) is None


def test_disk_models(tmp_path):
    root = tmp_path / "models with space" / "manifests"
    for rel in (
        "registry.ollama.ai/library/gemma3/4b",
        "registry.ollama.ai/library/moondream/latest",
        "hf.co/user/repo/Q4",
        "registry.ollama.ai/someuser/model/v1",
    ):
        f = root / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("{}")
    assert real_disk_models(tmp_path / "models with space") == {
        "gemma3:4b",
        "moondream:latest",
        "hf.co/user/repo:Q4",
        "someuser/model:v1",
    }
    assert real_disk_models(tmp_path / "missing") == set()


NETSTAT = (
    "  TCP    127.0.0.1:11434    0.0.0.0:0    LISTENING    4242\n"
    "  TCP    0.0.0.0:80    0.0.0.0:0    LISTENING    4\n"
)
URL = "http://127.0.0.1:11434"


def runner(netstat, tasklist=""):
    def run(cmd, timeout):
        assert timeout == 3
        if cmd[0] == "netstat":
            return netstat
        return proc(out=tasklist)

    return run


def test_port_hint():
    assert real_port_hint(URL, "linux", runner(proc())) == ""
    quiet = proc(out="  TCP 0.0.0.0:80 0.0.0.0:0 LISTENING 4")
    assert real_port_hint(URL, "windows", runner(quiet)) == "no_windows_process"
    wsl = runner(proc(out=NETSTAT), '"wslrelay.exe","4242","Services","0","1 K"')
    assert real_port_hint(URL, "windows", wsl) == "wsl"
    dock = runner(proc(out=NETSTAT), '"com.docker.backend.exe","4242","Console","1","1 K"')
    assert real_port_hint(URL, "windows", dock) == "docker"
    other = runner(proc(out=NETSTAT), '"thing.exe","4242","Console","1","1 K"')
    assert real_port_hint(URL, "windows", other) == ""
    assert real_port_hint(URL, "windows", runner(proc(127))) == ""


def test_diagnose_older_server(tmp_path):
    d = conflict.diagnose(
        Server("0.22.0"),
        tmp_path,
        installed="0.40.8",
        hint_for=lambda u: "wsl",
        disk=no_disk,
    )
    assert d.kind == "older_server" and d.hint == "wsl"
    assert "0.22.0" in d.message and "0.40.8" in d.message and "WSL" in d.steps[0]
    assert d.message.endswith("Open Help for the steps.")
    assert "update ollama" not in d.message.lower()


def test_diagnose_no_problem_cases(tmp_path):
    def boom(url):
        raise AssertionError("port_hint must not run without a problem")

    kw = {"hint_for": boom, "disk": no_disk}
    assert conflict.diagnose(Server("0.40.8"), tmp_path, installed="0.40.8", **kw) is None
    assert conflict.diagnose(Server("0.50.0"), tmp_path, installed="0.40.8", **kw) is None
    assert conflict.diagnose(Server(down=True), tmp_path, installed="0.40.8", **kw) is None
    remote = Server("0.1", url="http://gpu-box:11434")
    assert conflict.diagnose(remote, tmp_path, installed="0.40.8", **kw) is None
    listed = Server("0.40.8", tags=["gemma3:4b"])
    same = conflict.diagnose(
        listed, tmp_path, installed="0.40.8", disk=lambda p: {"gemma3:4b"}, hint_for=boom
    )
    assert same is None


def test_unrelated_unlisted_models_do_not_alarm(tmp_path):
    kw = {"installed": "0.40.8", "hint_for": lambda u: "", "disk": lambda p: {"old:latest"}}
    server = Server("0.40.8", tags=["a:latest"])
    assert conflict.diagnose(server, tmp_path, chosen="a", **kw) is None
    assert conflict.diagnose(server, tmp_path, chosen=None, **kw) is None


def test_diagnose_unlisted_models_only(tmp_path):
    d = conflict.diagnose(
        Server("0.40.8", tags=["a:latest"]),
        tmp_path,
        installed="0.40.8",
        hint_for=lambda u: "",
        disk=lambda p: {"a:latest", "gemma3:4b"},
        chosen="gemma3:4b",
    )
    assert d.kind == "unlisted_models" and d.unlisted == ["gemma3:4b"]
    assert "may" in d.message and str(tmp_path) in d.message and "gemma3:4b" in d.message


OWNER_412 = (
    "Ollama pull failed: pull model manifest: 412: this model requires a newer version of Ollama"
)


def test_pull_412_message(tmp_path):
    assert conflict.pull_412_message("Ollama HTTP 500: boom", None, None) is None
    d = conflict.diagnose(
        Server("0.22.0"), tmp_path, installed="0.40.8", hint_for=lambda u: "", disk=no_disk
    )
    text = conflict.pull_412_message(OWNER_412, d, "0.22.0")
    assert "0.22.0" in text and "older Ollama" in text and "1)" in text
    assert "update ollama" not in text.lower()
    old = conflict.pull_412_message(OWNER_412, None, "0.3.0")
    assert "version 0.3.0" in old and "Update Ollama" in old
    assert "Update Ollama" in conflict.pull_412_message("Ollama HTTP 412: nope", None, None)
    assert conflict.pull_412_message("blob sha256:4125 size 412 MB", None, None) is None


def test_missing_installed_version_is_not_cached(monkeypatch):
    answers = iter([None, "1.0", "2.0"])
    monkeypatch.setattr(conflict, "installed_version", lambda: next(answers))
    conflict.clear_installed_cache()
    assert conflict.installed_version_cached() is None
    assert conflict.installed_version_cached() == "1.0"
    assert conflict.installed_version_cached() == "1.0"


def test_cached_installed_version_runs_once(monkeypatch):
    calls = []
    monkeypatch.setattr(conflict, "installed_version", lambda: calls.append(1) or "1.0")
    conflict.clear_installed_cache()
    assert conflict.installed_version_cached() == conflict.installed_version_cached() == "1.0"
    assert len(calls) == 1
