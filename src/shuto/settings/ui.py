"""Saved UI settings: keys, validators, getters and setters."""

from __future__ import annotations

import re
from typing import Any

from shuto.ai import filters as filters_mod
from shuto.core.config import Config
from shuto.core.errors import PipelineError
from shuto.core.validate import clean_place_part
from shuto.metadata import rules as metadata_rules
from shuto.store import Store
from shuto.workflow import checks, review

UI_SETTING_KEYS = (
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
)


PROFILE_RE = re.compile(r"^[A-Za-z0-9_.@:+=,-]{1,100}$")


MAX_STYLE_TEXT = 200


MAX_EXTRA_INSTRUCTIONS = 2000


MAX_ALWAYS_INCLUDE = 20


def _ui_key(name: str) -> str:
    short = name.removeprefix("ui.")
    if short not in UI_SETTING_KEYS:
        raise PipelineError(f"Unknown setting: {name!r}.")
    return f"ui.{short}"


def get_ui_setting(store: Store, name: str, default: Any = None) -> Any:
    return store.get_setting(_ui_key(name), default)


REGION_FOCUS = ("", "japan")


def _clean_style(value: Any) -> dict[str, Any]:
    """Validate the style settings. A field that was submitted (even empty) is stored, so
    clearing it overrides the config default; a field that was not submitted stays unset."""
    if not isinstance(value, dict):
        raise PipelineError("Style settings must be a set of named values.")
    out: dict[str, Any] = {}
    for key in ("theme", "tone"):
        if key not in value:
            continue
        text = str(value.get(key) or "").strip()
        if len(text) > MAX_STYLE_TEXT:
            raise PipelineError(f"Keep the {key} under {MAX_STYLE_TEXT} characters.")
        out[key] = text
    if "extra_instructions" in value:
        extra = str(value.get("extra_instructions") or "").strip()
        if len(extra) > MAX_EXTRA_INSTRUCTIONS:
            raise PipelineError(f"Keep the extra notes under {MAX_EXTRA_INSTRUCTIONS} characters.")
        out["extra_instructions"] = extra
    if "always_include" in value:
        words = value.get("always_include") or []
        if isinstance(words, str):
            words = words.replace("\n", ",").split(",")
        words = review._fold(words)
        if len(words) > MAX_ALWAYS_INCLUDE:
            raise PipelineError(f"Use at most {MAX_ALWAYS_INCLUDE} always-include keywords.")
        out["always_include"] = words
    if "region_focus" in value:
        focus = str(value.get("region_focus") or "").strip().casefold()
        if focus not in REGION_FOCUS:
            raise PipelineError("Region focus must be None or Japan.")
        out["region_focus"] = focus
    for key, label in (("default_city", "city"), ("default_country", "country")):
        if key in value:
            try:
                out[key] = clean_place_part(value.get(key))
            except ValueError as exc:
                raise PipelineError(f"Default place, {label}: {exc}") from exc
    return out


def _clean_language_setting(value: Any) -> dict[str, Any]:
    """Validate the metadata language setting: strict English, or a language name to write in."""
    if not isinstance(value, dict):
        raise PipelineError("The language setting must be a set of named values.")
    english = value.get("english_only", True)
    if not isinstance(english, bool):
        raise PipelineError("English only must be on or off.")
    if english:
        return {"english_only": True}
    try:
        return {
            "english_only": False,
            "language": metadata_rules.clean_language(value.get("language")),
        }
    except ValueError as exc:
        raise PipelineError(str(exc)) from exc


def _clean_filters(value: Any) -> dict[str, Any]:
    """Validate the theme/rules settings. Only submitted keys are stored (a stored key wins over
    the config file). Raises PipelineError with a plain-English message."""
    if not isinstance(value, dict):
        raise PipelineError("The rules settings must be a set of named values.")
    out: dict[str, Any] = {}
    if "theme_check" in value:
        out["theme_check"] = bool(value["theme_check"])
    if "theme_partly" in value:
        partly = str(value["theme_partly"] or "").strip().casefold()
        if partly not in filters_mod.PARTLY_MODES:
            raise PipelineError('Photos that fit the theme "partly" can be let through or flagged.')
        out["theme_partly"] = partly
    if "rules" in value:
        try:
            rules = filters_mod.clean_rules(value["rules"])
        except filters_mod.FilterRuleError as exc:
            raise PipelineError(str(exc)) from exc
        out["rules"] = [{"id": r.id, "kind": r.kind, "text": r.text, "mode": r.mode} for r in rules]
    return out


def get_filter_rules(cfg: Config) -> list[filters_mod.UserRule]:
    """The rules new photos are checked with (config plus web app settings)."""
    return list(checks.filter_spec(cfg).rules)


def _clean_update_cache(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PipelineError("The update cache must be a mapping.")
    version, checked_at, ok = value.get("version"), value.get("checked_at"), value.get("ok")
    if not isinstance(version, str) or not isinstance(ok, bool):
        raise PipelineError("The update cache is not valid.")
    if isinstance(checked_at, bool) or not isinstance(checked_at, int | float):
        raise PipelineError("The update cache is not valid.")
    return {"version": version, "checked_at": float(checked_at), "ok": ok}


def _clean_max_tries(value: Any) -> int:
    message = f"Max tries per photo must be a number from 1 to {metadata_rules.MAX_TRIES}."
    try:
        tries = int(value)
    except (TypeError, ValueError):
        raise PipelineError(message) from None
    if isinstance(value, bool) or not 1 <= tries <= metadata_rules.MAX_TRIES:
        raise PipelineError(message)
    return tries


def set_ui_setting(store: Store, name: str, value: Any) -> Any:
    """Save a UI setting (validated for `style`); returns the stored value."""
    key = _ui_key(name)
    if key == "ui.style":
        value = _clean_style(value)
    elif key == "ui.filters":
        new = _clean_filters(value)
        try:
            old = _clean_filters(store.get_setting("ui.filters") or {})
        except PipelineError:  # a damaged saved value is replaced by the new one
            old = {}
        value = {**old, **new}
    elif key == "ui.bedrock_profile":
        value = str(value or "").strip()
        if value and not PROFILE_RE.match(value):
            raise PipelineError("That AWS profile name is not valid.")
    elif key == "ui.language":
        value = _clean_language_setting(value)
    elif key in ("ui.update_check", "ui.include_gps"):
        value = bool(value)
    elif key == "ui.update_cache":
        value = _clean_update_cache(value)
    elif key == "ui.max_tries":
        value = _clean_max_tries(value)
    store.set_setting(key, value)
    return value
