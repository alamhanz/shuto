from __future__ import annotations

import json
import subprocess
import sys
import threading
import urllib.error
from pathlib import Path

import pytest

from shuto import pipeline, updates
from shuto.updates import Latest

REAL_DIGEST = updates.fetch_wheel_digest
REAL_FETCH = updates.fetch_latest  # the autouse fixture replaces the module attribute

HASH = "ab" * 32
TAG = "https://github.com/alamhanz/shuto/releases/tag/v0.3.0"


class FakeResp:
    def __init__(self, url, code=200):
        self.url, self.code = url, code

    def geturl(self):
        return self.url

    def getcode(self):
        return self.code

    def close(self):
        pass


class FakeOpener:
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def open(self, req, timeout=None):
        self.calls.append((req.get_method(), req.full_url))
        result = self.routes[req.full_url]
        if isinstance(result, Exception):
            raise result
        return result


class Runner:
    def __init__(self, busy=False):
        self._busy = busy

    def busy(self):
        return self._busy


def test_version_patterns():
    for ok in ("0.3.0", "10.20.30"):
        assert updates.VERSION_RE.fullmatch(ok)
    for bad in ("v0.3.0", "0.3.0rc1", "../x", "0.3.0;rm", "0.3", "0.3.0\n.1", "0.3.0\n", ""):
        assert not updates.VERSION_RE.fullmatch(bad), bad
    assert updates.TAG_URL_RE.fullmatch(TAG)
    for bad in (
        TAG.replace("https", "http"),
        TAG.replace("alamhanz", "evil"),
        TAG.replace("github.com", "github.com.evil.io"),
        TAG + "rc1",
        TAG + "/../x",
        TAG + "\n",
    ):
        assert not updates.TAG_URL_RE.fullmatch(bad), bad
    with pytest.raises(ValueError):
        updates.wheel_url("0.3.0rc1")
    assert updates.wheel_url("0.3.0").endswith("/download/v0.3.0/shuto-0.3.0-py3-none-any.whl")


def test_is_newer():
    assert updates.is_newer("0.10.0", "0.9.9")
    assert not updates.is_newer("0.3.0", "0.3.0")
    assert not updates.is_newer("0.3.0", "dev")
    assert not updates.is_newer("0.3.0", "0.4.0rc1")


def routes(final=TAG, wheel_ok=True):
    wheel = updates.wheel_url("0.3.0")
    return {
        updates.LATEST_PAGE: FakeResp(final),
        wheel: FakeResp(wheel) if wheel_ok else OSError("404"),
    }


def test_fetch_latest_ok():
    opener = FakeOpener(routes())
    found = REAL_FETCH(opener)
    assert found and found.version == "0.3.0" and found.notes_url == TAG
    assert [m for m, _ in opener.calls] == ["HEAD", "HEAD"]


def test_fetch_latest_refuses_odd_answers():
    assert REAL_FETCH(FakeOpener(routes(final="https://evil.example/releases/tag/v0.3.0"))) is None
    assert REAL_FETCH(FakeOpener(routes(final=TAG.replace("v0.3.0", "v0.3.0rc1")))) is None
    assert REAL_FETCH(FakeOpener(routes(wheel_ok=False))) is None
    assert REAL_FETCH(FakeOpener({updates.LATEST_PAGE: TimeoutError("slow")})) is None


def test_cached_latest_ttl_force_and_failures(store):
    clock = {"t": 1000.0}
    calls = []

    def fetch():
        calls.append(1)
        return Latest("0.3.0", updates.wheel_url("0.3.0"), TAG, clock["t"])

    kw = {"now": lambda: clock["t"], "fetch": fetch}
    assert updates.cached_latest(store, **kw).version == "0.3.0"
    assert updates.cached_latest(store, **kw).version == "0.3.0"
    assert len(calls) == 1
    updates.cached_latest(store, force=True, **kw)
    assert len(calls) == 2
    clock["t"] += updates.CHECK_TTL_S + 1
    updates.cached_latest(store, **kw)
    assert len(calls) == 3
    # a failed check is cached too, so offline means one try a day
    clock["t"] += updates.CHECK_TTL_S + 1
    assert updates.cached_latest(store, now=kw["now"], fetch=lambda: None) is None
    assert updates.last_check_failed(store)
    assert updates.cached_latest(store, now=kw["now"], fetch=fetch) is None
    assert len(calls) == 3


def test_cached_latest_fetches_once_for_concurrent_callers(store):
    calls = []
    gate = threading.Event()

    def fetch():
        calls.append(1)
        gate.wait(0.2)
        return Latest("0.3.0", updates.wheel_url("0.3.0"), TAG, 0.0)

    threads = [
        threading.Thread(target=updates.cached_latest, args=(store,), kwargs={"fetch": fetch})
        for _ in range(4)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(calls) == 1


def test_bad_cache_is_ignored(store):
    store.set_setting("ui.update_cache", {"version": "../x", "checked_at": 1.0, "ok": True})
    assert updates.peek_cached(store) is None
    with pytest.raises(pipeline.PipelineError):
        pipeline.set_ui_setting(store, "update_cache", {"version": 1})


def test_install_kind(tmp_path, monkeypatch):
    class Dist:
        def __init__(self, direct):
            self.direct = direct

        def read_text(self, name):
            return self.direct

    monkeypatch.setattr(sys, "prefix", str(tmp_path))
    monkeypatch.setattr(updates.metadata, "distribution", lambda n: Dist(None))
    assert updates.install_kind() == "other"
    (tmp_path / "uv-receipt.toml").write_text("x")
    assert updates.install_kind() == "uv-tool"
    monkeypatch.setattr(
        updates.metadata, "distribution", lambda n: Dist('{"dir_info": {"editable": true}}')
    )
    assert updates.install_kind() == "editable"

    def missing(name):
        raise updates.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(updates.metadata, "distribution", missing)
    assert updates.install_kind() == "dev"


def test_find_uv(tmp_path, monkeypatch):
    monkeypatch.setattr(updates.shutil, "which", lambda n: "/x/uv")
    assert updates.find_uv() == "/x/uv"
    monkeypatch.setattr(updates.shutil, "which", lambda n: None)
    monkeypatch.setattr(updates.Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(sys, "platform", "linux")
    assert updates.find_uv() is None
    (tmp_path / ".local" / "bin").mkdir(parents=True)
    (tmp_path / ".local" / "bin" / "uv").write_text("x")
    assert updates.find_uv() == str(tmp_path / ".local" / "bin" / "uv")


def test_eligibility_reasons_in_order(monkeypatch):
    ok = updates.eligibility(Runner(), client_host="127.0.0.1", kind="uv-tool", uv="uv")
    assert ok.ok and ok.reason is None
    assert "git" in updates.eligibility(Runner(), client_host="::1", kind="dev", uv="uv").reason
    other = updates.eligibility(Runner(), client_host="127.0.0.1", kind="other", uv="uv")
    assert "not installed with the installer" in other.reason
    monkeypatch.setattr(updates, "find_uv", lambda: None)
    assert "uv" in updates.eligibility(Runner(), client_host="::1", kind="uv-tool").reason
    remote = updates.eligibility(Runner(), client_host="10.0.0.5", kind="uv-tool", uv="uv")
    assert "this computer" in remote.reason
    busy = updates.eligibility(Runner(True), client_host="127.0.0.1", kind="uv-tool", uv="uv")
    assert "current task" in busy.reason


def cmd_kwargs(**over):
    base = {
        "python": ["py", "-I"],
        "script": Path("/tmp/my dir/_update_helper.py"),
        "pids": [42, 43],
        "uv": "C:/Program Files/uv/uv.exe",
        "spec": updates.pip_spec("0.3.0", HASH),
        "rollback_spec": updates.pip_spec("0.2.1", HASH),
        "version": "0.3.0",
        "from_version": "0.2.1",
        "status": Path("/w s/status.json"),
        "log": Path('/w s/up"date.log'),
        "relaunch": ["/bin dir/shuto", "ui", "--no-browser"],
    }
    return {**base, **over}


def test_build_helper_cmd_is_an_argument_list():
    cmd = updates.build_helper_cmd(**cmd_kwargs())
    assert isinstance(cmd, list) and all(isinstance(c, str) for c in cmd)
    assert cmd[:2] == ["py", "-I"]
    assert [cmd[i + 1] for i, c in enumerate(cmd) if c == "--pid"] == ["42", "43"]
    assert cmd[cmd.index("--uv") + 1] == "C:/Program Files/uv/uv.exe"
    assert cmd[cmd.index("--log") + 1] == str(Path('/w s/up"date.log'))
    assert cmd[cmd.index("--relaunch") + 1 :] == ["/bin dir/shuto", "ui", "--no-browser"]
    no_rollback = updates.build_helper_cmd(**cmd_kwargs(rollback_spec=None))
    assert "--rollback-spec" not in no_rollback


def test_build_helper_cmd_rejects_foreign_sources():
    with pytest.raises(ValueError):
        updates.build_helper_cmd(**cmd_kwargs(spec="shuto[heic] @ https://evil.example/x.whl"))
    with pytest.raises(ValueError):
        updates.build_helper_cmd(**cmd_kwargs(rollback_spec="shuto @ file:///x.whl"))
    good = cmd_kwargs()["spec"]
    for bad in (good.split("#")[0], good[:-1], good[:-1] + "G", good.replace("sha256", "md5")):
        with pytest.raises(ValueError):
            updates.build_helper_cmd(**cmd_kwargs(spec=bad))
    with pytest.raises(ValueError):
        updates.build_helper_cmd(**cmd_kwargs(rollback_spec=good.split("#")[0]))
    with pytest.raises(ValueError):
        updates.build_helper_cmd(**cmd_kwargs(spec=good + "\n"))
    with pytest.raises(ValueError):
        updates.build_helper_cmd(**cmd_kwargs(spec=good.replace("shuto-0.3.0", "shuto-0.3.1")))


def test_helper_python(tmp_path, monkeypatch):
    base = tmp_path / "base" / "python.exe"
    base.parent.mkdir()
    base.write_text("x")
    tool = tmp_path / "tool"
    tool.mkdir()
    monkeypatch.setattr(sys, "prefix", str(tool))
    monkeypatch.setattr(sys, "_base_executable", str(base), raising=False)
    assert updates.helper_python("uv") == [str(base), "-I"]
    inside = tool / "python.exe"
    inside.write_text("x")
    monkeypatch.setattr(sys, "_base_executable", str(inside), raising=False)
    assert updates.helper_python("uv")[:2] == ["uv", "run"]
    monkeypatch.setattr(sys, "_base_executable", str(tmp_path / "gone"), raising=False)
    assert updates.helper_python("uv")[-3:] == ["--", "python", "-I"]


def test_relaunch_cmd_macos_is_headless_even_with_a_command_file(tmp_path, monkeypatch):
    monkeypatch.setattr(updates.Path, "home", staticmethod(lambda: tmp_path))
    (tmp_path / "Desktop").mkdir()
    (tmp_path / "Desktop" / "shuto.command").write_text("x")
    shim = tmp_path / "bin" / "shuto"
    shim.parent.mkdir()
    shim.write_text("x")
    monkeypatch.setattr(sys, "argv", [str(shim)])
    cmd = updates.relaunch_cmd("macos")
    assert cmd == [str(shim.resolve()), "ui", "--no-browser"]


def test_relaunch_cmd_keeps_the_ui_options(tmp_path, monkeypatch):
    shim = tmp_path / "bin dir" / "shuto"
    shim.parent.mkdir()
    shim.write_text("x")
    monkeypatch.setattr(sys, "argv", [str(shim)])
    opts = ["ui", "--host", "127.0.0.1", "--port", "9000", "--config", "C:/my cfg/s.yaml", "-v"]
    assert updates.relaunch_cmd("linux", None, opts) == [str(shim.resolve()), *opts, "--no-browser"]


def test_relaunch_cmd_uses_the_shim_or_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(updates.Path, "home", staticmethod(lambda: tmp_path))
    shim = tmp_path / "bin" / "shuto"
    shim.parent.mkdir()
    shim.write_text("x")
    monkeypatch.setattr(sys, "argv", [str(shim)])
    assert updates.relaunch_cmd("linux") == [str(shim.resolve()), "ui", "--no-browser"]
    monkeypatch.setattr(sys, "argv", ["-m"])
    monkeypatch.setattr(updates.shutil, "which", lambda n: None)
    with pytest.raises(updates.UpdateError):
        updates.relaunch_cmd("linux")


class FakePopen:
    calls: list = []
    fail_breakaway = False

    def __init__(self, cmd, **kw):
        FakePopen.calls.append((cmd, kw))
        if FakePopen.fail_breakaway and kw.get("creationflags", 0) & 0x01000000:
            raise OSError("access denied")


def test_spawn_detached_windows_flags_and_breakaway_retry(monkeypatch):
    FakePopen.calls, FakePopen.fail_breakaway = [], True
    monkeypatch.setattr(subprocess, "Popen", FakePopen)
    monkeypatch.setattr(sys, "platform", "win32")
    updates.spawn_detached(["a", "b c"])
    first, second = FakePopen.calls
    assert first[0] == ["a", "b c"] and "shell" not in first[1]
    assert first[1]["creationflags"] == 0x01000000 | 0x200 | 0x08000000
    assert second[1]["creationflags"] == 0x200 | 0x08000000


def test_spawn_detached_posix_and_errors(monkeypatch):
    FakePopen.calls, FakePopen.fail_breakaway = [], False
    monkeypatch.setattr(subprocess, "Popen", FakePopen)
    monkeypatch.setattr(sys, "platform", "linux")
    updates.spawn_detached(["x"])
    assert FakePopen.calls[0][1]["start_new_session"] is True

    def boom(*a, **k):
        raise OSError("nope")

    monkeypatch.setattr(subprocess, "Popen", boom)
    with pytest.raises(updates.UpdateError):
        updates.spawn_detached(["x"])


def fake_start_env(monkeypatch, current):
    monkeypatch.setattr(updates, "current_version", lambda: current)
    monkeypatch.setattr(
        updates,
        "relaunch_cmd",
        lambda os_name, uv=None, ui_args=None: ["shuto", *(ui_args or ["ui"])],
    )
    monkeypatch.setattr(updates, "helper_python", lambda uv: ["py", "-I"])


def drop_helper_copy(cmd):
    helper = Path(cmd[2])
    helper.unlink()
    helper.parent.rmdir()


def test_start_update_writes_status_and_spawns_once(cfg, monkeypatch):
    fake_start_env(monkeypatch, "0.2.1")
    spawned = []
    latest = Latest("0.3.0", updates.wheel_url("0.3.0"), TAG, 0.0)
    updates.start_update(
        cfg,
        latest,
        uv="uv",
        pids=[7, 8],
        ui_args=["ui", "--port", "9"],
        spawn=spawned.append,
        fetch_digest=lambda v: HASH,
    )
    (cmd,) = spawned
    assert cmd[cmd.index("--spec") + 1] == updates.pip_spec("0.3.0", HASH)
    assert [cmd[i + 1] for i, c in enumerate(cmd) if c == "--pid"] == ["7", "8"]
    assert cmd[cmd.index("--relaunch") + 1 :] == ["shuto", "ui", "--port", "9"]
    assert cmd[cmd.index("--rollback-spec") + 1].endswith(
        f"v0.2.1/shuto-0.2.1-py3-none-any.whl#sha256={HASH}"
    )
    assert Path(cmd[2]).name == "_update_helper.py" and Path(cmd[2]).is_file()
    status = updates.read_status(cfg)
    assert status["state"] == "started" and status["to"] == "0.3.0" and status["from"] == "0.2.1"
    updates.mark_status_seen(cfg)
    assert updates.read_status(cfg)["seen"] is True
    drop_helper_copy(cmd)


def test_start_update_without_a_release_version_has_no_rollback(cfg, monkeypatch):
    fake_start_env(monkeypatch, "dev")
    spawned = []
    latest = Latest("0.3.0", updates.wheel_url("0.3.0"), TAG, 0.0)
    updates.start_update(cfg, latest, uv="uv", spawn=spawned.append, fetch_digest=lambda v: HASH)
    assert "--rollback-spec" not in spawned[0]
    drop_helper_copy(spawned[0])


def test_start_update_refuses_without_a_digest(cfg, monkeypatch):
    fake_start_env(monkeypatch, "0.2.1")
    spawned = []
    latest = Latest("0.3.0", updates.wheel_url("0.3.0"), TAG, 0.0)
    with pytest.raises(updates.UpdateError, match="Could not check the update download"):
        updates.start_update(cfg, latest, uv="uv", spawn=spawned.append)  # conftest: no digest
    assert spawned == [] and updates.read_status(cfg) is None


def test_start_update_without_old_digest_has_no_rollback(cfg, monkeypatch):
    fake_start_env(monkeypatch, "0.2.1")
    spawned = []

    def fetch(version):
        if version == "0.2.1":
            raise updates.UpdateError(updates.MSG_NO_DIGEST)
        return HASH

    latest = Latest("0.3.0", updates.wheel_url("0.3.0"), TAG, 0.0)
    updates.start_update(cfg, latest, uv="uv", spawn=spawned.append, fetch_digest=fetch)
    assert "--rollback-spec" not in spawned[0]
    drop_helper_copy(spawned[0])


class ApiResp:
    def __init__(self, payload):
        self.raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()

    def read(self, n=-1):
        return self.raw[:n] if n >= 0 else self.raw

    def close(self):
        pass


def release_json(version="0.3.0", **asset):
    entry = {"name": f"shuto-{version}-py3-none-any.whl", "digest": f"sha256:{HASH}", **asset}
    return {"tag_name": f"v{version}", "assets": [{"name": "shuto-0.3.0.tar.gz"}, entry]}


API_URL = "https://api.github.com/repos/alamhanz/shuto/releases/tags/v0.3.0"


def test_fetch_wheel_digest_good_and_request_headers():
    seen = {}

    class Opener:
        def open(self, req, timeout=None):
            seen.update(url=req.full_url, headers=dict(req.header_items()), timeout=timeout)
            return ApiResp(release_json())

    assert REAL_DIGEST("0.3.0", opener=Opener()) == HASH
    assert seen["url"] == API_URL and seen["timeout"] == updates.HTTP_TIMEOUT_S
    assert seen["headers"]["Accept"] == "application/vnd.github+json"
    assert seen["headers"]["User-agent"].startswith("shuto/")
    assert updates.SPEC_RE.fullmatch(updates.pip_spec("0.3.0", HASH))


@pytest.mark.parametrize(
    "result",
    [
        {"tag_name": "v0.3.0", "assets": [{"name": "shuto-0.3.0-py3-none-any.whl"}]},
        release_json(digest="sha256:abc"),
        release_json(digest="md5:" + "a" * 32),
        release_json(digest="sha256:" + "AB" * 32),
        release_json(digest=None),
        release_json(name="shuto-0.2.9-py3-none-any.whl"),  # asset name for another version
        release_json("0.3.1"),  # tag mismatch
        {"tag_name": "v0.3.0", "assets": []},
        {"tag_name": "v0.3.0", "assets": "nope"},
        ["not", "an", "object"],
        b"<html>not json</html>",
        urllib.error.HTTPError(API_URL, 403, "rate limited", {}, None),
        urllib.error.URLError("offline"),
        TimeoutError(),
    ],
)
def test_fetch_wheel_digest_refuses_bad_answers(result):
    routes = {API_URL: result if isinstance(result, Exception) else ApiResp(result)}
    with pytest.raises(updates.UpdateError, match="Could not check the update download"):
        REAL_DIGEST("0.3.0", opener=FakeOpener(routes))


def test_fetch_wheel_digest_rejects_bad_versions_before_any_request():
    opener = FakeOpener({})
    for bad in ("0.3.0rc1", "../x", "0.3.0\n", ""):
        with pytest.raises(updates.UpdateError):
            REAL_DIGEST(bad, opener=opener)
    assert opener.calls == []


def test_pip_spec_rejects_a_bad_hash():
    for bad in ("", "AB" * 32, "ab" * 31, "ab" * 32 + "\n", "../x"):
        with pytest.raises(ValueError):
            updates.pip_spec("0.3.0", bad)


def test_wait_for_helper(cfg):
    ticks = {"t": 0.0}

    def clock():
        return ticks["t"]

    def sleep(n):
        ticks["t"] += n

    updates.write_status(cfg, state="started", to="9.9.9")
    assert updates.wait_for_helper(cfg, timeout=2, sleep=sleep, clock=clock) is False

    def helper_reports(n):
        sleep(n)
        updates.write_status(cfg, state="waiting", to="9.9.9")

    ticks["t"] = 0.0
    assert updates.wait_for_helper(cfg, timeout=2, sleep=helper_reports, clock=clock) is True


def test_wheel_probe_accepts_a_redirect_without_following_it():
    import urllib.error

    wheel = updates.wheel_url("0.3.0")
    redirect = urllib.error.HTTPError(
        wheel, 302, "Found", {"Location": "https://objects.example/x"}, None
    )
    plain = FakeOpener({updates.LATEST_PAGE: FakeResp(TAG)})
    wheel_only = FakeOpener({wheel: redirect})
    found = REAL_FETCH(plain, wheel_opener=wheel_only)
    assert found and found.version == "0.3.0"
    assert wheel_only.calls == [("HEAD", wheel)]
    bad = urllib.error.HTTPError(wheel, 404, "Not Found", {}, None)
    assert REAL_FETCH(plain, wheel_opener=FakeOpener({wheel: bad})) is None


@pytest.mark.parametrize("state", ["failed", "rolled_back", "done"])
def test_wait_for_helper_surfaces_the_helpers_own_failure(cfg, state):
    updates.write_status(cfg, state=state, to="9.9.9", message="It gave up.")
    with pytest.raises(updates.UpdateError, match="It gave up"):
        updates.wait_for_helper(cfg, timeout=1, sleep=lambda n: None)


def test_wait_for_helper_accepts_installing(cfg):
    updates.write_status(cfg, state="installing", to="9.9.9", message="")
    assert updates.wait_for_helper(cfg, timeout=1, sleep=lambda n: None) is True
