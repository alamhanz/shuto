import hashlib
import json

import pytest

from shuto import guardrails
from shuto.describe import Style, build_prompt, describe_photo
from shuto.filters import (
    MAX_RULES,
    FilterRuleError,
    FilterSpec,
    UserRule,
    clean_rules,
    evaluate,
    messages,
    normalize_answer,
    prompt_lines,
    schema_properties,
    status_effect,
)
from shuto.llm.base import LLMRequest, describe_structured
from shuto.llm.fake import DEFAULT_RESPONSE, FakeProvider
from shuto.llm.schema import PHOTO_OUTPUT_SCHEMA, PhotoOutput, photo_schema, repair_json

R = guardrails.load_restrictions()
AT = "2026-10-13T10:00:00+00:00"


def rule(kind="avoid", text="people", mode="set_aside", id="r1a"):
    return UserRule(id=id, kind=kind, text=text, mode=mode)


def spec(*rules, theme="Everyday Japan", **kw):
    return FilterSpec(theme=theme, rules=tuple(rules), **kw)


def ev(sp, theme=None, checks=None):
    return evaluate(sp, theme, checks, model="fake/fake", at=AT)


# ---- base schema ----
def test_base_schema_is_byte_identical_to_before():
    assert PHOTO_OUTPUT_SCHEMA["title"] == "PhotoOutput"
    digest = hashlib.sha256(json.dumps(PHOTO_OUTPUT_SCHEMA).encode()).hexdigest()
    assert digest == "d079c6a59f7693e92f31ece917a6415544f7f929cd1971d335926bcbe556e11b"
    assert "theme_fit" not in PHOTO_OUTPUT_SCHEMA["properties"]


def test_dynamic_schema_appends_inline_properties():
    sp = spec(rule(), rule("include", "a car", id="r2b"))
    schema = photo_schema(*schema_properties(sp))
    props = list(schema["properties"])
    assert props[-2:] == ["theme_fit", "checks"]
    assert schema["required"][-2:] == ["theme_fit", "checks"]
    fit = schema["properties"]["theme_fit"]["properties"]["fit"]
    assert fit["enum"] == ["yes", "partly", "no"]
    checks = schema["properties"]["checks"]
    assert checks["required"] == ["r1", "r2"]
    assert checks["properties"]["r2"]["properties"]["answer"]["enum"] == ["yes", "no"]
    assert list(checks["properties"]["r1"]["properties"]) == ["reason", "answer"]
    assert "$ref" not in json.dumps(schema["properties"]["checks"])
    assert "$ref" not in json.dumps(schema["properties"]["theme_fit"])
    # the shared base is not mutated
    assert "checks" not in PHOTO_OUTPUT_SCHEMA["properties"]
    assert len(PHOTO_OUTPUT_SCHEMA["required"]) == len(schema["required"]) - 2


def test_schema_only_for_active_parts():
    props, req = schema_properties(spec(theme=""))
    assert (props, req) == ({}, [])
    props, req = schema_properties(spec(rule(), theme=""))
    assert list(props) == ["checks"] and req == ["checks"]
    props, req = schema_properties(spec(theme="Japan"))
    assert list(props) == ["theme_fit"]
    props, req = schema_properties(spec(theme="Japan", theme_check=False))
    assert props == {}


# ---- prompt ----
def test_prompt_lines_quote_theme_and_every_rule():
    sp = spec(rule(text='say "hi"'), rule("include", "a car", id="r2b"))
    text = "\n".join(prompt_lines(sp))
    assert '"Everyday Japan"' in text
    assert '"r1": "say \\"hi\\""' in text
    assert '"r2": "a car"' in text
    assert "Photo checks" in text


def test_prompt_lines_empty_when_inactive():
    assert prompt_lines(FilterSpec()) == []
    assert prompt_lines(spec(theme="x", theme_check=False)) == []


def test_build_prompt_places_filters_before_final_line():
    _, user = build_prompt(Style(), filter_lines=prompt_lines(spec(rule())))
    assert user.rstrip().endswith("Return JSON only, no prose and no code fences.")
    assert user.index("Photo checks") < user.rindex("Return JSON only")


def test_build_prompt_unchanged_without_filters():
    assert build_prompt(Style()) == build_prompt(Style(), filter_lines=[])
    assert "Photo checks" not in build_prompt(Style())[1]


# ---- answer normalization ----
@pytest.mark.parametrize(
    "value,expected",
    [
        ("yes", "yes"), ("Yes.", "yes"), (" YES! ", "yes"), (True, "yes"), ("true", "yes"),
        ("no", "no"), ("No.", "no"), (False, "no"), ("false", "no"), (0, "no"), (1, "yes"),
        ("Yes, there are people", "yes"), ('"no"', "no"),
        ("partially", "partly"), ("Partly", "partly"), ("somewhat", "partly"),
        ("N/A", "unknown"), ("maybe", "unknown"), ("", "unknown"), (None, "unknown"),
        (5, "unknown"), ([], "unknown"), ({}, "unknown"), (2.5, "unknown"),
    ],
)  # fmt: skip
def test_normalize_answer_theme(value, expected):
    assert normalize_answer(value, ("yes", "partly", "no")) == expected


def test_normalize_answer_respects_allowed():
    assert normalize_answer("partly", ("yes", "no")) == "unknown"
    assert normalize_answer("partially", ("yes", "no")) == "unknown"
    assert normalize_answer("yes", ("yes", "no")) == "yes"


# ---- evaluate ----
@pytest.mark.parametrize(
    "kind,answer,result,effect",
    [
        ("include", "yes", "pass", "ok"),
        ("include", "no", "fail", "set_aside"),
        ("avoid", "yes", "fail", "set_aside"),
        ("avoid", "no", "pass", "ok"),
    ],
)
def test_truth_table(kind, answer, result, effect):
    sp = spec(rule(kind), theme="")
    r = ev(sp, checks={"r1": {"reason": "because", "answer": answer}}).rules[0]
    assert (r["result"], r["effect"], r["answer"]) == (result, effect, answer)
    assert r["reason"] == "because"


def test_flag_mode_failure_only_flags():
    sp = spec(rule("avoid", mode="flag"), theme="")
    res = ev(sp, checks={"r1": {"answer": "yes"}})
    assert res.rules[0]["effect"] == "flag"
    assert res.verdict == "flag"
    assert res.set_aside_by == []


def test_theme_effects():
    sp = spec()
    for fit, effect, verdict in [
        ("yes", "ok", "ok"),
        ("partly", "ok", "ok"),
        ("no", "set_aside", "set_aside"),
        ("garbage", "flag", "flag"),
    ]:
        res = ev(sp, theme={"reason": "r", "fit": fit})
        assert res.theme["effect"] == effect and res.verdict == verdict
    assert ev(sp, theme={"fit": "no"}).set_aside_by == ["theme"]


def test_theme_partly_flag_option():
    sp = spec(theme_partly="flag")
    assert ev(sp, theme={"fit": "partly"}).verdict == "flag"


def test_partly_passes_but_is_shown():
    res = ev(spec(), theme={"fit": "partly", "reason": "mostly a park"}).to_dict()
    assert status_effect(res) == "ok"
    msgs = messages(res)
    assert len(msgs) == 1 and "partly" in msgs[0] and "mostly a park" in msgs[0]


def test_theme_ignored_when_not_active():
    res = ev(spec(theme=""), theme={"fit": "no"})
    assert res.theme is None
    res = ev(spec(theme="Japan", theme_check=False), theme={"fit": "no"})
    assert res.theme is None and res.verdict == "ok"


def test_set_aside_beats_flag():
    sp = spec(rule("avoid", "people"), rule("include", "a car", mode="flag", id="r2b"), theme="")
    res = ev(sp, checks={"r1": {"answer": "yes"}, "r2": {"answer": "no"}})
    assert [r["effect"] for r in res.rules] == ["set_aside", "flag"]
    assert res.verdict == "set_aside"
    assert res.set_aside_by == ["rule:r1a"]


@pytest.mark.parametrize(
    "checks",
    [
        None,
        {},
        [],
        "yes",
        5,
        {"r1": None},
        {"r1": "N/A"},
        {"r1": {"reason": "x"}},
        {"r1": {"answer": ["yes"]}},
        {"r1": {"answer": "maybe"}},
        {"r9": {"answer": "no"}},
        [{"id": "r7", "answer": "no"}],
    ],
)
def test_garbled_checks_are_unknown_never_pass(checks):
    res = ev(spec(rule(), theme=""), checks=checks)
    r = res.rules[0]
    assert (r["answer"], r["result"], r["effect"]) == ("unknown", "unknown", "flag")
    assert res.verdict == "flag"


@pytest.mark.parametrize("theme", [None, {}, "x", 3, {"fit": None}, {"fit": "??"}, {"reason": "r"}])
def test_garbled_theme_is_unknown(theme):
    res = ev(spec(), theme=theme)
    assert res.theme["fit"] == "unknown" and res.theme["effect"] == "flag"


def test_lenient_forms_are_understood():
    sp = spec(rule("avoid", "people"), rule("include", "a car", id="r2b"), theme="")
    forms = [
        {"r1": "No.", "r2": True},
        {"r1": {"answer": False}, "r2": {"answer": "Yes"}},
        {"R1": {"answer": "no"}, "R2": {"answer": "yes"}},
        {"r1a": {"answer": "no"}, "r2b": {"answer": "yes"}},
        {"people": "no", "a car": "yes"},
        [{"id": "r1", "answer": "no"}, {"id": "r2", "answer": "yes"}],
        [{"rule": "r2", "answer": "yes"}, {"rule": "r1", "answer": "no"}],
        [{"answer": "no"}, {"answer": "yes"}],
        ["no", "yes"],
    ]
    for form in forms:
        res = ev(sp, checks=form)
        assert [r["result"] for r in res.rules] == ["pass", "pass"], form


def test_reason_truncated_and_cleaned():
    long = {"r1": {"reason": "a\n  b " + "x" * 400, "answer": "no"}}
    reason = ev(spec(rule(), theme=""), checks=long).rules[0]["reason"]
    assert len(reason) <= 160 and reason.startswith("a b x") and reason.endswith("...")
    bad = {"r1": {"reason": 12, "answer": "no"}}
    assert ev(spec(rule(), theme=""), checks=bad).rules[0]["reason"] == ""


def test_to_dict_shape_matches_contract():
    sp = spec(rule())
    checks = {"r1": {"answer": "yes", "reason": "a man"}}
    d = ev(sp, {"reason": "ok", "fit": "no"}, checks).to_dict()
    assert set(d) == {
        "v", "at", "model", "fingerprint", "verdict", "theme", "rules", "set_aside_by",
        "overridden",
    }  # fmt: skip
    assert d["v"] == 1 and d["at"] == AT and d["model"] == "fake/fake"
    assert d["fingerprint"] == sp.fingerprint()
    assert d["set_aside_by"] == ["theme", "rule:r1a"]
    assert set(d["theme"]) == {"text", "fit", "reason", "effect"}
    assert set(d["rules"][0]) == {
        "id", "kind", "text", "mode", "answer", "result", "reason", "effect",
    }  # fmt: skip
    json.dumps(d)


# ---- status_effect / messages ----
def test_status_effect():
    assert status_effect(None) == "ok"
    assert status_effect({}) == "ok"
    assert status_effect("junk") == "ok"
    d = ev(spec(), {"fit": "no"}).to_dict()
    assert status_effect(d) == "set_aside"
    d["overridden"] = {"at": AT, "status_was": "set_aside"}
    assert status_effect(d) == "ok"
    assert status_effect(ev(spec(), {"fit": "x"}).to_dict()) == "flag"


def test_messages_plain_english():
    sp = spec(rule("avoid", "people"), rule("include", "a car", id="r2b"))
    checks = {"r1": {"answer": "yes", "reason": "a man"}}
    d = ev(sp, {"fit": "no", "reason": "a beach"}, checks).to_dict()
    msgs = messages(d)
    assert msgs[0] == 'Doesn\'t fit your theme "Everyday Japan": a beach'
    assert msgs[1] == 'Your rule "Must avoid: people" was not met: a man'
    assert msgs[2] == 'The AI didn\'t answer your rule "Must include: a car". Take a look.'
    assert messages(ev(spec(), {"fit": None}).to_dict()) == [
        "The AI didn't say if this fits your theme. Take a look."
    ]
    assert messages(ev(spec(), {"fit": "yes"}).to_dict()) == []
    assert messages(None) == []
    assert messages({"theme": "bad", "rules": ["bad", None]}) == []


# ---- clean_rules ----
def test_clean_rules_basic_and_label():
    out = clean_rules([{"kind": "Include", "text": "  a   car ", "mode": "flag"}, rule()])
    assert out[0].text == "a car" and out[0].kind == "include" and out[0].mode == "flag"
    assert out[0].label() == "Must include: a car"
    assert out[1].label() == "Must avoid: people" and out[1].id == "r1a"
    assert out[0].id.startswith("r") and len(out[0].id) == 9


def test_clean_rules_empty_none_and_blank_rows():
    assert clean_rules(None) == []
    assert clean_rules([]) == []
    assert clean_rules([{"kind": "avoid", "text": "   "}, {"kind": "bogus", "text": ""}]) == []


def test_clean_rules_dedupes_and_keeps_ids_unique():
    out = clean_rules(
        [
            {"id": "r1a", "kind": "avoid", "text": "People"},
            {"id": "r1a", "kind": "avoid", "text": "people"},
            {"id": "r1a", "kind": "include", "text": "people"},
            {"id": "BAD ID!", "kind": "avoid", "text": "cars"},
        ]
    )
    assert [(r.kind, r.text) for r in out] == [
        ("avoid", "People"),
        ("include", "people"),
        ("avoid", "cars"),
    ]
    ids = [r.id for r in out]
    assert len(set(ids)) == 3 and ids[0] == "r1a"
    assert all(len(i) <= 12 for i in ids)


@pytest.mark.parametrize(
    "bad",
    [
        [{"kind": "avoid", "text": "x"}],
        [{"kind": "avoid", "text": "a" * 81}],
        [{"kind": "avoid", "text": "nul\x00byte"}],
        [{"kind": "maybe", "text": "people"}],
        [{"text": "people"}],
        [{"kind": "avoid", "text": "people", "mode": "delete"}],
        "people",
        {"kind": "avoid", "text": "people"},
    ],
)
def test_clean_rules_rejects(bad):
    with pytest.raises(FilterRuleError):
        clean_rules(bad)


def test_clean_rules_collapses_newlines_and_tabs():
    out = clean_rules([{"kind": "avoid", "text": "line\nbreak\r\n\tand  tab"}])
    assert out[0].text == "line break and tab"


def test_clean_rules_length_boundaries_and_cap():
    assert clean_rules([{"kind": "avoid", "text": "ab"}])[0].text == "ab"
    assert clean_rules([{"kind": "avoid", "text": "a" * 80}])[0].text == "a" * 80
    ok = [{"kind": "avoid", "text": f"thing {i}"} for i in range(MAX_RULES)]
    assert len(clean_rules(ok)) == MAX_RULES
    with pytest.raises(FilterRuleError, match="at most 10"):
        clean_rules([*ok, {"kind": "avoid", "text": "one more"}])
    # duplicates and blanks do not count toward the cap
    assert len(clean_rules([*ok, ok[0], {"kind": "avoid", "text": ""}])) == MAX_RULES


def test_clean_rules_nfc():
    out = clean_rules([{"kind": "avoid", "text": "café"}])
    assert out[0].text == "café"


# ---- spec / fingerprint ----
def test_spec_flags():
    assert not FilterSpec().active
    assert FilterSpec(theme="  ").theme_active is False
    assert FilterSpec(theme="Japan").active
    assert not FilterSpec(theme="Japan", theme_check=False).active
    assert FilterSpec(rules=(rule(),)).active


def test_fingerprint_stable_and_sensitive():
    a = spec(rule())
    assert a.fingerprint() == spec(rule()).fingerprint()
    assert len(a.fingerprint()) == 12
    assert a.fingerprint() == spec(rule(id="other")).fingerprint()  # id is not content
    assert a.fingerprint() != spec(rule(text="crowds")).fingerprint()
    assert a.fingerprint() != spec(rule(mode="flag")).fingerprint()
    assert a.fingerprint() != spec(rule(), theme="Other").fingerprint()
    assert a.fingerprint() != spec(rule(), theme_partly="flag").fingerprint()
    assert a.fingerprint() != spec(rule(), rule("include", "car", id="b")).fingerprint()
    # an inactive theme does not matter
    assert spec(theme="A", theme_check=False).fingerprint() == FilterSpec().fingerprint()


# ---- PhotoOutput tolerance ----
def test_photo_output_filter_fields_never_raise():
    base = {"description": "A street", "keywords": ["a"]}
    for junk in ("text", 5, True, 1.5, object()):
        out = PhotoOutput.model_validate({**base, "theme_fit": junk, "checks": junk})
        assert out.theme_fit is None and out.checks is None
    out = PhotoOutput.model_validate({**base, "theme_fit": {"fit": "no"}, "checks": [1]})
    assert out.theme_fit == {"fit": "no"} and out.checks == [1]


def test_truncated_filter_answer_repaired():
    raw = json.dumps({**DEFAULT_RESPONSE})[:-1] + (
        ', "theme_fit": {"reason": "nice", "fit": "yes"}, "checks": {"r1": {"reason": "a m'
    )
    out = PhotoOutput.model_validate(repair_json(raw))
    res = ev(spec(rule()), out.theme_fit, out.checks)
    assert res.theme["fit"] == "yes"
    assert res.rules[0]["result"] == "unknown"


# ---- describe integration (one call) ----
def _describe(response, sp, **kw):
    llm = FakeProvider(response=response)
    res = describe_photo(
        llm, b"jpg", "a.jpg", style=Style(), restrictions=R, filters=sp, at=AT, **kw
    )
    return llm, res


def _answers(**over):
    d = {
        **DEFAULT_RESPONSE,
        "theme_fit": {"reason": "street in Tokyo", "fit": "yes"},
        "checks": {
            "r1": {"reason": "nobody", "answer": "no"},
            "r2": {"reason": "bike", "answer": "yes"},
        },
    }
    return {**d, **over}


SP2 = spec(rule("avoid", "people"), rule("include", "a bicycle", id="r2b"))


def test_describe_one_call_with_filters_passing():
    llm, res = _describe(_answers(), SP2)
    assert len(llm.calls) == 1
    assert res.filters.verdict == "ok"
    assert res.filters.model == "fake/fake" and res.filters.at == AT
    assert [r["result"] for r in res.filters.rules] == ["pass", "pass"]
    assert "checks" in llm.schemas[0]["properties"]
    assert "Photo checks" in llm.calls[0].prompt and '"a bicycle"' in llm.calls[0].prompt
    assert res.metadata.description


def test_describe_theme_no_and_rule_fail():
    resp = _answers(
        theme_fit={"reason": "a beach in Spain", "fit": "no"},
        checks={"r1": {"reason": "a man", "answer": "yes"}, "r2": {"answer": "yes"}},
    )
    _, res = _describe(resp, SP2)
    assert res.filters.verdict == "set_aside"
    assert res.filters.set_aside_by == ["theme", "rule:r1a"]


def test_describe_filters_off_sends_exact_old_schema_and_prompt():
    llm, res = _describe(DEFAULT_RESPONSE, None)
    assert llm.schemas[0] == PHOTO_OUTPUT_SCHEMA
    assert res.filters is None
    assert "Photo checks" not in llm.calls[0].prompt
    llm2, res2 = _describe(DEFAULT_RESPONSE, FilterSpec())
    assert llm2.schemas[0] == PHOTO_OUTPUT_SCHEMA and res2.filters is None
    assert llm2.calls[0].prompt == llm.calls[0].prompt
    assert llm2.calls[0].max_tokens == llm.calls[0].max_tokens == 1500
    assert "theme_fit" not in res2.llm.data and "checks" not in res2.llm.data


def test_describe_token_headroom():
    llm, _ = _describe(_answers(), SP2, max_tokens=1000)
    assert llm.calls[0].max_tokens == 1000 + 60 * 3
    llm, _ = _describe(_answers(), spec(theme=""), max_tokens=1000)
    assert llm.calls[0].max_tokens == 1000


def test_garbled_filter_answers_do_not_retry():
    resp = _answers(theme_fit="very much", checks="nope")
    llm, res = _describe(resp, SP2)
    assert len(llm.calls) == 1
    assert res.llm.retries == 0
    assert res.filters.theme["fit"] == "unknown"
    assert [r["result"] for r in res.filters.rules] == ["unknown", "unknown"]
    assert res.filters.verdict == "flag"


def test_missing_filter_answers_are_unknown_without_retry():
    llm, res = _describe(DEFAULT_RESPONSE, SP2)  # fake auto-fills theme_fit, never checks
    assert len(llm.calls) == 1
    assert res.filters.theme["fit"] == "yes"
    assert [r["result"] for r in res.filters.rules] == ["unknown", "unknown"]
    assert status_effect(res.filters.to_dict()) == "flag"


def test_core_failure_still_retries_once_and_filters_survive():
    llm = FakeProvider(response=["not json at all", _answers()])
    res = describe_photo(llm, b"j", "a.jpg", style=Style(), restrictions=R, filters=SP2, at=AT)
    assert len(llm.calls) == 2 and res.llm.retries == 1
    assert res.filters.verdict == "ok"


def test_describe_with_broken_json_repairs_filters():
    raw = "```json\n" + json.dumps(_answers())[:-1] + ",}\n```"
    _, res = _describe(raw, SP2)
    assert res.llm.repaired and res.filters.verdict == "ok"


def test_describe_structured_default_schema_unchanged():
    llm = FakeProvider(response=DEFAULT_RESPONSE)
    describe_structured(llm, LLMRequest(b"j", "s", "p"))
    assert llm.schemas == [PHOTO_OUTPUT_SCHEMA]


def test_fake_autofills_theme_only_when_requested_and_missing():
    req = LLMRequest(b"j", "s", "p")
    llm = FakeProvider(response=DEFAULT_RESPONSE)
    _, native, *_ = llm.complete(req, PHOTO_OUTPUT_SCHEMA)
    assert "theme_fit" not in native
    schema = photo_schema(*schema_properties(spec(rule())))
    _, native, *_ = llm.complete(req, schema)
    assert native["theme_fit"] == {"reason": "Fake answer.", "fit": "yes"}
    assert "checks" not in native
    given = {**DEFAULT_RESPONSE, "theme_fit": {"reason": "r", "fit": "no"}}
    _, native, *_ = FakeProvider(response=given).complete(req, schema)
    assert native["theme_fit"]["fit"] == "no"
    _, native, *_ = FakeProvider(response="raw text").complete(req, schema)
    assert native is None
