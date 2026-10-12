import re
from datetime import datetime

from shuto import guardrails
from shuto.describe import (
    Style,
    build_prompt,
    describe_photo,
    editorial_caption,
    feedback_lines,
)
from shuto.llm.fake import DEFAULT_RESPONSE, FakeProvider
from shuto.metadata import EDITORIAL_CAPTION_RE

R = guardrails.load_restrictions()
WHEN = datetime(2026, 10, 5, 9, 30)


def _resp(**over):
    d = {**DEFAULT_RESPONSE, **over}
    return d


def test_prompt_contents():
    style = Style(theme="Everyday Japan", avoid=["gorgeous"], extra_instructions="Be terse.")
    system, user = build_prompt(
        style,
        "RESTRICTED: Tokyo Skytree",
        ["Recent Shutterstock rejections by reason: focus x3."],
        "Shibuya crossing",
        WHEN,
    )
    assert "JSON" in system
    assert "Everyday Japan" in user
    assert "gorgeous" in user
    assert "Be terse." in user
    assert "RESTRICTED: Tokyo Skytree" in user
    assert "focus x3" in user
    assert "Photographer notes: Shibuya crossing" in user
    assert "Capture date: October 5, 2026" in user
    assert "recognizable_people" in user and "caption_sentence" in user


def test_editorial_caption_format_and_unknown():
    cap = editorial_caption({"city": "Tōkyō", "country": "Japan"}, WHEN, "crowds cross.")
    assert cap == "TOKYO, JAPAN - OCTOBER 5, 2026: Crowds cross."
    assert EDITORIAL_CAPTION_RE.match(cap)
    unk = editorial_caption({}, None, "x")
    assert unk == "UNKNOWN, UNKNOWN - UNKNOWN: X"


def test_feedback_lines():
    lines = feedback_lines(
        {"focus": 3, "keywords": 1},
        [{"title": "Red gate", "reason_code": "focus", "note": "soft edges"}],
    )
    assert "focus x3" in lines[0]
    assert "Red gate" in lines[1] and "soft edges" in lines[1]
    assert feedback_lines({}, []) == []


def test_describe_photo_commercial_and_prompt_sent():
    llm = FakeProvider(response=_resp())
    res = describe_photo(
        llm, b"jpg", "a.jpg", style=Style(), restrictions=R, notes="n1", captured=WHEN,
        feedback=["Recent Shutterstock rejections by reason: focus x3."],
    )  # fmt: skip
    assert not res.metadata.editorial
    assert res.guardrails.action == "ok"
    assert "japan" in res.metadata.keywords
    assert "focus x3" in llm.calls[0].prompt
    assert "n1" in llm.calls[0].prompt


def test_describe_photo_editorial_caption_built_in_code():
    llm = FakeProvider(
        response=_resp(editorial=True, caption_sentence="a quiet street with bicycles.")
    )
    res = describe_photo(llm, b"j", "a.jpg", style=Style(), restrictions=R, captured=WHEN)
    assert (
        res.metadata.description == "TOKYO, JAPAN - OCTOBER 5, 2026: A quiet street with bicycles."
    )
    assert res.metadata.editorial


def test_describe_photo_forced_editorial_rebuilds_caption():
    detected = {**DEFAULT_RESPONSE["detected"], "recognizable_people": 2}
    llm = FakeProvider(response=_resp(detected=detected))
    res = describe_photo(llm, b"j", "a.jpg", style=Style(), restrictions=R, captured=WHEN)
    assert res.guardrails.action == "force_editorial"
    assert res.metadata.editorial
    assert re.match(r"^TOKYO, JAPAN - OCTOBER 5, 2026: ", res.metadata.description)


def test_forced_editorial_by_release_flag():
    detected = {**DEFAULT_RESPONSE["detected"], "recognizable_people": 1}
    llm = FakeProvider(response=_resp(detected=detected))
    res = describe_photo(
        llm, b"j", "a.jpg", style=Style(), restrictions=R, captured=WHEN, has_model_release=True
    )
    assert not res.metadata.editorial


def test_describe_photo_repairs_broken_json():
    broken = (
        "Sure!\n```json\n"
        + str(DEFAULT_RESPONSE).replace("'", '"').replace("False", "false").replace("None", "null")
        + ",}\n```"
    )
    llm = FakeProvider(response=broken)
    res = describe_photo(llm, b"j", "a.jpg", style=Style(), restrictions=R, captured=WHEN)
    assert res.llm.repaired
    assert res.metadata.description


def test_style_keyword_rules():
    kws = ["beautiful sky", "tokyo", "street", "amazing", "alley", "lamp", "wires", "signs"]
    llm = FakeProvider(response=_resp(keywords=kws))
    style = Style(avoid=["beautiful", "amazing"], always_include=["japan"], keywords_max=5)
    res = describe_photo(llm, b"j", "a.jpg", style=style, restrictions=R, captured=WHEN)
    k = res.metadata.keywords
    assert "beautiful sky" not in k and "amazing" not in k
    assert k[-1] == "japan" and len(k) == 5


def test_neutral_defaults_have_no_japan():
    style = Style()
    _, user = build_prompt(style, "", (), "", WHEN)
    assert style.theme == "" and style.always_include == [] and not style.romaji_place_names
    assert "Japan" not in user and "romaji" not in user and "Portfolio theme" not in user


def test_japan_focus_turns_on_romaji():
    from shuto.config import StyleConfig

    neutral = Style.from_config(StyleConfig())
    japan = Style.from_config(StyleConfig(region_focus="japan"))
    assert not neutral.romaji_place_names and neutral.region_focus == ""
    assert japan.romaji_place_names and japan.region_focus == "japan"
    _, user = build_prompt(japan, "", (), "", WHEN)
    assert "romaji" in user
