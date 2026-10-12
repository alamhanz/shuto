from __future__ import annotations

import collections
import copy
from dataclasses import dataclass, field

import pytest
import yaml

from shuto import guardrails as g

DESC = "A quiet side street at dusk with lanterns glowing in Kyoto"
CAPTION = "TOKYO, JAPAN - OCTOBER 5, 2026: Commuters wait on the platform at Shinjuku Station."


@dataclass
class Meta:
    description: str = DESC
    keywords: list[str] = field(default_factory=lambda: ["japan", "street", "dusk"])
    editorial: bool = False


@pytest.fixture(scope="module")
def r():
    return g.load_restrictions()


def run(r, meta=None, detected=None, **kw):
    return g.check(meta or Meta(), detected or {}, r, **kw)


def test_builtin_yaml_valid_and_unique(r):
    ids = [x.id for x in r.rules]
    assert len(ids) == len(set(ids)) > 150
    assert r.levels == {
        "restricted": "force_editorial",
        "prohibited": "block",
        "no_upload": "block",
    }
    raw = yaml.safe_load(
        (
            __import__("importlib.resources").resources.files("shuto.data") / "restrictions.yaml"
        ).read_text(encoding="utf-8")
    )
    assert not [i for i, c in collections.Counter(x["id"] for x in raw["rules"]).items() if c > 1]


def test_clean_photo_ok(r):
    out, res = run(r)
    assert res.action == "ok" and not res.hits and out.editorial is False


def test_skytree_forces_editorial(r):
    meta = Meta(description=CAPTION, keywords=["japan", "Tokyo Skytree"])
    out, res = run(r, meta, {"landmarks": ["Tokyo Skytree"]})
    assert res.action == "force_editorial" and out.editorial is True
    assert meta.editorial is False  # input untouched


def test_property_release_clears_restricted(r):
    _, res = run(r, Meta(keywords=["skytree"]), has_property_release=True)
    assert res.action == "ok"


def test_singapore_zoo_blocks(r):
    _, res = run(r, Meta(keywords=["singapore zoo"]))
    assert res.action == "block"


def test_event_no_upload_blocks(r):
    _, res = run(r, Meta(keywords=["met gala"]))
    assert res.action == "block"


def test_seven_eleven_stripped(r):
    meta = Meta(description=CAPTION, keywords=["japan", "7-Eleven", "street"])
    snapshot = copy.deepcopy(meta)
    out, res = run(r, meta)
    assert out.keywords == ["japan", "street"]
    assert res.removed_keywords == ["7-Eleven"]
    assert meta == snapshot
    assert res.action == "force_editorial"  # konbini rule


def test_eiffel_tower_goes_to_review(r):
    _, res = run(r, Meta(keywords=["eiffel tower", "paris"]))
    assert res.action == "review"


def test_no_substring_false_hits(r):
    _, res = run(r, Meta(keywords=["lawsonia", "japan"]))
    assert res.action == "ok" and not res.removed_keywords


def test_accents_and_case(r):
    _, res = run(r, Meta(keywords=["SACRÉ-CŒUR".replace("Œ", "OE"), "x"]))
    assert res.action == "block"


def test_people_without_release(r):
    out, res = run(r, Meta(description=CAPTION), {"recognizable_people": True})
    assert res.action == "force_editorial" and out.editorial
    _, res2 = run(r, detected={"recognizable_people": True}, has_model_release=True)
    assert res2.action == "ok"


def test_logos_force_editorial(r):
    out, res = run(r, Meta(description=CAPTION), {"logos": ["brand"]})
    assert res.action == "force_editorial" and out.editorial


def test_short_commercial_description_review(r):
    _, res = run(r, Meta(description="Tokyo street"))
    assert res.action == "review"


def test_editorial_caption_format(r):
    _, ok = run(r, Meta(description=CAPTION, editorial=True))
    assert ok.action == "ok"
    _, bad = run(r, Meta(description="People at a station", editorial=True))
    assert bad.action == "review"


def test_severity_block_beats_others(r):
    _, res = run(r, Meta(keywords=["singapore zoo", "skytree"]), {"logos": True})
    assert res.action == "block"


def test_extra_file_override_and_disable(tmp_path):
    extra = tmp_path / "extra.yaml"
    extra.write_text(
        "trademark_terms: [Acme]\nrules:\n"
        "  - {id: tokyo-skytree, name: Tokyo Skytree, level: prohibited, category: architecture}\n"
        "  - {id: my-spot, name: Secret Spot, level: restricted, category: other}\n",
        encoding="utf-8",
    )
    r2 = g.load_restrictions(extra_files=[extra], disabled=["merlion"])
    _, res = run(r2, Meta(keywords=["tokyo skytree"]))
    assert res.action == "block"
    _, res = run(r2, Meta(description=CAPTION, keywords=["secret spot", "acme"]))
    assert res.action == "force_editorial" and res.removed_keywords == ["acme"]
    _, res = run(r2, Meta(keywords=["merlion"]))
    assert res.action == "ok"
    assert not any(x.id == "merlion" for x in r2.rules)


def test_load_without_builtin(tmp_path):
    extra = tmp_path / "e.yaml"
    extra.write_text("rules:\n  - {id: a, name: Foo, level: restricted}\n", encoding="utf-8")
    r2 = g.load_restrictions(builtin=False, extra_files=[extra])
    assert [x.id for x in r2.rules] == ["a"]


def test_bad_level_rejected(tmp_path):
    bad = tmp_path / "b.yaml"
    bad.write_text("rules:\n  - {id: a, name: Foo, level: nope}\n", encoding="utf-8")
    with pytest.raises(ValueError):
        g.load_restrictions(builtin=False, extra_files=[bad])


def test_prompt_summary(r):
    text = g.prompt_summary(r, region_hint="japan", max_items=10)
    assert "Skytree" in text and "Never put brand names" in text
    assert text.count(";") < 12


def test_forced_editorial_with_commercial_description_goes_to_review(r):
    """Y1: guardrails newly forcing editorial must not leave a clean commercial sentence."""
    out, res = run(r, Meta(), {"logos": ["brand"]})
    assert out.editorial is True
    assert res.action == "review"
    assert any(h.kind == "caption" for h in res.hits)


def test_property_release_does_not_clear_logos(r):
    out, res = run(r, Meta(description=CAPTION), {"logos": True}, has_property_release=True)
    assert res.action == "force_editorial" and out.editorial


def test_notes_only_match_is_a_hint(r):
    """Y2: a named rule matched only in batch notes never forces editorial or blocks."""
    for note in ("Skytree area walk", "Shot at the Met Gala", "Singapore Zoo day trip"):
        out, res = run(r, notes=note)
        assert res.action == "ok" and out.editorial is False
        assert any(h.where == "notes" and h.action == "ok" for h in res.hits)
        assert any("batch notes" in m for m in res.messages)


def test_generic_words_do_not_force_editorial(r):
    for word in ("poster", "advertisement", "billboard", "anime", "manga"):
        meta = Meta(description=f"A colorful {word} on a quiet street in Kyoto", keywords=[word])
        _, res = run(r, meta)
        assert res.action == "ok", word
    _, res = run(r, Meta(description=CAPTION, keywords=["anime character", "japan"]))
    assert res.action == "force_editorial"


def test_sapporo_trademark_stays_strict(r):
    """Intentional: Sapporo is a beer brand and a city; both go to review (strict by decision)."""
    out, res = run(r, Meta(description="SAPPORO, JAPAN - JANUARY 5, 2026: Snow festival crowd."))
    assert res.action == "review"
    out, res = run(r, Meta(keywords=["sapporo", "japan"]))
    assert out.keywords == ["japan"] and res.removed_keywords == ["sapporo"]


@pytest.mark.parametrize(
    "caption,ok",
    [
        (CAPTION, True),
        ("SÃO PAULO, BRAZIL - MARCH 3, 2026: Rush hour.", True),
        ("ZÜRICH, SWITZERLAND - MAY 1, 2026: Tram stop.", True),
        ("Kyoto, Japan - MARCH 3, 2026: Lanterns.", False),
        ("KYOTO, JAPAN - UNKNOWN: Lanterns.", False),
        ("UNKNOWN, UNKNOWN - UNKNOWN: Lanterns.", False),
        ("KYOTO, JAPAN - MARCH 3, 2026:", False),
    ],
)
def test_shared_caption_rule(r, caption, ok):
    from shuto.metadata import is_editorial_caption

    assert is_editorial_caption(caption) is ok
    _, res = run(r, Meta(description=caption, editorial=True))
    assert (res.action == "ok") is ok
