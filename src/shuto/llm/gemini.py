"""Gemini provider (google-genai): JSON mime type plus a response JSON schema."""

from __future__ import annotations

from typing import Any

from .base import LLMError, LLMRequest

DEFAULT_MODEL = "gemini-2.5-flash"


def map_api_error(exc: Exception) -> LLMError:
    status = getattr(exc, "code", None)
    if not isinstance(status, int):
        status = getattr(exc, "status_code", None)
    name = type(exc).__name__
    retryable = (
        status in (408, 429)
        or (isinstance(status, int) and status >= 500)
        or "Connect" in name
        or "Timeout" in name
    )
    return LLMError(f"Gemini API error ({name}): {exc}", retryable=bool(retryable))


THINKING_HEADROOM = 4096


class GeminiProvider:
    name = "gemini"
    default_image_side = 1568
    default_concurrency = 4

    def __init__(
        self, model: str = DEFAULT_MODEL, api_key: str | None = None, client: Any = None, **_: Any
    ):
        self.model = model or DEFAULT_MODEL
        if client is None:
            try:
                from google import genai
            except ImportError as e:
                raise LLMError("Gemini SDK missing: pip install 'shuto[gemini]'") from e
            if not api_key:
                raise LLMError("GEMINI_API_KEY is not set (env, .env or keyring)")
            client = genai.Client(api_key=api_key)
        self.client = client

    def complete(self, req: LLMRequest, schema: dict) -> tuple[str, dict | None, int, int]:
        # Thinking tokens count toward max_output_tokens and can truncate the JSON. 2.5 Flash can
        # switch thinking off; other models cannot, so they get extra room instead.
        no_thinking = "2.5-flash" in self.model
        config = {
            "system_instruction": req.system,
            "max_output_tokens": req.max_tokens + (0 if no_thinking else THINKING_HEADROOM),
            "temperature": req.temperature,
            "response_mime_type": "application/json",
            "response_json_schema": schema,
            "http_options": {"timeout": int(req.timeout_s * 1000)},
        }
        if no_thinking:
            config["thinking_config"] = {"thinking_budget": 0}
        contents = [
            {
                "role": "user",
                "parts": [
                    {"inline_data": {"mime_type": "image/jpeg", "data": req.image_jpeg}},
                    {"text": req.prompt},
                ],
            }
        ]
        try:
            resp = self.client.models.generate_content(
                model=self.model, contents=contents, config=config
            )
        except LLMError:
            raise
        except Exception as e:  # SDK exception types vary by version
            raise map_api_error(e) from e
        usage = getattr(resp, "usage_metadata", None)
        in_tok = int(getattr(usage, "prompt_token_count", 0) or 0)
        out_tok = int(getattr(usage, "candidates_token_count", 0) or 0)
        try:
            text = resp.text or ""
        except Exception:  # blocked or empty candidates raise on .text
            text = ""
        if not text:
            raise LLMError("Gemini returned no text (blocked or empty response)", retryable=False)
        return text, None, in_tok, out_tok
