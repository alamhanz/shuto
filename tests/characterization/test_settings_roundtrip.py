"""Saved UI settings: what each key accepts and rejects (with the text), and the config overlay."""

from __future__ import annotations

import re
from pathlib import Path

from . import check_json


def _seam():
    from shuto import metadata_rules, pipeline

    return (
        pipeline.set_ui_setting,
        pipeline.get_ui_setting,
        pipeline.apply_style_overrides,
        pipeline.UI_SETTING_KEYS,
        metadata_rules.MAX_TRIES,
    )


def _templates_dir() -> Path:
    from shuto.web import common

    return Path(common.__file__).parent / "templates"


RULES = [
    {"id": "r1", "kind": "avoid", "text": "people close up", "mode": "flag"},
    {"kind": "include", "text": "street scenes"},
]

MATRIX = {
    "onboarded": [True, False, "", None, "yes"],
    "last_folder": ["D:/photos", "", None],
    "style": [
        {"theme": "Everyday Japan", "tone": "calm"},
        {},
        {"theme": "x" * 201},
        {"extra_instructions": "y" * 2001},
        {"always_include": "tokyo\nkyoto, tokyo,Tokyo"},
        {"always_include": [f"w{i}" for i in range(21)]},
        {"region_focus": "Japan"},
        {"region_focus": "france"},
        {"default_city": "Kyoto", "default_country": "Japan"},
        {"default_city": "Kyoto 2"},
        {"default_country": "x" * 61},
        "damaged",
        None,
    ],
    "filters": [
        {"theme_check": True, "theme_partly": "flag"},
        {"theme_partly": "sometimes"},
        {"rules": RULES},
        {"rules": [{"kind": "bogus", "text": "a"}]},
        {"rules": "damaged"},
        "damaged",
    ],
    "bedrock_profile": ["default", "", "  my-profile  ", "bad profile!", "a" * 101, None],
    "update_check": [True, False, 0, "yes", None],
    "update_cache": [
        {"version": "0.5.0", "checked_at": 1700000000, "ok": True},
        {"version": "0.5.0", "checked_at": True, "ok": True},
        {"version": 5, "checked_at": 1.0, "ok": True},
        {"version": "0.5.0", "checked_at": 1.0, "ok": "yes"},
        "damaged",
    ],
    "include_gps": [True, False, 1, "", None],
    "max_tries": [0, 1, 3, 8, 9, True, "3", "x", None, 2.7],
    "language": [
        {"english_only": True},
        {"english_only": False, "language": "Japanese"},
        {"english_only": False, "language": "  french  "},
        {"english_only": False, "language": "Jap@nese!"},
        {"english_only": False, "language": ""},
        {"english_only": False, "language": "L" * 31},
        {"english_only": "no"},
        {},
        "damaged",
    ],
}


def _outcome(call):
    try:
        return {"ok": call()}
    except Exception as exc:
        return {"error": str(exc)}


def test_every_ui_setting_key_has_a_matrix_row():
    *_, keys, _ = _seam()
    assert set(keys) == set(MATRIX)
    assert list(keys) == [
        "onboarded",
        "last_folder",
        "style",
        "filters",
        "bedrock_profile",
        "update_check",
        "update_cache",
        "include_gps",
        "max_tries",
        "language",
    ]


def test_set_ui_setting_matrix_golden(store):
    set_ui, get_ui, *_ = _seam()
    out = {}
    for key, values in MATRIX.items():
        rows = []
        for value in values:
            result = _outcome(lambda v=value, k=key: set_ui(store, k, v))
            result["input"] = repr(value)
            result["stored"] = get_ui(store, key, "<unset>")
            rows.append(result)
        out[key] = rows
    out["unknown"] = _outcome(lambda: set_ui(store, "nope", 1))
    out["prefixed"] = _outcome(lambda: set_ui(store, "ui.max_tries", 4))
    check_json("settings_matrix.json", out)


def test_filters_merge_with_what_was_saved(store):
    set_ui, get_ui, *_ = _seam()
    set_ui(store, "filters", {"theme_check": True})
    set_ui(store, "filters", {"theme_partly": "flag"})
    assert get_ui(store, "filters") == {"theme_check": True, "theme_partly": "flag"}
    store.set_setting("ui.filters", "damaged")
    set_ui(store, "filters", {"theme_check": False})
    assert get_ui(store, "filters") == {"theme_check": False}


def _view(cfg) -> dict:
    return {
        "style": {
            "theme": cfg.style.theme,
            "tone": cfg.style.tone,
            "extra_instructions": cfg.style.extra_instructions,
            "region_focus": cfg.style.region_focus,
            "always_include": list(cfg.style.keywords.always_include),
            "default_city": cfg.style.default_place.city,
            "default_country": cfg.style.default_place.country,
            "english_only": cfg.style.english_only,
            "metadata_language": cfg.style.metadata_language,
        },
        "filters": {
            "theme_check": cfg.filters.theme_check,
            "theme_partly": cfg.filters.theme_partly,
            "rules": [[r.id, r.kind, r.text, r.mode] for r in cfg.filters.rules],
        },
        "bedrock_profile": cfg.llm.bedrock.profile,
        "max_tries": cfg.llm.max_tries,
        "include_gps": cfg.prepare.include_gps,
    }


OVERLAYS = {
    "nothing saved": {},
    "everything valid": {
        "ui.style": {
            "theme": "Everyday Japan",
            "tone": "calm",
            "extra_instructions": "short",
            "region_focus": "japan",
            "always_include": ["tokyo", "japan"],
            "default_city": "Kyoto",
            "default_country": "Japan",
        },
        "ui.filters": {
            "theme_check": False,
            "theme_partly": "flag",
            "rules": [{"id": "r1", "kind": "avoid", "text": "people", "mode": "flag"}],
        },
        "ui.bedrock_profile": "work",
        "ui.max_tries": 3,
        "ui.include_gps": False,
        "ui.language": {"english_only": False, "language": "Japanese"},
    },
    "empty style fields override": {
        "ui.style": {"theme": "", "tone": "", "always_include": [], "default_city": ""},
    },
    "damaged values are ignored": {
        "ui.style": {"theme": 5, "always_include": "x", "default_city": 7},
        "ui.filters": {"theme_check": "yes", "theme_partly": "nope", "rules": "x"},
        "ui.bedrock_profile": 5,
        "ui.max_tries": 99,
        "ui.include_gps": "yes",
        "ui.language": {"english_only": False, "language": "Jap@nese!"},
    },
    "max tries bool": {"ui.max_tries": True},
    "max tries zero": {"ui.max_tries": 0},
    "language damaged shape": {"ui.language": "Japanese"},
    "language strict": {"ui.language": {"english_only": True, "language": "Japanese"}},
    "filters with bad rule list": {"ui.filters": {"rules": [{"kind": "bogus", "text": "a"}]}},
}


def test_apply_style_overrides_golden(cfg, store):
    _set, _get, apply, _keys, _max = _seam()
    out = {}
    for label, saved in OVERLAYS.items():
        for key, value in saved.items():
            store.set_setting(key, value)
        out[label] = _view(apply(cfg, store))
        for key in saved:
            store.set_setting(key, None)
    check_json("settings_overlay.json", out)


def test_apply_style_overrides_does_not_touch_the_base_config(cfg, store):
    _set, _get, apply, _keys, _max = _seam()
    before = cfg.model_dump()
    store.set_setting("ui.max_tries", 2)
    store.set_setting("ui.style", {"theme": "x"})
    assert apply(cfg, store).llm.max_tries == 2
    assert cfg.model_dump() == before


def test_tries_menu_matches_the_limit():
    *_, max_tries = _seam()
    html = (_templates_dir() / "settings.html").read_text(encoding="utf-8")
    match = re.search(r"range\(1,\s*(\d+)\)", html)
    assert match and int(match.group(1)) == max_tries + 1
    assert f"n == {max_tries} " in html
