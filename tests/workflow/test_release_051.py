"""Fixes after 0.5.0 (shipped in 0.6.0): thinking models, keyword top-up, warmer retries."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from shuto.ai.describe import describe_photo
from shuto.ai.prompts import Style, topup_prompt
from shuto.ai.providers.base import LLMError, LLMRequest
from shuto.ai.providers.fake import DEFAULT_RESPONSE, FakeProvider
from shuto.ai.providers.ollama import OllamaClient, OllamaProvider
from shuto.cli import cmd_prepare
from shuto.core import paths
from shuto.core.models import Status
from shuto.metadata import guardrails
from shuto.workflow import delivering

from ..ai.test_llm_core import FakeResp, patch_urlopen
from .test_pipeline_batch import _approved

R = guardrails.load_restrictions()


def _req() -> LLMRequest:
    return LLMRequest(system="s", prompt="p", image_jpeg=b"x", max_tokens=100, temperature=0.2)


def _server(monkeypatch, capabilities, chat_reply):
    def handler(r):
        if r.full_url.endswith("/api/show"):
            return FakeResp({"capabilities": capabilities})
        return FakeResp(chat_reply)

    return patch_urlopen(monkeypatch, handler)


def _chats(seen):
    return [json.loads(r.data) for r in seen if r.full_url.endswith("/api/chat")]


# ------------------------------------------------------------------ 1: thinking models


def test_think_false_is_sent_to_models_that_can_think(monkeypatch):
    seen = _server(
        monkeypatch, ["completion", "vision", "thinking"], {"message": {"content": "{}"}}
    )
    client = OllamaClient("http://x:1")
    client.chat("qwen3.5:4b", [])
    client.chat("qwen3.5:4b", [])
    assert [b["think"] for b in _chats(seen)] == [False, False]
    assert sum(r.full_url.endswith("/api/show") for r in seen) == 1  # asked once, then cached


def test_think_is_not_sent_to_models_without_the_capability(monkeypatch):
    seen = _server(monkeypatch, ["completion", "vision"], {"message": {"content": "{}"}})
    OllamaClient("http://x:1").chat("minicpm-v:8b", [])
    assert "think" not in _chats(seen)[0]


def test_failed_capability_lookup_sends_no_think_and_is_asked_again(monkeypatch):
    calls = []

    import urllib.error

    def fake(request, timeout=None):
        calls.append(request.full_url)
        if request.full_url.endswith("/api/show"):
            raise urllib.error.HTTPError(request.full_url, 500, "x", {}, None)
        return FakeResp({"message": {"content": "{}"}})

    monkeypatch.setattr("urllib.request.urlopen", fake)
    client = OllamaClient("http://x:1")
    client.chat("m", [])
    client.chat("m", [])
    assert calls.count("http://x:1/api/show") == 2


def test_empty_answer_with_thinking_is_a_clear_retryable_error(monkeypatch):
    _server(
        monkeypatch,
        ["thinking"],
        {"message": {"content": "", "thinking": "hmm"}, "done_reason": "stop"},
    )
    with pytest.raises(LLMError, match="thinking") as ei:
        OllamaProvider("qwen3.5:4b", url="http://x:1").complete(_req(), {})
    assert ei.value.retryable is True


def test_thinking_that_runs_out_twice_is_not_retryable(monkeypatch):
    _server(
        monkeypatch,
        ["thinking"],
        {"message": {"content": "", "thinking": "hmm"}, "done_reason": "length"},
    )
    with pytest.raises(LLMError, match="thinks before it answers") as ei:
        OllamaProvider("qwen3.5:4b", url="http://x:1").complete(_req(), {})
    assert ei.value.retryable is False


def test_empty_answer_without_thinking_stays_empty_text(monkeypatch):
    _server(monkeypatch, [], {"message": {"content": ""}})
    text, _, _, _ = OllamaProvider("m", url="http://x:1").complete(_req(), {})
    assert text == ""


# ------------------------------------------------------------------ 2: keyword top-up


def _resp(**over):
    return {**DEFAULT_RESPONSE, **over}


def _describe(llm, **kw):
    return describe_photo(llm, b"j", "a.jpg", style=Style(), restrictions=R, **kw)


FEW = ["street", "morning", "japan", "city", "tokyo"]
MORE = [f"detail {c}" for c in "abcdefghijklmnopqrst"]


def test_too_few_keywords_asks_for_more_and_merges():
    llm = FakeProvider(response=[_resp(keywords=FEW), _resp(keywords=[*MORE, "street", "tokyo"])])
    res = _describe(llm)
    assert len(llm.calls) == 2
    prompt = llm.calls[1].prompt
    assert "street, morning, japan, city, tokyo" in prompt
    assert "ONLY 20 NEW" in prompt and "English only" in prompt
    assert llm.calls[1].temperature > llm.calls[0].temperature
    kws = res.metadata.keywords
    assert kws[:5] == FEW and len(kws) == 25 and len(set(kws)) == 25
    assert res.metadata.description == DEFAULT_RESPONSE["description"]
    assert not [i for i in res.issues if i.hard]
    assert res.llm.retries == 1


def test_topup_keeps_asking_until_enough():
    llm = FakeProvider(
        response=[_resp(keywords=FEW), _resp(keywords=MORE[:1]), _resp(keywords=MORE)]
    )
    res = _describe(llm)
    assert len(llm.calls) == 3
    assert "ONLY 19 NEW" in llm.calls[2].prompt and "detail a" in llm.calls[2].prompt
    assert len(res.metadata.keywords) >= 25


def test_topup_without_new_keywords_stops_after_two_repeats_and_flags():
    llm = FakeProvider(response=[_resp(keywords=FEW), _resp(keywords=FEW)])
    res = _describe(llm)
    assert len(llm.calls) == 3
    assert any("Could not be fixed" in w for w in res.metadata.warnings)


def test_topup_prompt_follows_language_and_short_setting():
    english = topup_prompt("P", "desc", ["a", "b"], 5, Style())
    assert "ONLY 5 NEW" in english and "English only" in english and english.startswith("P")
    short = topup_prompt("P", "desc", ["a", "b"], 5, Style(), short=True)
    assert len(short) < len(english) and "Follow the keyword rules" not in short
    french = topup_prompt("P", "desc", ["a"], 3, Style(english_only=False, language_name="French"))
    assert "French" in french


# ------------------------------------------------------------------ 4: prepare warning


def test_prepare_warns_about_an_approved_photo_with_a_hard_failure(cfg, store, photos_dir):
    batch = _approved(cfg, store, photos_dir)
    bad = store.list_photos(batch, [Status.APPROVED])[0]
    store.update_photo(bad.id, metadata={**bad.metadata, "keywords": ["street", "tokyo"]})
    summary = delivering.prepare_batch(cfg, store, batch)
    assert bad.orig_name and len(summary.prepared) == 3  # not blocked
    assert list(summary.warnings) == [bad.id]
    assert "Only 2 keywords" in summary.warnings[bad.id][0]
    assert "Warning: 1 prepared photo(s)" in summary.warning_text()


def test_prepare_has_no_warning_for_good_photos(cfg, store, photos_dir):
    batch = _approved(cfg, store, photos_dir)
    summary = delivering.prepare_batch(cfg, store, batch)
    assert summary.prepared and not summary.warnings and summary.warning_text() == ""


def test_cli_prepare_prints_the_warning(monkeypatch, capsys):
    from types import SimpleNamespace

    summary = delivering.PrepareSummary(prepared=["a.jpg"], warnings={7: ["Only 2 keywords (x)"]})
    monkeypatch.setattr(delivering, "prepare_batch", lambda *a, **k: summary)
    monkeypatch.setattr(paths, "batch_dir", lambda *a: Path("d"))
    ctx = SimpleNamespace(
        args=SimpleNamespace(batch="b", ids=None),
        run_cfg=None,
        store=None,
        cfg=None,
    )
    cmd_prepare(ctx)
    assert (
        "WARNING photo 7 prepared with a problem Shutterstock will reject"
        in capsys.readouterr().out
    )
