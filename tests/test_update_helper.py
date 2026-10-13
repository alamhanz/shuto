from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

HELPER = Path(__file__).parent.parent / "src" / "shuto" / "_update_helper.py"


@pytest.fixture(scope="module")
def helper():
    spec = importlib.util.spec_from_file_location("update_helper_under_test", HELPER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeRun:
    """Answers `uv tool install` calls from a script of (returncode, output)."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.cmds = []

    def __call__(self, cmd, **kw):
        self.cmds.append(cmd)
        code, out = self.answers.pop(0)
        return subprocess.CompletedProcess(cmd, code, stdout=out)


class Env:
    def __init__(self, tmp_path):
        self.wheels = tmp_path / "ws" / "updates" / "wheels"
        self.wheels.mkdir(parents=True)
        self.hashes = {}
        for v in ("0.3.0", "0.2.1"):
            data = f"wheel {v}".encode()
            (self.wheels / f"shuto-{v}-py3-none-any.whl").write_bytes(data)
            self.hashes[v] = hashlib.sha256(data).hexdigest()
        self.spec = self.spec_for("0.3.0")
        self.old = self.spec_for("0.2.1")
        self.status = tmp_path / "w s" / "status.json"
        self.log = tmp_path / "w s" / "update.log"
        self.spawned = []
        self.sleeps = []
        self.waited = []

    def spec_for(self, version):
        wheel = self.wheels / f"shuto-{version}-py3-none-any.whl"
        return f"shuto[heic] @ {wheel.resolve().as_uri()}"

    def argv(self, rollback=True, sha=None, wheels=None):
        out = ["--pid", "99", "--pid", "98", "--uv", "C:/uv dir/uv.exe"]
        out += ["--wheels", str(wheels or self.wheels), "--sha256", sha or self.hashes["0.3.0"]]
        if rollback:
            out += ["--rollback-sha256", self.hashes["0.2.1"]]
        out += ["--version", "0.3.0", "--from-version", "0.2.1"]
        out += ["--status", str(self.status), "--log", str(self.log)]
        return out + ["--relaunch", "shuto", "ui", "--no-browser"]

    def go(self, helper, run, rollback=True, alive=(), **argv):
        return helper.main(
            self.argv(rollback, **argv),
            run=run,
            wait_pid=lambda pid: self.waited.append(pid) or pid not in alive,
            spawn=self.spawned.append,
            sleep=self.sleeps.append,
        )

    def state(self):
        return json.loads(self.status.read_text(encoding="utf-8"))


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


def test_success(helper, env):
    run = FakeRun((0, "installed"))
    assert env.go(helper, run) == 0
    assert env.waited == [99, 98]
    assert run.cmds == [
        ["C:/uv dir/uv.exe", "tool", "install", "--force", "--python", "3.12", env.spec]
    ]
    assert env.state()["state"] == "done" and env.state()["to"] == "0.3.0"
    assert env.spawned == [["shuto", "ui", "--no-browser"]]
    assert "installed" in env.log.read_text(encoding="utf-8")


def test_lock_errors_are_retried(helper, env):
    run = FakeRun((1, "error: Access is denied. (os error 5)"), (1, "os error 32"), (0, "ok"))
    assert env.go(helper, run) == 0
    assert len(run.cmds) == 3 and env.sleeps == [3, 3]
    assert env.state()["state"] == "done"


def test_lock_errors_give_up_and_the_old_version_still_works(helper, env):
    locked = (1, "being used by another process")
    run = FakeRun(*([locked] * 5), (0, "shuto 0.2.1"))
    assert env.go(helper, run) == 1
    assert run.cmds[-1][1:] == ["--version"]
    assert all(env.old not in c for c in run.cmds)  # no rollback reinstall needed
    state = env.state()
    assert state["state"] == "rolled_back"
    assert "file was in use" in state["message"] and "still on 0.2.1" in state["message"]
    assert env.spawned  # shuto is started again


def test_broken_install_is_rolled_back(helper, env):
    run = FakeRun((1, "boom"), (1, "not found"), (0, "reinstalled"), (0, "shuto 0.2.1"))
    assert env.go(helper, run) == 1
    assert run.cmds[2][-1] == env.old
    assert env.state()["state"] == "rolled_back"


def test_failure_with_a_working_old_version_does_not_reinstall(helper, env):
    run = FakeRun((1, "no network"), (0, "shuto 0.2.1"))
    assert env.go(helper, run) == 1
    assert len(run.cmds) == 2 and env.sleeps == []
    assert env.state()["state"] == "rolled_back"


def test_rollback_fails_too(helper, env):
    run = FakeRun((1, "boom"), (1, "broken"), (1, "no network"))
    assert env.go(helper, run) == 1
    state = env.state()
    assert state["state"] == "failed" and "install line" in state["message"]
    assert env.spawned


def test_failure_without_rollback_spec(helper, env):
    run = FakeRun((1, "boom"), (1, "broken"))
    assert env.go(helper, run, rollback=False) == 1
    assert env.state()["state"] == "failed" and len(run.cmds) == 2


def test_nothing_is_installed_while_shuto_is_still_running(helper, env):
    run = FakeRun()
    assert env.go(helper, run, alive=(99,)) == 3
    assert run.cmds == [] and env.spawned == []
    state = env.state()
    assert state["state"] == "failed" and "Nothing was changed" in state["message"]


def test_only_the_launcher_lingering_brings_shuto_back(helper, env):
    run = FakeRun()
    assert env.go(helper, run, alive=(98,)) == 3
    assert run.cmds == []  # nothing installed
    assert env.spawned == [["shuto", "ui", "--no-browser"]]
    state = env.state()
    assert state["state"] == "failed" and "opened again" in state["message"]


def test_uv_missing_counts_as_failure(helper, env):
    def run(cmd, **kw):
        raise FileNotFoundError("uv")

    assert env.go(helper, run, rollback=False) == 1
    assert env.state()["state"] == "failed"


def test_install_spec_is_a_file_uri_of_the_verified_wheel(helper, env):
    spec = helper.verified_spec(env.wheels, "0.3.0", env.hashes["0.3.0"])
    assert spec == env.spec
    assert spec.startswith("shuto[heic] @ file:///")


def test_bad_sources_are_refused_before_anything_runs(helper, env, tmp_path):
    run = FakeRun()
    good = env.hashes["0.3.0"]
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    for kw in (
        {"sha": good + chr(10)},
        {"sha": good.upper()},
        {"sha": good[:-1]},
        {"sha": "g" * 64},
        {"wheels": outside},  # not an updates/wheels folder
    ):
        assert env.go(helper, run, **kw) == 2, kw
    assert run.cmds == [] and env.state()["state"] == "failed"


def test_bad_rollback_hash_is_refused(helper, env):
    run = FakeRun()
    argv = env.argv(rollback=False)
    at = argv.index("--relaunch")
    argv[at:at] = ["--rollback-sha256", "xyz"]
    assert helper.main(argv, run=run, wait_pid=lambda pid: True) == 2
    assert run.cmds == []


def test_a_wheel_changed_after_download_is_refused_just_before_install(helper, env):
    (env.wheels / "shuto-0.3.0-py3-none-any.whl").write_bytes(b"tampered")
    run = FakeRun()
    assert env.go(helper, run) == 2
    assert run.cmds == []
    state = env.state()
    assert state["state"] == "failed" and "changed" in state["message"]
    assert env.spawned == [["shuto", "ui", "--no-browser"]]  # shuto is started again


def test_a_missing_wheel_is_refused(helper, env):
    (env.wheels / "shuto-0.3.0-py3-none-any.whl").unlink()
    run = FakeRun()
    assert env.go(helper, run) == 2 and run.cmds == []


def test_a_tampered_rollback_wheel_is_not_installed(helper, env):
    (env.wheels / "shuto-0.2.1-py3-none-any.whl").write_bytes(b"tampered")
    run = FakeRun((1, "boom"), (1, "broken"))
    assert env.go(helper, run) == 1
    assert len(run.cmds) == 2  # the failed install and the --version check; no rollback install
    assert env.state()["state"] == "failed"


def test_log_is_trimmed(helper, tmp_path):
    log = tmp_path / "u.log"
    log.write_bytes(b"x" * (helper.MAX_LOG_BYTES + 10))
    helper.log(log, "hello")
    assert log.stat().st_size < helper.MAX_LOG_BYTES
    assert log.read_text(encoding="utf-8").rstrip().endswith("hello")
