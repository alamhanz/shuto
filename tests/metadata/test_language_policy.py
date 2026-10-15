"""metadata.rules: the language policy and the shared commercial-words rule."""

from __future__ import annotations

from types import SimpleNamespace

from shuto.metadata import guardrails, model, rules


def test_language_policy_from_style_config():
    strict = rules.language_policy(SimpleNamespace(english_only=True, metadata_language="Thai"))
    assert strict == rules.LanguagePolicy(True, "Thai") and strict.text_language == "English"
    thai = rules.language_policy(SimpleNamespace(english_only=False, metadata_language="Thai"))
    assert not thai.strict and thai.text_language == "Thai"
    nameless = rules.language_policy(SimpleNamespace(english_only=False))
    assert nameless.text_language == "English"
    assert rules.language_policy(SimpleNamespace()).strict is True


def test_latin_words_only_drops_foreign_script_words():
    assert rules.latin_words_only("Tokyo 渋谷 street") == "Tokyo street"


def test_one_commercial_words_constant():
    assert model.MIN_COMMERCIAL_WORDS is guardrails.MIN_COMMERCIAL_WORDS
    assert rules.MIN_COMMERCIAL_WORDS == model.MIN_COMMERCIAL_WORDS
