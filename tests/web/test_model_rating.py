"""The model cards show only models rated Good for this computer, unless asked for more."""

# ruff: noqa: F811  (fixtures imported from test_web)
from __future__ import annotations

import re
import traceback

import pytest

from shuto.ai import machine
from shuto.core.models import LLMCall
from shuto.workflow import models as workflow_models

from ..ai.test_rating import CPU_16, OWNER
from .test_web import ollama, web  # noqa: F401

GIB = 1024**3


@pytest.fixture
def owner(monkeypatch):
    monkeypatch.setattr(machine, "hardware_profile", lambda refresh=False: OWNER)


def cards(html: str) -> dict[str, str]:
    """model tag -> the card's HTML, for the model cards only (not the online AI cards)."""
    section = html.split('<details id="online"')[0]
    found = re.findall(
        r'<article class="card" id="row-\d+".*?<h3[^>]*>([^<]+)</h3>.*?</article>', section, re.S
    )
    parts = section.split('<article class="card" id="row-')[1:]
    assert len(found) == len(parts)
    return dict(zip(found, parts, strict=True))


def choose(web, tag):
    web.state.store.set_setting("llm.provider", "ollama")
    web.state.store.set_setting("llm.model", tag)


def test_default_view_shows_only_good_models(web, owner):
    html = web.get("/settings").text
    got = cards(html)
    assert set(got) == {"minicpm-v:8b", "qwen2.5vl:3b", "gemma3:4b"}
    assert "Good for this computer" in got["minicpm-v:8b"]
    assert "Fits in your graphics card with room to spare" in got["minicpm-v:8b"]
    assert re.search(r"\d+ more models are hidden because they are slow, basic or too big", html)
    assert "Show models that are not recommended for this computer" in html
    assert 'name="all" value="1"' in html and " checked" not in html.split('name="all"')[1][:30]


def test_the_tick_box_shows_everything_with_labels(web, owner):
    html = web.get("/settings?all=1").text
    got = cards(html)
    assert len(got) == len(workflow_models.list_models(web.state.cfg, web.state.store))
    assert "Not recommended for this computer" in got["qwen3-vl:8b"]
    assert "OK for this computer" in got["moondream"]
    assert "more models are hidden" not in html
    assert re.search(r'name="all" value="1" checked', html)
    assert web.get("/settings?all=0").text == web.get("/settings").text  # only "1" counts


def test_tick_box_is_a_plain_form_with_an_htmx_shortcut(web, owner):
    html = web.get("/settings").text
    assert 'method="get" action="/settings"' in html
    assert 'hx-get="/settings"' in html and 'hx-trigger="change"' in html
    assert '<button type="submit" class="secondary">Show</button>' in html
    wizard = web.get("/welcome?step=3").text
    assert 'name="step" value="3"' in wizard and 'action="/welcome"' in wizard


def test_the_model_in_use_always_shows_with_a_warning_and_is_never_switched(web, ollama, owner):
    ollama.pulled = ["qwen3-vl:8b"]
    choose(web, "qwen3-vl:8b")
    html = web.get("/settings").text
    got = cards(html)
    assert "qwen3-vl:8b" in got and "In use" in got["qwen3-vl:8b"]
    assert "Not recommended for this computer" in got["qwen3-vl:8b"]
    assert "qwen3-vl:8b is not recommended for this computer" in html
    assert "shuto keeps using it. For better speed choose minicpm-v:8b." in html
    assert web.state.store.get_setting("llm.model") == "qwen3-vl:8b"


def test_no_suggestion_when_no_model_is_good(web, ollama, monkeypatch):
    monkeypatch.setattr(machine, "hardware_profile", lambda refresh=False: CPU_16)
    ollama.pulled = ["minicpm-v:8b"]
    choose(web, "minicpm-v:8b")
    html = web.get("/settings").text
    assert "minicpm-v:8b is not recommended for this computer" in html
    assert "For better speed choose" not in html


def test_without_a_good_model_the_best_ok_ones_show_with_an_explanation(web, monkeypatch):
    monkeypatch.setattr(machine, "hardware_profile", lambda refresh=False: CPU_16)
    html = web.get("/settings").text
    got = cards(html)
    assert 1 <= len(got) <= 3
    assert all("OK for this computer" in c for c in got.values())
    assert "No model is a good fit for this computer." in html
    assert "An online AI is faster." in html


def test_use_and_download_of_a_not_recommended_model_ask_first(web, ollama, owner):
    ollama.pulled = ["qwen3-vl:8b", "qwen2.5vl:7b"]
    got = cards(web.get("/settings?all=1").text)
    bad = got["qwen3-vl:8b"]
    assert 'hx-post="/settings/use"' in bad and "Use it anyway?" in bad
    assert "This model is not recommended for this computer" in bad
    assert "OK for this computer" in got["qwen2.5vl:7b"]
    assert "anyway" not in got["qwen2.5vl:7b"]  # OK: no confirm to use or download it
    ollama.pulled = []
    bad = cards(web.get("/settings?all=1").text)["llama3.2-vision:11b"]
    assert "Download it anyway?" in bad and 'hx-get="/settings/advice"' in bad


def test_using_a_model_by_htmx_redirects(web, ollama, owner):
    ollama.pulled = ["qwen3-vl:8b"]
    r = web.post("/settings/use", {"spec": "qwen3-vl:8b"}, headers={"HX-Request": "true"})
    assert r.status_code == 204 and r.headers["HX-Redirect"].startswith("/settings")
    assert web.state.store.get_setting("llm.model") == "qwen3-vl:8b"


def test_wizard_first_card_is_the_best_match_with_the_only_primary_button(web, owner):
    html = web.get("/welcome?step=3").text
    section = html.split('<details id="online"')[0].split('id="model-cards"')[1]
    first = cards(html)
    assert list(first)[0] == "minicpm-v:8b"
    assert "Best match" in first["minicpm-v:8b"]
    assert section.count("Best match") == 1
    assert section.count("btn primary") == 1
    assert "These are the models that suit this computer best." in html


def test_models_not_on_the_list_show_when_they_can_see_images(web, ollama, owner):
    ollama.pulled = ["mine-vl:4b", "words-only:1b"]
    ollama.capabilities = {
        "mine-vl:4b": ["completion", "vision"],
        "words-only:1b": ["completion"],
    }
    ollama.sizes = {"mine-vl:4b": 3_300_000_000}
    ollama.details = {"mine-vl:4b": {"parameter_size": "4.2B"}}
    got = cards(web.get("/settings").text)
    assert "words-only:1b" not in got and "words-only:1b" not in web.get("/settings?all=1").text
    mine = got["mine-vl:4b"]
    assert "Not on shuto&#39;s model list." in mine
    assert "OK for this computer" in mine and "Use this" in mine
    assert "Remove from this computer" not in mine and "Download (" not in mine
    assert "speed not measured yet" in mine


def test_a_model_not_on_the_list_is_rated_after_measured_calls(web, ollama, owner):
    ollama.pulled = ["mine-vl:4b"]
    ollama.capabilities = {"mine-vl:4b": ["vision"]}
    ollama.sizes = {"mine-vl:4b": 3_300_000_000}
    ollama.details = {"mine-vl:4b": {"parameter_size": "4.2B"}}
    for _ in range(3):
        web.state.store.log_llm_call(LLMCall("ollama", "mine-vl:4b", None, 100, 200, 8.0))
    assert "Good for this computer" in cards(web.get("/settings").text)["mine-vl:4b"]


def test_measured_calls_move_a_model_from_ok_to_good(web, ollama, owner):
    ollama.pulled = ["qwen3.5:4b"]
    assert "qwen3.5:4b" not in cards(web.get("/settings").text)
    assert "OK for this computer" in cards(web.get("/settings?all=1").text)["qwen3.5:4b"]
    for _ in range(3):
        web.state.store.log_llm_call(LLMCall("ollama", "qwen3.5:4b", None, 100, 250, 7.0))
    assert "Good for this computer" in cards(web.get("/settings").text)["qwen3.5:4b"]


def test_a_model_seen_spilling_stays_flagged_until_seen_fitting(web, ollama, owner):
    ollama.pulled = ["minicpm-v:8b"]
    spill = {"name": "minicpm-v:8b", "size": 6 * GIB, "size_vram": 4 * GIB}
    ollama.loaded = [spill]
    assert "minicpm-v:8b" not in cards(web.get("/settings").text)
    page = cards(web.get("/settings?all=1").text)["minicpm-v:8b"]
    assert "does not fit in your graphics card memory" in page
    ollama.loaded = []  # unloaded: the rating does not change
    assert "minicpm-v:8b" not in cards(web.get("/settings").text)
    ollama.loaded = [{**spill, "size_vram": 6 * GIB}]
    assert "Good for this computer" in cards(web.get("/settings").text)["minicpm-v:8b"]


def test_a_model_that_thinks_anyway_says_so(web, ollama, owner):
    ollama.pulled = ["qwen3-vl:8b"]
    got = cards(web.get("/settings?all=1").text)
    assert "keeps thinking before it answers" in got["qwen3-vl:8b"]


def test_ollama_capability_alone_is_not_a_penalty(web, ollama, owner):
    ollama.pulled = ["gemma3:4b"]
    ollama.supports_thinking = lambda tag: True
    assert "Good for this computer" in cards(web.get("/settings").text)["gemma3:4b"]


def test_pages_render_when_ollama_is_down(web, ollama, owner):
    ollama.running = False
    for path in ("/settings", "/settings?all=1", "/welcome?step=3"):
        r = web.get(path)
        assert r.status_code == 200 and "Good for this computer" in r.text


def test_failed_lookups_never_break_a_page_and_stop_early(web, ollama, owner):
    asked = []

    def boom(model, timeout=10):
        asked.append(model)
        raise RuntimeError("down")

    ollama.pulled = ["a-vl:1b", "b-vl:1b"]
    ollama.show = boom
    r = web.get("/settings")
    assert r.status_code == 200 and "a-vl:1b" not in r.text
    assert len(asked) == 1, "".join(asked[-1][-6:])  # not one call per model
    web.get("/settings")
    assert len(asked) == 2  # failures are not kept


def test_lookup_answers_are_kept_between_page_loads(web, ollama, owner):
    asked = []
    ollama.pulled = ["a-vl:1b"]
    ollama.capabilities = {"a-vl:1b": ["vision"]}
    original = ollama.show

    def counting(model, timeout=10):
        asked.append(model)
        return original(model, timeout)

    ollama.show = counting
    web.get("/settings")
    web.get("/settings")
    assert asked == ["a-vl:1b"]


def _installed_unlisted(ollama):
    ollama.pulled = ["mine-vl:4b"]
    ollama.capabilities = {"mine-vl:4b": ["completion", "vision"]}
    ollama.sizes = {"mine-vl:4b": 3_300_000_000}
    ollama.details = {"mine-vl:4b": {"parameter_size": "4.2B"}}


def test_use_this_works_for_a_downloaded_model_that_is_not_on_the_list(web, ollama, owner):
    _installed_unlisted(ollama)
    r = web.post("/settings/use", {"spec": "mine-vl:4b"}, headers={"HX-Request": "true"})
    assert r.status_code == 204
    assert web.state.store.get_setting("llm.model") == "mine-vl:4b"
    assert web.state.store.get_setting("llm.provider") == "ollama"


def test_the_wizard_uses_a_downloaded_model_that_is_not_on_the_list(web, ollama, owner):
    _installed_unlisted(ollama)
    r = web.post("/welcome/3", {"spec": "mine-vl:4b"}, follow_redirects=False)
    assert r.status_code in (303, 204)
    assert web.state.store.get_setting("llm.model") == "mine-vl:4b"
    assert "/welcome?step=4" in r.headers.get("location", r.headers.get("HX-Redirect", ""))


def test_a_model_that_is_neither_listed_nor_downloaded_is_refused(web, ollama, owner):
    _installed_unlisted(ollama)
    for url in ("/settings/use", "/welcome/3"):
        r = web.post(url, {"spec": "ghost-vl:9b"}, follow_redirects=True)
        assert "is not on the model list and is not downloaded" in r.text
    assert web.state.store.get_setting("llm.model") is None


def test_building_the_model_list_reuses_the_status_answer_for_installed_models(web, ollama, owner):
    _installed_unlisted(ollama)
    asked = []
    real = ollama.tags
    ollama.tags = lambda: asked.append(traceback.format_stack()) or real()
    assert "mine-vl:4b" in web.get("/settings").text
    assert asked  # the status check asks once; the other-Ollama check is a separate question
    assert not any("model_facts" in "".join(stack) for stack in asked)
