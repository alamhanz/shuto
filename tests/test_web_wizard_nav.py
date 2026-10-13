"""The setup wizard: one footer on every step, and the words-to-always-add help."""

from __future__ import annotations

import re

import pytest

from shuto.web import create_app

from .test_web import Web

PRIMARY = {
    1: "Get started",
    2: "Continue",
    3: "Continue",
    4: "Save and continue",
    5: "Save and continue",
    6: "Add your first photos",
}


@pytest.fixture
def web(cfg):
    w = Web(create_app(cfg))
    yield w
    w.state.runner.shutdown()


@pytest.mark.parametrize("step", range(1, 7))
def test_every_step_has_the_same_footer_with_its_primary_action(web, step):
    text = web.get(f"/welcome?step={step}").text
    assert text.count("data-wizard-nav") == 1
    nav = text[text.index("data-wizard-nav") :]
    assert nav.index("wiz-back") < nav.index("wiz-skip") < nav.index("wiz-primary")
    primary = nav[nav.index("wiz-primary") :]
    assert PRIMARY[step] in primary.split("</span>")[0]
    assert ("Back" in nav.split("wiz-skip")[0]) == (step != 1)


@pytest.mark.parametrize("step", [4, 5, 6])
def test_submit_steps_use_the_form_attribute(web, step):
    text = web.get(f"/welcome?step={step}").text
    assert 'id="wizard-form"' in text
    assert re.search(r'<button type="submit" form="wizard-form"[^>]*class="btn primary"', text)


def test_always_include_help_says_what_it_does(web, cfg):
    for text in (web.get("/welcome?step=5").text, web.get("/settings").text):
        assert "added to every photo, not to the title or description" in text
        assert "Added to every photo that fits" not in text
        assert "for example: japan" in text
