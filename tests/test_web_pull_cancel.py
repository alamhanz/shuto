"""Cancelling and failing model downloads, with a fake Ollama that streams like the real one."""

from __future__ import annotations

import json
import re
import threading
from types import SimpleNamespace

import pytest

from shuto import pipeline
from shuto.llm.ollama import OllamaClient
from shuto.web.common import WebCtx

from .conftest import FakeOllama
from .test_web_model_required import make_web, page


def line(**ev) -> bytes:
    return json.dumps(ev).encode() + b"\n"


PROGRESS = line(status="pulling abc", total=100_000_000, completed=40_000_000)
SUCCESS = line(status="success")


class Stream:
    """What urlopen returns: iterable lines. Items: bytes, a threading.Event to wait for,
    or an exception to raise. `closed` tells whether the client hung up."""

    def __init__(self, script):
        self.script = list(script)
        self.closed = False
        self.reached_gate = threading.Event()

    def __iter__(self):
        for item in self.script:
            if isinstance(item, threading.Event):
                self.reached_gate.set()
                assert item.wait(30)
            elif isinstance(item, BaseException):
                raise item
            else:
                yield item

    def close(self):
        self.closed = True


class StreamingOllama(FakeOllama):
    """FakeOllama whose pull is the real OllamaClient.pull reading a scripted stream."""

    timeout = 300

    def __init__(self, script, **kw):
        super().__init__(**kw)
        self.stream = Stream(script)

    def _open(self, path, body=None, timeout=None, method=None):
        assert path == "/api/pull"
        return self.stream

    pull = OllamaClient.pull


def setup(cfg, monkeypatch, script, pulled=("gemma3:4b",)):
    fake = StreamingOllama(script, pulled=list(pulled))
    web = make_web(cfg, monkeypatch, fake)
    tag = next(r.tag for r in pipeline.list_models(cfg) if r.tag not in pulled and r.fits_hardware)
    return web, fake, tag


def start(web, tag, back="/settings"):
    r = web.post("/settings/pull", {"tag": tag, "confirm": "yes", "return_to": back})
    return int(re.search(r'data-job="(\d+)"', r.text).group(1))


def test_cancel_mid_stream_closes_the_stream_and_keeps_nothing(cfg, monkeypatch):
    gate = threading.Event()
    web, fake, tag = setup(cfg, monkeypatch, [PROGRESS, gate, PROGRESS, SUCCESS])
    try:
        job_id = start(web, tag, back="/welcome?step=3")
        assert fake.stream.reached_gate.wait(30)
        web.post(f"/jobs/{job_id}/cancel", {})
        gate.set()
        job = web.state.runner.wait(job_id)
        assert job.state == "cancelled" and fake.stream.closed
        assert tag not in fake.pulled
        text = web.get(f"/downloads/bar?job={job_id}").text
        assert "Download stopped" in text and "Try again" in text
        assert web.state.store.get_setting("llm.model") in (None, "")
    finally:
        gate.set()
        web.state.runner.shutdown()


def test_cancel_racing_completion_reports_the_truth(cfg, monkeypatch):
    gate = threading.Event()
    web, fake, tag = setup(cfg, monkeypatch, [PROGRESS, gate, SUCCESS])
    try:
        job_id = start(web, tag, back="/welcome?step=3")
        assert fake.stream.reached_gate.wait(30)
        web.post(f"/jobs/{job_id}/cancel", {})
        fake.pulled.append(tag)  # Ollama finished the model in the meantime
        gate.set()
        job = web.state.runner.wait(job_id)
        assert job.state == "done"
        text = web.get(f"/downloads/bar?job={job_id}").text
        assert "finished before it could be stopped" in text
        assert web.state.store.get_setting("llm.model") in (None, "")  # not chosen
    finally:
        gate.set()
        web.state.runner.shutdown()


def test_cancel_after_the_last_event_is_not_reported_as_cancelled(cfg, monkeypatch):
    gate = threading.Event()
    web, fake, tag = setup(cfg, monkeypatch, [PROGRESS, SUCCESS, gate])
    try:
        job_id = start(web, tag)
        assert fake.stream.reached_gate.wait(30)
        web.post(f"/jobs/{job_id}/cancel", {})
        gate.set()
        assert web.state.runner.wait(job_id).state == "done"
        assert "finished before it could be stopped" in web.get(f"/downloads/bar?job={job_id}").text
    finally:
        gate.set()
        web.state.runner.shutdown()


@pytest.mark.parametrize(
    ("script", "words"),
    [
        ([PROGRESS, ConnectionResetError("reset")], "interrupted"),
        ([PROGRESS], "interrupted"),  # the stream just ends: never a silent success
        ([PROGRESS, TimeoutError("timed out")], "stalled"),
        ([PROGRESS, line(error="write blob: no space left on device")], "not enough free space"),
    ],
)
def test_broken_downloads_end_failed_with_plain_words(cfg, monkeypatch, script, words):
    web, fake, tag = setup(cfg, monkeypatch, script)
    try:
        job_id = start(web, tag)
        job = web.state.runner.wait(job_id)
        assert job.state == "failed" and fake.stream.closed
        text = web.get(f"/downloads/bar?job={job_id}").text
        assert words in text and "Try again" in text and "every 2s" not in text
        assert "Error" not in text and "Traceback" not in text
    finally:
        web.state.runner.shutdown()


def test_wizard_continue_opens_and_model_is_chosen_when_the_download_ends(cfg, monkeypatch):
    gate = threading.Event()
    web, fake, tag = setup(cfg, monkeypatch, [PROGRESS, gate, SUCCESS], pulled=())
    try:
        assert 'href="/welcome?step=4">Continue' not in page(web, "/welcome?step=3")
        job_id = start(web, tag, back="/welcome?step=3")
        assert fake.stream.reached_gate.wait(30)
        text = page(web, "/welcome?step=3")
        assert 'href="/welcome?step=4"' in text and f"Downloading {tag}..." in text
        assert "No AI model set" not in page(web, "/model/banner")
        assert f"Downloading {tag}" in page(web, "/downloads/bar")
        fake.pulled.append(tag)
        gate.set()
        assert web.state.runner.wait(job_id).state == "done"
        assert web.state.store.get_setting("llm.model") == tag
        assert "now the AI model shuto uses" in web.get(f"/downloads/bar?job={job_id}").text
        assert web.state.pending_model is None
    finally:
        gate.set()
        web.state.runner.shutdown()


def test_download_does_not_replace_a_model_chosen_meanwhile(cfg, monkeypatch):
    gate = threading.Event()
    web, fake, tag = setup(cfg, monkeypatch, [PROGRESS, gate, SUCCESS])
    try:
        job_id = start(web, tag, back="/welcome?step=3")
        assert fake.stream.reached_gate.wait(30)
        web.post("/welcome/3", {"spec": "gemma3:4b"})  # a downloaded model, chosen meanwhile
        fake.pulled.append(tag)
        gate.set()
        assert web.state.runner.wait(job_id).state == "done"
        assert web.state.store.get_setting("llm.model") == "gemma3:4b"
    finally:
        gate.set()
        web.state.runner.shutdown()


def test_describe_still_refuses_while_the_model_downloads(cfg, monkeypatch):
    gate = threading.Event()
    web, fake, tag = setup(cfg, monkeypatch, [PROGRESS, gate, SUCCESS], pulled=())
    try:
        start(web, tag, back="/welcome?step=3")
        assert fake.stream.reached_gate.wait(30)

        request = SimpleNamespace(state=SimpleNamespace())
        block = WebCtx(web.state).model_block(request)
        assert block and f"Downloading {tag}" in block  # the guard still says no
    finally:
        gate.set()
        web.state.runner.shutdown()
