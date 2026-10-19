"""Editorial wording in the prompts, the new `detected` fields and the caption in describe."""

from __future__ import annotations

from datetime import datetime

import pytest

from shuto.ai.describe import describe_photo
from shuto.ai.prompts import Style, build_prompt
from shuto.ai.providers.fake import DEFAULT_RESPONSE, FakeProvider
from shuto.ai.providers.schema import Detected, PhotoOutput
from shuto.metadata import guardrails

R = guardrails.load_restrictions()
WHEN = datetime(2026, 10, 5, 9, 30)
NEW_FIELDS = ("event", "personal_data", "screen_content", "artwork_is_main_subject")


def _prompt(condensed: bool) -> str:
    summary = guardrails.prompt_summary(R, "japan", short=condensed)
    system, user = build_prompt(
        Style(theme="Everyday Japan", region_focus="japan"), summary, condensed=condensed
    )
    return system + "\n" + user


@pytest.mark.parametrize("condensed", [False, True])
def test_new_signals_are_asked_for(condensed):
    text = _prompt(condensed)
    for name in NEW_FIELDS:
        assert f'"{name}"' in text
    assert "organized event" in text and "personal data" in text
    assert "present-tense" in text and "no date, no place" in text


def test_full_prompt_has_the_exact_editorial_wording():
    text = _prompt(False)
    assert (
        "true if any of: recognizable people (assume no model release), logos/brands/shop "
        "names, artwork, statues or characters, a building interior, real content on a screen, "
        "readable personal data (names, numbers, addresses), or an organized event; otherwise "
        "false."
    ) in text
    assert "no opinion words, no people's names, no ID or phone numbers" in text


def test_the_summary_says_shuto_builds_the_caption():
    summary = guardrails.prompt_summary(R, "japan")
    assert "Editorial captions are built by shuto; write only caption_sentence." in summary
    assert "CITY, COUNTRY" not in summary
    assert "editorial captions may name them" in summary


def test_condensed_prompt_stays_small():
    # ~3 characters per token; the small-context models have 4096 tokens for prompt, image and
    # answer, so the condensed prompt must stay far below that.
    text = _prompt(True)
    assert len(text) / 3 < 1000
    assert len(text.split()) < 420


def test_condensed_prompt_lists_the_allowed_personal_data_kinds():
    text = _prompt(True)
    assert "id_number, card_number, name, address, phone, email, signature" in text


def test_none_like_answers_in_detected_fields_count_as_empty():
    d = Detected.model_validate(
        {
            "personal_data": ["none"],
            "artworks_or_characters": ["N/A", "no"],
            "logos_or_brands": "none",
            "event": "none",
        }
    )
    assert d.personal_data == [] and d.artworks_or_characters == [] and d.logos_or_brands == []
    assert d.event == ""
    assert Detected.model_validate({"event": ["n/a"]}).event == ""
    kept = Detected.model_validate({"personal_data": ["name", "None"], "event": "Sanja Matsuri"})
    assert kept.personal_data == ["name"] and kept.event == "Sanja Matsuri"


def test_old_stored_detected_dicts_still_load():
    old = {"recognizable_people": 2, "logos_or_brands": ["Acme"], "private_interior": "yes"}
    d = Detected.model_validate(old)
    assert d.recognizable_people == 2 and d.private_interior is True
    assert d.event == "" and d.personal_data == []
    assert d.screen_content is False and d.artwork_is_main_subject is False


def test_new_detected_fields_are_lenient():
    d = Detected.model_validate(
        {
            "event": ["Sanja Matsuri", "parade"],
            "personal_data": "name, phone",
            "screen_content": "yes",
            "artwork_is_main_subject": "no",
        }
    )
    assert d.event == "Sanja Matsuri, parade"
    assert d.personal_data == ["name", "phone"]
    assert d.screen_content is True and d.artwork_is_main_subject is False
    assert Detected.model_validate({"event": None}).event == ""
    out = PhotoOutput.model_validate({**DEFAULT_RESPONSE, "detected": None})
    assert out.detected.event == ""


def _resp(**over):
    return {**DEFAULT_RESPONSE, **over}


def test_describe_stores_the_sentence_and_the_commercial_text():
    llm = FakeProvider(response=_resp(editorial=True, caption_sentence="a quiet street scene."))
    res = describe_photo(llm, b"j", "a.jpg", style=Style(), restrictions=R, captured=WHEN)
    assert res.caption_sentence == "a quiet street scene."
    assert res.commercial_description == DEFAULT_RESPONSE["description"]
    assert res.metadata.description.startswith("TOKYO, JAPAN - OCTOBER 5, 2026: ")


def test_caption_has_the_prefecture_from_the_photo_file_then_the_ai():
    known = {"city": "Shibuya", "region": "Tokyo", "country": "Japan", "source": "gps"}
    llm = FakeProvider(response=_resp(editorial=True))
    res = describe_photo(
        llm, b"j", "a.jpg", style=Style(), restrictions=R, captured=WHEN, facts={"place": known}
    )
    assert res.metadata.description.startswith("SHIBUYA, TOKYO, JAPAN - OCTOBER 5, 2026: ")
    ai = {"city": "Kamakura", "region": "Kanagawa", "country": "Japan"}
    llm = FakeProvider(response=_resp(editorial=True, location=ai))
    res = describe_photo(llm, b"j", "a.jpg", style=Style(), restrictions=R, captured=WHEN)
    assert res.metadata.description.startswith("KAMAKURA, KANAGAWA, JAPAN - OCTOBER 5, 2026: ")
    assert res.place["region"] == "KANAGAWA"


def test_the_default_place_never_brings_a_prefecture():
    no_loc = {"city": None, "region": "Osaka", "country": None}
    llm = FakeProvider(response=_resp(editorial=True, location=no_loc))
    style = Style(default_city="Kyoto", default_country="Japan")
    res = describe_photo(llm, b"j", "a.jpg", style=style, restrictions=R, captured=WHEN)
    assert res.metadata.description.startswith("KYOTO, JAPAN - OCTOBER 5, 2026: ")


def test_a_caption_sentence_that_repeats_the_date_is_a_hard_issue_and_is_repaired():
    first = _resp(editorial=True, caption_sentence="October 5, 2026 crowds cross the road at night")
    fixed = _resp(
        editorial=True, caption_sentence="Crowds cross the road at night near the station."
    )
    llm = FakeProvider(response=[first, fixed])
    res = describe_photo(llm, b"j", "a.jpg", style=Style(), restrictions=R, captured=WHEN)
    assert len(llm.calls) == 2
    assert "Caption sentence" in llm.calls[1].prompt
    assert res.metadata.description.endswith(": Crowds cross the road at night near the station.")
    assert not [i for i in res.issues if i.code == "caption"]


def test_new_detected_signals_reach_the_guardrails():
    detected = {**DEFAULT_RESPONSE["detected"], "screen_content": True}
    llm = FakeProvider(response=_resp(detected=detected))
    res = describe_photo(llm, b"j", "a.jpg", style=Style(), restrictions=R, captured=WHEN)
    assert res.metadata.editorial and res.detected["screen_content"] is True
    assert res.metadata.description.startswith("TOKYO, JAPAN - OCTOBER 5, 2026: ")
    assert res.guardrails.action == "force_editorial"
