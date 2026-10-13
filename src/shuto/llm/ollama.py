"""Ollama client (stdlib urllib only) and the local vision provider."""

from __future__ import annotations

import base64
import http.client
import json
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

from .base import LLMError, LLMRequest

DEFAULT_URL = "http://127.0.0.1:11434"


def _close(resp: Any) -> None:
    close = getattr(resp, "close", None)
    if close:
        close()


class OllamaClient:
    def __init__(self, url: str = DEFAULT_URL, timeout: float = 300):
        self.url = url.rstrip("/")
        self.timeout = timeout

    def _not_running(self) -> LLMError:
        return LLMError(
            f"Ollama is not running at {self.url}; start it or run `shuto doctor`",
            retryable=False,
        )

    def _timeout(self) -> LLMError:
        return LLMError(f"Ollama timed out at {self.url}", retryable=True)

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
            raise LLMError(msg, retryable=e.code >= 500) from e
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
            raise LLMError("Ollama returned a non-JSON response", retryable=True) from e

    def version(self) -> str:
        return str(self._json("/api/version", timeout=min(5, self.timeout)).get("version", ""))

    def tags(self) -> list[dict]:
        return list(self._json("/api/tags", timeout=min(10, self.timeout)).get("models", []))

    def delete(self, tag: str) -> None:
        """Remove a downloaded model from this computer (Ollama frees its files)."""
        _close(self._open("/api/delete", {"model": tag}, timeout=60, method="DELETE"))

    def ps(self) -> list[dict]:
        return list(self._json("/api/ps", timeout=10).get("models", []))

    def pull(self, tag: str, on_progress: Callable[[dict], None] | None = None) -> None:
        """Download a model. Anything raised by `on_progress` (a cancel) stops the stream: the
        response is closed at once, so Ollama sees the client leave and aborts the download.
        Partly downloaded files stay on disk, so a later pull continues where this one stopped.
        Raises LLMError if the stream breaks, stalls or ends without Ollama's `success`."""
        resp = self._open("/api/pull", {"model": tag, "stream": True}, timeout=self.timeout)
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

    def chat(
        self,
        model: str,
        messages: list[dict],
        format: Any = None,
        options: dict | None = None,
        keep_alive: str | None = None,
        timeout: float | None = None,
    ) -> dict:
        body: dict[str, Any] = {"model": model, "messages": messages, "stream": False}
        if format is not None:
            body["format"] = format
        if options:
            body["options"] = options
        if keep_alive:
            body["keep_alive"] = keep_alive
        return self._json("/api/chat", body, timeout)


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
        **_: Any,
    ):
        self.model = model
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
        resp = self.client.chat(
            self.model,
            messages,
            format=schema,
            options={"temperature": req.temperature, "num_predict": req.max_tokens},
            keep_alive=self.keep_alive,
            timeout=req.timeout_s,
        )
        text = (resp.get("message") or {}).get("content", "") or ""
        return (
            text,
            None,
            int(resp.get("prompt_eval_count") or 0),
            int(resp.get("eval_count") or 0),
        )
