import pytest

from shuto.core.errors import PipelineError, SettingsError
from shuto.core.validate import MAX_TRIES
from shuto.settings import effective, ui


def test_effective_config_applies_saved_values(cfg, store):
    s = store
    ui.set_ui_setting(s, "max_tries", 3)
    ui.set_ui_setting(s, "include_gps", True)
    ui.set_ui_setting(s, "language", {"english_only": False, "language": "Japanese"})
    ui.set_ui_setting(s, "style", {"theme": "Rain", "default_city": "Kyoto"})
    out = effective.effective_config(cfg, s)
    assert out.llm.max_tries == 3 and out.prepare.include_gps is True
    assert out.style.english_only is False and out.style.metadata_language == "Japanese"
    assert out.style.theme == "Rain" and out.style.default_place.city == "Kyoto"


def test_effective_config_ignores_damaged_values(cfg, store):
    s = store
    s.set_setting("ui.max_tries", MAX_TRIES + 1)
    s.set_setting("ui.language", {"english_only": False, "language": "x1"})
    out = effective.effective_config(cfg, s)
    assert out.llm.max_tries == cfg.llm.max_tries
    assert out.style.english_only == cfg.style.english_only


def test_validators_raise_settings_error_still_a_pipeline_error(store):
    s = store
    for name, bad in (("max_tries", 0), ("bedrock_profile", "no good!"), ("nope", 1)):
        with pytest.raises(SettingsError):
            ui.set_ui_setting(s, name, bad)
    with pytest.raises(PipelineError):
        ui.set_ui_setting(s, "language", {"english_only": "yes"})
