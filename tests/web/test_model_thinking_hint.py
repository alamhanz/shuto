"""Settings tells the owner which downloaded models think before they answer (slower)."""

# ruff: noqa: F811  (fixtures imported from test_web)
from __future__ import annotations

from .test_web import ollama, web  # noqa: F401

HINT = "This model thinks before it answers, so it is slower (about a minute per photo)."


def test_hint_shows_only_for_downloaded_models_that_think(web, ollama):
    ollama.pulled = ["gemma3:4b", "gemma3:12b"]
    ollama.supports_thinking = lambda tag: tag == "gemma3:12b"
    text = web.get("/settings").text
    assert text.count(HINT) == 1


def test_no_hint_when_no_model_thinks(web, ollama):
    ollama.supports_thinking = lambda tag: False
    assert HINT not in web.get("/settings").text


def test_settings_survives_a_failed_capability_lookup(web, ollama):
    def boom(tag):
        raise RuntimeError("down")

    ollama.supports_thinking = boom
    r = web.get("/settings")
    assert r.status_code == 200 and HINT not in r.text


def test_thinking_answers_are_kept_between_page_loads(web, ollama):
    ollama.pulled = ["gemma3:4b", "gemma3:12b"]
    asked = []

    def supports(tag):
        asked.append(tag)
        return tag == "gemma3:12b"

    ollama.supports_thinking = supports
    assert web.get("/settings").text.count(HINT) == 1
    first = len(asked)
    assert first >= 1
    assert web.get("/settings").text.count(HINT) == 1
    assert len(asked) == first


def test_lookups_stop_after_the_first_failure(web, ollama):
    ollama.pulled = ["gemma3:4b", "gemma3:12b"]
    asked = []

    def boom(tag):
        asked.append(tag)
        raise RuntimeError("down")

    ollama.supports_thinking = boom
    assert HINT not in web.get("/settings").text
    assert len(asked) == 1  # not one call per model
