"""The metadata-language flag: strict English (True) or another language (False), and every place
that reads it. Parameter names (`strict=`, `english=`) are part of what is recorded."""

from __future__ import annotations

import dataclasses

from pydantic import ValidationError

from shuto.config import Config

from . import check_json


def _seam():
    from shuto import csv_export, describe, metadata_rules, photo_context, pipeline, prepare

    return {
        "not_english": pipeline.not_english,
        "release_check": prepare.release_check,
        "build_csv": csv_export.build_csv,
        "NotEnglishError": csv_export.NotEnglishError,
        "language_rule": describe.language_rule,
        "Style": describe.Style,
        "clean_text": photo_context._clean_text,
        "english_only_fn": metadata_rules.english_only,
        "has_foreign_script": metadata_rules.has_foreign_script,
        "clean_language": metadata_rules.clean_language,
    }


ENGLISH = {
    "title": "Quiet street",
    "description": "A quiet street in Tokyo with parked bicycles",
    "keywords": ["tokyo", "street"],
}
JAPANESE = {
    "title": "静かな通り",
    "description": "東京の 静かな 通りに 駐輪された 自転車",
    "keywords": ["東京", "通り"],
}
MIXED = {**ENGLISH, "keywords": ["tokyo", "渋谷"]}
ACCENTED = {**ENGLISH, "title": "Café Köln"}


def _outcome(call):
    try:
        return {"ok": call()}
    except ValidationError as exc:  # only the message: the full text carries a pydantic URL
        return {"error": f"ValidationError: {exc.errors()[0]['msg']}"}
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


def _style_view(style) -> dict:
    return {**dataclasses.asdict(style), "text_language": style.text_language}


def test_language_flag_golden():
    s = _seam()
    out: dict = {}
    data = {"english": ENGLISH, "japanese": JAPANESE, "mixed": MIXED, "accented": ACCENTED}
    for strict in (True, False):
        flag = f"strict={strict}"
        out[f"not_english / {flag}"] = {k: s["not_english"](v, strict) for k, v in data.items()}
        out[f"release_check / {flag}"] = {
            k: s["release_check"]({**v, "editorial": False}, None, [], strict=strict)
            for k, v in data.items()
        }

        def csv_of(item, strict=strict):
            row = {
                "filename": "a.jpg",
                "description": item["description"],
                "keywords": item["keywords"],
                "categories": ["Transportation"],
            }
            return s["build_csv"]([row], strict=strict)

        out[f"build_csv / {flag}"] = {k: _outcome(lambda v=v: csv_of(v)) for k, v in data.items()}
        out[f"clean_text / {flag}"] = {
            text: s["clean_text"](text, 40, strict)
            for text in ("Shibuya 渋谷 crossing", "渋谷", "Köln  Hbf", "x" * 60)
        }
    check_json("language_flag.json", out)


def test_language_rule_and_style_golden():
    s = _seam()
    out: dict = {}
    for label, style_cfg in {
        "strict": {"english_only": True},
        "strict with a name left over": {"english_only": True, "metadata_language": "Japanese"},
        "japanese": {"english_only": False, "metadata_language": "Japanese"},
        "french": {"english_only": False, "metadata_language": "  french "},
    }.items():
        config = Config.model_validate({"style": style_cfg})
        style = s["Style"].from_config(config.style)
        out[label] = {
            "style": _style_view(style),
            "rule": s["language_rule"](style),
            "rule_short": s["language_rule"](style, short=True),
            "config_language": [config.style.english_only, config.style.metadata_language],
        }
    out["non-english without a name"] = _outcome(
        lambda: Config.model_validate({"style": {"english_only": False}}).style.metadata_language
    )
    out["bad language name"] = _outcome(
        lambda: (
            Config.model_validate(
                {"style": {"english_only": False, "metadata_language": "Jap@nese"}}
            ).style.metadata_language
        )
    )
    check_json("language_style.json", out)


def test_language_helpers_golden():
    s = _seam()
    texts = ["Tokyo street", "Tokyo 渋谷 street", "渋谷", "Café", "", "Ｔｏｋｙｏ", "ñandú, 東京"]
    out = {
        "latin_words_only (english_only)": {t: s["english_only_fn"](t) for t in texts},
        "has_foreign_script": {t: s["has_foreign_script"](t) for t in texts},
        "clean_language": {
            repr(v): _outcome(lambda v=v: s["clean_language"](v))
            for v in ("Japanese", " french ", "Jap@nese", "", None, "L" * 30, "L" * 31, 5)
        },
    }
    check_json("language_helpers.json", out)
