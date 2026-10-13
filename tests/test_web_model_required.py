"""Soft-required AI model: wizard Continue, 'Set up later', app-wide banner, describe guards."""

from __future__ import annotations

import html
import re
import threading

import pytest

from shuto import pipeline
from shuto.web import create_app

from .conftest import FakeOllama
from .test_web import JARGON, Web, main_text


def make_web(cfg, monkeypatch, fake, choose=False):
    monkeypatch.setattr(pipeline, "ollama_client", lambda c: fake)
    local = cfg.model_copy(update={"llm": cfg.llm.model_copy(update={"provider": "ollama"})})
    w = Web(create_app(local))
    pipeline.set_ui_setting(w.state.store, "onboarded", True)
    if choose:
        choose_model(w)
    return w


def choose_model(w):
    w.state.store.set_setting("llm.provider", "ollama")
    w.state.store.set_setting("llm.model", "gemma3:4b")


def page(w, path, **kw):
    """Response text with HTML entities decoded, so apostrophes can be compared."""
    return html.unescape(w.get(path, **kw).text)


@pytest.fixture
def nomodel(cfg, monkeypatch):
    w = make_web(cfg, monkeypatch, FakeOllama(pulled=["gemma3:4b"]))
    yield w
    w.state.runner.shutdown()


@pytest.fixture
def unreachable(tmp_path, monkeypatch):
    from shuto.config import Config

    other = Config.model_validate({"workspace": str(tmp_path / "ws2"), "llm": {"provider": "fake"}})
    w = make_web(other, monkeypatch, FakeOllama(running=False), choose=True)
    yield w
    w.state.runner.shutdown()


def test_banner_shows_until_a_model_is_chosen(nomodel):
    text = page(nomodel, "/model/banner")
    assert "No AI model set" in text and 'href="/settings#model"' in text
    assert 'hx-get="/model/banner"' in page(nomodel, "/help")
    choose_model(nomodel)
    assert page(nomodel, "/model/banner").strip() == ""


def test_banner_is_quiet_in_wizard_and_during_download(nomodel):
    wizard = page(nomodel, "/model/banner", headers={"hx-current-url": "http://x/welcome?step=3"})
    assert wizard.strip() == ""
    release = threading.Event()
    job = nomodel.state.runner.submit("pull", "models", lambda ctx: release.wait(30))
    try:
        assert page(nomodel, "/model/banner").strip() == ""  # the download bar has the slot
    finally:
        release.set()
        nomodel.state.runner.wait(job)
    assert "No AI model set" in page(nomodel, "/model/banner")


def test_banner_stays_quiet_when_local_ai_unreachable(unreachable):
    assert page(unreachable, "/model/banner").strip() == ""
    for path in ("/settings", "/welcome?step=3"):
        assert "can't reach the local AI" in page(unreachable, path)


def test_wizard_continue_needs_a_usable_model(nomodel):
    text = page(nomodel, "/welcome?step=3")
    assert "disabled" in text and 'href="/welcome?step=4">Continue' not in text
    assert "Set up later" in text and "Skip this step" not in text
    choose_model(nomodel)
    ok = page(nomodel, "/welcome?step=3")
    assert 'href="/welcome?step=4">Continue' in ok and 'id="why-wait"' not in ok


def test_wizard_continue_open_when_local_ai_unreachable(unreachable):
    text = page(unreachable, "/welcome?step=3")
    assert 'href="/welcome?step=4">Continue' in text and "can't reach the local AI" in text


def test_set_up_later_needs_confirmation_without_js(nomodel):
    text = page(nomodel, "/welcome?step=3&later=1")
    assert 'id="later"' in text and "Yes, set up later" in text and "No, go back" in text
    r = nomodel.post("/welcome/3", {})  # no confirm, no model: stay on the step
    assert r.status_code == 303 and "step=3" in r.headers["location"]
    r = nomodel.post("/welcome/3", {"later": "yes"})
    assert r.status_code == 303 and "step=4" in r.headers["location"]
    assert nomodel.state.store.get_setting("llm.model") is None


def test_no_jargon_on_new_text(nomodel, unreachable):
    for w in (nomodel, unreachable):
        for path in ("/welcome?step=3", "/welcome?step=3&later=1", "/settings"):
            assert not re.findall(JARGON, main_text(w.get(path).text), flags=re.I), path
    banner = re.sub(r"<[^>]+>", " ", page(nomodel, "/model/banner"))
    assert not re.findall(JARGON, banner, flags=re.I)


def test_describe_is_blocked_without_a_model(nomodel, unreachable):
    nomodel.state.store.upsert_batch("b1")
    r = nomodel.post("/batch/b1/describe")
    assert "No AI model is set" in html.unescape(r.text) and 'id="job-' not in r.text
    unreachable.state.store.upsert_batch("b1")
    r = unreachable.post("/batch/b1/describe")
    assert "can't reach the local AI" in html.unescape(r.text) and 'id="job-' not in r.text


def test_check_is_blocked_and_explained_without_a_model(nomodel, photos_dir):
    # import needs no AI, so the batch exists; Check then must refuse to start
    job, _ = nomodel.run_job("/import", {"path": str(photos_dir), "batch_name": "b1"})
    assert job.state == "done", job.error
    (batch_id,) = [b["id"] for b in pipeline.batch_report(nomodel.state.store)]
    text = page(nomodel, f"/batch/{batch_id}/check")
    assert "No AI model is set" in text and "Fix in Settings" in text and "disabled" in text
    r = nomodel.post(f"/batch/{batch_id}/check")
    assert "No AI model is set" in html.unescape(r.text) and 'id="job-' not in r.text


class CountingFactory:
    def __init__(self, fake):
        self.fake, self.calls = fake, 0

    def __call__(self, cfg):
        self.calls += 1
        return self.fake


def test_status_is_cached_and_cleared_by_any_change(cfg, monkeypatch):
    w = make_web(cfg, monkeypatch, FakeOllama(pulled=["gemma3:4b"]), choose=True)
    factory = CountingFactory(FakeOllama(pulled=["gemma3:4b"]))
    monkeypatch.setattr(pipeline, "ollama_client", factory)
    try:
        w.get("/model/banner")
        first = factory.calls
        assert first == 1
        w.get("/model/banner")
        assert factory.calls == first  # reused, no second round trip
        w.post("/welcome/3", {"later": "yes"})  # any change clears it
        w.get("/model/banner")
        assert factory.calls == first + 1
    finally:
        w.state.runner.shutdown()


def test_cache_is_per_ollama_url(nomodel):
    from shuto.web.common import WebCtx

    state = nomodel.state
    helper = WebCtx(state)
    choose_model(nomodel)
    assert helper.model_status().ready
    assert len(state.model_cache) == 1
    ollama_cfg = state.cfg.llm.ollama.model_copy(update={"url": "http://127.0.0.1:1"})
    llm = state.cfg.llm.model_copy(update={"ollama": ollama_cfg})
    state.cfg = state.cfg.model_copy(update={"llm": llm})
    helper.model_status()
    assert len(state.model_cache) == 2  # a different URL is a separate entry


def test_local_check_really_times_out(cfg, monkeypatch):
    import socket
    import time

    from shuto.llm.ollama import OllamaClient
    from shuto.web import common
    from shuto.web.common import WebCtx

    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(5)  # accepts connections but never answers
    url = f"http://127.0.0.1:{srv.getsockname()[1]}"
    monkeypatch.setattr(common, "CHECK_TIMEOUT_S", 0.4)
    w = make_web(cfg, monkeypatch, FakeOllama(), choose=True)
    monkeypatch.setattr(pipeline, "ollama_client", lambda c: OllamaClient(url, timeout=300))
    try:
        start = time.monotonic()
        status = WebCtx(w.state).model_status()
        assert time.monotonic() - start < 3  # capped at 0.4 s, not the 5 s Ollama default
        assert status.state == "unknown" and "can't reach the local AI" in status.message
    finally:
        srv.close()
        w.state.runner.shutdown()


def api_web(cfg, monkeypatch, *, sdk, key):
    monkeypatch.setattr(pipeline, "_sdk_installed", lambda module: sdk)
    monkeypatch.setattr(pipeline, "get_secret", lambda name: "k" if key else None)
    w = make_web(cfg, monkeypatch, FakeOllama())
    w.state.store.set_setting("llm.provider", "anthropic")
    w.state.store.set_setting("llm.model", "claude-test")
    return w


@pytest.mark.parametrize(
    ("sdk", "key", "banner"), [(True, True, False), (True, False, True), (False, True, True)]
)
def test_online_ai_needs_sdk_and_key(cfg, monkeypatch, sdk, key, banner):
    w = api_web(cfg, monkeypatch, sdk=sdk, key=key)
    try:
        shown = "No AI model set" in page(w, "/model/banner")
        assert shown is banner
    finally:
        w.state.runner.shutdown()


def test_banner_check_does_not_touch_cloud_credentials(cfg, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("must not look up cloud credentials for the banner")

    monkeypatch.setattr(pipeline, "_api_preflight", boom)
    w = api_web(cfg, monkeypatch, sdk=True, key=True)
    try:
        assert page(w, "/model/banner").strip() == ""
    finally:
        w.state.runner.shutdown()


def test_unreadable_choice_is_unknown_not_missing(nomodel, monkeypatch):
    def broken(*a, **k):
        raise pipeline.PipelineError("settings are unreadable")

    monkeypatch.setattr(pipeline, "resolve_selection", broken)
    assert page(nomodel, "/model/banner").strip() == ""
    from shuto.web.common import WebCtx

    status = WebCtx(nomodel.state).model_status()
    assert status.state == "unknown" and "can't tell which AI model" in status.message


def test_banner_only_skips_the_wizard_page_itself(nomodel):
    url = "http://x/settings?next=/welcome"
    assert "No AI model set" in page(nomodel, "/model/banner", headers={"hx-current-url": url})
