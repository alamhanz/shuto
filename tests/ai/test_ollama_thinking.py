"""Thinking models (qwen3-vl:8b is thinking-only) get room to think and one bigger retry."""

from __future__ import annotations

import json

import pytest

from shuto.ai.providers.base import LLMError, LLMRequest
from shuto.ai.providers.fake import DEFAULT_RESPONSE
from shuto.ai.providers.ollama import THINKING_HEADROOM, OllamaProvider
from shuto.core.models import Status
from shuto.web.ux import friendly_error
from shuto.workflow import describing, importing

from .test_llm_core import FakeResp, patch_urlopen

GOOD = json.dumps(DEFAULT_RESPONSE)
THINKING_ONLY = {"message": {"content": "", "thinking": "hmm"}, "done_reason": "length"}
ANSWER = {"message": {"content": GOOD}, "done_reason": "stop", "eval_count": 600}


def _req(max_tokens: int = 1500) -> LLMRequest:
    return LLMRequest(
        system="s", prompt="p", image_jpeg=b"x", max_tokens=max_tokens, temperature=0.2
    )


def _server(monkeypatch, capabilities, replies):
    """Fake Ollama: /api/show lists `capabilities`; each /api/chat takes the next reply
    (a callable gets the request body)."""
    queue = list(replies)

    def handler(r):
        if r.full_url.endswith("/api/show"):
            return FakeResp({"capabilities": capabilities})
        reply = queue.pop(0) if len(queue) > 1 else queue[0]
        return FakeResp(reply(json.loads(r.data)) if callable(reply) else reply)

    return patch_urlopen(monkeypatch, handler)


def _chats(seen):
    return [json.loads(r.data) for r in seen if r.full_url.endswith("/api/chat")]


def test_thinking_model_gets_headroom_and_plain_model_does_not(monkeypatch):
    seen = _server(monkeypatch, ["completion", "vision", "thinking"], [ANSWER])
    OllamaProvider("qwen3.5:4b", url="http://x:1").complete(_req(), {})
    assert _chats(seen)[0]["options"]["num_predict"] == 1500 + THINKING_HEADROOM

    seen = _server(monkeypatch, ["completion", "vision"], [ANSWER])
    OllamaProvider("minicpm-v:8b", url="http://x:1").complete(_req(), {})
    assert _chats(seen)[0]["options"]["num_predict"] == 1500


def test_out_of_tokens_retries_once_with_a_bigger_budget(monkeypatch):
    seen = _server(monkeypatch, ["thinking"], [THINKING_ONLY, ANSWER])
    text, _, _, _ = OllamaProvider("qwen3-vl:8b", url="http://x:1").complete(_req(), {})
    assert json.loads(text) == DEFAULT_RESPONSE
    first, second = (c["options"]["num_predict"] for c in _chats(seen))
    assert first == 1500 + THINKING_HEADROOM
    assert second == 1500 + 2 * THINKING_HEADROOM


def test_thinking_despite_think_false_raises_num_ctx_for_the_next_call(monkeypatch):
    seen = _server(monkeypatch, ["thinking"], [THINKING_ONLY, ANSWER])
    llm = OllamaProvider("qwen3-vl:8b", url="http://x:1")
    assert llm.client.always_thinks("qwen3-vl:8b") is False
    llm.complete(_req(), {})
    assert llm.client.always_thinks("qwen3-vl:8b") is True
    first, second = (c["options"]["num_ctx"] for c in _chats(seen))
    assert first == llm.num_ctx and second == llm.num_ctx + THINKING_HEADROOM


def test_small_memory_num_ctx_is_not_raised(monkeypatch):
    seen = _server(monkeypatch, ["thinking"], [THINKING_ONLY, ANSWER])
    llm = OllamaProvider("qwen3-vl:8b", url="http://x:1", num_ctx=4096)
    llm.complete(_req(), {})
    assert [c["options"]["num_ctx"] for c in _chats(seen)] == [4096, 4096]


def test_model_that_honours_think_false_never_raises_num_ctx(monkeypatch):
    _server(monkeypatch, ["thinking"], [ANSWER])
    llm = OllamaProvider("qwen3.5:4b", url="http://x:1")
    llm.complete(_req(), {})
    assert llm.client.always_thinks("qwen3.5:4b") is False


def test_two_length_failures_give_a_final_error_with_a_plain_hint(monkeypatch):
    seen = _server(monkeypatch, ["thinking"], [THINKING_ONLY])
    with pytest.raises(LLMError, match="thinks before it answers") as ei:
        OllamaProvider("qwen3-vl:8b", url="http://x:1").complete(_req(), {})
    assert ei.value.retryable is False
    assert "minicpm-v:8b" in str(ei.value) and "Settings" in str(ei.value)
    assert len(_chats(seen)) == 2
    assert friendly_error(ei.value)["what"] == (
        "This AI model spends its time thinking and ran out of room."
    )
    assert friendly_error(ei.value)["todo"] == "Choose another model in Settings."


def test_describe_batch_with_the_owner_failure_replayed_describes_the_photo(
    cfg, store, photos_dir, monkeypatch
):
    def reply(body):  # like the owner's qwen3-vl:8b: not enough room means thinking only
        room = body["options"]["num_predict"]
        return ANSWER if room >= 1500 + 2 * THINKING_HEADROOM else THINKING_ONLY

    seen = _server(monkeypatch, ["thinking"], [reply])
    bid = next(iter(importing.import_folder(cfg, store, photos_dir).batches))
    for p in store.list_photos(bid):
        store.set_status(p.id, Status.READY, force=True)
    llm = OllamaProvider("qwen3-vl:8b", url="http://x:1")
    summary = describing.describe_batch(cfg, store, bid, llm=llm)
    assert summary.failed == 0 and summary.aborted is None
    assert summary.described + summary.needs_review == len(store.list_photos(bid))
    assert len(_chats(seen)) >= 2
