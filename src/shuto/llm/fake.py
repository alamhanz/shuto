"""Deterministic provider for tests and dry runs. Never touches the network."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from typing import Any

from .base import LLMRequest

DEFAULT_RESPONSE = {
    "description": "A quiet residential street in Tokyo with parked bicycles and utility poles",
    "caption_sentence": "A quiet residential street with parked bicycles.",
    "keywords": [
        "japan", "tokyo", "street", "bicycle", "residential", "urban", "quiet", "morning",
        "city", "asphalt", "utility pole", "houses", "neighborhood", "travel", "daily life",
        "asia", "road", "outdoor", "architecture", "wires", "suburb", "calm", "empty",
        "sidewalk", "town",
    ],
    "categories": ["Buildings/Landmarks", "Transportation"],
    "editorial": False,
    "mature": False,
    "detected": {
        "recognizable_people": 0,
        "logos_or_brands": [],
        "landmarks": [],
        "artworks_or_characters": [],
        "private_interior": False,
    },
    "location": {"city": "Tokyo", "region": "Tokyo", "country": "Japan"},
}  # fmt: skip


def vary(base: dict) -> Callable[[LLMRequest], dict]:
    """A response function: ``base`` with a description and keywords that differ per photo.

    The variation comes from the image bytes, so the same photo always gets the same answer and
    different photos never look like duplicates of each other (the batch duplicate check).
    """

    def make(req: LLMRequest) -> dict:
        n = int(hashlib.sha1(req.image_jpeg).hexdigest()[:6], 16)
        kws = list(base.get("keywords") or DEFAULT_RESPONSE["keywords"])
        return {
            **base,
            "description": f"{base['description']} view {n} of series {n % 97}",
            "keywords": [*kws[:19], *(f"extra {n} {i}" for i in range(6))],
        }

    return make


class FakeProvider:
    """Canned provider.

    `response` may be a dict, a raw string (e.g. deliberately broken JSON), a callable
    (req) -> dict | str, or a list of those consumed in call order (the last one repeats).
    Every request is recorded in `calls` and every schema in `schemas`. When the schema asks for
    `theme_fit` and a dict response lacks it, a passing answer is added (never for `checks`).
    """

    name = "fake"
    default_image_side = 512
    default_concurrency = 4

    def __init__(self, model: str = "fake", response: Any = None, **_: Any):
        self.model = model
        self._responses = response if isinstance(response, list) else [response]
        self.calls: list[LLMRequest] = []
        self.schemas: list[dict] = []

    def complete(self, req: LLMRequest, schema: dict) -> tuple[str, dict | None, int, int]:
        idx = min(len(self.calls), len(self._responses) - 1)
        self.calls.append(req)
        self.schemas.append(schema)
        resp = self._responses[idx]
        if resp is None:  # the stock answer differs a little per photo, like a real model
            resp = vary(DEFAULT_RESPONSE)
        if callable(resp):
            resp = resp(req)
        if (
            isinstance(resp, dict)
            and "theme_fit" in (schema.get("properties") or {})
            and "theme_fit" not in resp
        ):
            resp = {**resp, "theme_fit": {"reason": "Fake answer.", "fit": "yes"}}
        if isinstance(resp, dict):
            return json.dumps(resp), resp, 100, 50
        return str(resp), None, 100, 50
