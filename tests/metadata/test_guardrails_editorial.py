"""Guardrails: the editorial signals the model reports (artwork, interiors, screens, events,
personal data), with and without releases."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from shuto.metadata import guardrails as g

CAPTION = "SHIBUYA, TOKYO, JAPAN - OCTOBER 5, 2026: Commuters wait on the platform at the station."
DESC = "A quiet side street at dusk with lanterns glowing near the old town"


@dataclass
class Meta:
    description: str = DESC
    keywords: list[str] = field(default_factory=lambda: ["japan", "street", "dusk"])
    editorial: bool = False


@pytest.fixture(scope="module")
def r():
    return g.load_restrictions()


def run(r, detected, meta=None, **kw):
    return g.check(meta or Meta(), detected, r, **kw)


def texts(res):
    return " | ".join(res.messages)


def test_yaml_is_version_2_with_the_documented_mapping(r):
    assert r.version == 2 and r.compiled == "2026-10-16"
    assert r.detected_actions == g.DEFAULT_DETECTED_ACTIONS
    assert "press conference" in r.credential_events


def test_a_version_1_file_still_loads_with_defaults(tmp_path):
    old = tmp_path / "old.yaml"
    old.write_text("version: 1\nrules: []\n", encoding="utf-8")
    r1 = g.load_restrictions(builtin=False, extra_files=[old])
    assert r1.detected_actions == g.DEFAULT_DETECTED_ACTIONS
    assert r1.credential_events == g.DEFAULT_CREDENTIAL_EVENTS


def test_artwork_or_character_forces_editorial_unless_property_release(r):
    out, res = run(r, {"artworks_or_characters": ["wall mural"]}, Meta(description=CAPTION))
    assert res.action == "force_editorial" and out.editorial
    out, res = run(r, {"artworks_or_characters": ["wall mural"]}, has_property_release=True)
    assert res.action == "ok" and not out.editorial


def test_artwork_as_main_subject_goes_to_review_even_as_editorial(r):
    for editorial in (False, True):
        meta = Meta(description=CAPTION if editorial else DESC, editorial=editorial)
        _, res = run(r, {"artwork_is_main_subject": True}, meta)
        assert res.action == "review"
        assert "Artwork fills the frame" in texts(res)


def test_private_interior_needs_a_property_release(r):
    out, res = run(r, {"private_interior": True}, Meta(description=CAPTION))
    assert res.action == "force_editorial" and out.editorial
    _, res = run(r, {"private_interior": True}, has_property_release=True)
    assert res.action == "ok"


def test_screen_content_forces_editorial_even_with_releases(r):
    out, res = run(
        r,
        {"screen_content": True},
        Meta(description=CAPTION),
        has_property_release=True,
        has_model_release=True,
    )
    assert res.action == "force_editorial" and out.editorial


@pytest.mark.parametrize(
    "event", ["street concert", "football match", "political rally", "fashion show", "red carpet"]
)
def test_credential_events_go_to_review(r, event):
    _, res = run(r, {"event": event}, Meta(description=CAPTION, editorial=True))
    assert res.action == "review"
    assert f"Looks like a {event}" in texts(res) and "press credentials" in texts(res)


def test_other_events_only_add_a_soft_note_when_the_caption_does_not_name_them(r):
    _, res = run(r, {"event": "Sanja Matsuri"}, Meta(description=CAPTION, editorial=True))
    assert res.action == "ok"
    assert "Name the event in the caption: Sanja Matsuri." in res.messages
    named = "SHIBUYA, TOKYO, JAPAN - OCTOBER 5, 2026: Crowds carry a shrine at the Sanja Matsuri."
    _, res = run(r, {"event": "Sanja Matsuri"}, Meta(description=named, editorial=True))
    assert not [m for m in res.messages if m.startswith("Name the event")]
    _, res = run(r, {"event": "Sanja Matsuri"})  # commercial: no note
    assert not [m for m in res.messages if m.startswith("Name the event")]


@pytest.mark.parametrize("kind", ["id_number", "card_number", "passport number", "credit card"])
def test_readable_id_or_card_numbers_are_blocked_commercial_and_editorial(r, kind):
    for meta in (Meta(), Meta(description=CAPTION, editorial=True)):
        _, res = run(r, {"personal_data": [kind]}, meta)
        assert res.action == "block"
        assert "does not accept" in texts(res)


@pytest.mark.parametrize(
    "kind",
    [
        "Account number",
        "bank-account",
        "IBAN",
        "ID card",
        "driver's license number",
        "my number card",
        "id card number",
        "social security number",
        "passport no.",
        "card no",
        "residence card",
        "card #",
        "ID",
    ],
)
def test_exact_id_kinds_in_free_text_still_block(r, kind):
    _, res = run(r, {"personal_data": [kind]})
    assert res.action == "block"


@pytest.mark.parametrize(
    "kind",
    ["license plate", "number plate", "business card", "name card", "social media account"],
)
def test_look_alike_kinds_do_not_block(r, kind):
    out, res = run(r, {"personal_data": [kind]})
    assert res.action != "block"
    assert "does not accept" not in texts(res)


@pytest.mark.parametrize("kind", ["name", "address", "phone", "email", "signature"])
def test_other_personal_data_is_editorial_and_needs_a_look(r, kind):
    out, res = run(r, {"personal_data": [kind]})
    assert res.action == "review" and out.editorial
    assert "Readable personal data" in texts(res)


def test_block_beats_the_softer_signals(r):
    _, res = run(r, {"personal_data": ["id_number", "name"], "screen_content": True})
    assert res.action == "block"


@pytest.mark.parametrize(
    "text,kind",
    [
        ("Contact me at jane.doe@example.com for prints and more", "email"),
        ("Call +81 3 1234 5678 to book this lovely street tour", "phone"),
        ("Phone 03-1234-5678 on the shop sign in the old town", "phone"),
        ("A card numbered 4111 1111 1111 1111 on the table here", "card"),
    ],
)
def test_text_check_finds_personal_data_without_echoing_it(r, text, kind):
    _, res = run(r, {}, Meta(description=text))
    assert res.action == "review"
    assert texts(res).count("Personal data in the text") == 1
    assert [h.name for h in res.hits if h.kind == "pii"][0].startswith(kind)
    assert "example.com" not in str(res.to_dict()) and "1111" not in str(res.to_dict())


def test_text_check_ignores_dates_and_ordinary_numbers(r):
    for text in (
        "A street photographed on 2026-10-13 with 45 cyclists in 2026 and 3 flags",
        "Shot on 13.10.2026 at the Meiji shrine with 120 visitors and 7 lanterns",
    ):
        _, res = run(r, {}, Meta(description=text))
        assert res.action == "ok", text
    _, res = run(r, {}, Meta(keywords=["japan", "street", "dusk", "room 1204"]))
    assert res.action == "ok"


def test_brand_names_are_kept_in_editorial_but_stripped_from_commercial(r):
    kws = ["japan", "starbucks", "street"]
    commercial, res = run(r, {}, Meta(keywords=kws))
    assert "starbucks" not in commercial.keywords and res.removed_keywords == ["starbucks"]
    editorial, res = run(
        r,
        {},
        Meta(description=CAPTION.replace("the station", "Starbucks"), keywords=kws, editorial=True),
    )
    assert "starbucks" in editorial.keywords and not res.removed_keywords
    assert res.action == "ok"


def test_brand_in_a_commercial_description_goes_to_review_but_not_in_editorial(r):
    _, res = run(r, {}, Meta(description="A tall Starbucks sign above a quiet side street"))
    assert res.action == "review"
    _, res = run(
        r, {}, Meta(description=CAPTION.replace("the station", "Starbucks"), editorial=True)
    )
    assert res.action == "ok"


def test_caption_check_uses_the_shared_problems(r):
    bad = "SHIBUYA, JAPAN - OCTOBER 5, 2026: Crowds."
    _, res = run(r, {}, Meta(description=bad, editorial=True))
    assert res.action == "review" and "at least 3 words" in texts(res)
    _, res = run(r, {}, Meta(description=CAPTION, editorial=True))
    assert res.action == "ok"
