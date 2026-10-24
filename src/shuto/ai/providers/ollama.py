"""Ollama client (stdlib urllib only) and the local vision provider."""

from __future__ import annotations

import base64
import http.client
import json
import queue
import socket
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

from shuto.ai.catalog import NUM_CTX_TARGET, SMALL_CONTEXT_TOKENS, context_tokens, load_catalog
from shuto.ai.providers.base import LLMCancelled, LLMError, LLMRequest
from shuto.ai.recommend import suggest_model

DEFAULT_URL = "http://127.0.0.1:11434"
DEFAULT_NUM_CTX = NUM_CTX_TARGET  # Ollama's own default (2048/4096) cuts the start of a long prompt
THINKING_HEADROOM = 4096  # extra num_predict for models that may think (num_predict is only a cap)
FIRST_CHUNK_WAIT_S = 600  # Ollama may spend minutes loading the model before the first chunk
MAX_ANSWER_S = 1800  # one answer may never take longer than this, however steadily it streams
CANCEL_POLL_S = 0.25  # how often a quiet stream looks at the cancel check

# (address, tag) of models seen thinking although `think: false` was asked. Kept for the whole run
# (not per client) because every page and job makes its own client.
_ALWAYS: set[tuple[str, str]] = set()


def forget_always_thinking() -> None:
    """Forget which models were seen thinking anyway (tests)."""
    _ALWAYS.clear()


class OllamaTimeout(LLMError):
    """Ollama stopped sending data (or the whole answer took too long)."""

    def __init__(self, message: str):
        super().__init__(message, retryable=True, kind="model")


def _close(resp: Any) -> None:
    close = getattr(resp, "close", None)
    if close:
        close()


def _socket_of(resp: Any) -> Any:
    """The socket under a urllib response (None for anything else or once it is closed)."""
    return getattr(getattr(getattr(resp, "fp", None), "raw", None), "_sock", None)


def _shutdown(resp: Any) -> None:
    """Cut the connection under `resp` from another thread. A read blocked on it wakes at once,
    and Ollama sees the client leave, so it stops working on the request. The thread that owns
    `resp` still closes it."""
    sock = _socket_of(resp)
    if sock is None:
        return
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass  # already closed or reset: nothing left to cut


class OllamaClient:
    def __init__(self, url: str = DEFAULT_URL, timeout: float = 300):
        self.url = url.rstrip("/")
        self.timeout = timeout
        self._thinking: dict[str, bool] = {}

    def _not_running(self) -> LLMError:
        return LLMError(
            f"Ollama is not running at {self.url}; start it or run `shuto doctor`",
            retryable=False,
            kind="connection",
        )

    def _timeout(self) -> LLMError:
        return OllamaTimeout(f"Ollama timed out at {self.url}")

    def _open(
        self,
        path: str,
        body: dict | None = None,
        timeout: float | None = None,
        method: str | None = None,
    ):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(
            self.url + path,
            data=data,
            method=method or ("POST" if body is not None else "GET"),
            headers={"Content-Type": "application/json"},
        )
        try:
            return urllib.request.urlopen(req, timeout=timeout or self.timeout)
        except urllib.error.HTTPError as e:
            try:
                detail = json.loads(e.read().decode("utf-8", "replace")).get("error", "")
            except Exception:
                detail = ""
            msg = f"Ollama HTTP {e.code}: {detail or e.reason}"
            if e.code == 404 and body and body.get("model"):
                msg += f" (model not pulled? run `shuto models pull {body['model']}`)"
            kind = "missing" if e.code == 404 else "model" if e.code >= 500 else "other"
            raise LLMError(msg, retryable=e.code >= 500, kind=kind) from e
        except urllib.error.URLError as e:
            if isinstance(e.reason, TimeoutError):
                raise self._timeout() from e
            raise self._not_running() from e
        except TimeoutError as e:
            raise self._timeout() from e
        except OSError as e:
            raise self._not_running() from e

    def _json(self, path: str, body: dict | None = None, timeout: float | None = None) -> dict:
        resp = self._open(path, body, timeout)
        try:
            try:
                raw = resp.read()
            except TimeoutError as e:
                raise self._timeout() from e
        finally:
            _close(resp)
        try:
            return json.loads(raw.decode("utf-8"))
        except ValueError as e:
            raise LLMError(
                "Ollama returned a non-JSON response", retryable=True, kind="other"
            ) from e

    def version(self) -> str:
        return str(self._json("/api/version", timeout=min(5, self.timeout)).get("version", ""))

    def tags(self) -> list[dict]:
        return list(self._json("/api/tags", timeout=min(10, self.timeout)).get("models", []))

    def delete(self, tag: str) -> None:
        """Remove a downloaded model from this computer (Ollama frees its files)."""
        _close(self._open("/api/delete", {"model": tag}, timeout=60, method="DELETE"))

    def show(self, model: str, timeout: float = 10) -> dict:
        """What Ollama knows about a downloaded model (its capabilities among other things)."""
        return self._json("/api/show", {"model": model}, timeout=timeout)

    def ps(self, timeout: float = 10) -> list[dict]:
        return list(self._json("/api/ps", timeout=timeout).get("models", []))

    def slow_model_hint(self, model: str) -> str:
        """Plain advice when `model` only partly fits in graphics card memory ("" otherwise).
        One short /api/ps call; only used after a timeout, never on the happy path."""
        try:
            running = self.ps(timeout=3)
        except LLMError:
            return ""
        names = {model, f"{model}:latest"}
        for m in running:
            if (m.get("name") in names or m.get("model") in names) and (
                0 < int(m.get("size_vram") or 0) < int(m.get("size") or 0)
            ):
                break
        else:
            return ""
        who = model if model in {lm.tag for lm in load_catalog()[0]} else "This model"
        hint = (
            f"{who} does not fit in your graphics card memory, so part of it runs on the "
            "processor and is very slow."
        )
        better = suggest_model(model)
        if better:
            hint += f" Pick {better} in Settings, which suits this computer better."
        return hint

    def pull(self, tag: str, on_progress: Callable[[dict], None] | None = None) -> None:
        """Download a model. Anything raised by `on_progress` (a cancel) stops the stream: the
        response is closed at once, so Ollama sees the client leave and aborts the download.
        Partly downloaded files stay on disk, so a later pull continues where this one stopped.
        Raises LLMError if the stream breaks, stalls or ends without Ollama's `success`."""
        resp = self._open("/api/pull", {"model": tag, "stream": True}, timeout=self.timeout)
        # A cancel is noticed at the next progress event (on_progress raises). During a silent
        # stretch, such as verifying a big file, it waits for the next event or the timeout.
        finished = False
        try:
            try:
                for line in resp:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        ev = json.loads(line.decode("utf-8"))
                    except ValueError:
                        continue
                    if ev.get("error"):
                        raise LLMError(f"Ollama pull failed: {ev['error']}", retryable=False)
                    if ev.get("status") == "success":
                        finished = True
                    if on_progress:
                        on_progress(ev)
            except TimeoutError as e:
                raise LLMError(
                    f"The download stalled: no data from Ollama for {self.timeout:.0f} s",
                    retryable=True,
                ) from e
            except (OSError, http.client.HTTPException) as e:
                raise LLMError(
                    f"The download connection was lost ({type(e).__name__})", retryable=True
                ) from e
            if not finished:
                raise LLMError("The download connection was lost: it ended early", retryable=True)
        finally:
            _close(resp)

    def supports_thinking(self, model: str) -> bool:
        """Whether Ollama lists the `thinking` capability for this model (asked once per model;
        a failed lookup counts as no and is asked again next time)."""
        if model not in self._thinking:
            try:
                caps = self._json("/api/show", {"model": model}, timeout=min(10, self.timeout))
            except LLMError:
                return False
            self._thinking[model] = "thinking" in (caps.get("capabilities") or [])
        return self._thinking[model]

    def always_thinks(self, model: str) -> bool:
        """True once this model sent thinking although `think: false` was asked (some builds
        only think and ignore the switch)."""
        return self._always_key(model) in _ALWAYS

    def _always_key(self, model: str) -> tuple[str, str]:
        return (self.url, model.removesuffix(":latest"))

    def chat(
        self,
        model: str,
        messages: list[dict],
        format: Any = None,
        options: dict | None = None,
        keep_alive: str | None = None,
        timeout: float | None = None,
        cancel: Callable[[], bool] | None = None,
    ) -> dict:
        """One answer, streamed. `timeout` is the idle limit: no data from Ollama for that many
        seconds is a stall (the first chunk gets FIRST_CHUNK_WAIT_S while the model loads).
        `cancel` is checked on every chunk and every CANCEL_POLL_S while it is quiet; a cancel
        closes the connection (Ollama then stops generating) and raises LLMCancelled."""
        body: dict[str, Any] = {"model": model, "messages": messages, "stream": True}
        think_off = self.supports_thinking(model)
        if think_off:
            body["think"] = False  # else the hidden thinking eats the whole token budget
        if format is not None:
            body["format"] = format
        if options:
            body["options"] = options
        if keep_alive:
            body["keep_alive"] = keep_alive
        try:
            resp = self._stream_chat(body, timeout or self.timeout, cancel)
        except OllamaTimeout as e:
            hint = self.slow_model_hint(model)
            if hint:
                raise OllamaTimeout(f"{e}. {hint}") from e
            raise
        if think_off and (resp.get("message") or {}).get("thinking"):
            _ALWAYS.add(self._always_key(model))
        return resp

    def _stream_chat(
        self, body: dict, idle: float, cancel: Callable[[], bool] | None
    ) -> dict[str, Any]:
        """Read the NDJSON stream into one reply shaped like the old non-streamed one.

        A helper thread owns the connection and does the blocking reads; this thread waits for
        its chunks, so a cancel or a stall is noticed even while a read is blocked. When this
        thread stops waiting (cancel, stall, done) it cuts the connection at once, which wakes
        the blocked read and makes Ollama stop generating. Only the helper closes the response,
        so nothing is closed twice."""
        inbox: queue.Queue[tuple[str, Any]] = queue.Queue()
        stop = threading.Event()
        first_wait = max(idle, FIRST_CHUNK_WAIT_S)
        lock = threading.Lock()
        opened: list[Any] = []  # the response, once the helper has it

        def reader() -> None:
            resp = None
            try:
                resp = self._open("/api/chat", body, timeout=first_wait)
                with lock:
                    opened.append(resp)
                    if stop.is_set():
                        return
                first = True
                for line in resp:
                    if stop.is_set():
                        return
                    if first:
                        first = False
                        sock = _socket_of(resp)
                        if sock is not None:
                            sock.settimeout(idle)  # after the first chunk only the idle limit
                    inbox.put(("line", line))
                inbox.put(("end", None))
            except BaseException as e:  # handed to the waiting thread
                inbox.put(("error", e))
            finally:
                if resp is not None:
                    _close(resp)

        threading.Thread(target=reader, name="shuto-ollama-stream", daemon=True).start()
        content: list[str] = []
        thinking: list[str] = []
        final: dict[str, Any] = {}
        started = last = time.monotonic()
        seen_data = False
        try:
            while True:
                now = time.monotonic()
                limit = idle if seen_data else first_wait
                left = min(limit - (now - last), MAX_ANSWER_S - (now - started))
                try:
                    kind, value = inbox.get(timeout=max(0.0, min(left, CANCEL_POLL_S)))
                except queue.Empty:
                    if cancel and cancel():
                        raise LLMCancelled() from None
                    now = time.monotonic()
                    if now - started >= MAX_ANSWER_S:
                        raise OllamaTimeout(
                            f"Ollama timed out at {self.url}: the answer took longer than "
                            f"{MAX_ANSWER_S / 60:.0f} minutes"
                        ) from None
                    if now - last >= limit:
                        raise OllamaTimeout(
                            f"Ollama timed out at {self.url}: it stalled, no data for {limit:.0f} s"
                        ) from None
                    continue
                if cancel and cancel():
                    raise LLMCancelled()
                if kind == "end":
                    break
                if kind == "error":
                    self._raise_stream_error(value, idle)
                last, seen_data = time.monotonic(), True
                text = value.strip()
                if not text:
                    continue
                try:
                    ev = json.loads(text.decode("utf-8"))
                except ValueError:
                    continue
                if ev.get("error"):
                    raise LLMError(
                        f"Ollama chat failed: {ev['error']}", retryable=False, kind="model"
                    )
                msg = ev.get("message") or {}
                if msg.get("content"):
                    content.append(msg["content"])
                if msg.get("thinking"):
                    thinking.append(msg["thinking"])
                if ev.get("done"):
                    final = ev
                    break
        finally:
            with lock:
                stop.set()
                live = opened[0] if opened else None
            if live is not None:
                _shutdown(live)
        if not final:
            raise LLMError(
                "The AI connection was lost: Ollama ended the answer early", True, "connection"
            )
        message: dict[str, Any] = {"role": "assistant", "content": "".join(content)}
        if thinking:
            message["thinking"] = "".join(thinking)
        return {
            "message": message,
            "done_reason": final.get("done_reason"),
            "prompt_eval_count": final.get("prompt_eval_count"),
            "eval_count": final.get("eval_count"),
        }

    def _raise_stream_error(self, exc: BaseException, idle: float) -> None:
        if isinstance(exc, LLMError):
            raise exc
        if isinstance(exc, TimeoutError):
            raise OllamaTimeout(
                f"Ollama timed out at {self.url}: it stalled, no data for {idle:.0f} s"
            ) from exc
        if isinstance(exc, OSError | http.client.HTTPException):
            raise LLMError(
                f"The AI connection was lost ({type(exc).__name__})",
                retryable=True,
                kind="connection",
            ) from exc
        raise exc


class OllamaProvider:
    name = "ollama"
    default_image_side = 1024
    default_concurrency = 1

    def __init__(
        self,
        model: str,
        url: str = DEFAULT_URL,
        keep_alive: str | None = "10m",
        timeout_s: float = 300,
        client: OllamaClient | None = None,
        num_ctx: int | None = None,
        **_: Any,
    ):
        self.model = model
        self.num_ctx = int(num_ctx) if num_ctx else DEFAULT_NUM_CTX
        self.keep_alive = keep_alive
        self.client = client or OllamaClient(url, timeout_s)

    def complete(self, req: LLMRequest, schema: dict) -> tuple[str, dict | None, int, int]:
        messages = []
        if req.system:
            messages.append({"role": "system", "content": req.system})
        messages.append(
            {
                "role": "user",
                "content": req.prompt,
                "images": [base64.b64encode(req.image_jpeg).decode("ascii")],
            }
        )
        extra = THINKING_HEADROOM if self.client.supports_thinking(self.model) else 0
        resp = self._chat(messages, schema, req, req.max_tokens + extra)
        message = resp.get("message") or {}
        text = message.get("content", "") or ""
        if not text.strip() and message.get("thinking") and resp.get("done_reason") == "length":
            # the thinking ate the budget: one more try with more room, then give up for good
            resp = self._chat(messages, schema, req, req.max_tokens + 2 * THINKING_HEADROOM)
            message = resp.get("message") or {}
            text = message.get("content", "") or ""
            if not text.strip() and message.get("thinking"):
                raise LLMError(
                    f"{self.model} thinks before it answers and ran out of room. Pick a model "
                    "without thinking (for example minicpm-v:8b or gemma3:4b) in Settings.",
                    retryable=False,
                    kind="model",
                )
        if not text.strip() and message.get("thinking"):
            raise LLMError(
                f"{self.model} used all its tokens on thinking and wrote no answer "
                f"(stopped: {resp.get('done_reason') or 'unknown'})",
                retryable=True,
                kind="model",
            )
        return (
            text,
            None,
            int(resp.get("prompt_eval_count") or 0),
            int(resp.get("eval_count") or 0),
        )

    def _num_ctx(self) -> int:
        """The context size to ask for: more room when the model thinks although told not to
        (capped at its catalogue window; left alone on small-memory machines)."""
        if not self.client.always_thinks(self.model) or self.num_ctx <= SMALL_CONTEXT_TOKENS:
            return self.num_ctx
        bumped = self.num_ctx + THINKING_HEADROOM
        window = context_tokens(self.model)
        return min(bumped, window) if window else bumped

    def _chat(self, messages: list[dict], schema: dict, req: LLMRequest, num_predict: int) -> dict:
        return self.client.chat(
            self.model,
            messages,
            format=schema,
            options={
                "temperature": req.temperature,
                "num_predict": num_predict,
                "num_ctx": self._num_ctx(),
            },
            keep_alive=self.keep_alive,
            timeout=req.timeout_s,
            cancel=req.cancel,
        )
