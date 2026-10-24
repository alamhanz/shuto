"""Check again with AI on the photo page: start, compare, use or keep, and the busy state."""

from __future__ import annotations

import re
import threading
from pathlib import Path
from urllib.parse import unquote

import pytest

from shuto.ai import selection
from shuto.ai.providers.fake import DEFAULT_RESPONSE, FakeProvider
from shuto.core.models import Status
from shuto.settings import ui as settings_ui
from shuto.web import create_app

from ..helpers import FakeOllama
from ..workflow.test_uploaded_memory import uploaded  # noqa: F401 (fixture)
from .test_web import Web

NEW_TEXT = "A brand new quiet street in Tokyo at dawn"


class Gate(FakeProvider):
    """A fake AI that holds every answer until the test opens the gate."""

    def __init__(self):
        super().__init__("fake", response={**DEFAULT_RESPONSE, "description": NEW_TEXT})
        self.gate = threading.Event()
        self.gate.set()

    def complete(self, req, schema):
        assert self.gate.wait(30), "the test never opened the gate"
        return super().complete(req, schema)


@pytest.fixture
def ai(monkeypatch):
    g = Gate()
    monkeypatch.setattr(selection, "resolve_llm", lambda cfg, store=None, *a, **k: g)
    yield g
    g.gate.set()


@pytest.fixture
def web(cfg, monkeypatch, ai):
    monkeypatch.setattr(selection, "ollama_client", lambda c: FakeOllama(pulled=["gemma3:4b"]))
    w = Web(create_app(cfg))
    settings_ui.set_ui_setting(w.state.store, "onboarded", True)
    yield w
    w.state.runner.shutdown()


def _first(web, batch):
    return web.state.store.list_photos(batch, [Status.UPLOADED, Status.EXPORTED])[0]


def _start(web, pid):
    r = web.post(f"/photo/{pid}/recheck")
    assert r.status_code == 200, r.text
    found = re.search(r'id="job-(\d+)"', r.text)
    if found:  # read from the answer: the job may already be gone when the test looks
        web.job_id = int(found.group(1))
    return r


def _finish(web, batch=None):
    return web.state.runner.wait(web.job_id, timeout=60)


def test_sent_photo_page_offers_check_again(web, uploaded):  # noqa: F811
    p = _first(web, uploaded)
    text = web.get(f"/photo/{p.id}").text
    assert "Check again with AI" in text and f'hx-post="/photo/{p.id}/recheck"' in text
    assert "only its text in the Shutterstock info file" in text


def test_not_sent_photo_has_no_check_again(web, uploaded):  # noqa: F811
    p = _first(web, uploaded)
    web.state.store.set_status(p.id, Status.APPROVED, force=True)
    assert "Check again with AI" not in web.get(f"/photo/{p.id}").text


def test_submitted_photo_says_the_text_is_changed_on_the_site(web, uploaded):  # noqa: F811
    p = _first(web, uploaded)
    web.state.store.set_status(p.id, Status.SUBMITTED, force=True)
    text = web.get(f"/photo/{p.id}").text
    assert "can only be changed on the Shutterstock site" in text
    assert f"/photo/{p.id}/recheck" not in text


def test_full_flow_compare_then_use(web, uploaded):  # noqa: F811
    p = _first(web, uploaded)
    old = p.metadata["description"]
    _start(web, p.id)
    job = _finish(web, uploaded)
    assert job.state == "done", job.error
    now = web.state.store.get_photo(p.id)
    assert now.draft["state"] == "ready" and now.status == p.status
    page = web.get(f"/photo/{p.id}").text
    assert "Old text" in page and "New text" in page and old in page and NEW_TEXT in page
    assert "Use the new text" in page and "Keep the old text" in page
    r = web.post(
        f"/photo/{p.id}/draft/use",
        {"description": NEW_TEXT, "keywords": ", ".join(now.draft["metadata"]["keywords"])},
    )
    assert r.status_code == 303
    note = unquote(r.headers["location"])
    assert "New text saved" in note and "Upload > CSV" in note
    after = web.state.store.get_photo(p.id)
    assert after.draft is None and after.metadata["description"] == NEW_TEXT
    assert after.status == Status.EXPORTED
    assert NEW_TEXT in Path(after.export_file).read_text(encoding="utf-8")


def test_keep_the_old_text(web, uploaded):  # noqa: F811
    p = _first(web, uploaded)
    _start(web, p.id)
    _finish(web, uploaded)
    r = web.post(f"/photo/{p.id}/draft/drop")
    assert r.status_code == 303 and "Kept the old text" in unquote(r.headers["location"])
    now = web.state.store.get_photo(p.id)
    assert now.draft is None and now.metadata == p.metadata


def test_new_posts_need_the_csrf_token(web, uploaded):  # noqa: F811
    p = _first(web, uploaded)
    for path in ("recheck", "draft/use", "draft/drop"):
        assert web.post(f"/photo/{p.id}/{path}", token=False).status_code == 403
    assert web.state.store.get_photo(p.id).draft is None


def test_use_with_non_english_text_is_refused_with_a_message(web, uploaded):  # noqa: F811
    p = _first(web, uploaded)
    _start(web, p.id)
    _finish(web, uploaded)
    r = web.post(
        f"/photo/{p.id}/draft/use", {"description": "東京の静かな通り", "keywords": "a, b"}
    )
    assert r.status_code == 303 and "error=" in r.headers["location"]
    assert web.state.store.get_photo(p.id).draft["state"] == "ready"


def test_buttons_are_disabled_while_the_ai_runs(web, ai, uploaded):  # noqa: F811
    a, b, _c = web.state.store.list_photos(uploaded, [Status.UPLOADED, Status.EXPORTED])
    ai.gate.clear()
    _start(web, a.id)
    page = web.get(f"/photo/{b.id}").text
    assert "The AI is running" in page
    assert re.search(r"<button[^>]*disabled[^>]*>Check again with AI", page)
    r = _start(web, b.id)  # a second click queues up behind the running job
    assert "this photo is next" in r.text
    assert web.state.store.get_photo(b.id).draft == {"state": "waiting"}
    waiting = web.get(f"/photo/{a.id}").text
    assert "The AI is writing new text" in waiting and 'hx-trigger="every 3s"' in waiting
    ai.gate.set()
    _finish(web, uploaded)
    assert web.state.store.get_photo(a.id).draft["state"] == "ready"
    assert web.state.store.get_photo(b.id).draft["state"] == "ready"


def test_upload_and_submit_pages_list_new_text_waiting(web, uploaded):  # noqa: F811
    p = _first(web, uploaded)
    assert "new text waiting" not in web.get(f"/batch/{uploaded}/submit").text
    _start(web, p.id)
    _finish(web, uploaded)
    for page in ("upload", "submit"):
        text = web.get(f"/batch/{uploaded}/{page}").text
        assert "has new text waiting for you" in text and f'href="/photo/{p.id}"' in text


def test_failed_draft_shows_a_plain_message_and_try_again(web, ai, uploaded, monkeypatch):  # noqa: F811
    from shuto.ai.providers.base import LLMError

    def boom(req, schema):
        raise LLMError("the model used all its tokens on thinking")

    monkeypatch.setattr(ai, "complete", boom)
    p = _first(web, uploaded)
    _start(web, p.id)
    _finish(web, uploaded)
    page = web.get(f"/photo/{p.id}").text
    assert "could not write new text" in page and "Try again" in page
    assert "old text is kept" in page


def _hold_job(web, batch, kind):
    """A job of this kind that stays running until the returned event is set."""
    release = threading.Event()
    started = threading.Event()

    def run(job):
        started.set()
        release.wait(30)
        return "ok"

    job_id = web.state.runner.submit(kind, batch, run)
    assert started.wait(10)
    return job_id, release


def test_another_kind_of_task_does_not_promise_the_photo_is_next(web, uploaded):  # noqa: F811
    p = _first(web, uploaded)
    job_id, release = _hold_job(web, uploaded, "deliver")
    try:
        r = _start(web, p.id)
        assert "this photo is next" not in r.text
        assert "Another task is running" in r.text and "Press Check again with AI" in r.text
        page = web.get(f"/photo/{p.id}").text
        assert "The AI is writing new text" not in page
        assert not re.search(r"<button[^>]*disabled[^>]*>Check again with AI", page)
    finally:
        release.set()
    web.state.runner.wait(job_id, timeout=60)


def test_use_the_new_text_waits_for_a_running_upload(web, uploaded):  # noqa: F811
    p = _first(web, uploaded)
    _start(web, p.id)
    _finish(web)
    now = web.state.store.get_photo(p.id)
    job_id, release = _hold_job(web, uploaded, "deliver")
    try:
        r = web.post(
            f"/photo/{p.id}/draft/use",
            {"description": NEW_TEXT, "keywords": ", ".join(now.draft["metadata"]["keywords"])},
        )
        assert r.status_code == 303
        assert "Wait until the upload has finished" in unquote(r.headers["location"])
        after = web.state.store.get_photo(p.id)
        assert after.draft["state"] == "ready" and after.metadata == p.metadata
    finally:
        release.set()
    web.state.runner.wait(job_id, timeout=60)
