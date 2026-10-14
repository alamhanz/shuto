"""Provider-neutral request/result types and the shared structured-output loop."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Protocol

from pydantic import ValidationError

from shuto.ai.providers.schema import PHOTO_OUTPUT_SCHEMA, PhotoOutput, repair_json

_FILTER_KEYS = ("theme_fit", "checks")


class LLMError(Exception):
    def __init__(self, message: str, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


@dataclass
class LLMRequest:
    image_jpeg: bytes
    system: str
    prompt: str
    max_tokens: int = 1500
    temperature: float = 0.2
    timeout_s: float = 300


@dataclass
class LLMResult:
    data: dict
    raw_text: str
    provider: str
    model: str
    input_tokens: int
    output_tokens: int
    seconds: float
    repaired: bool
    retries: int


class VisionLLM(Protocol):
    name: str
    model: str
    default_image_side: int
    default_concurrency: int

    def complete(self, req: LLMRequest, schema: dict) -> tuple[str, dict | None, int, int]:
        """Return (raw text, natively parsed data or None, input tokens, output tokens)."""
        ...


def _parse(raw: str, native: dict | None) -> tuple[PhotoOutput, bool]:
    """Validate native data, else strict JSON, else repair. Returns (output, repaired)."""
    if native is not None:
        return PhotoOutput.model_validate(native), False
    try:
        return PhotoOutput.model_validate(json.loads(raw)), False
    except (ValueError, ValidationError):
        pass
    return PhotoOutput.model_validate(repair_json(raw)), True


def describe_structured(
    llm: VisionLLM,
    req: LLMRequest,
    *,
    retries: int = 1,
    schema: dict = PHOTO_OUTPUT_SCHEMA,
) -> LLMResult:
    """Call the provider and return validated data; retry with the error appended.

    Only the core fields can trigger a retry; the optional filter fields never do.
    """
    t0 = time.monotonic()
    in_tok = out_tok = 0
    current = req
    last_err = ""
    for attempt in range(retries + 1):
        raw, native, i, o = llm.complete(current, schema)
        in_tok += i or 0
        out_tok += o or 0
        try:
            out, repaired = _parse(raw, native)
        except (ValueError, ValidationError) as e:
            last_err = str(e)
            current = LLMRequest(
                image_jpeg=req.image_jpeg,
                system=req.system,
                prompt=(
                    f"{req.prompt}\n\nYour previous answer was invalid: {last_err}. "
                    "Return only JSON matching the schema."
                ),
                max_tokens=req.max_tokens,
                temperature=0.0,
                timeout_s=req.timeout_s,
            )
            continue
        return LLMResult(
            data={
                k: v for k, v in out.model_dump().items() if not (k in _FILTER_KEYS and v is None)
            },
            raw_text=raw,
            provider=llm.name,
            model=llm.model,
            input_tokens=in_tok,
            output_tokens=out_tok,
            seconds=time.monotonic() - t0,
            repaired=repaired,
            retries=attempt,
        )
    raise LLMError(
        f"{llm.name}/{llm.model} returned invalid output after {retries + 1} attempt(s): "
        f"{last_err[:300]}",
        retryable=False,
    )
