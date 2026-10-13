"""Regression tests for the review findings fixed in run shuto-16."""

from __future__ import annotations

import ftplib
import re
import shutil
import types
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from shuto import cli, deliver, ftps, hardware, pipeline
from shuto.collect import ScanItem, batch_id_for
from shuto.imaging import ImageInfo
from shuto.jobs import JobRunner
from shuto.llm.fake import FakeProvider
from shuto.models import Status
from shuto.web import create_app

from .conftest import FakeOllama
from .test_pipeline import _setup

BASE = "http://127.0.0.1:8765"


# ---- Y1: forced editorial through the manual edit path
def test_unticking_editorial_on_logo_photo_goes_to_review(cfg, store, photos_dir):
    batch = _setup(cfg, store, photos_dir)
    pipeline.describe_batch(cfg, store, batch, llm=FakeProvider("fake"))
    photo = store.list_photos(batch, [Status.DESCRIBED])[0]
    store.update_photo(photo.id, detected={"logos_or_brands": ["Acme"]})
    edited = pipeline.update_metadata(
        cfg,
        store,
        photo.id,
        description="A quiet street with a parked bicycle at dusk",
        editorial=False,
    )
    assert edited.metadata["editorial"] is True
    assert edited.status == Status.NEEDS_REVIEW


# ---- Y4: a saved choice beats an explicit config model
def test_saved_model_choice_beats_config_model(cfg, store):
    cfg.llm.provider = "ollama"
    cfg.llm.model = "gemma3:4b"
    assert pipeline.resolve_selection(cfg, store).model == "gemma3:4b"
    pipeline.use_model(cfg, store, "moondream")
    sel = pipeline.resolve_selection(cfg, store)
    assert (sel.model, sel.source) == ("moondream", "settings")
    assert pipeline.resolve_selection(cfg, store, model="qwen2.5vl:7b").model == "qwen2.5vl:7b"


# ---- Y5: API preflight
def test_api_preflight_reports_missing_key_and_sdk(cfg, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    sel = pipeline.Selection("gemini", "m", "flag")
    monkeypatch.setattr(pipeline, "_sdk_installed", lambda module: False)
    problems = pipeline.describe_preflight(cfg, sel)
    assert any("SDK" in p and "shuto[gemini]" in p for p in problems)
    assert any("GEMINI_API_KEY" in p for p in problems)
    monkeypatch.setattr(pipeline, "_sdk_installed", lambda module: True)
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    assert pipeline.describe_preflight(cfg, sel) == []
    assert pipeline.describe_preflight(cfg, pipeline.Selection("fake", "fake", "flag")) == []


def test_bedrock_preflight_without_credentials(cfg, monkeypatch):
    boto3 = pytest.importorskip("boto3")

    class NoCreds:
        def __init__(self, **kw):
            pass

        def get_credentials(self):
            return None

    monkeypatch.setattr(boto3, "Session", NoCreds)
    problems = pipeline.describe_preflight(cfg, pipeline.Selection("bedrock", "m", "flag"))
    assert any("AWS credentials" in p for p in problems)


# ---- Y6: batch ids
def _item(path: Path) -> ScanItem:
    info = ImageInfo(1, 1, None, None, "JPEG", False)
    return ScanItem(path=path, sha256="x", dhash=0, info=info)


def test_folder_batch_ids_are_url_safe(tmp_path):
    root = tmp_path / "photos"
    for folder in ("trip #2", "50% off?", "a b", "京都"):
        bid = batch_id_for(_item(root / folder / "a.jpg"), "folder", root)
        assert all(ch.isalnum() or ch in "._-" for ch in bid), bid
    assert batch_id_for(_item(root / "trip #2" / "a.jpg"), "folder", root) == "trip--2"
    assert batch_id_for(_item(root / ".." / "a.jpg"), "folder", root) not in ("..", ".")


def test_legacy_unsafe_batch_id_is_url_encoded_in_web(cfg, store):
    app = create_app(cfg)
    try:
        app.state.web.store.upsert_batch("trip #2", notes="")
        client = TestClient(app, base_url=BASE)
        assert 'href="/batch/trip%20%232"' in client.get("/").text
        page = client.get("/batch/trip%20%232", follow_redirects=False)
        assert page.status_code == 303 and "trip%20%232" in page.headers["location"]
        page = client.get("/batch/trip%20%232/review")
        assert page.status_code == 200 and "/batch/trip #2" not in page.text
    finally:
        app.state.web.runner.shutdown()


# ---- G8: a moved original is followed, not a duplicate
def test_moved_original_updates_src_path(cfg, store, photos_dir, tmp_path):
    pipeline.import_folder(cfg, store, photos_dir)
    moved = tmp_path / "moved"
    moved.mkdir()
    shutil.move(str(photos_dir / "a.jpg"), str(moved / "a.jpg"))
    summary = pipeline.import_folder(cfg, store, moved)
    assert summary.duplicates == 0 and summary.already_known == 1
    assert any(p.src_path == str(moved / "a.jpg") for p in store.list_photos())
    shutil.copy(moved / "a.jpg", photos_dir / "copy.jpg")  # a real copy is still a duplicate
    assert pipeline.import_folder(cfg, store, photos_dir).duplicates == 1


# ---- G9: marking a prepared photo ready drops the prepared copy
def test_override_status_drops_prepared_copy(cfg, store, photos_dir):
    batch = _setup(cfg, store, photos_dir)
    pipeline.describe_batch(cfg, store, batch, llm=FakeProvider("fake"))
    pipeline.approve(cfg, store, batch_id=batch)
    pipeline.prepare_batch(cfg, store, batch)
    photo = store.list_photos(batch, [Status.PREPARED])[0]
    path = Path(photo.prepared_path)
    assert path.exists()
    after = pipeline.override_status(store, photo.id, Status.READY)
    assert after.status == Status.READY
    assert after.prepared_path is None and after.submit_name is None and not path.exists()


# ---- G1: local read errors are not dropped connections
def test_missing_local_file_does_not_reconnect(tmp_path):
    reconnects = []
    results = {}

    class FTP:
        def quit(self):
            pass

    ftps.upload_many(
        FTP(),
        [("gone.jpg", tmp_path / "gone.jpg")],
        lambda name, exc: results.update({name: exc}),
        reconnect=lambda: reconnects.append(1) or FTP(),
    )
    assert isinstance(results["gone.jpg"], FileNotFoundError) and not reconnects
    assert ftps._is_dropped(ftplib.error_temp("421")) is True


# ---- G2: check-ftps logs in once
def test_check_ftps_upload_uses_one_login(tmp_path, monkeypatch):
    logins = []

    class FTP:
        def storbinary(self, cmd, fh):
            fh.read()

        def quit(self):
            pass

    def connect(host, user, password, *a, **k):
        logins.append(host)
        return FTP()

    f = tmp_path / "f.jpg"
    f.write_bytes(b"x")
    monkeypatch.setattr(ftps, "connect", connect)
    monkeypatch.setattr(
        deliver, "creds_from_secrets", lambda host: deliver.FtpsCreds(host, "u", "p")
    )
    ctx = types.SimpleNamespace(
        cfg=types.SimpleNamespace(deliver=types.SimpleNamespace(ftps_host="h")),
        args=types.SimpleNamespace(upload=str(f)),
    )
    assert cli.cmd_check_ftps(ctx) == 0
    assert logins == ["h"]


# ---- G3: hardware probes decode as UTF-8 and the Windows GPU probe waits longer
def test_probe_encoding_and_gpu_timeout():
    seen = []

    def run(cmd, **kw):
        seen.append(kw)
        return types.SimpleNamespace(returncode=0, stdout="")

    hardware._run(run, ["tool"], [], "tool")
    hardware._windows_gpu_names(run, [])
    kw1, kw2 = seen
    assert kw1["encoding"] == "utf-8" and kw1["errors"] == "replace" and kw1["timeout"] == 5
    assert kw2["timeout"] >= 10


# ---- G4: the UI is told which config file is loaded
def test_cmd_ui_passes_config_path(monkeypatch, tmp_path):
    got = {}
    monkeypatch.setattr("shuto.web.serve", lambda cfg, **kw: got.update(kw))
    cfg_file = tmp_path / "shuto.yaml"
    web_cfg = types.SimpleNamespace(host="127.0.0.1", port=1, open_browser=False)
    ctx = types.SimpleNamespace(
        cfg=types.SimpleNamespace(web=web_cfg), args=types.SimpleNamespace(), config_path=cfg_file
    )
    assert cli.cmd_ui(ctx) == 0
    assert got["config_path"] == str(cfg_file)
    assert got["launch_args"] == [
        "ui",
        "--host",
        "127.0.0.1",
        "--port",
        "1",
        "--config",
        str(cfg_file),
    ]


def test_cmd_ui_remembers_verbosity_for_the_update_relaunch(monkeypatch):
    got = {}
    monkeypatch.setattr("shuto.web.serve", lambda cfg, **kw: got.update(kw))
    web_cfg = types.SimpleNamespace(host="127.0.0.1", port=1, open_browser=False)
    ctx = types.SimpleNamespace(
        cfg=types.SimpleNamespace(web=web_cfg),
        args=types.SimpleNamespace(verbose=2, port=9000),
        config_path=None,
    )
    assert cli.cmd_ui(ctx) == 0
    assert got["launch_args"] == ["ui", "--host", "127.0.0.1", "--port", "9000", "-v", "-v"]


def test_version_flag(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["--version"])
    assert exc.value.code == 0 and capsys.readouterr().out.startswith("shuto ")


# ---- G5: bounded job results, throttled pull progress
def test_job_results_are_capped(store, monkeypatch):
    monkeypatch.setattr("shuto.jobs.MAX_KEPT_RESULTS", 2)
    runner = JobRunner(store)
    try:
        ids = [runner.submit("x", f"b{i}", lambda ctx, i=i: i) for i in range(4)]
        for jid in ids:
            runner.wait(jid)
        assert len(runner._results) <= 2
        assert runner.result(ids[-1]) == 3 and runner.result(ids[0]) is None
    finally:
        runner.shutdown()


def test_pull_progress_is_throttled(cfg, monkeypatch):
    class Noisy(FakeOllama):
        def pull(self, tag, on_progress=None):
            for done in range(0, 1001):
                on_progress({"status": "pulling", "total": 1000, "completed": done})

    monkeypatch.setattr(pipeline, "ollama_client", lambda c: Noisy())
    monkeypatch.setattr(
        pipeline, "model_disk_advice", lambda *a, **k: types.SimpleNamespace(fits=True)
    )
    app = create_app(cfg)
    state = app.state.web
    writes = []
    real = state.store.update_job

    def spy(*a, **k):
        writes.append(k)
        return real(*a, **k)

    monkeypatch.setattr(state.store, "update_job", spy)
    try:
        client = TestClient(app, base_url=BASE)
        r = client.post(
            "/settings/pull",
            data={"tag": "moondream", "confirm": "yes", "csrf_token": state.csrf},
        )
        assert r.status_code == 200, r.text
        state.runner.wait(int(re.search(r'(?:id="job-|data-job=")(\d+)"', r.text).group(1)))
        progress_writes = [w for w in writes if "done" in w]
        assert 0 < len(progress_writes) <= 105
    finally:
        state.runner.shutdown()


# ---- G11: the page preview is a cached downscaled copy
def test_image_route_serves_cached_preview(cfg, store, photos_dir):
    from PIL import Image

    pipeline.import_folder(cfg, store, photos_dir)
    app = create_app(cfg)
    try:
        photo = app.state.web.store.list_photos()[0]
        client = TestClient(app, base_url=BASE)
        r = client.get(f"/image/{photo.id}")
        assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
        cached = cfg.workspace_path("previews", f"{photo.sha256}.jpg")
        assert cached.is_file()
        with Image.open(cached) as im:
            assert max(im.size) <= 2048
        assert Path(photo.src_path).exists()  # original untouched
    finally:
        app.state.web.runner.shutdown()
