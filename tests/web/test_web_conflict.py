"""Another Ollama answering: banner, settings, wizard, doctor, pull error and Help."""

from __future__ import annotations

import pytest

from shuto.ai.providers.base import LLMError
from shuto.ollama import conflict
from shuto.web.ux import friendly_error
from shuto.workflow import models as workflow_models

from ..helpers import FakeOllama
from .test_web_model_required import make_web, page


class OldOllama(FakeOllama):
    def version(self):
        return "0.22.0"

    def pull(self, tag, on_progress=None):
        raise LLMError(
            "Ollama pull failed: pull model manifest: 412: this model requires a newer version",
            retryable=False,
        )


@pytest.fixture
def clash(monkeypatch):
    monkeypatch.setattr(conflict, "installed_version", lambda *a, **k: "0.40.8")
    conflict.clear_installed_cache()


@pytest.fixture
def web(cfg, monkeypatch, clash):
    w = make_web(cfg, monkeypatch, OldOllama())
    yield w
    w.state.runner.shutdown()


def test_banner_explains_the_other_ollama(web):
    text = page(web, "/model/banner")
    assert "0.22.0" in text and "/help#other-ollama" in text and "Choose a model" in text


def test_banner_plain_without_conflict(cfg, monkeypatch):
    w = make_web(cfg, monkeypatch, FakeOllama())
    try:
        assert "No AI model set" in page(w, "/model/banner")
    finally:
        w.state.runner.shutdown()


def test_settings_and_wizard_show_the_steps(web):
    for path in ("/settings", "/welcome?step=3"):
        text = page(web, path)
        assert "Another Ollama may be answering" in text and "Close the other Ollama" in text


def test_help_has_the_entry(web):
    text = page(web, "/help")
    assert 'id="other-ollama"' in text and "wsl -l -v" in text and "docker stop" in text


def test_pull_error_is_rewritten_and_friendly_keeps_it(cfg, clash):
    with pytest.raises(LLMError) as err:
        workflow_models.pull_model(cfg, "gemma3:4b", client=OldOllama())
    assert "0.22.0" in str(err.value) and "412" not in str(err.value)
    assert friendly_error(err.value)["what"] == str(err.value)


def test_other_pull_errors_pass_through(cfg, clash):
    class Broken(OldOllama):
        def pull(self, tag, on_progress=None):
            raise LLMError("Ollama HTTP 500: boom", retryable=True)

    with pytest.raises(LLMError, match="boom"):
        workflow_models.pull_model(cfg, "x", client=Broken())


def test_doctor_report_carries_the_conflict(cfg, store, clash):
    report = workflow_models.doctor(cfg, store, client=OldOllama())
    assert report.conflict and report.conflict.kind == "older_server"
    same = OldOllama()
    same.version = lambda: "0.40.8"
    assert workflow_models.doctor(cfg, store, client=same).conflict is None


def test_doctor_can_skip_the_conflict_check(cfg, store, clash):
    class Counting(OldOllama):
        calls = 0

        def tags(self):
            Counting.calls += 1
            return super().tags()

    report = workflow_models.doctor(cfg, store, client=Counting(), with_conflict=False)
    assert report.conflict is None and Counting.calls == 1


def test_unrelated_stale_manifest_keeps_the_plain_message(cfg, monkeypatch):
    monkeypatch.setattr(conflict, "installed_version", lambda *a, **k: "0.40.8")
    monkeypatch.setattr(conflict, "disk_models", lambda _dir: {"stale:latest"})
    fake = FakeOllama(pulled=["other:latest"])
    fake.version = lambda: "0.40.8"
    w = make_web(cfg, monkeypatch, fake, choose=True)
    try:
        from shuto.web.state import WebCtx

        status = WebCtx(w.state).fresh_model_status()
        assert "not downloaded yet" in status.message
        monkeypatch.setattr(conflict, "disk_models", lambda _dir: {"gemma3:4b"})
        w.state.conflict_cache = None
        status = WebCtx(w.state).fresh_model_status()
        assert "Another Ollama may be answering" in status.message
    finally:
        w.state.runner.shutdown()
