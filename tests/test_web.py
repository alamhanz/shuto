from __future__ import annotations

import json
import re

import pytest
from fastapi.testclient import TestClient

from shuto import pipeline
from shuto.models import Status
from shuto.web import create_app

from .conftest import FakeOllama

BASE = "http://127.0.0.1:8765"


class Web:
    """TestClient wrapper that adds the CSRF token to posts and waits for jobs."""

    def __init__(self, app):
        self.app = app
        self.state = app.state.web
        self.client = TestClient(app, base_url=BASE, follow_redirects=False)

    def get(self, path, **kw):
        return self.client.get(path, **kw)

    def post(self, path, data=None, token=True, **kw):
        data = dict(data or {})
        if token:
            data["csrf_token"] = self.state.csrf
        return self.client.post(path, data=data, **kw)

    def run_job(self, path, data=None):
        r = self.post(path, data)
        assert r.status_code == 200, r.text
        match = re.search(r'id="job-(\d+)"', r.text)
        assert match, r.text
        job = self.state.runner.wait(int(match.group(1)), timeout=60)
        return job, self.get(f"/jobs/{job.id}").text


@pytest.fixture
def ollama(monkeypatch):
    fake = FakeOllama(pulled=["gemma3:4b"])
    monkeypatch.setattr(pipeline, "ollama_client", lambda cfg: fake)
    return fake


@pytest.fixture
def web(cfg, ollama):
    w = Web(create_app(cfg))
    yield w
    w.state.runner.shutdown()


@pytest.fixture
def batch(web, photos_dir):
    job, _ = web.run_job("/import", {"path": str(photos_dir), "notes": "test batch"})
    assert job.state == "done"
    (b,) = [x["id"] for x in pipeline.batch_report(web.state.store)]
    return b


@pytest.fixture
def described(web, batch):
    assert web.run_job(f"/batch/{batch}/cull")[0].state == "done"
    job, _ = web.run_job(f"/batch/{batch}/describe")
    assert job.state == "done", job.error
    return batch


# ------------------------------------------------------------------ security


def test_bad_host_rejected(web):
    assert web.get("/", headers={"Host": "evil.example"}).status_code == 400
    assert web.get("/").status_code == 200


def test_missing_or_wrong_csrf_is_403(web, batch):
    assert web.post(f"/batch/{batch}/cull", token=False).status_code == 403
    r = web.client.post(f"/batch/{batch}/cull", data={"csrf_token": "nope"})
    assert r.status_code == 403
    r = web.client.post(f"/batch/{batch}/cull", data={}, headers={"X-CSRF-Token": web.state.csrf})
    assert r.status_code == 200  # the header form used by htmx works too


def test_foreign_origin_is_403(web, batch):
    r = web.post(f"/batch/{batch}/cull", headers={"Origin": "http://evil.example"})
    assert r.status_code == 403
    r = web.post(f"/batch/{batch}/cull", headers={"Origin": BASE})
    assert r.status_code == 200


def test_security_headers_and_no_cdn(web):
    r = web.get("/")
    assert "script-src 'self'" in r.headers["content-security-policy"]
    assert "http" not in re.sub(
        r"http://127\.0\.0\.1", "", " ".join(re.findall(r'src="[^"]+"', r.text))
    )
    assert web.get("/static/htmx.min.js").status_code == 200


def test_unknown_ids_are_404(web, batch):
    assert web.get("/image/9999").status_code == 404
    assert web.get("/thumb/9999").status_code == 404
    assert web.get("/photo/9999").status_code == 404
    assert web.get("/batch/nope").status_code == 404
    assert web.get("/jobs/9999").status_code == 404
    assert web.get(f"/batch/{batch}/exports/..%2Fshuto.db").status_code == 404
    assert web.post(f"/batch/{batch}/format-disk").status_code == 404


# ------------------------------------------------------------------ pages and flow


def test_empty_index_has_onboarding(web):
    r = web.get("/")
    assert r.status_code == 200 and "New here?" in r.text


def test_import_validates_folder(web, tmp_path):
    r = web.post("/import", {"path": str(tmp_path / "missing")})
    assert "not a folder" in r.text


def test_batch_page_estimate_cull_reasons_and_images(web, batch):
    page = web.get(f"/batch/{batch}")
    assert page.status_code == 200
    assert "Commercial or Editorial" in page.text and "submit.shutterstock.com" in page.text
    web.run_job(f"/batch/{batch}/cull")
    page = web.get(f"/batch/{batch}").text
    assert "Estimate:" in page  # shown before Describe
    assert "too small" in page or "blurry" in page  # cull reasons on the cards
    photo = web.state.store.list_photos(batch)[0]
    assert web.get(f"/thumb/{photo.id}").headers["content-type"] == "image/jpeg"
    assert web.get(f"/image/{photo.id}").status_code == 200
    filtered = web.get(f"/batch/{batch}?status=culled").text
    assert "culled" in filtered


def test_describe_approve_prepare_flow_and_upload_confirm(web, described, monkeypatch):
    store = web.state.store
    page = web.get(f"/batch/{described}").text
    assert "described" in page
    r = web.post(f"/batch/{described}/bulk", {"action": "approve_all"})
    assert r.status_code == 303
    assert store.list_photos(described, [Status.APPROVED])
    job, text = web.run_job(f"/batch/{described}/prepare")
    assert job.state == "done" and "Prepared" in text
    assert store.list_photos(described, [Status.PREPARED])

    # upload needs the explicit confirm box; the service is not even called without it
    calls = []

    def fake_upload(cfg, store_, batch_id, **kw):
        calls.append(kw)
        return pipeline.UploadSummary(uploaded=["x.jpg"])

    monkeypatch.setattr(pipeline, "upload_batch", fake_upload)
    r = web.post(f"/batch/{described}/upload")
    assert "confirm" in r.text and not calls
    job, text = web.run_job(f"/batch/{described}/upload", {"confirm": "yes"})
    assert job.state == "done" and calls and calls[0]["confirm"] is True
    assert "Uploaded 1" in text


def test_export_nothing_and_submitted(web, described):
    job, text = web.run_job(f"/batch/{described}/export")
    assert "Nothing to export" in text
    r = web.post(f"/batch/{described}/submitted")
    assert r.status_code == 303


def test_export_download(web, described, tmp_path):
    photo = web.state.store.list_photos(described, [Status.DESCRIBED])[0]
    csv = tmp_path / "ws" / "exports" / "out.csv"
    csv.parent.mkdir(parents=True, exist_ok=True)
    csv.write_text("Filename\n", encoding="utf-8")
    web.state.store.update_photo(photo.id, export_file=str(csv))
    r = web.get(f"/batch/{described}/exports/out.csv")
    assert r.status_code == 200 and r.text.startswith("Filename")
    assert "out.csv" in web.get(f"/batch/{described}").text


def test_photo_page_edit_reruns_guardrails(web, described):
    store = web.state.store
    photo = store.list_photos(described, [Status.DESCRIBED])[0]
    page = web.get(f"/photo/{photo.id}")
    assert page.status_code == 200
    assert "Guardrails" in page.text and "Editorial" in page.text and "next (j)" in page.text

    r = web.post(
        f"/photo/{photo.id}",
        {
            "description": "A quiet street with a parked bicycle",
            "title": "Quiet street",
            "keywords": "japan",
            "categories": ["Nature", "Objects"],
        },
    )
    assert r.status_code == 303
    after = store.get_photo(photo.id)
    assert after.status == Status.NEEDS_REVIEW  # too few keywords, caught by the re-run
    assert after.metadata["title"] == "Quiet street"
    assert after.metadata["keywords"] == ["japan"]

    web.post(
        f"/photo/{photo.id}",
        {"description": "A Nike store front in the street", "keywords": "japan, street"},
    )
    flagged = store.get_photo(photo.id)
    assert flagged.guardrail["action"] in ("review", "block", "force_editorial")
    assert "Nike" in web.get(f"/photo/{photo.id}").text  # reasons are visible on the page

    too_many = web.post(f"/photo/{photo.id}", {"categories": ["Nature", "Objects", "Arts"]})
    assert "at%20most%202" in too_many.headers["location"]


def test_photo_escapes_html_and_shows_gps_warning(web, described):
    store = web.state.store
    photo = store.list_photos(described, [Status.DESCRIBED])[0]
    web.post(f"/photo/{photo.id}", {"description": "<script>alert(1)</script> street"})
    store.update_photo(photo.id, has_gps=True)
    text = web.get(f"/photo/{photo.id}").text
    assert "<script>alert(1)</script>" not in text
    assert "&lt;script&gt;" in text
    assert "GPS" in text and "does not strip GPS" in text


def test_approve_and_ready_actions(web, described):
    store = web.state.store
    photo = store.list_photos(described, [Status.DESCRIBED])[0]
    assert web.post(f"/photo/{photo.id}/approve").status_code == 303
    assert store.get_photo(photo.id).status == Status.APPROVED
    web.post(f"/photo/{photo.id}/ready")
    assert store.get_photo(photo.id).status == Status.READY
    culled = store.list_photos(described, [Status.CULLED])[0]
    web.post(f"/batch/{described}/bulk", {"action": "ready", "ids": [str(culled.id)]})
    assert store.get_photo(culled.id).status == Status.READY


def test_regenerate_runs_as_job(web, described):
    photo = web.state.store.list_photos(described, [Status.DESCRIBED])[0]
    job, text = web.run_job(f"/photo/{photo.id}/regenerate")
    assert job.state == "done", job.error
    assert "Described again" in text


def test_job_busy_message(web, batch):
    started = web.state.runner.submit("cull", batch, lambda ctx: __import__("time").sleep(0.3))
    r = web.post(f"/batch/{batch}/cull")
    assert "already has job" in r.text
    web.state.runner.wait(started)


def test_describe_blocked_without_local_choice(cfg, ollama):
    local = cfg.model_copy(update={"llm": cfg.llm.model_copy(update={"provider": "ollama"})})
    w = Web(create_app(local))
    try:
        w.state.store.upsert_batch("b1")
        r = w.post("/batch/b1/describe")
        assert "Settings" in r.text
    finally:
        w.state.runner.shutdown()


def test_result_recording(web, described):
    store = web.state.store
    photo = store.list_photos(described, [Status.DESCRIBED])[0]
    store.update_photo(photo.id, submit_name="a.jpg")
    store.set_status(photo.id, Status.APPROVED)
    store.set_status(photo.id, Status.PREPARED)
    store.set_status(photo.id, Status.UPLOADED)
    store.set_status(photo.id, Status.EXPORTED)
    assert "Shutterstock result" in web.get(f"/photo/{photo.id}").text
    r = web.post(f"/photo/{photo.id}/result", {"outcome": "reject", "reason": "focus"})
    assert r.status_code == 303
    assert store.get_photo(photo.id).status == Status.REJECTED
    assert store.list_rejections(photo.id)[0].reason_code == "focus"


# ------------------------------------------------------------------ settings


def test_settings_page_shows_hardware_models_and_providers(web, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-SECRET-VALUE")
    r = web.get("/settings")
    assert r.status_code == 200
    text = r.text
    assert "Test CPU" in text and "linux" in text
    assert "Top local models" in text and "Use this model" in text
    assert text.count('<tr id="row-') <= 8 and 'id="row-1"' in text
    for prov in ("anthropic", "gemini", "bedrock"):
        assert prov in text
    assert "ANTHROPIC_API_KEY: <strong>set</strong>" in text
    assert "GEMINI_API_KEY: <strong>not set</strong>" in text
    assert "SECRET-VALUE" not in text


def test_use_model_persists(web, cfg):
    rows = pipeline.list_models(cfg)
    tag = rows[0].tag
    r = web.post("/settings/use", {"spec": tag})
    assert r.status_code == 303
    assert json.loads(pipeline.model_choice_path().read_text(encoding="utf-8"))
    assert web.state.store.get_setting("llm.provider") == "ollama"
    assert tag in web.get("/settings").text

    r = web.post("/settings/use", {"spec": "gemini:"})
    assert r.status_code == 303 and "notice=" in r.headers["location"]
    assert web.state.store.get_setting("llm.provider") == "gemini"
    assert web.state.store.get_setting("llm.model")

    bad = web.post("/settings/use", {"spec": "not-in-catalog:1b"})
    assert "error=" in bad.headers["location"]
    assert web.state.store.get_setting("llm.provider") == "gemini"


def test_download_needs_explicit_confirm(web, cfg, ollama):
    tag = next(r.tag for r in pipeline.list_models(cfg) if not r.pulled and r.fits_hardware)
    advice = web.get(f"/settings/advice?tag={tag}")
    assert advice.status_code == 200 and "Yes, download" in advice.text
    r = web.post("/settings/pull", {"tag": tag})  # no confirm
    assert "not confirmed" in r.text and not ollama.pull_calls
    job, text = web.run_job("/settings/pull", {"tag": tag, "confirm": "yes"})
    assert job.state == "done" and ollama.pull_calls == [tag]
    assert web.get("/settings/advice?tag=nope").status_code == 404


def test_advice_when_model_does_not_fit_disk(web, cfg, monkeypatch):
    from shuto import local_models

    tag = pipeline.list_models(cfg)[0].tag
    monkeypatch.setattr(
        pipeline,
        "model_disk_advice",
        lambda cfg, t: local_models.DiskAdvice(
            tag=t,
            fits=False,
            models_dir="/m",
            free_gb=1.0,
            need_gb=9.0,
            volumes=[
                local_models.VolumeOption(
                    "D:", 500.0, "D:\\ollama", "setx OLLAMA_MODELS D:\\ollama"
                )
            ],
            api_options=[
                local_models.ApiOption("gemini", "m1", 0.002, "2026-10", "~$0.002 per image")
            ],
            message="Not enough room.",
        ),
    )
    text = web.get(f"/settings/advice?tag={tag}").text
    assert "setx OLLAMA_MODELS" in text and "~$0.002 per image" in text
    assert "Yes, download" not in text
    r = web.post("/settings/pull", {"tag": tag, "confirm": "yes"})
    assert "Not enough room" in r.text


def test_benchmark_job(web, cfg, monkeypatch):
    seen = []
    monkeypatch.setattr(
        pipeline,
        "bench_model",
        lambda cfg, store, model=None: (
            seen.append(model) or {"cold_s": 3.0, "warm_s": 1.5, "ok_json": True}
        ),
    )
    tag = pipeline.list_models(cfg)[0].tag
    job, text = web.run_job("/settings/benchmark", {"tag": tag})
    assert job.state == "done" and seen == [tag] and "1.5 s per image" in text
