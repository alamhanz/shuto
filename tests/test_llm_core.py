import io
import json
import urllib.error
from types import SimpleNamespace

import pytest

from shuto.llm import PROVIDERS, get_provider
from shuto.llm.anthropic_api import AnthropicProvider
from shuto.llm.base import LLMError, LLMRequest, describe_structured
from shuto.llm.fake import DEFAULT_RESPONSE, FakeProvider
from shuto.llm.ollama import OllamaClient, OllamaProvider
from shuto.llm.schema import PHOTO_OUTPUT_SCHEMA, PhotoOutput, repair_json

GOOD = {"description": "A street", "keywords": ["a", "b"]}


def req():
    return LLMRequest(image_jpeg=b"\xff\xd8jpeg", system="sys", prompt="describe")


# ---- repair_json ----
@pytest.mark.parametrize(
    "text",
    [
        json.dumps(GOOD),
        "```json\n" + json.dumps(GOOD) + "\n```",
        "Sure! Here is the JSON:\n" + json.dumps(GOOD) + "\nHope that helps.",
        '{"description": "A street", "keywords": ["a", "b",],}',
        "{'description': 'A street', 'keywords': ['a', 'b']}",
        "{“description”: “A street”, “keywords”: [“a”, “b”]}",
        '{"description": "A street", "keywords": ["a", "b"]',
    ],
)
def test_repair_cases(text):
    assert repair_json(text) == GOOD


def test_repair_truncated_mid_value():
    out = repair_json('{"description": "A street", "keywords": ["a", "b"], "categories": ["Bu')
    assert out["description"] == "A street" and out["keywords"] == ["a", "b"]


def test_repair_truncated_dangling_key():
    out = repair_json('{"description": "A street", "keywords": ["a"], "editor')
    assert out["keywords"] == ["a"]


def test_repair_failures():
    for bad in ["", "no json here", "[1, 2]"]:
        with pytest.raises(ValueError):
            repair_json(bad)


# ---- schema leniency ----
def test_schema_leniency():
    out = PhotoOutput.model_validate(
        {
            "description": "x",
            "keywords": "tokyo, street; night",
            "editorial": "yes",
            "mature": "false",
            "detected": {"recognizable_people": "2 people", "landmarks": "Skytree"},
            "location": None,
            "bogus": 1,
        }
    )
    assert out.keywords == ["tokyo", "street", "night"]
    assert out.editorial is True and out.mature is False
    assert out.detected.recognizable_people == 2 and out.detected.landmarks == ["Skytree"]
    assert out.location.city is None
    assert (
        PhotoOutput(description="d", keywords=[]).detected
        is not PhotoOutput(description="d", keywords=[]).detected
    )
    assert "description" in PHOTO_OUTPUT_SCHEMA["properties"]


# ---- describe_structured ----
def test_native_data():
    llm = FakeProvider()
    r = describe_structured(llm, req())
    assert r.data["keywords"][:19] == DEFAULT_RESPONSE["keywords"][:19]
    assert not r.repaired and r.retries == 0 and r.input_tokens == 100


def test_repair_path():
    llm = FakeProvider(response="```json\n" + json.dumps(GOOD) + ",\n```")
    r = describe_structured(llm, req())
    assert r.repaired and r.data["description"] == "A street" and len(llm.calls) == 1


def test_corrective_retry():
    llm = FakeProvider(response=["totally not json", GOOD])
    r = describe_structured(llm, req(), retries=1)
    assert r.retries == 1 and len(llm.calls) == 2
    assert "Your previous answer was invalid" in llm.calls[1].prompt
    assert "Return only JSON matching the schema." in llm.calls[1].prompt
    assert llm.calls[1].temperature == 0.0
    assert r.input_tokens == 200 and r.output_tokens == 100


def test_validation_error_triggers_retry_then_fails():
    llm = FakeProvider(response={"keywords": ["a"]})  # missing description
    with pytest.raises(LLMError) as ei:
        describe_structured(llm, req(), retries=1)
    assert ei.value.retryable is False and len(llm.calls) == 2


def test_zero_retries():
    llm = FakeProvider(response="garbage")
    with pytest.raises(LLMError):
        describe_structured(llm, req(), retries=0)
    assert len(llm.calls) == 1


# ---- ollama ----
class FakeResp:
    def __init__(self, payload=None, lines=None):
        self._payload = payload
        self._lines = lines or []
        self.closed = False

    def read(self):
        return json.dumps(self._payload).encode()

    def __iter__(self):
        return iter(self._lines)

    def close(self):
        self.closed = True


def patch_urlopen(monkeypatch, handler):
    seen = []

    def fake(request, timeout=None):
        seen.append(request)
        return handler(request)

    monkeypatch.setattr("urllib.request.urlopen", fake)
    return seen


def test_ollama_chat_and_provider(monkeypatch):
    reply = {
        "message": {"content": json.dumps(GOOD)},
        "prompt_eval_count": 321,
        "eval_count": 45,
    }
    seen = patch_urlopen(monkeypatch, lambda r: FakeResp(reply))
    llm = OllamaProvider("qwen3-vl:8b", url="http://127.0.0.1:11434/")
    r = describe_structured(llm, req())
    assert r.data["description"] == "A street" and not r.repaired
    assert (r.input_tokens, r.output_tokens) == (321, 45)
    body = json.loads(seen[0].data)
    assert seen[0].full_url == "http://127.0.0.1:11434/api/chat"
    assert body["model"] == "qwen3-vl:8b" and body["stream"] is False
    assert body["format"] == PHOTO_OUTPUT_SCHEMA
    assert body["options"] == {"temperature": 0.2, "num_predict": 1500}
    assert body["keep_alive"] == "10m"
    assert body["messages"][0]["role"] == "system"
    assert len(body["messages"][1]["images"]) == 1


def test_ollama_tags_version_ps(monkeypatch):
    def handler(r):
        path = r.full_url.rsplit("/api/", 1)[1]
        return FakeResp(
            {
                "tags": {"models": [{"name": "m:1", "size": 5, "details": {}}]},
                "version": {"version": "0.9.0"},
                "ps": {"models": []},
            }[path]
        )

    patch_urlopen(monkeypatch, handler)
    c = OllamaClient("http://x:1")
    assert c.tags()[0]["name"] == "m:1"
    assert c.version() == "0.9.0"
    assert c.ps() == []


def test_ollama_pull_progress(monkeypatch):
    lines = [
        b'{"status": "pulling manifest"}\n',
        b"\n",
        b'{"status": "downloading", "total": 100, "completed": 50}\n',
        b'{"status": "success"}\n',
    ]
    patch_urlopen(monkeypatch, lambda r: FakeResp(lines=lines))
    events = []
    OllamaClient().pull("gemma3:4b", events.append)
    assert [e["status"] for e in events] == ["pulling manifest", "downloading", "success"]


def test_ollama_delete_uses_the_delete_api(monkeypatch):
    seen = patch_urlopen(monkeypatch, lambda r: FakeResp({}))
    OllamaClient("http://x:1").delete("gemma3:4b")
    assert seen[0].get_method() == "DELETE" and seen[0].full_url == "http://x:1/api/delete"
    assert json.loads(seen[0].data) == {"model": "gemma3:4b"}


def test_ollama_pull_error(monkeypatch):
    patch_urlopen(monkeypatch, lambda r: FakeResp(lines=[b'{"error": "nope"}\n']))
    with pytest.raises(LLMError, match="nope"):
        OllamaClient().pull("x")


def test_ollama_connection_refused(monkeypatch):
    def boom(r):
        raise urllib.error.URLError(ConnectionRefusedError(111, "refused"))

    patch_urlopen(monkeypatch, boom)
    with pytest.raises(LLMError) as ei:
        OllamaProvider("m", url="http://127.0.0.1:1").complete(req(), {})
    assert "Ollama is not running at http://127.0.0.1:1" in str(ei.value)
    assert "shuto doctor" in str(ei.value) and ei.value.retryable is False


def test_ollama_model_missing_and_5xx(monkeypatch):
    def http(code):
        def h(r):
            raise urllib.error.HTTPError(
                r.full_url, code, "err", {}, io.BytesIO(b'{"error": "model not found"}')
            )

        return h

    patch_urlopen(monkeypatch, http(404))
    with pytest.raises(LLMError, match="shuto models pull m") as ei:
        OllamaProvider("m").complete(req(), {})
    assert ei.value.retryable is False
    patch_urlopen(monkeypatch, http(503))
    with pytest.raises(LLMError) as ei:
        OllamaProvider("m").complete(req(), {})
    assert ei.value.retryable is True


# ---- anthropic ----
class FakeMessages:
    def __init__(self, outer):
        self.outer = outer

    def create(self, **kw):
        self.outer.kwargs = kw
        if self.outer.error:
            raise self.outer.error
        return self.outer.msg


class FakeClient:
    def __init__(self, msg=None, error=None):
        self.msg, self.error, self.kwargs = msg, error, None
        self.messages = FakeMessages(self)


def test_anthropic_tool_call():
    block = SimpleNamespace(type="tool_use", name="photo_metadata", input=GOOD)
    msg = SimpleNamespace(content=[block], usage=SimpleNamespace(input_tokens=11, output_tokens=7))
    client = FakeClient(msg)
    llm = AnthropicProvider("some-model", client=client)
    r = describe_structured(llm, req())
    assert r.data["keywords"] == ["a", "b"] and (r.input_tokens, r.output_tokens) == (11, 7)
    kw = client.kwargs
    assert kw["model"] == "some-model"
    assert kw["tool_choice"] == {"type": "tool", "name": "photo_metadata"}
    assert kw["tools"][0]["input_schema"] == PHOTO_OUTPUT_SCHEMA
    assert kw["messages"][0]["content"][0]["source"]["media_type"] == "image/jpeg"


def test_anthropic_text_fallback_and_errors():
    msg = SimpleNamespace(
        content=[SimpleNamespace(type="text", text=json.dumps(GOOD))],
        usage=SimpleNamespace(input_tokens=1, output_tokens=2),
    )
    r = describe_structured(AnthropicProvider("m", client=FakeClient(msg)), req())
    assert r.data["description"] == "A street" and r.repaired is False

    class RateLimitError(Exception):
        status_code = 429

    with pytest.raises(LLMError) as ei:
        AnthropicProvider("m", client=FakeClient(error=RateLimitError("slow"))).complete(req(), {})
    assert ei.value.retryable is True

    class BadRequest(Exception):
        status_code = 400

    with pytest.raises(LLMError) as ei:
        AnthropicProvider("m", client=FakeClient(error=BadRequest("bad"))).complete(req(), {})
    assert ei.value.retryable is False


def test_anthropic_requires_key(monkeypatch):
    pytest.importorskip("anthropic")
    with pytest.raises(LLMError, match="ANTHROPIC_API_KEY"):
        AnthropicProvider("m")


# ---- registry ----
def test_get_provider():
    assert set(PROVIDERS) == {"ollama", "anthropic", "gemini", "bedrock", "fake"}
    assert get_provider("fake", "fake").name == "fake"
    p = get_provider("ollama", "m", url="http://h:1", keep_alive="5m", timeout_s=9)
    assert p.model == "m" and p.client.url == "http://h:1" and p.default_concurrency == 1
    with pytest.raises(LLMError, match="Unknown LLM provider"):
        get_provider("nope", "m")


def test_get_provider_missing_extra(monkeypatch):
    import importlib

    def fake_import(name, *a, **k):
        raise ModuleNotFoundError("No module named 'boto3'", name="boto3")

    monkeypatch.setattr(importlib, "import_module", fake_import)
    with pytest.raises(LLMError, match=r"pip install 'shuto\[bedrock\]'"):
        get_provider("bedrock", "m")
