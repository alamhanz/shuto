from __future__ import annotations

import json
import os
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
    pipeline.set_ui_setting(w.state.store, "onboarded", True)
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


def test_empty_index_has_empty_state(web):
    r = web.get("/")
    assert r.status_code == 200 and "Add your first photos" in r.text


def test_home_with_batches_skips_wizard(web, batch):
    web.state.store.set_setting("ui.onboarded", None)
    r = web.get("/")
    assert r.status_code == 200 and batch in r.text and "Continue:" in r.text


def test_first_run_with_no_batches_redirects(cfg, ollama):
    w = Web(create_app(cfg))
    try:
        r = w.get("/")
        assert r.status_code == 303 and r.headers["location"] == "/welcome"
    finally:
        w.state.runner.shutdown()


def test_import_validates_folder(web, tmp_path):
    r = web.post("/import", {"path": str(tmp_path / "missing")})
    assert "not a folder" in r.text


def test_batch_page_redirects_to_stage_and_serves_images(web, batch):
    r = web.get(f"/batch/{batch}")
    assert r.status_code == 303 and r.headers["location"].endswith("/check")
    assert web.get(f"/batch/{batch}/check").status_code == 200
    web.run_job(f"/batch/{batch}/cull")
    photo = web.state.store.list_photos(batch)[0]
    assert web.get(f"/thumb/{photo.id}").headers["content-type"] == "image/jpeg"
    assert web.get(f"/image/{photo.id}").status_code == 200


def test_describe_approve_prepare_flow_and_upload_confirm(web, described, monkeypatch):
    store = web.state.store
    assert web.get(f"/batch/{described}/review").status_code == 200
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


def test_photo_page_edit_reruns_guardrails(web, described):
    store = web.state.store
    photo = store.list_photos(described, [Status.DESCRIBED])[0]
    page = web.get(f"/photo/{photo.id}")
    assert page.status_code == 200
    assert "Shutterstock rules" in page.text and "Editorial" in page.text

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
    assert "Continue" in text


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


def test_settings_page_shows_sections_models_and_providers(web, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-SECRET-VALUE")
    r = web.get("/settings")
    assert r.status_code == 200
    text = r.text
    assert "Test CPU" in text and "linux" in text
    for anchor in ("model", "shutterstock", "style", "computer", "advanced"):
        assert f'id="{anchor}"' in text
    assert text.count('<article class="card" id="row-') <= 8 and 'id="row-1"' in text
    assert "Works on this computer" in text and "Use this" in text
    for prov in ("Anthropic", "Gemini", "Bedrock"):
        assert prov in text
    assert "Access key:" in text and "Set</span>" in text and "Not set</span>" in text
    assert "ANTHROPIC_API_KEY" not in text and "shuto secrets" not in text
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
    assert "~$0.002 per image" in text and "D:\\ollama" in text
    assert "setx" not in text and "OLLAMA_MODELS" not in text and "/help#for-helpers" in text
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
    assert job.state == "done" and seen == [tag] and "1.5 s per photo" in text


def test_healthz(web):
    r = web.get("/healthz")
    assert r.status_code == 200
    assert r.text.startswith("shuto")


def test_cancel_requires_csrf_and_known_job(web):
    assert web.post("/jobs/999/cancel", token=False).status_code == 403
    assert web.post("/jobs/999/cancel").status_code == 404


def test_browse_route_is_read_only_and_confined(web, tmp_path, monkeypatch):
    from shuto.web import browse

    monkeypatch.setattr(browse, "roots", lambda: [browse.Root("Home", str(tmp_path))])
    raw = "a&b" if os.name == "nt" else "<b>shots</b>"  # Windows forbids < > in names
    escaped = "a&amp;b" if os.name == "nt" else "&lt;b&gt;shots&lt;/b&gt;"
    (tmp_path / raw).mkdir()
    ok = web.get("/browse", params={"path": str(tmp_path)})
    assert ok.status_code == 200
    assert raw not in ok.text
    assert escaped in ok.text
    outside = web.get("/browse", params={"path": str(tmp_path.parent)})
    assert outside.status_code == 200
    assert "outside" in outside.text
    assert web.post("/browse", {}).status_code == 405


def test_css_has_no_hex_colour_outside_root():
    from pathlib import Path

    css = (Path(__file__).parents[1] / "src/shuto/web/static/shuto.css").read_text("utf-8")
    start = css.find(":root")
    if start < 0:
        pytest.xfail("design system not landed yet")
    end = css.find("}", start)
    rest = css[:start] + css[end:]
    assert not re.findall(r"#[0-9a-fA-F]{3,8}\b", re.sub(r"/\*.*?\*/", "", rest, flags=re.S))


# ------------------------------------------------------------------ welcome wizard


def visible_text(html: str) -> str:
    html = re.sub(r"<(script|style)\b.*?</\1>", " ", html, flags=re.S | re.I)
    return re.sub(r"<[^>]+>", " ", html)


def test_wizard_steps_render_and_clamp(web):
    for step in range(1, 7):
        r = web.get(f"/welcome?step={step}")
        assert r.status_code == 200, step
        assert f"Step {step} of 6" in r.text
    assert "Step 6 of 6" in web.get("/welcome?step=99").text
    assert "Step 1 of 6" in web.get("/welcome?step=0").text
    assert web.post("/welcome/9").status_code == 404


def test_wizard_posts_need_csrf(web):
    for step in (3, 4, 5, 6):
        assert web.post(f"/welcome/{step}", token=False).status_code == 403


def test_wizard_model_login_style_and_done(web, cfg, monkeypatch):
    tag = pipeline.list_models(cfg)[0].tag
    r = web.post("/welcome/3", {"spec": tag})
    assert r.status_code == 303 and r.headers["location"].startswith("/welcome?step=4")
    assert web.state.store.get_setting("llm.model") == tag

    saved = {}

    def fake_set(name, value):
        saved[name] = value
        monkeypatch.setenv(name, value)

    monkeypatch.setattr("shuto.secrets.set_secret", fake_set)
    r = web.post("/welcome/4", {"username": "me@example.com", "password": "hunter2-PASSWORD"})
    assert r.status_code == 303 and r.headers["location"].startswith("/welcome?step=5")
    assert saved == {
        "SHUTO_FTPS_USER": "me@example.com",
        "SHUTO_FTPS_PASSWORD": "hunter2-PASSWORD",
    }
    assert "hunter2" not in r.headers["location"]
    for path in ("/welcome?step=4", "/welcome?step=6", "/settings"):
        body = web.get(path).text
        assert "hunter2-PASSWORD" not in body
        assert "A login is saved" in body or "saved" in body

    r = web.post("/welcome/5", {"theme": "Street food", "always_include": "japan, ramen"})
    assert r.status_code == 303
    assert web.state.cfg.style.theme == "Street food"
    assert web.state.cfg.style.keywords.always_include == ["japan", "ramen"]

    web.state.store.set_setting("ui.onboarded", None)
    r = web.post("/welcome/6", {"then": "add"})
    assert r.status_code == 303 and r.headers["location"] == "/add"
    assert pipeline.get_ui_setting(web.state.store, "onboarded") is True


def test_login_without_password_is_refused_and_secret_failure_is_reported(web, monkeypatch):
    r = web.post("/settings/ftps", {"username": "me@example.com"})
    assert "error=" in r.headers["location"]
    r = web.post("/settings/ftps", {})
    assert "error=" in r.headers["location"]

    def boom(name, value):
        raise RuntimeError("Cannot save the password: no keyring.")

    monkeypatch.setattr("shuto.secrets.set_secret", boom)
    r = web.post("/settings/ftps", {"username": "a", "password": "secret-pw-1"})
    assert "no%20keyring" in r.headers["location"] and "secret-pw-1" not in r.headers["location"]


def test_login_test_and_forget(web, monkeypatch):
    monkeypatch.setattr(pipeline, "test_ftps_login", lambda cfg: None)
    job, text = web.run_job("/settings/ftps/test")
    assert job.state == "done" and "login works" in text

    def refuse(cfg):
        raise pipeline.PipelineError("The upload server refused this email or password.")

    monkeypatch.setattr(pipeline, "test_ftps_login", refuse)
    job, text = web.run_job("/settings/ftps/test")
    assert job.state == "failed"
    assert "refused this email or password" in text and "Try again" in text
    assert "PipelineError" not in text

    deleted = []
    monkeypatch.setattr("shuto.secrets.delete_secret", deleted.append)
    r = web.post("/settings/ftps/forget")
    assert r.status_code == 303 and deleted == ["SHUTO_FTPS_USER", "SHUTO_FTPS_PASSWORD"]


def test_settings_style_saved_and_applied(web):
    r = web.post(
        "/settings/style", {"theme": "Mountains", "tone": "calm", "always_include": "alps"}
    )
    assert r.status_code == 303 and "notice=" in r.headers["location"]
    assert web.state.cfg.style.theme == "Mountains"
    assert 'value="Mountains"' in web.get("/settings").text
    too_long = web.post("/settings/style", {"theme": "x" * 300})
    assert "error=" in too_long.headers["location"]


def test_wizard_download_keeps_confirm_and_return_page(web, cfg, ollama):
    tag = next(r.tag for r in pipeline.list_models(cfg) if not r.pulled and r.fits_hardware)
    advice = web.get(f"/settings/advice?tag={tag}&return_to=/welcome?step=3")
    assert 'name="return_to" value="/welcome?step=3"' in advice.text
    evil = web.get(f"/settings/advice?tag={tag}&return_to=http://evil.example")
    assert "evil.example" not in evil.text
    r = web.post("/settings/pull", {"tag": tag})
    assert "not confirmed" in r.text and not ollama.pull_calls
    job, text = web.run_job(
        "/settings/pull", {"tag": tag, "confirm": "yes", "return_to": "/welcome?step=3"}
    )
    assert job.state == "done" and 'href="/welcome?step=3"' in text


# ------------------------------------------------------------------ home, add, import, jobs


def test_import_with_batch_name_creates_one_batch(web, photos_dir):
    job, text = web.run_job(
        "/import", {"path": str(photos_dir), "batch_name": "Kyoto walk", "recursive": "yes"}
    )
    assert job.state == "done", job.error
    assert "photo(s) added" in text and "Open batch" in text
    batches = [b["id"] for b in pipeline.batch_report(web.state.store)]
    assert len(batches) == 1 and "Kyoto" in batches[0]
    assert pipeline.get_ui_setting(web.state.store, "last_folder") == str(photos_dir)
    assert str(photos_dir) in web.get("/add").text


def test_import_default_batch_name_is_folder_name(web, photos_dir):
    web.run_job("/import", {"path": str(photos_dir)})
    (b,) = [x["id"] for x in pipeline.batch_report(web.state.store)]
    assert photos_dir.name.lower() in b.lower()


def test_home_cards_show_stage_and_estimate_fallback(web, batch):
    text = web.get("/").text
    assert "Continue: Check" in text and f"/batch/{batch}/check" in text
    assert "Add photos" in text


def test_failed_job_shows_plain_words(web):
    def boom(ctx):
        raise RuntimeError("Ollama is not running")

    job_id = web.state.runner.submit("import", "import", boom)
    web.state.runner.wait(job_id)
    text = web.get(f"/jobs/{job_id}").text
    assert "The AI helper is not running." in text and "Try again" in text
    assert "RuntimeError" not in text


def test_active_job_polls_and_can_cancel(web):
    import threading

    gate = threading.Event()
    job_id = web.state.runner.submit("pull", "models", lambda ctx: gate.wait(5), total=4)
    try:
        text = web.get(f"/jobs/{job_id}").text
        assert f'hx-get="/jobs/{job_id}"' in text and f"/jobs/{job_id}/cancel" in text
        assert "of 4" in text
    finally:
        gate.set()
        web.state.runner.wait(job_id)


JARGON = r"dHash|IPTC|CSRF|FTPS|cull|guardrail|env|keyring|terminal|setx|systemctl"


def main_text(html: str) -> str:
    """Visible text without the "For helpers" disclosure, which may hold commands."""
    html = re.sub(r"<details[^>]*data-helpers.*?</details>", " ", html, flags=re.S)
    return visible_text(html)


def test_pages_have_no_jargon(web, batch):
    pages = ["/", "/add", "/settings", "/help", "/browse"]
    pages += [f"/welcome?step={n}" for n in range(1, 7)]
    pages += ["/welcome?step=3&online=1"]
    pages += [f"/batch/{batch}/{p}" for p in ("check", "review", "upload", "submit")]
    photo = web.state.store.list_photos(batch)[0]
    pages += [f"/photo/{photo.id}"]
    for path in pages:
        r = web.get(path)
        assert r.status_code == 200, path
        found = re.findall(JARGON, main_text(r.text), flags=re.I)
        assert not found, (path, found)


def test_advice_fragment_has_no_jargon(web, cfg, monkeypatch):
    from shuto import local_models

    tag = pipeline.list_models(cfg)[0].tag
    vol = local_models.VolumeOption("D:", 500.0, "D:\ollama", "setx OLLAMA_MODELS D:\ollama")
    monkeypatch.setattr(
        pipeline,
        "model_disk_advice",
        lambda cfg, t: local_models.DiskAdvice(
            tag=t,
            fits=False,
            models_dir="/m",
            free_gb=1.0,
            need_gb=9.0,
            volumes=[vol],
            api_options=[],
            message="Not enough room.",
        ),
    )
    text = web.get(f"/settings/advice?tag={tag}").text
    assert not re.findall(JARGON, visible_text(text), flags=re.I)
    help_text = web.get("/help").text
    assert "data-helpers" in help_text and "OLLAMA_MODELS" in help_text


def test_failed_upload_job_gets_upload_advice(web):
    def boom(ctx):
        raise TimeoutError("timed out (TimeoutError)")

    for kind, expect in (
        ("deliver", "Shutterstock"),
        ("ftps_test", "Shutterstock"),
        ("pull", "AI"),
    ):
        job_id = web.state.runner.submit(kind, kind, boom)
        web.state.runner.wait(job_id)
        text = web.get(f"/jobs/{job_id}").text
        assert expect in text, kind
        assert ("took too long" in text) == (kind == "pull"), kind


def test_job_status_line_is_the_only_live_region(web):
    assert "aria-live" not in web.get("/").text
    job_id = web.state.runner.submit("import", "import", lambda ctx: "ok")
    web.state.runner.wait(job_id)
    assert 'role="status"' in web.get(f"/jobs/{job_id}").text


# ------------------------------------------------------------------ access keys


SECRET = "sk-ant-TOPSECRET-123456"


@pytest.fixture
def keyring(monkeypatch):
    store: dict[str, str] = {}
    monkeypatch.setattr("shuto.secrets.set_secret", lambda n, v: store.__setitem__(n, v))
    monkeypatch.setattr("shuto.secrets.delete_secret", lambda n: store.pop(n, None))
    monkeypatch.setattr("shuto.secrets.get_secret", lambda n: store.get(n))
    return store


def test_access_key_save_never_echoed(web, keyring):
    r = web.post("/settings/key", {"provider": "anthropic", "key": SECRET})
    assert r.status_code == 303
    assert keyring == {"ANTHROPIC_API_KEY": SECRET}
    assert SECRET not in r.headers["location"] and SECRET not in r.text
    for path in ("/settings", "/welcome?step=3&online=1", r.headers["location"]):
        page = web.get(path).text
        assert SECRET not in page
    assert "Set</span>" in web.get("/settings").text


def test_access_key_needs_csrf(web, keyring):
    r = web.post("/settings/key", {"provider": "anthropic", "key": SECRET}, token=False)
    assert r.status_code == 403 and keyring == {}
    r = web.post("/settings/key/forget", {"provider": "anthropic"}, token=False)
    assert r.status_code == 403


def test_access_key_forget_and_validation(web, keyring):
    web.post("/settings/key", {"provider": "gemini", "key": "g" * 20})
    assert "GEMINI_API_KEY" in keyring
    r = web.post("/settings/key/forget", {"provider": "gemini"})
    assert r.status_code == 303 and keyring == {}
    r = web.post("/settings/key/forget", {"provider": "gemini"}, headers={"HX-Request": "true"})
    assert r.status_code == 204 and "HX-Redirect" in r.headers
    bad = web.post("/settings/key", {"provider": "anthropic", "key": "has space in it ok"})
    assert "error=" in bad.headers["location"] and keyring == {}
    assert web.post("/settings/key", {"provider": "nope", "key": SECRET}).status_code == 404


def test_key_forget_form_uses_htmx_confirm(web, keyring):
    web.post("/settings/key", {"provider": "gemini", "key": "g" * 20})
    page = web.get("/settings").text
    assert 'hx-post="/settings/key/forget"' in page and "hx-confirm=" in page
    r = web.post("/settings/key/forget", {"provider": "gemini"}, headers={"HX-Request": "true"})
    assert r.status_code == 204 and keyring == {}


def test_bedrock_profile_saved_from_settings(web):
    r = web.post("/settings/key", {"provider": "bedrock", "profile": "photos"})
    assert r.status_code == 303
    assert web.state.cfg.llm.bedrock.profile == "photos"
    assert 'value="photos"' in web.get("/settings").text
    web.post("/settings/key/forget", {"provider": "bedrock"})
    assert not web.state.cfg.llm.bedrock.profile


# ------------------------------------------------------------------ style


def test_style_can_be_cleared_and_region_focus_set(web):
    web.post("/settings/style", {"theme": "Mine", "always_include": "a, b", "tone": "calm"})
    assert web.state.cfg.style.theme == "Mine"
    web.post(
        "/settings/style", {"theme": "", "always_include": "", "tone": "", "region_focus": "japan"}
    )
    style = web.state.cfg.style
    assert style.theme == "" and style.keywords.always_include == [] and style.tone == ""
    assert style.region_focus == "japan"
    assert 'value="japan" selected' in web.get("/settings").text
    web.post("/settings/style", {"theme": "", "always_include": "", "tone": "", "region_focus": ""})
    assert web.state.cfg.style.region_focus == ""


def test_help_page_has_glossary_and_shortcuts(web):
    text = web.get("/help").text
    for word in ("Batch", "Commercial", "Editorial", "Needs a look", "Sent before", "CSV"):
        assert word in text
    assert "<kbd>j</kbd>" in text


def test_ftps_back_is_local_only(web, monkeypatch):
    monkeypatch.setattr("shuto.secrets.set_secret", lambda n, v: None)
    ok = web.post("/settings/ftps", {"username": "a", "password": "b", "back": "/batch/x/upload"})
    assert ok.headers["location"].startswith("/batch/x/upload?")
    for bad in ("//evil.example", "http://evil.example", "/" + chr(92) + "evil.example"):
        r = web.post("/settings/ftps", {"username": "a", "password": "b", "back": bad})
        assert r.headers["location"].startswith("/settings?")
