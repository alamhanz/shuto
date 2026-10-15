"""Saved UI settings: keys, validators, getters and setters."""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any, TypedDict

from shuto.ai import filters as filters_mod
from shuto.core.config import Config
from shuto.core.errors import SettingsError
from shuto.core.validate import clean_place_part
from shuto.metadata import rules as metadata_rules
from shuto.store import Store
from shuto.workflow import checks, review


class UiStyle(TypedDict, total=False):
    """`ui.style`: only the fields that were submitted are stored."""

    theme: str
    tone: str
    extra_instructions: str
    always_include: list[str]
    region_focus: str
    default_city: str
    default_country: str


class UiFilterRule(TypedDict):
    id: str
    kind: str
    text: str
    mode: str


class UiFilters(TypedDict, total=False):
    """`ui.filters`: only the keys that were submitted are stored."""

    theme_check: bool
    theme_partly: str
    rules: list[UiFilterRule]


class UiLanguage(TypedDict, total=False):
    """`ui.language`: strict English, or the language name to write in."""

    english_only: bool
    language: str


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
        raise SettingsError(f"Unknown setting: {name!r}.")
    return f"ui.{short}"


def get_ui_setting(store: Store, name: str, default: Any = None) -> Any:
    return store.get_setting(_ui_key(name), default)


REGION_FOCUS = ("", "japan")


def _clean_style(value: Any) -> UiStyle:
    """Validate the style settings. A field that was submitted (even empty) is stored, so
    clearing it overrides the config default; a field that was not submitted stays unset."""
    if not isinstance(value, dict):
        raise SettingsError("Style settings must be a set of named values.")
    out: UiStyle = {}
    for key in ("theme", "tone"):
        if key not in value:
            continue
        text = str(value.get(key) or "").strip()
        if len(text) > MAX_STYLE_TEXT:
            raise SettingsError(f"Keep the {key} under {MAX_STYLE_TEXT} characters.")
        out[key] = text
    if "extra_instructions" in value:
        extra = str(value.get("extra_instructions") or "").strip()
        if len(extra) > MAX_EXTRA_INSTRUCTIONS:
            raise SettingsError(f"Keep the extra notes under {MAX_EXTRA_INSTRUCTIONS} characters.")
        out["extra_instructions"] = extra
    if "always_include" in value:
        words = value.get("always_include") or []
        if isinstance(words, str):
            words = words.replace("\n", ",").split(",")
        words = review._fold(words)
        if len(words) > MAX_ALWAYS_INCLUDE:
            raise SettingsError(f"Use at most {MAX_ALWAYS_INCLUDE} always-include keywords.")
        out["always_include"] = words
    if "region_focus" in value:
        focus = str(value.get("region_focus") or "").strip().casefold()
        if focus not in REGION_FOCUS:
            raise SettingsError("Region focus must be None or Japan.")
        out["region_focus"] = focus
    for key, label in (("default_city", "city"), ("default_country", "country")):
        if key in value:
            try:
                out[key] = clean_place_part(value.get(key))
            except ValueError as exc:
                raise SettingsError(f"Default place, {label}: {exc}") from exc
    return out


def _clean_language_setting(value: Any) -> UiLanguage:
    """Validate the metadata language setting: strict English, or a language name to write in."""
    if not isinstance(value, dict):
        raise SettingsError("The language setting must be a set of named values.")
    english = value.get("english_only", True)
    if not isinstance(english, bool):
        raise SettingsError("English only must be on or off.")
    if english:
        return {"english_only": True}
    try:
        return {
            "english_only": False,
            "language": metadata_rules.clean_language(value.get("language")),
        }
    except ValueError as exc:
        raise SettingsError(str(exc)) from exc


def _clean_filters(value: Any) -> UiFilters:
    """Validate the theme/rules settings. Only submitted keys are stored (a stored key wins over
    the config file). Raises SettingsError with a plain-English message."""
    if not isinstance(value, dict):
        raise SettingsError("The rules settings must be a set of named values.")
    out: UiFilters = {}
    if "theme_check" in value:
        out["theme_check"] = bool(value["theme_check"])
    if "theme_partly" in value:
        partly = str(value["theme_partly"] or "").strip().casefold()
        if partly not in filters_mod.PARTLY_MODES:
            raise SettingsError('Photos that fit the theme "partly" can be let through or flagged.')
        out["theme_partly"] = partly
    if "rules" in value:
        try:
            rules = filters_mod.clean_rules(value["rules"])
        except filters_mod.FilterRuleError as exc:
            raise SettingsError(str(exc)) from exc
        out["rules"] = [{"id": r.id, "kind": r.kind, "text": r.text, "mode": r.mode} for r in rules]
    return out


def get_filter_rules(cfg: Config) -> list[filters_mod.UserRule]:
    """The rules new photos are checked with (config plus web app settings)."""
    return list(checks.filter_spec(cfg).rules)


def _clean_update_cache(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SettingsError("The update cache must be a mapping.")
    version, checked_at, ok = value.get("version"), value.get("checked_at"), value.get("ok")
    if not isinstance(version, str) or not isinstance(ok, bool):
        raise SettingsError("The update cache is not valid.")
    if isinstance(checked_at, bool) or not isinstance(checked_at, int | float):
        raise SettingsError("The update cache is not valid.")
    return {"version": version, "checked_at": float(checked_at), "ok": ok}


def _clean_max_tries(value: Any) -> int:
    message = f"Max tries per photo must be a number from 1 to {metadata_rules.MAX_TRIES}."
    try:
        tries = int(value)
    except (TypeError, ValueError):
        raise SettingsError(message) from None
    if isinstance(value, bool) or not 1 <= tries <= metadata_rules.MAX_TRIES:
        raise SettingsError(message)
    return tries


def _clean_filters_merged(store: Store, value: Any) -> UiFilters:
    """New filters settings laid over the saved ones (a damaged saved value is replaced)."""
    new = _clean_filters(value)
    try:
        old = _clean_filters(store.get_setting("ui.filters") or {})
    except SettingsError:
        old = {}
    return {**old, **new}


def _clean_bedrock_profile(value: Any) -> str:
    value = str(value or "").strip()
    if value and not PROFILE_RE.match(value):
        raise SettingsError("That AWS profile name is not valid.")
    return value


# key -> cleaner; a key that is not here is stored as given (`ui.onboarded`, `ui.last_folder`).
_CLEANERS: dict[str, Callable[[Any], Any]] = {
    "ui.style": _clean_style,
    "ui.bedrock_profile": _clean_bedrock_profile,
    "ui.language": _clean_language_setting,
    "ui.update_check": bool,
    "ui.include_gps": bool,
    "ui.update_cache": _clean_update_cache,
    "ui.max_tries": _clean_max_tries,
}


def set_ui_setting(store: Store, name: str, value: Any) -> Any:
    """Save a UI setting (validated for `style`); returns the stored value."""
    key = _ui_key(name)
    if key == "ui.filters":  # the one cleaner that needs the saved value
        value = _clean_filters_merged(store, value)
    elif key in _CLEANERS:
        value = _CLEANERS[key](value)
    store.set_setting(key, value)
    return value
