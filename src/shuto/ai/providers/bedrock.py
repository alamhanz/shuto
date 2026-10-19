"""Bedrock provider: Converse API with a forced tool whose input schema is the shared schema."""

from __future__ import annotations

from typing import Any

from shuto.ai.providers.base import LLMError, LLMRequest

TOOL_NAME = "photo_metadata"
RETRYABLE_CODES = {
    "ThrottlingException",
    "TooManyRequestsException",
    "ServiceUnavailableException",
    "InternalServerException",
    "ModelTimeoutException",
    "ModelNotReadyException",
    "RequestTimeout",
    "RequestTimeoutException",
}


def map_api_error(exc: Exception) -> LLMError:
    resp = getattr(exc, "response", None)
    resp = resp if isinstance(resp, dict) else {}
    code = (resp.get("Error") or {}).get("Code", "")
    status = (resp.get("ResponseMetadata") or {}).get("HTTPStatusCode")
    name = type(exc).__name__
    retryable = (
        code in RETRYABLE_CODES
        or status in (408, 429)
        or (isinstance(status, int) and status >= 500)
        or "Connect" in name
        or "Timeout" in name
    )
    return LLMError(f"Bedrock error ({code or name}): {exc}", retryable=bool(retryable))


class BedrockProvider:
    name = "bedrock"
    default_image_side = 1568
    default_concurrency = 4

    def __init__(
        self,
        model: str,
        region: str | None = None,
        profile: str | None = None,
        client: Any = None,
        timeout_s: float = 300,
        **_: Any,
    ):
        self.model = model
        if client is None:
            try:
                import boto3
                from botocore.config import Config
            except ImportError as e:
                raise LLMError("boto3 missing: pip install 'shuto[bedrock]'") from e
            session = boto3.Session(profile_name=profile or None, region_name=region or None)
            client = session.client(
                "bedrock-runtime",
                config=Config(read_timeout=timeout_s, retries={"max_attempts": 1}),
            )
        self.client = client

    def complete(self, req: LLMRequest, schema: dict) -> tuple[str, dict | None, int, int]:
        try:
            resp = self.client.converse(
                modelId=self.model,
                system=[{"text": req.system}],
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"image": {"format": "jpeg", "source": {"bytes": req.image_jpeg}}},
                            {"text": req.prompt},
                        ],
                    }
                ],
                inferenceConfig={"maxTokens": req.max_tokens, "temperature": req.temperature},
                toolConfig={
                    "tools": [
                        {
                            "toolSpec": {
                                "name": TOOL_NAME,
                                "description": "Record the stock-photo metadata for this image.",
                                "inputSchema": {"json": schema},
                            }
                        }
                    ],
                    "toolChoice": {"tool": {"name": TOOL_NAME}},
                },
            )
        except LLMError:
            raise
        except Exception as e:  # botocore exceptions vary
            raise map_api_error(e) from e
        usage = resp.get("usage") or {}
        in_tok = int(usage.get("inputTokens", 0) or 0)
        out_tok = int(usage.get("outputTokens", 0) or 0)
        text = ""
        for block in ((resp.get("output") or {}).get("message") or {}).get("content") or []:
            tool = block.get("toolUse")
            if tool and tool.get("name", TOOL_NAME) == TOOL_NAME:
                data = tool.get("input")
                if isinstance(data, dict):
                    return str(data), data, in_tok, out_tok
            elif "text" in block:
                text += block["text"]
        return text, None, in_tok, out_tok
