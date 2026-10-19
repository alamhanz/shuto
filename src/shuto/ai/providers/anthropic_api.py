"""Anthropic provider: forced tool call whose input_schema is the shared schema."""

from __future__ import annotations

import base64
from typing import Any

from shuto.ai.providers.base import LLMError, LLMRequest

TOOL_NAME = "photo_metadata"


def map_api_error(exc: Exception) -> LLMError:
    status = getattr(exc, "status_code", None)
    name = type(exc).__name__
    retryable = (
        status in (408, 409, 429)
        or (isinstance(status, int) and status >= 500)
        or "Connection" in name
        or "Timeout" in name
        or "RateLimit" in name
    )
    return LLMError(f"Anthropic API error ({name}): {exc}", retryable=bool(retryable))


class AnthropicProvider:
    name = "anthropic"
    default_image_side = 1568
    default_concurrency = 4

    def __init__(self, model: str, api_key: str | None = None, client: Any = None, **_: Any):
        self.model = model
        if client is None:
            try:
                import anthropic
            except ImportError as e:
                raise LLMError("Anthropic SDK missing: pip install 'shuto[anthropic]'") from e
            if not api_key:
                raise LLMError("ANTHROPIC_API_KEY is not set (env, .env or keyring)")
            client = anthropic.Anthropic(api_key=api_key)
        self.client = client

    def complete(self, req: LLMRequest, schema: dict) -> tuple[str, dict | None, int, int]:
        b64 = base64.b64encode(req.image_jpeg).decode("ascii")
        try:
            msg = self.client.messages.create(
                model=self.model,
                max_tokens=req.max_tokens,
                temperature=req.temperature,
                system=req.system,
                tools=[
                    {
                        "name": TOOL_NAME,
                        "description": "Record the stock-photo metadata for this image.",
                        "input_schema": schema,
                    }
                ],
                tool_choice={"type": "tool", "name": TOOL_NAME},
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": "image/jpeg",
                                    "data": b64,
                                },
                            },
                            {"type": "text", "text": req.prompt},
                        ],
                    }
                ],
                timeout=req.timeout_s,
            )
        except LLMError:
            raise
        except Exception as e:  # SDK exception types vary by version
            raise map_api_error(e) from e
        usage = getattr(msg, "usage", None)
        in_tok = int(getattr(usage, "input_tokens", 0) or 0)
        out_tok = int(getattr(usage, "output_tokens", 0) or 0)
        text = ""
        for block in getattr(msg, "content", []) or []:
            btype = getattr(block, "type", None)
            if btype == "tool_use" and getattr(block, "name", TOOL_NAME) == TOOL_NAME:
                data = getattr(block, "input", None)
                if isinstance(data, dict):
                    return str(data), data, in_tok, out_tok
            elif btype == "text":
                text += getattr(block, "text", "")
        return text, None, in_tok, out_tok
