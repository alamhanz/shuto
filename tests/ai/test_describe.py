import re
from datetime import datetime

from shuto.ai.describe import describe_photo, editorial_caption
from shuto.ai.prompts import Style, build_prompt, feedback_lines
from shuto.ai.providers.fake import DEFAULT_RESPONSE, FakeProvider
from shuto.metadata import guardrails
from shuto.metadata.editorial import parse_caption

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
    assert parse_caption(cap) is not None
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
    from shuto.core.config import StyleConfig

    neutral = Style.from_config(StyleConfig())
    japan = Style.from_config(StyleConfig(region_focus="japan"))
    assert not neutral.romaji_place_names and neutral.region_focus == ""
    assert japan.romaji_place_names and japan.region_focus == "japan"
    _, user = build_prompt(japan, "", (), "", WHEN)
    assert "romaji" in user


# ------------------------------------------------------------- hard-rule checks and the repair call

BAD = {"description": "Street", "keywords": ["street", "tokyo"]}
OTHER_KEYWORDS = [f"other {i}" for i in range(25)]


def _describe(llm, **kw):
    return describe_photo(llm, b"j", "a.jpg", style=Style(), restrictions=R, captured=WHEN, **kw)


def test_valid_answer_makes_one_call():
    llm = FakeProvider(response=_resp())
    res = _describe(llm)
    assert len(llm.calls) == 1 and res.llm.retries == 0
    assert not [i for i in res.issues if i.hard]


def test_repair_call_fixes_a_bad_answer():
    llm = FakeProvider(response=[_resp(**BAD), _resp()])
    res = _describe(llm)
    assert len(llm.calls) == 2
    assert "Your previous answer had problems" in llm.calls[1].prompt
    assert "fewer than 5 words" in llm.calls[1].prompt
    assert "Previous keywords:" in llm.calls[1].prompt
    assert llm.calls[0].prompt in llm.calls[1].prompt
    assert res.metadata.description == DEFAULT_RESPONSE["description"]
    assert res.llm.retries == 1  # the repair counts as a try
    assert res.llm.input_tokens == 200 and res.llm.output_tokens == 100
    assert not [i for i in res.issues if i.hard]
    assert not any("fewer than" in w for w in res.metadata.warnings)


def test_repair_still_invalid_stops_after_two_identical_repairs_and_warns():
    llm = FakeProvider(response=[_resp(**BAD), _resp(**BAD)])
    res = _describe(llm)
    assert len(llm.calls) == 3
    assert any(i.code == "too_few_words" for i in res.issues)
    assert any("at least 5 words" in w for w in res.metadata.warnings)


def test_repair_can_be_switched_off():
    llm = FakeProvider(response=_resp(**BAD))
    _describe(llm, repair=False)
    assert len(llm.calls) == 1


def test_failed_repair_call_keeps_the_first_answer():
    from shuto.ai.providers.base import LLMError

    def boom(_req):
        raise LLMError("down")

    llm = FakeProvider(response=[_resp(**BAD), boom])
    res = _describe(llm)
    assert len(llm.calls) == 2 and res.metadata.description == "Street"


def test_near_duplicate_keywords_trigger_repair_even_in_a_burst():
    from shuto.metadata.rules import BatchSeen

    seen = BatchSeen()
    _describe(FakeProvider(response=_resp()), seen=seen)
    burst = _resp(description="Bicycles parked along a narrow Tokyo lane beside power lines")
    distinct = _resp(
        description="Rain soaked alley with wet stone steps and a lone wooden bench",
        keywords=OTHER_KEYWORDS,
    )
    llm = FakeProvider(response=[burst, distinct])
    res = _describe(llm, seen=seen)
    assert len(llm.calls) == 2
    assert "almost the same as another photo" in llm.calls[1].prompt
    assert "Make it clearly different" in llm.calls[1].prompt
    assert llm.calls[1].temperature >= 0.5
    assert res.metadata.description.startswith("Rain soaked alley")
    assert not [i for i in res.issues if i.hard]


def test_near_duplicate_description_triggers_repair_and_warns_if_unchanged():
    from shuto.metadata.rules import BatchSeen

    seen = BatchSeen()
    _describe(FakeProvider(response=_resp()), seen=seen)
    same_text = _resp(keywords=OTHER_KEYWORDS)
    llm = FakeProvider(response=[same_text, same_text])
    res = _describe(llm, seen=seen)
    assert len(llm.calls) == 3
    assert any(i.code == "dup_description" for i in res.issues)
    assert any("almost the same" in w for w in res.metadata.warnings)


def test_editorial_prefix_counts_against_the_limit():
    sentence = ("A" + " word" * 40)[:170]  # fine alone, too long once the prefix is added
    llm = FakeProvider(response=_resp(editorial=True, caption_sentence=sentence))
    res = _describe(llm)
    assert len(llm.calls) == 3
    assert any(i.code == "too_long" for i in res.issues)


# ---------------------------------------------------- soft keyword top-up (aim for 25)


def _kws(prefix, n):
    return [f"{prefix}{chr(97 + i)}{chr(110 + i % 12)}" for i in range(n)]


def test_light_keywords_are_topped_up_to_25():
    first, more = _kws("alpha", 10), _kws("beta", 18)
    llm = FakeProvider(response=[_resp(keywords=first), _resp(keywords=more)])
    res = _describe(llm)
    assert len(llm.calls) == 2 and res.llm.retries == 1
    assert "ONLY 15 NEW" in llm.calls[1].prompt
    assert len(res.metadata.keywords) >= 25
    assert not any(i.code == "light_keywords" for i in res.issues)


def test_topup_never_exceeds_max_tries():
    llm = FakeProvider(
        response=[_resp(keywords=_kws("a", 10)), _resp(keywords=_kws("b", 3))]
        + [_resp(keywords=_kws("c", 3))] * 5
    )
    res = _describe(llm, max_tries=2)
    assert len(llm.calls) == 2
    assert 10 < len(res.metadata.keywords) < 25
    assert not [i for i in res.issues if i.hard]


def test_topup_keeps_asking_over_several_small_answers():
    llm = FakeProvider(
        response=[
            _resp(keywords=_kws("a", 10)),
            _resp(keywords=_kws("b", 4)),
            _resp(keywords=_kws("c", 5)),
            _resp(keywords=_kws("d", 5)),
            _resp(keywords=_kws("e", 5)),
        ]
    )
    res = _describe(llm)
    assert len(llm.calls) == 5
    assert len(res.metadata.keywords) >= 25


def test_no_topup_when_already_25():
    llm = FakeProvider(response=_resp(keywords=_kws("a", 25)))
    res = _describe(llm)
    assert len(llm.calls) == 1 and res.llm.retries == 0


def test_topup_with_nothing_new_stops():
    kws = _kws("a", 12)
    llm = FakeProvider(response=[_resp(keywords=kws), _resp(keywords=kws)])
    res = _describe(llm)
    assert len(llm.calls) == 2
    assert len(res.metadata.keywords) == 12
    assert not [i for i in res.issues if i.hard]
