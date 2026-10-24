"""Safer model Remove: not during a describe job, a no-JavaScript confirm page, bad settings."""

# ruff: noqa: F811  (fixtures imported from test_web)
from __future__ import annotations

import re
import threading

from shuto.ai import selection
from shuto.core import errors

from .test_web import JARGON, _setup_three, main_text, ollama, visible_text, web  # noqa: F401

BUSY = "Wait for shuto to finish writing titles and keywords"


def _two_models(web, ollama):
    ollama.pulled = ["gemma3:4b", "gemma3:12b"]
    web.post("/settings/use", {"spec": "gemma3:4b"})


class Blocker:
    """A job that holds the one worker thread until released."""

    def __init__(self, web, kind, batch_id):
        self.web = web
        self.gate = threading.Event()
        self.started = threading.Event()

        def hold(job):
            self.started.set()
            self.gate.wait(10)

        self.id = web.state.runner.submit(kind, batch_id, hold)

    def release(self):
        self.gate.set()
        self.web.state.runner.wait(self.id)


def test_remove_refused_while_titles_are_being_written(web, ollama):
    _two_models(web, ollama)
    hold = Blocker(web, "describe", "some-batch")
    try:
        r = web.post("/settings/model/remove", {"tag": "gemma3:12b", "confirm": "yes"})
        assert r.status_code == 303 and "error=" in r.headers["location"]
        assert "finish%20writing%20titles" in r.headers["location"]
        assert ollama.delete_calls == []
        got = web.get("/settings/model/remove?tag=gemma3:12b")
        assert got.status_code == 303 and "error=" in got.headers["location"]
    finally:
        hold.release()
    r = web.post("/settings/model/remove", {"tag": "gemma3:12b", "confirm": "yes"})
    assert "notice=" in r.headers["location"] and ollama.delete_calls == ["gemma3:12b"]


def test_remove_refused_while_a_check_is_running(web, ollama):
    _two_models(web, ollama)
    hold = Blocker(web, "check", "some-batch")
    try:
        r = web.post("/settings/model/remove", {"tag": "gemma3:12b", "confirm": "yes"})
        assert "error=" in r.headers["location"] and ollama.delete_calls == []
    finally:
        hold.release()


def _replace_setup(web, ollama, monkeypatch, tmp_path):
    _setup_three(web, ollama, tmp_path, monkeypatch)
    return "qwen3-vl:8b"


def test_replace_refused_while_titles_are_being_written(web, ollama, monkeypatch, tmp_path):
    tag = _replace_setup(web, ollama, monkeypatch, tmp_path)
    hold = Blocker(web, "describe", "some-batch")
    try:
        r = web.post("/settings/pull", {"tag": tag, "replace": "gemma3:12b", "confirm": "yes"})
        assert r.status_code == 200 and BUSY in r.text
        assert ollama.delete_calls == [] and ollama.pull_calls == []
    finally:
        hold.release()


def test_replace_job_checks_again_before_deleting(web, ollama, monkeypatch, tmp_path):
    tag = _replace_setup(web, ollama, monkeypatch, tmp_path)
    hold = Blocker(web, "export", "other-batch")  # keeps the replace job waiting in the queue
    assert hold.started.wait(5)
    r = web.post("/settings/pull", {"tag": tag, "replace": "gemma3:12b", "confirm": "yes"})
    assert r.status_code == 200, r.text
    job_id = int(re.search(r'(?:id="job-|data-job=")(\d+)"', r.text).group(1))
    # a describe job starts while the replace job is still queued
    late = web.state.runner.submit("describe", "late-batch", lambda job: None)
    hold.release()
    job = web.state.runner.wait(job_id)
    web.state.runner.wait(late)
    assert job.state == "failed" and BUSY in (job.error or "")
    assert ollama.delete_calls == [] and ollama.pull_calls == []
    assert "gemma3:12b" in ollama.pulled


def test_confirm_page_is_read_only_and_posts_to_remove(web, ollama):
    _two_models(web, ollama)
    r = web.get("/settings/model/remove?tag=gemma3:12b&return_to=/settings")
    assert r.status_code == 200
    assert ollama.delete_calls == []
    text = main_text(r.text)
    assert "gemma3:12b will be deleted from this computer. You can download it again later." in text
    assert 'action="/settings/model/remove"' in r.text and 'method="post"' in r.text
    assert f'name="csrf_token" value="{web.state.csrf}"' in r.text
    assert 'name="confirm" value="yes"' in r.text
    assert not re.search(r"<script(?![^>]*\bsrc=)", r.text) and " style=" not in r.text
    assert not re.findall(JARGON, visible_text(r.text), flags=re.I)
    form = {"tag": "gemma3:12b", "return_to": "/settings", "confirm": "yes"}
    done = web.post("/settings/model/remove", form)
    assert done.status_code == 303 and done.headers["location"].startswith("/settings?notice=")
    assert ollama.delete_calls == ["gemma3:12b"]


def test_settings_remove_button_works_without_javascript(web, ollama):
    _two_models(web, ollama)
    html = web.get("/settings?all=1").text
    assert 'method="get" action="/settings/model/remove"' in html
    assert "Remove gemma3:12b from this computer?" in html  # hx-confirm kept for JS users


def test_confirm_page_checks_tag_and_return_page(web, ollama):
    _two_models(web, ollama)
    assert web.get("/settings/model/remove?tag=bad%20tag%3Bx").status_code == 404
    assert web.get("/settings/model/remove").status_code == 422
    r = web.get("/settings/model/remove?tag=gemma3:12b&return_to=https://evil.example/")
    assert r.status_code == 200 and 'name="return_to" value="/settings"' in r.text
    assert "evil.example" not in r.text
    r = web.get("/settings/model/remove?tag=gemma3:12b&return_to=/welcome?step=3")
    assert 'name="return_to" value="/welcome?step=3"' in r.text
    # the model in use gets a confirm page with a warning
    assert "uses now" in web.get("/settings/model/remove?tag=gemma3:4b").text
    # not downloaded, or not on the list: sent back with a reason, nothing to confirm
    for tag in ("llava:7b", "my-own-model:latest"):
        r = web.get(f"/settings/model/remove?tag={tag}")
        assert r.status_code == 303 and "error=" in r.headers["location"]
    r = web.get("/settings/model/remove?tag=llava:7b&return_to=//evil.example")
    assert r.headers["location"].startswith("/settings?error=")


def test_settings_page_survives_an_unreadable_model_choice(web, cfg, monkeypatch):
    def boom(*a, **k):
        raise errors.PipelineError("No default model for provider 'nope'.")

    monkeypatch.setattr(selection, "resolve_selection", boom)
    r = web.get("/settings")
    assert r.status_code == 200
    assert "shuto can&#39;t tell which AI model is set. Choose one below." in r.text or (
        "shuto can't tell which AI model is set. Choose one below." in r.text
    )
    assert not re.findall(JARGON, main_text(r.text), flags=re.I)


def test_settings_page_has_no_hint_when_the_model_choice_is_fine(web):
    assert "can't tell which AI model" not in web.get("/settings").text


def test_remove_accepts_the_csrf_header_alone(web, ollama):
    _two_models(web, ollama)
    r = web.post(
        "/settings/model/remove",
        {"tag": "gemma3:12b", "confirm": "yes"},
        token=False,
        headers={"X-CSRF-Token": web.state.csrf},
    )
    assert r.status_code == 303 and ollama.delete_calls == ["gemma3:12b"]


def test_remove_without_a_token_is_refused(web, ollama):
    _two_models(web, ollama)
    r = web.post("/settings/model/remove", {"tag": "gemma3:12b", "confirm": "yes"}, token=False)
    assert r.status_code == 403 and ollama.delete_calls == []
