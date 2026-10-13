"""The model download bar: visible on every page, resumes on the model step and Settings."""

from __future__ import annotations

import threading

import pytest

from shuto import pipeline
from shuto.web import create_app

from .conftest import FakeOllama
from .test_web import Web


class SlowOllama(FakeOllama):
    """A pull that stays at 40% until the test lets it finish (or fail)."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.started = threading.Event()
        self.release = threading.Event()
        self.fail = False

    def pull(self, tag, on_progress=None):
        self.pull_calls.append(tag)
        on_progress({"status": "pulling", "total": 100_000_000, "completed": 40_000_000})
        self.started.set()
        assert self.release.wait(30)
        if self.fail:
            raise OSError("connection lost")
        on_progress({"status": "success", "total": 100_000_000, "completed": 100_000_000})


@pytest.fixture
def slow(monkeypatch):
    fake = SlowOllama(pulled=["gemma3:4b"])
    monkeypatch.setattr(pipeline, "ollama_client", lambda cfg: fake)
    return fake


@pytest.fixture
def web(cfg, slow):
    w = Web(create_app(cfg))
    pipeline.set_ui_setting(w.state.store, "onboarded", True)
    yield w
    slow.release.set()
    w.state.runner.shutdown()


def start_pull(web, cfg, slow, back="/settings"):
    tag = next(r.tag for r in pipeline.list_models(cfg) if not r.pulled and r.fits_hardware)
    web.post("/settings/pull", {"tag": tag, "confirm": "yes", "return_to": back})
    assert slow.started.wait(30)
    return tag


def test_bar_is_empty_without_a_download(web):
    assert web.get("/downloads/bar").text.strip() == ""
    assert 'hx-get="/downloads/bar"' in web.get("/help").text


def test_bar_shows_running_download_then_done(web, cfg, slow):
    tag = start_pull(web, cfg, slow)
    text = web.get("/downloads/bar").text
    assert f"Downloading {tag}" in text and "40%" in text and "40 of 100 MB" in text
    assert 'hx-trigger="every 2s"' in text and "pulling" in text
    job_id = web.state.runner.active_job("models")
    slow.release.set()
    web.state.runner.wait(job_id)
    done = web.get(f"/downloads/bar?job={job_id}").text
    assert f"Downloaded {tag}" in done and "every 2s" not in done and "Dismiss" in done
    assert web.get("/downloads/bar").text.strip() == ""  # a later page does not nag


def test_bar_shows_failure(web, cfg, slow):
    start_pull(web, cfg, slow)
    job_id = web.state.runner.active_job("models")
    slow.fail = True
    slow.release.set()
    web.state.runner.wait(job_id)
    text = web.get(f"/downloads/bar?job={job_id}").text
    assert 'role="alert"' in text and "every 2s" not in text and "Dismiss" in text


def test_model_step_and_settings_show_running_download(web, cfg, slow):
    tag = start_pull(web, cfg, slow, back="/welcome?step=3")
    for page in ("/welcome?step=3", "/settings"):
        text = web.get(page).text
        assert 'id="job-resume"' in text and "Downloading the AI model" in text
        assert "Downloading now" in text
        assert f'"tag": "{tag}"' not in text  # no second Download button for it
    slow.release.set()
    web.state.runner.wait(web.state.runner.active_job("models"))
    assert "Downloading now" not in web.get("/welcome?step=3").text
