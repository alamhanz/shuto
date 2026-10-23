"""Send back to the AI: it runs in the background while the owner keeps reviewing."""

from __future__ import annotations

import re
import threading
from urllib.parse import quote, unquote

import pytest

from shuto.ai import selection
from shuto.ai.providers.base import LLMError
from shuto.ai.providers.fake import FakeProvider
from shuto.core.models import Status
from shuto.web.state import WebCtx

from .test_web_batch import _photos, batch, described, web  # noqa: F401


class Gate(FakeProvider):
    """A fake AI that holds every answer until the test opens the gate."""

    def __init__(self):
        super().__init__("fake")
        self.gate = threading.Event()
        self.entered = threading.Event()

    def complete(self, req, schema):
        self.entered.set()
        assert self.gate.wait(30), "the test never opened the gate"
        return super().complete(req, schema)


@pytest.fixture
def gate(monkeypatch):
    g = Gate()
    monkeypatch.setattr(selection, "resolve_llm", lambda cfg, store=None, *a, **k: g)
    yield g
    g.gate.set()


def _bulk(w, batch_id, action, ids, **extra):
    data = {"action": action, "ids": [str(i) for i in ids], **extra}
    return w.post(f"/batch/{batch_id}/bulk", data)


def _text(r):
    return unquote(r.headers["location"])


def _finish(w, batch_id):
    job_id = w.state.runner.active_job(batch_id)
    assert job_id is not None
    return w.state.runner.wait(job_id, timeout=60)


def _all_ids(w, batch_id):
    return [p.id for p in w.state.store.list_photos(batch_id)]


def test_send_back_all_leaves_out_what_shutterstock_has(web, described, gate):  # noqa: F811
    store = web.state.store
    ids = _all_ids(web, described)
    on_shutterstock = [p.id for p in _photos(web, described, Status.DESCRIBED)][:2]
    for pid in on_shutterstock:
        store.set_status(pid, Status.EXPORTED, force=True)
    r = _bulk(web, described, "ready", ids)
    assert r.status_code == 303
    note = _text(r)
    assert "2 photos are already on Shutterstock and left out" in note
    assert "not allowed" not in note and "3 photos sent back" in note
    assert web.state.runner.active_job(described) is not None
    assert len(_photos(web, described, Status.READY)) == 3
    gate.gate.set()
    assert _finish(web, described).state == "done"
    assert not _photos(web, described, Status.READY)
    assert {p.id for p in _photos(web, described, Status.EXPORTED)} == set(on_shutterstock)


def test_review_stays_usable_while_the_ai_works(web, described, gate):  # noqa: F811
    store = web.state.store
    good = _photos(web, described, Status.DESCRIBED)
    sent, keep = good[:2], good[2]
    total = len(_all_ids(web, described))
    r = _bulk(web, described, "ready", [p.id for p in sent])
    assert r.status_code == 303 and "2 photos sent back" in _text(r)

    page = web.get(f"/batch/{described}/review?show=good").text
    assert keep.orig_name in page  # the others are still there, with nothing locked
    assert not any(p.orig_name in page for p in sent)
    assert "The AI is working on 2 photos" in page and "(0 done)" in page
    assert 'hx-trigger="every 2s"' in page
    assert f"/batch/{described}/ai-status?seen={total - 2}" in page

    r = web.post(f"/photo/{keep.id}/approve", {})  # editing and approving go on meanwhile
    assert r.status_code == 303
    assert store.get_photo(keep.id).status == Status.APPROVED
    assert "working on 2 photos" in web.get(f"/batch/{described}/ai-status?seen={total - 2}").text

    gate.gate.set()
    assert _finish(web, described).state == "done"
    back = web.get(f"/batch/{described}/ai-status?seen={total - 2}&show=good").text
    assert "2 photos are back from the AI" in back
    assert 'hx-trigger="every 2s"' not in back  # the polling stops
    assert f"/batch/{described}/review?show=good" in back
    assert "back from the AI" not in web.get(f"/batch/{described}/ai-status?seen={total}").text
    page = web.get(f"/batch/{described}/review?show=good").text
    assert all(p.orig_name in page for p in sent)


def test_sending_more_joins_the_running_job(web, described, gate):  # noqa: F811
    store = web.state.store
    first, second, third = (p.id for p in _photos(web, described, Status.DESCRIBED))
    web.state.cfg.llm.concurrency = 1
    _bulk(web, described, "ready", [first])
    job_id = web.state.runner.active_job(described)
    assert gate.entered.wait(10)
    r = _bulk(web, described, "ready", [second, third])
    assert "should pick them up" in _text(r) and "Run the AI now" in _text(r)
    assert web.state.runner.active_job(described) == job_id  # no second job
    assert "working on" in web.get(f"/batch/{described}/ai-status").text
    gate.gate.set()
    assert web.state.runner.wait(job_id, timeout=60).state == "done"
    assert not _photos(web, described, Status.READY)
    assert len(gate.calls) == 3  # each photo was written once
    with store._read() as conn:
        n = conn.execute("SELECT COUNT(*) FROM jobs WHERE kind='redo'").fetchone()[0]
    assert n == 1


def test_approve_all_only_approves_photos_that_were_on_screen(web, described):  # noqa: F811
    store = web.state.store
    page = web.get(f"/batch/{described}/review?show=good").text
    clean = re.findall(r'name="clean_ids" value="(\d+)"', page)
    assert len(clean) == 3
    late = _photos(web, described, Status.CULLED)[0]
    store.set_status(late.id, Status.DESCRIBED, force=True)  # came back after the page was drawn
    r = _bulk(web, described, "approve_all", [], clean_ids=clean)
    assert r.status_code == 303
    assert {str(p.id) for p in _photos(web, described, Status.APPROVED)} == set(clean)
    assert store.get_photo(late.id).status == Status.DESCRIBED


def test_approve_all_skips_photos_that_changed_status_since(web, described):  # noqa: F811
    store = web.state.store
    page = web.get(f"/batch/{described}/review?show=good").text
    clean = re.findall(r'name="clean_ids" value="(\d+)"', page)
    flagged, sent = int(clean[0]), int(clean[1])
    store.set_status(flagged, Status.NEEDS_REVIEW)
    store.set_status(sent, Status.READY)
    _bulk(web, described, "approve_all", [], clean_ids=clean)
    assert store.get_photo(flagged).status == Status.NEEDS_REVIEW
    assert store.get_photo(sent).status == Status.READY
    assert {str(p.id) for p in _photos(web, described, Status.APPROVED)} == set(clean[2:])


def test_approve_all_without_the_list_approves_nothing(web, described):  # noqa: F811
    r = _bulk(web, described, "approve_all", [])
    assert "Reload the page" in _text(r)
    assert not _photos(web, described, Status.APPROVED)


def test_approve_all_ignores_ids_of_other_batches(web, described):  # noqa: F811
    store = web.state.store
    store.upsert_batch("other")
    pid = store.add_photo(sha256="e" * 64, src_path="/x/o.jpg", orig_name="o.jpg", batch_id="other")
    store.set_status(pid, Status.DESCRIBED, force=True)
    _bulk(web, described, "approve_all", [], clean_ids=[str(pid)])
    assert store.get_photo(pid).status == Status.DESCRIBED


def test_sent_cards_have_no_checkbox(web, described):  # noqa: F811
    store = web.state.store
    sent, other = _photos(web, described, Status.DESCRIBED)[:2]
    store.set_status(sent.id, Status.EXPORTED, force=True)
    page = web.get(f"/batch/{described}/review?show=all").text
    assert f'value="{sent.id}" form="bulk-form"' not in page
    assert f'value="{other.id}" form="bulk-form"' in page
    assert "Sent" in page


def test_use_this_photo_after_all_starts_the_ai(web, described, gate):  # noqa: F811
    culled = _photos(web, described, Status.CULLED)[0]
    r = web.post(f"/photo/{culled.id}/ready", {})
    assert r.status_code == 303 and "sent back" in _text(r)
    assert web.state.runner.active_job(described) is not None
    gate.gate.set()
    assert _finish(web, described).state == "done"
    assert web.state.store.get_photo(culled.id).status != Status.READY


def test_blocked_model_keeps_photos_waiting_with_a_run_button(web, described, monkeypatch):  # noqa: F811
    monkeypatch.setattr(WebCtx, "model_block", lambda self, request: "No AI model is set.")
    victim = _photos(web, described, Status.DESCRIBED)[0]
    r = _bulk(web, described, "ready", [victim.id])
    note = _text(r)
    assert "1 photo sent back" in note and "The AI cannot run now: No AI model is set." in note
    assert web.state.runner.active_job(described) is None
    assert web.state.store.get_photo(victim.id).status == Status.READY
    page = web.get(f"/batch/{described}/review").text
    assert "1 photo is waiting for the AI" in page and "Run the AI now" in page
    assert victim.orig_name not in page
    fragment = web.post(f"/batch/{described}/redo", {"seen": "4"}).text
    assert "No AI model is set." in fragment and "Run the AI now" in fragment


def test_run_the_ai_now_writes_the_waiting_photos(web, described, gate):  # noqa: F811
    victim = _photos(web, described, Status.DESCRIBED)[0]
    web.state.store.set_status(victim.id, Status.READY)  # waiting, no job (as after a closed app)
    page = web.get(f"/batch/{described}/review").text
    assert "Run the AI now" in page and "hx-post" in page
    fragment = web.post(f"/batch/{described}/redo", {"seen": "4", "show": "good"}).text
    assert "The AI is working on 1 photo" in fragment
    gate.gate.set()
    assert _finish(web, described).state == "done"
    assert web.state.store.get_photo(victim.id).status != Status.READY


def test_run_the_ai_now_with_nothing_waiting(web, described):  # noqa: F811
    fragment = web.post(f"/batch/{described}/redo", {}).text
    assert "No photos are waiting" in fragment
    assert web.state.runner.active_job(described) is None


def test_nothing_to_send_back_when_everything_is_on_shutterstock(web, described):  # noqa: F811
    store = web.state.store
    ids = _all_ids(web, described)
    for pid in ids:
        store.set_status(pid, Status.EXPORTED, force=True)
    r = _bulk(web, described, "ready", ids)
    note = _text(r)
    assert "Nothing to send back" in note and "already on Shutterstock" in note
    assert web.state.runner.active_job(described) is None
    assert not _photos(web, described, Status.READY)


def test_stopping_the_job_keeps_finished_photos_and_offers_a_rerun(web, described, gate):  # noqa: F811
    web.state.cfg.llm.concurrency = 1
    ids = [p.id for p in _photos(web, described, Status.DESCRIBED)]
    _bulk(web, described, "ready", ids)
    job_id = web.state.runner.active_job(described)
    assert gate.entered.wait(10)
    assert web.post(f"/jobs/{job_id}/cancel").status_code == 200
    gate.gate.set()
    assert web.state.runner.wait(job_id, timeout=60).state == "cancelled"
    assert len(_photos(web, described, Status.READY)) == 2  # the first one finished and stayed
    page = web.get(f"/batch/{described}/review").text
    assert "2 photos are waiting for the AI" in page and "Run the AI now" in page


def test_failed_photo_comes_back_under_needs_a_look(web, described, monkeypatch):  # noqa: F811
    def fail(req):
        raise LLMError("the model used all its tokens on thinking")

    broken = FakeProvider("fake", response=fail)
    monkeypatch.setattr(selection, "resolve_llm", lambda cfg, store=None, *a, **k: broken)
    victim = _photos(web, described, Status.DESCRIBED)[0]
    old = victim.metadata["description"]
    _bulk(web, described, "ready", [victim.id])
    assert _finish(web, described).state == "done"
    photo = web.state.store.get_photo(victim.id)
    assert photo.status == Status.NEEDS_REVIEW and photo.metadata["description"] == old
    page = web.get(f"/batch/{described}/review?show=attention").text
    assert victim.orig_name in page and "could not write new text" in page
    assert "waiting for the AI" not in page


def test_review_does_not_wait_for_other_jobs(web, described):  # noqa: F811
    release = threading.Event()
    job_id = web.state.runner.submit("deliver", described, lambda ctx: release.wait(30))
    try:
        keep = _photos(web, described, Status.DESCRIBED)[0]
        assert web.get(f"/batch/{described}/review").status_code == 200
        assert _bulk(web, described, "approve", [keep.id]).status_code == 303
        assert web.state.store.get_photo(keep.id).status == Status.APPROVED
        other = _photos(web, described, Status.DESCRIBED)[0]
        note = _text(_bulk(web, described, "ready", [other.id]))
        assert "Another task is running" in note
        assert "Another task is running" in web.get(f"/batch/{described}/review").text
    finally:
        release.set()
    web.state.runner.wait(job_id)


def test_new_urls_work_for_odd_batch_names(web):  # noqa: F811
    store = web.state.store
    name = "Trip été 2026"
    store.upsert_batch(name)
    pid = store.add_photo(sha256="d" * 64, src_path="/x/p.jpg", orig_name="p.jpg", batch_id=name)
    store.set_status(pid, Status.READY, force=True)
    page = web.get(f"/batch/{quote(name, safe='')}/review").text
    assert f"/batch/{quote(name, safe='')}/redo" in page
    r = web.get(f"/batch/{quote(name, safe='')}/ai-status?seen=0")
    assert r.status_code == 200 and "waiting for the AI" in r.text
    assert web.get("/batch/nope/ai-status").status_code == 404
    assert web.post("/batch/nope/redo", {}).status_code == 404


def test_app_start_marks_leftover_jobs_as_interrupted(cfg):
    from fastapi.testclient import TestClient

    from shuto.web import create_app

    app = create_app(cfg)
    store = app.state.web.store
    job_id = store.create_job("redo", "b1", 4)
    store.update_job(job_id, state="running")
    with TestClient(app, base_url="http://127.0.0.1:8765"):
        job = store.get_job(job_id)
    assert job.state == "failed" and "shuto was closed" in job.error
