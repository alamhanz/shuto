"""Streamed Ollama answers: assembly, idle timeout, cancel, slow-model hint, UTC job times."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta

import pytest

from shuto.ai.providers import ollama as ollama_mod
from shuto.ai.providers.base import LLMCancelled, LLMError, LLMRequest
from shuto.ai.providers.fake import DEFAULT_RESPONSE, FakeProvider
from shuto.ai.providers.ollama import OllamaClient, OllamaProvider, OllamaTimeout
from shuto.core.jobs import JobRunner
from shuto.core.models import Status
from shuto.web.ux import friendly_error
from shuto.workflow import check_batch, importing, redo

from .test_llm_core import patch_urlopen

GOOD = json.dumps(DEFAULT_RESPONSE)
WAIT = 10  # seconds a test may wait on an Event before it gives up (never a sleep)


def _line(**chunk) -> bytes:
    return json.dumps(chunk).encode() + b"\n"


class StreamResp:
    """A streamed /api/chat reply. `steps` are bytes lines, Events to wait for (a gate) or
    callables to run."""

    def __init__(self, steps):
        self.steps = steps
        self.closed = threading.Event()

    def __iter__(self):
        for step in self.steps:
            if isinstance(step, threading.Event):
                step.wait(WAIT)
            elif callable(step):
                step()
            else:
                yield step

    def close(self):
        self.closed.set()

    def read(self):
        return b"{}"


def _json(payload):
    resp = StreamResp([])
    raw = json.dumps(payload).encode()
    resp.read = lambda: raw
    return resp


def _server(monkeypatch, chat, capabilities=(), ps=None):
    """Fake Ollama. `chat` is a StreamResp (or a list of them, used in turn)."""
    replies = list(chat) if isinstance(chat, list) else [chat]

    def handler(r):
        path = r.full_url.rsplit("/api/", 1)[1]
        if path == "show":
            return _json({"capabilities": list(capabilities)})
        if path == "ps":
            return _json({"models": ps or []})
        return replies.pop(0) if len(replies) > 1 else replies[0]

    return patch_urlopen(monkeypatch, handler)


def _req(cancel=None, timeout_s=300) -> LLMRequest:
    return LLMRequest(system="s", prompt="p", image_jpeg=b"x", cancel=cancel, timeout_s=timeout_s)


def _answer(*pieces, thinking=(), reason="stop") -> StreamResp:
    steps = [_line(message={"thinking": t}, done=False) for t in thinking]
    steps += [_line(message={"content": c}, done=False) for c in pieces]
    steps.append(
        _line(
            message={"content": ""},
            done=True,
            done_reason=reason,
            prompt_eval_count=7,
            eval_count=9,
        )
    )
    return StreamResp(steps)


def test_streamed_chunks_are_assembled(monkeypatch):
    seen = _server(monkeypatch, _answer(GOOD[:10], GOOD[10:], thinking=("a", "b")))
    text, _, in_tok, out_tok = OllamaProvider("m", url="http://x:1").complete(_req(), {})
    assert text == GOOD and (in_tok, out_tok) == (7, 9)
    chat = [json.loads(r.data) for r in seen if r.full_url.endswith("/api/chat")]
    assert chat[0]["stream"] is True
    assert not [r for r in seen if r.full_url.endswith("/api/ps")]  # nothing slow on the happy path


def test_streamed_thinking_that_ran_out_is_retried_with_more_room(monkeypatch):
    first = _answer(thinking=("hmm",), reason="length")
    seen = _server(monkeypatch, [first, _answer(GOOD)], capabilities=["thinking"])
    text, _, _, _ = OllamaProvider("qwen3-vl:8b", url="http://x:1").complete(_req(), {})
    assert text == GOOD
    chats = [json.loads(r.data) for r in seen if r.full_url.endswith("/api/chat")]
    assert len(chats) == 2
    assert chats[1]["options"]["num_predict"] > chats[0]["options"]["num_predict"]


def test_error_chunk_and_early_end_are_errors(monkeypatch):
    _server(monkeypatch, StreamResp([_line(error="boom")]))
    with pytest.raises(LLMError, match="boom"):
        OllamaProvider("m", url="http://x:1").complete(_req(), {})
    _server(monkeypatch, StreamResp([_line(message={"content": "x"}, done=False)]))
    with pytest.raises(LLMError, match="ended the answer early"):
        OllamaProvider("m", url="http://x:1").complete(_req(), {})


def test_idle_timeout_is_a_friendly_timeout(monkeypatch):
    gate = threading.Event()
    stuck = StreamResp([_line(message={"content": "{"}, done=False), gate])
    _server(monkeypatch, stuck)
    try:
        with pytest.raises(OllamaTimeout, match="stalled, no data for") as ei:
            OllamaProvider("m", url="http://x:1", timeout_s=0.3).complete(_req(timeout_s=0.3), {})
    finally:
        gate.set()
    assert friendly_error(ei.value)["what"] == "The AI took too long to answer."
    assert stuck.closed.wait(WAIT)  # the helper thread let go of the connection


def test_first_chunk_gets_the_longer_wait(monkeypatch):
    monkeypatch.setattr(ollama_mod, "FIRST_CHUNK_WAIT_S", 0.3)
    gate = threading.Event()
    stuck = StreamResp([gate])
    _server(monkeypatch, stuck)
    try:
        with pytest.raises(OllamaTimeout, match="no data for 0 s|no data for 1 s"):
            OllamaClient("http://x:1", timeout=0.1).chat("m", [], timeout=0.1)
    finally:
        gate.set()


def test_cancel_closes_the_stream_and_raises(monkeypatch):
    gate = threading.Event()
    got_first = threading.Event()
    resp = StreamResp([_line(message={"thinking": "hm"}, done=False), gate, _line(done=True)])
    _server(monkeypatch, resp)
    cancel_now = threading.Event()

    def cancel():
        got_first.set()
        return cancel_now.is_set()

    cancel_now.set()
    try:
        with pytest.raises(LLMCancelled):
            OllamaProvider("m", url="http://x:1").complete(_req(cancel=cancel), {})
    finally:
        gate.set()
    assert got_first.is_set()
    assert resp.closed.wait(WAIT)


def test_cancel_is_noticed_while_the_stream_is_quiet(monkeypatch):
    gate = threading.Event()
    resp = StreamResp([gate])
    _server(monkeypatch, resp)
    polled = threading.Event()

    def cancel():
        polled.set()
        return True

    try:
        with pytest.raises(LLMCancelled):
            OllamaClient("http://x:1").chat("m", [], cancel=cancel)
    finally:
        gate.set()
    assert polled.is_set() and resp.closed.wait(WAIT)


def _ready_with_old_text(cfg, store, photos_dir):
    (batch,) = importing.import_folder(cfg, store, photos_dir).batches
    check_batch.check_batch(cfg, store, batch, llm=FakeProvider("fake"))
    ids = [p.id for p in store.list_photos(batch, [Status.DESCRIBED])]
    redo.send_back(cfg, store, batch, ids)
    return batch, ids


def test_cancelled_redo_job_ends_cancelled_and_keeps_old_text(cfg, store, photos_dir, monkeypatch):
    batch, ids = _ready_with_old_text(cfg, store, photos_dir)
    before = {i: store.get_photo(i).metadata["description"] for i in ids}
    gate, started = threading.Event(), threading.Event()
    resp = StreamResp([_line(message={"thinking": "hm"}, done=False), started.set, gate])
    _server(monkeypatch, resp)
    llm = OllamaProvider("minicpm-v:8b", url="http://x:1")
    runner = JobRunner(store)
    job_id = runner.submit(
        "redo",
        batch,
        lambda job: redo.run_redo(
            cfg, store, batch, llm=llm, progress=job.progress, cancel=job.cancelled
        ),
    )
    try:
        assert started.wait(WAIT)
        runner.cancel(job_id)
        job = runner.wait(job_id, timeout=WAIT)
    finally:
        gate.set()
        runner.shutdown()
    assert job.state == "cancelled" and job.error is None
    assert resp.closed.wait(WAIT)
    for pid in ids:
        photo = store.get_photo(pid)
        assert photo.status == Status.READY  # still waiting
        assert photo.metadata["description"] == before[pid]  # never half-written


# ---- the slow-model hint ----
SPILL = {"name": "qwen3-vl:8b", "model": "qwen3-vl:8b", "size": 6_500, "size_vram": 5_000}


def _stalled_chat(monkeypatch, model, ps):
    gate = threading.Event()
    seen = _server(monkeypatch, StreamResp([gate]), ps=ps)
    monkeypatch.setattr(ollama_mod, "FIRST_CHUNK_WAIT_S", 0.2)
    try:
        OllamaClient("http://x:1", timeout=0.2).chat(model, [], timeout=0.2)
    except OllamaTimeout as e:
        return e, seen
    finally:
        gate.set()
    raise AssertionError("no timeout")


def test_hint_appears_when_the_model_spills_to_the_processor(monkeypatch):
    err, seen = _stalled_chat(monkeypatch, "qwen3-vl:8b", [SPILL])
    msg = str(err)
    assert "qwen3-vl:8b does not fit in your graphics card memory" in msg
    assert "minicpm-v:8b" in msg and "Settings" in msg
    assert [r.full_url for r in seen if r.full_url.endswith("/api/ps")]
    fe = friendly_error(err)
    assert fe["what"] == "The AI took too long to answer." and "minicpm-v:8b" in fe["todo"]


def test_no_hint_when_the_model_fits_or_is_not_listed(monkeypatch):
    fits = {**SPILL, "size_vram": 6_500}
    for ps in ([fits], [], [{**SPILL, "name": "other:1", "model": "other:1"}]):
        err, _ = _stalled_chat(monkeypatch, "qwen3-vl:8b", ps)
        assert "graphics card" not in str(err)
        assert friendly_error(err)["todo"].startswith("Wait a minute")


def test_hint_does_not_name_a_model_missing_from_the_catalog(monkeypatch):
    custom = {**SPILL, "name": "my-own:7b", "model": "my-own:7b"}
    err, _ = _stalled_chat(monkeypatch, "my-own:7b", [custom])
    assert "my-own" not in str(err).split("Ollama timed out")[1].split(". ", 1)[1]
    assert "This model does not fit" in str(err)


# ---- job times ----
def test_finished_at_is_utc_with_offset_like_started_at(store):
    runner = JobRunner(store)
    job_id = runner.submit("noop", "b", lambda job: "ok")
    try:
        job = runner.wait(job_id, timeout=WAIT)
    finally:
        runner.shutdown()
    started, finished = (datetime.fromisoformat(t) for t in (job.started_at, job.finished_at))
    assert started.utcoffset() == timedelta(0) and finished.utcoffset() == timedelta(0)
    assert job.finished_at.endswith("+00:00")
    assert abs((datetime.now(finished.tzinfo) - finished).total_seconds()) < 60
