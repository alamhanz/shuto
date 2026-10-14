"""The model download bar: visible on every page, resumes on the model step and Settings."""

from __future__ import annotations

import threading

import pytest

from shuto.ai import selection
from shuto.settings import ui as settings_ui
from shuto.web import create_app
from shuto.workflow import models as workflow_models

from ..helpers import FakeOllama
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
    monkeypatch.setattr(selection, "ollama_client", lambda cfg: fake)
    return fake


@pytest.fixture
def web(cfg, slow):
    w = Web(create_app(cfg))
    settings_ui.set_ui_setting(w.state.store, "onboarded", True)
    yield w
    slow.release.set()
    w.state.runner.shutdown()


def start_pull(web, cfg, slow, back="/settings"):
    tag = next(r.tag for r in workflow_models.list_models(cfg) if not r.pulled and r.fits_hardware)
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
    assert 'hx-trigger="every 2s"' in text and "pulling" not in text  # no raw Ollama words
    job_id = web.state.runner.active_job("models")
    slow.release.set()
    web.state.runner.wait(job_id)
    done = web.get(f"/downloads/bar?job={job_id}").text
    assert f"Downloaded {tag}" in done and "every 2s" not in done and "Dismiss" in done
    assert web.get(f"/downloads/bar?job={job_id}").text == done  # same, from another tab
    assert web.get("/downloads/bar").text.strip() == ""  # Dismiss
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
        assert 'id="dl-slot"' in text and 'class="progress' not in text  # one slot, no second panel
        assert "Downloading now" in text
        assert f'"tag": "{tag}"' not in text  # no second Download button for it
    slow.release.set()
    web.state.runner.wait(web.state.runner.active_job("models"))
    assert "Downloading now" not in web.get("/welcome?step=3").text


def test_bar_status_is_plain_words(web, cfg, slow):
    start_pull(web, cfg, slow)
    slow.release.set()
    web.state.runner.wait(web.state.runner.active_job("models"))
    for raw in ("pulling manifest", "verifying sha256 digest", "writing manifest"):
        assert raw not in web.get("/downloads/bar?job=1").text


def test_wizard_download_continue_stays_in_wizard(web, cfg, slow):
    start_pull(web, cfg, slow, back="/welcome?step=3")
    text = web.get("/welcome?step=3").text
    assert 'id="dl-slot"' in text and "Progress is shown on this page" in text
    assert "Cancel" in web.get("/downloads/bar").text
    job_id = web.state.runner.active_job("models")
    slow.release.set()
    web.state.runner.wait(job_id)
    assert 'href="/welcome?step=3"' in web.get(f"/downloads/bar?job={job_id}").text


def test_bar_for_models_dir_job(web, monkeypatch, tmp_path):
    from shuto.ollama import models_dir as ollama_store

    monkeypatch.setattr(ollama_store, "supported", lambda name=None: True)
    monkeypatch.setenv("OLLAMA_MODELS", str(tmp_path / "old"))
    release = threading.Event()

    def fake_change(target, *, move, client, progress, cancelled, os_name=None):
        progress(1, 2, "Copying models")
        assert release.wait(30)
        return "Ollama now keeps its models in X."

    monkeypatch.setattr(ollama_store, "change_models_dir", fake_change)
    web.post("/settings/models-dir", {"path": str(tmp_path / "new"), "move": "yes"})
    job_id = web.state.runner.active_job("models")
    text = web.get("/downloads/bar").text
    assert "Moving AI models" in text and "every 2s" in text
    release.set()
    web.state.runner.wait(job_id)
    done = web.get(f"/downloads/bar?job={job_id}").text
    assert "keeps its models in X" in done and "Choose it" not in done
