"""Whose fault a failed AI call was: only the model's own failures count against its rating."""

from __future__ import annotations

import io
import urllib.error

import pytest

from shuto.ai import catalog, hardware, rating
from shuto.ai.providers.base import LLMError, LLMRequest
from shuto.ai.providers.ollama import OllamaClient, OllamaProvider, OllamaTimeout
from shuto.core.models import Status
from shuto.workflow import describing, importing
from shuto.workflow import models as workflow_models

from .test_llm_core import FakeResp, patch_urlopen
from .test_rating import OWNER

THINKING_ONLY = {"message": {"content": "", "thinking": "hmm"}, "done_reason": "length"}


def _req() -> LLMRequest:
    return LLMRequest(system="s", prompt="p", image_jpeg=b"x")


def _kind_of(monkeypatch, handler) -> tuple[str, str]:
    patch_urlopen(monkeypatch, handler)
    with pytest.raises(LLMError) as ei:
        OllamaProvider("m", url="http://x:1").complete(_req(), {})
    return ei.value.kind, str(ei.value)


def _raise(exc):
    def handler(request):
        raise exc

    return handler


def test_ollama_not_running_is_a_connection_failure(monkeypatch):
    kind, _ = _kind_of(monkeypatch, _raise(urllib.error.URLError(ConnectionRefusedError())))
    assert kind == "connection"


def test_a_model_that_is_not_downloaded_is_a_missing_failure(monkeypatch):
    err = urllib.error.HTTPError(
        "http://x:1/api/chat", 404, "Not Found", {}, io.BytesIO(b'{"error": "not found"}')
    )
    kind, text = _kind_of(monkeypatch, _raise(err))
    assert kind == "missing" and "404" in text


def test_a_server_error_is_the_models(monkeypatch):
    err = urllib.error.HTTPError(
        "http://x:1/api/chat", 500, "Oops", {}, io.BytesIO(b'{"error": "runner crashed"}')
    )
    assert _kind_of(monkeypatch, _raise(err))[0] == "model"


def test_a_timeout_is_the_models(monkeypatch):
    kind, _ = _kind_of(monkeypatch, _raise(TimeoutError()))
    assert kind == "model" and OllamaTimeout("x").kind == "model"


def test_thinking_that_ran_out_of_room_is_the_models(monkeypatch):
    def handler(r):
        if r.full_url.endswith("/api/show"):
            return FakeResp({"capabilities": ["thinking"]})
        return FakeResp(THINKING_ONLY)

    patch_urlopen(monkeypatch, handler)
    with pytest.raises(LLMError) as ei:
        OllamaProvider("qwen3-vl:8b", url="http://x:1").complete(_req(), {})
    assert ei.value.kind == "model"


def test_an_unusable_answer_is_the_models(monkeypatch):
    def handler(r):
        if r.full_url.endswith("/api/show"):
            return FakeResp({"capabilities": []})
        return FakeResp({"message": {"content": "not json at all"}, "done_reason": "stop"})

    patch_urlopen(monkeypatch, handler)
    from shuto.ai.providers.base import describe_structured

    with pytest.raises(LLMError, match="invalid output") as ei:
        describe_structured(OllamaProvider("m", url="http://x:1"), _req(), retries=1)
    assert ei.value.kind == "model"


class Failing:
    name = "ollama"
    model = "minicpm-v:8b"
    default_image_side = 256
    default_concurrency = 1

    def __init__(self, error: LLMError):
        self.error = error

    def complete(self, req, schema):
        raise self.error


def _describe_with(cfg, store, photos_dir, error: LLMError) -> list[dict]:
    bid = next(iter(importing.import_folder(cfg, store, photos_dir).batches))
    for p in store.list_photos(bid):
        store.set_status(p.id, Status.READY, force=True)
    describing.describe_batch(cfg, store, bid, llm=Failing(error))
    return store.recent_llm_calls("ollama")


def test_the_call_log_starts_a_failure_with_its_kind(cfg, store, photos_dir):
    rows = _describe_with(
        cfg,
        store,
        photos_dir,
        LLMError("Ollama is not running", retryable=False, kind="connection"),
    )
    assert rows and all(r["error"].startswith("[connection] Ollama is not running") for r in rows)
    assert not any(rating.caused_by_model(r["error"]) for r in rows)


def test_ollama_stopped_during_a_run_does_not_hurt_the_rating(cfg, store, photos_dir):
    rows = _describe_with(
        cfg,
        store,
        photos_dir,
        LLMError("Ollama is not running", retryable=False, kind="connection"),
    )
    rows = rows * 3
    measured = rating.measured_from_calls(rows)
    assert all(m.failures == 0 for m in measured.values())


def test_the_models_own_failures_are_logged_and_counted(cfg, store, photos_dir):
    rows = _describe_with(cfg, store, photos_dir, OllamaTimeout("Ollama timed out"))
    assert rows and all(r["error"].startswith("[model] ") for r in rows)
    rows = rows * 3
    (m,) = rating.measured_from_calls(rows).values()
    assert m.failures >= 3


class QuietClient(OllamaClient):
    """A real client that never touches the network."""

    def ps(self, timeout=10):
        return []

    def tags(self):
        return []

    def show(self, model, timeout=10):
        return {"capabilities": []}


def test_a_model_seen_thinking_anyway_is_remembered_by_later_clients(cfg, monkeypatch):
    def handler(r):
        if r.full_url.endswith("/api/show"):
            return FakeResp({"capabilities": ["thinking"]})
        return FakeResp({"message": {"content": "{}", "thinking": "hmm"}, "done_reason": "stop"})

    patch_urlopen(monkeypatch, handler)
    OllamaClient("http://x:1").chat("qwen3.5:4b", [])  # one page or job ...
    assert OllamaClient("http://x:1").always_thinks("qwen3.5:4b")  # ... and a later one
    assert OllamaClient("http://x:1").always_thinks("qwen3.5:4b:latest")
    assert not OllamaClient("http://other:1").always_thinks("qwen3.5:4b")
    assert not OllamaClient("http://x:1").always_thinks("minicpm-v:8b")


def test_the_rating_sees_a_model_that_thinks_anyway_from_a_new_client(cfg, store, monkeypatch):
    def handler(r):
        if r.full_url.endswith("/api/show"):
            return FakeResp({"capabilities": ["thinking"]})
        return FakeResp({"message": {"content": "{}", "thinking": "hmm"}, "done_reason": "stop"})

    patch_urlopen(monkeypatch, handler)
    OllamaClient("http://x:1").chat("gemma3:4b", [])
    status = hardware.OllamaStatus(True, True, "v", "http://x:1", [], [])
    facts, _ = workflow_models.model_facts(cfg, store, QuietClient("http://x:1"), status)
    assert facts["gemma3:4b"].always_thinks
    assert not facts["minicpm-v:8b"].always_thinks
    row = rating.rate(
        next(m for m in catalog.load_catalog()[0] if m.tag == "gemma3:4b"),
        OWNER,
        facts["gemma3:4b"],
    )
    assert row.thinks and row.rating != "good"
