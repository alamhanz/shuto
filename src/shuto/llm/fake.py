"""Deterministic provider for tests and dry runs. Never touches the network."""

from __future__ import annotations

import json
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


class FakeProvider:
    """Canned provider.

    `response` may be a dict, a raw string (e.g. deliberately broken JSON), a callable
    (req) -> dict | str, or a list of those consumed in call order (the last one repeats).
    Every request is recorded in `calls`.
    """

    name = "fake"
    default_image_side = 512
    default_concurrency = 4

    def __init__(self, model: str = "fake", response: Any = None, **_: Any):
        self.model = model
        self._responses = response if isinstance(response, list) else [response]
        self.calls: list[LLMRequest] = []

    def complete(self, req: LLMRequest, schema: dict) -> tuple[str, dict | None, int, int]:
        idx = min(len(self.calls), len(self._responses) - 1)
        self.calls.append(req)
        resp = self._responses[idx]
        if resp is None:
            resp = DEFAULT_RESPONSE
        if callable(resp):
            resp = resp(req)
        if isinstance(resp, dict):
            return json.dumps(resp), resp, 100, 50
        return str(resp), None, 100, 50
