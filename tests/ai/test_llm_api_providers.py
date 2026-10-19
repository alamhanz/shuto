import json
from types import SimpleNamespace

import pytest

from shuto.ai.providers import get_provider
from shuto.ai.providers.base import LLMError, LLMRequest, describe_structured
from shuto.ai.providers.schema import PHOTO_OUTPUT_SCHEMA

GOOD = {"description": "A street", "keywords": ["a", "b"]}
JPEG = b"\xff\xd8jpeg"
GOOD_JSON = json.dumps(GOOD)


def req():
    return LLMRequest(image_jpeg=JPEG, system="sys", prompt="describe")


class StatusError(Exception):
    def __init__(self, status):
        super().__init__(f"status {status}")
        self.status_code = status


# ---- Gemini ----
def _gemini_client(text=GOOD_JSON, exc=None):
    calls = []

    def generate_content(**kw):
        calls.append(kw)
        if exc:
            raise exc
        return SimpleNamespace(
            text=text,
            usage_metadata=SimpleNamespace(prompt_token_count=13, candidates_token_count=5),
        )

    return SimpleNamespace(models=SimpleNamespace(generate_content=generate_content)), calls


def test_gemini_request_and_result():
    pytest.importorskip("google.genai")
    client, calls = _gemini_client()
    p = get_provider("gemini", "gem-x", client=client)
    res = describe_structured(p, req())
    assert res.data["keywords"] == ["a", "b"]
    assert (res.input_tokens, res.output_tokens) == (13, 5)
    kw = calls[0]
    assert kw["model"] == "gem-x"
    assert kw["config"]["response_mime_type"] == "application/json"
    assert kw["config"]["response_json_schema"] == PHOTO_OUTPUT_SCHEMA
    parts = kw["contents"][0]["parts"]
    assert parts[0]["inline_data"] == {"mime_type": "image/jpeg", "data": JPEG}


def test_gemini_thinking_does_not_eat_output_budget():
    types = pytest.importorskip("google.genai.types")
    client, calls = _gemini_client()
    get_provider("gemini", "gemini-2.5-flash", client=client).complete(req(), PHOTO_OUTPUT_SCHEMA)
    cfg = calls[0]["config"]
    assert cfg["thinking_config"] == {"thinking_budget": 0}
    assert cfg["max_output_tokens"] == req().max_tokens
    types.GenerateContentConfig.model_validate(cfg)
    # A model that cannot disable thinking gets extra output room instead.
    client, calls = _gemini_client()
    get_provider("gemini", "gemini-2.5-pro", client=client).complete(req(), PHOTO_OUTPUT_SCHEMA)
    assert "thinking_config" not in calls[0]["config"]
    assert calls[0]["config"]["max_output_tokens"] > req().max_tokens


def test_gemini_config_accepted_by_sdk():
    types = pytest.importorskip("google.genai.types")
    client, calls = _gemini_client()
    get_provider("gemini", "m", client=client).complete(req(), PHOTO_OUTPUT_SCHEMA)
    types.GenerateContentConfig.model_validate(calls[0]["config"])


@pytest.mark.parametrize("code,retryable", [(429, True), (500, True), (400, False)])
def test_gemini_errors(code, retryable):
    pytest.importorskip("google.genai")
    exc = Exception("boom")
    exc.code = code
    client, _ = _gemini_client(exc=exc)
    with pytest.raises(LLMError) as ei:
        get_provider("gemini", "m", client=client).complete(req(), PHOTO_OUTPUT_SCHEMA)
    assert ei.value.retryable is retryable


def test_gemini_empty_and_missing_key():
    pytest.importorskip("google.genai")
    client, _ = _gemini_client(text="")
    with pytest.raises(LLMError, match="no text"):
        get_provider("gemini", "m", client=client).complete(req(), PHOTO_OUTPUT_SCHEMA)
    with pytest.raises(LLMError, match="GEMINI_API_KEY"):
        get_provider("gemini", "m")


# ---- Bedrock ----
class ClientError(Exception):
    def __init__(self, code, status):
        super().__init__(code)
        self.response = {"Error": {"Code": code}, "ResponseMetadata": {"HTTPStatusCode": status}}


def _bedrock_client(exc=None, content=None):
    calls = []

    def converse(**kw):
        calls.append(kw)
        if exc:
            raise exc
        default = [{"toolUse": {"name": "photo_metadata", "input": GOOD}}]
        return {
            "output": {"message": {"content": content or default}},
            "usage": {"inputTokens": 21, "outputTokens": 9},
        }

    return SimpleNamespace(converse=converse), calls


def test_bedrock_request_and_result():
    pytest.importorskip("boto3")
    client, calls = _bedrock_client()
    p = get_provider("bedrock", "eu.model", client=client, region="us-east-1")
    res = describe_structured(p, req())
    assert res.data["description"] == "A street"
    assert (res.input_tokens, res.output_tokens) == (21, 9)
    kw = calls[0]
    assert kw["modelId"] == "eu.model"
    assert kw["toolConfig"]["toolChoice"] == {"tool": {"name": "photo_metadata"}}
    assert kw["toolConfig"]["tools"][0]["toolSpec"]["inputSchema"] == {"json": PHOTO_OUTPUT_SCHEMA}
    img = kw["messages"][0]["content"][0]["image"]
    assert img == {"format": "jpeg", "source": {"bytes": JPEG}}
    assert kw["inferenceConfig"]["maxTokens"] == 1500


def test_bedrock_text_fallback_is_repaired():
    pytest.importorskip("boto3")
    client, _ = _bedrock_client(content=[{"text": "```json\n" + json.dumps(GOOD) + "\n```"}])
    res = describe_structured(get_provider("bedrock", "m", client=client), req())
    assert res.data["description"] == "A street"


@pytest.mark.parametrize(
    "code,status,retryable",
    [
        ("ThrottlingException", 429, True),
        ("ServiceUnavailableException", 503, True),
        ("ValidationException", 400, False),
        ("AccessDeniedException", 403, False),
    ],
)
def test_bedrock_errors(code, status, retryable):
    pytest.importorskip("boto3")
    client, _ = _bedrock_client(exc=ClientError(code, status))
    with pytest.raises(LLMError) as ei:
        get_provider("bedrock", "m", client=client).complete(req(), PHOTO_OUTPUT_SCHEMA)
    assert ei.value.retryable is retryable
