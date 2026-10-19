"""Shared structured output for every vision provider, plus tolerant JSON repair."""

from __future__ import annotations

import copy
import json
import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

_TRUE = {"true", "yes", "y", "1"}
_FALSE = {"false", "no", "n", "0", "", "none", "null"}


def _to_bool(v: Any) -> Any:
    if isinstance(v, str):
        s = v.strip().lower()
        if s in _TRUE:
            return True
        if s in _FALSE:
            return False
    return v


def _to_list(v: Any) -> Any:
    if v is None:
        return []
    if isinstance(v, str):
        return [p.strip() for p in re.split(r"[,;\n]", v) if p.strip()]
    if isinstance(v, (list, tuple)):
        return [str(x).strip() for x in v if x is not None and str(x).strip()]
    return v


class Detected(BaseModel):
    recognizable_people: int = 0
    logos_or_brands: list[str] = Field(default_factory=list)
    landmarks: list[str] = Field(default_factory=list)
    artworks_or_characters: list[str] = Field(default_factory=list)
    private_interior: bool = False
    event: str = ""
    personal_data: list[str] = Field(default_factory=list)
    screen_content: bool = False
    artwork_is_main_subject: bool = False

    @field_validator(
        "logos_or_brands", "landmarks", "artworks_or_characters", "personal_data", mode="before"
    )
    @classmethod
    def _lists(cls, v: Any) -> Any:
        return _to_list(v)

    @field_validator("private_interior", "screen_content", "artwork_is_main_subject", mode="before")
    @classmethod
    def _bool(cls, v: Any) -> Any:
        return _to_bool(v)

    @field_validator("event", mode="before")
    @classmethod
    def _event(cls, v: Any) -> Any:
        if v is None or isinstance(v, bool):
            return ""
        if isinstance(v, (list, tuple)):
            return ", ".join(str(x).strip() for x in v if str(x).strip())
        return re.sub(r"\s+", " ", str(v)).strip()

    @field_validator("recognizable_people", mode="before")
    @classmethod
    def _int(cls, v: Any) -> Any:
        if v is None or v == "":
            return 0
        if isinstance(v, bool):
            return int(v)
        if isinstance(v, str):
            m = re.search(r"\d+", v)
            return int(m.group()) if m else (1 if _to_bool(v) is True else 0)
        return v


class Location(BaseModel):
    city: str | None = None
    region: str | None = None
    country: str | None = None


class _PhotoCore(BaseModel):
    model_config = ConfigDict(title="PhotoOutput")  # keeps the base schema byte-identical

    description: str
    caption_sentence: str = ""
    keywords: list[str]
    categories: list[str] = Field(default_factory=list)
    editorial: bool = False
    mature: bool = False
    detected: Detected = Field(default_factory=Detected)
    location: Location = Field(default_factory=Location)

    @field_validator("keywords", "categories", mode="before")
    @classmethod
    def _lists(cls, v: Any) -> Any:
        return _to_list(v)

    @field_validator("editorial", "mature", mode="before")
    @classmethod
    def _bool(cls, v: Any) -> Any:
        return _to_bool(v)

    @field_validator("detected", "location", mode="before")
    @classmethod
    def _none_to_default(cls, v: Any) -> Any:
        return {} if v is None or v == "" else v


class PhotoOutput(_PhotoCore):
    """Core answer plus the optional filter answers (theme fit, user rules).

    The filter fields are kept raw and never raise: filters.evaluate() reads them leniently,
    so a garbled filter answer can never fail the describe call.
    """

    theme_fit: Any = None
    checks: Any = None

    @field_validator("theme_fit", "checks", mode="before")
    @classmethod
    def _keep_containers(cls, v: Any) -> Any:
        return v if isinstance(v, (dict, list)) else None


PHOTO_OUTPUT_SCHEMA: dict = _PhotoCore.model_json_schema()


def photo_schema(props: dict, required: list[str]) -> dict:
    """The base schema with extra top-level properties appended (after `location`)."""
    schema = copy.deepcopy(PHOTO_OUTPUT_SCHEMA)
    schema["properties"].update(copy.deepcopy(props))
    schema["required"] = [*schema.get("required", []), *required]
    return schema


_FENCE = re.compile(r"```(?:json|JSON)?\s*(.*?)```", re.DOTALL)
_SINGLE = re.compile(r"([{,\[:]\s*)'((?:[^'\\]|\\.)*)'")
_TRAILING_COMMA = re.compile(r",\s*([}\]])")
_SMART = {"“": '"', "”": '"', "‘": "'", "’": "'"}


def _loads_obj(text: str) -> dict | None:
    try:
        v = json.loads(text)
    except (ValueError, RecursionError):
        return None
    return v if isinstance(v, dict) else None


def _single_to_double(m: re.Match) -> str:
    inner = m.group(2).replace('\\"', '"').replace('"', '\\"')
    return f'{m.group(1)}"{inner}"'


def _close_truncated(text: str) -> str:
    """Close an unterminated string and any open brackets, dropping dangling tokens."""
    stack: list[str] = []
    in_str = False
    esc = False
    for ch in text:
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch in "{[":
            stack.append("}" if ch == "{" else "]")
        elif ch in "}]" and stack:
            stack.pop()
    out = text
    if in_str:
        if esc:
            out = out[:-1]
        out += '"'
    out = out.rstrip()
    if out.endswith(","):
        out = out[:-1]
    elif out.endswith(":"):
        out += "null"
    return out + "".join(reversed(stack))


def _try_repair(body: str) -> dict | None:
    obj = _loads_obj(body)
    if obj is not None:
        return obj
    fixed = body
    for k, v in _SMART.items():
        fixed = fixed.replace(k, v)
    fixed = _TRAILING_COMMA.sub(r"\1", fixed)
    obj = _loads_obj(fixed)
    if obj is not None:
        return obj
    fixed2 = _TRAILING_COMMA.sub(r"\1", _SINGLE.sub(_single_to_double, fixed))
    obj = _loads_obj(fixed2)
    if obj is not None:
        return obj
    for base in (fixed2, fixed):
        cur = base
        for _ in range(8):  # truncated output: close, else cut back to the previous comma
            obj = _loads_obj(_TRAILING_COMMA.sub(r"\1", _close_truncated(cur)))
            if obj is not None:
                return obj
            cut = cur.rfind(",")
            if cut <= 0:
                break
            cur = cur[:cut]
    return None


def repair_json(text: str) -> dict:
    """Best-effort extraction of a JSON object from model output. Raises ValueError."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError("empty model output")
    candidates = [m.group(1) for m in _FENCE.finditer(text)] + [text]
    for cand in candidates:
        start = cand.find("{")
        if start < 0:
            continue
        end = cand.rfind("}")
        if end > start:
            obj = _try_repair(cand[start : end + 1])
            if obj is not None:
                return obj
        obj = _try_repair(cand[start:])
        if obj is not None:
            return obj
    raise ValueError("no valid JSON object found in model output")
