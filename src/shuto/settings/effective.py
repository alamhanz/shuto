"""Apply saved UI settings on top of the config."""

from __future__ import annotations

from typing import Any

from shuto.ai import filters as filters_mod
from shuto.core.config import Config, FilterRuleConfig
from shuto.core.errors import SettingsError
from shuto.core.validate import MAX_TRIES
from shuto.settings import ui as settings_ui
from shuto.store import Store


def _apply_filter_overrides(cfg: Config, store: Store) -> Config:
    saved = store.get_setting("ui.filters")
    if not isinstance(saved, dict) or not saved:
        return cfg
    update: dict[str, Any] = {}
    if isinstance(saved.get("theme_check"), bool):
        update["theme_check"] = saved["theme_check"]
    if saved.get("theme_partly") in filters_mod.PARTLY_MODES:
        update["theme_partly"] = saved["theme_partly"]
    if isinstance(saved.get("rules"), list):
        try:
            update["rules"] = [
                FilterRuleConfig(id=r.id, kind=r.kind, text=r.text, mode=r.mode)
                for r in filters_mod.clean_rules(saved["rules"])
            ]
        except ValueError:  # a damaged saved value never stops the app; the file's rules stay
            pass
    return cfg.model_copy(update={"filters": cfg.filters.model_copy(update=update)})


def _apply_bedrock_profile(cfg: Config, store: Store) -> Config:
    profile = store.get_setting("ui.bedrock_profile")
    if not isinstance(profile, str):
        return cfg
    bedrock = cfg.llm.bedrock.model_copy(update={"profile": profile})
    return cfg.model_copy(update={"llm": cfg.llm.model_copy(update={"bedrock": bedrock})})


def _apply_include_gps(cfg: Config, store: Store) -> Config:
    gps = store.get_setting("ui.include_gps")
    if not isinstance(gps, bool):
        return cfg
    return cfg.model_copy(update={"prepare": cfg.prepare.model_copy(update={"include_gps": gps})})


def _apply_max_tries(cfg: Config, store: Store) -> Config:
    tries = store.get_setting("ui.max_tries")
    if isinstance(tries, bool) or not isinstance(tries, int) or not 1 <= tries <= MAX_TRIES:
        return cfg
    return cfg.model_copy(update={"llm": cfg.llm.model_copy(update={"max_tries": tries})})


def _apply_language(cfg: Config, store: Store) -> Config:
    lang = store.get_setting("ui.language")
    if not isinstance(lang, dict) or not isinstance(lang.get("english_only"), bool):
        return cfg
    try:
        saved = settings_ui._clean_language_setting(lang)
    except SettingsError:  # a damaged saved value never stops the app; strict English stays
        return cfg
    style = cfg.style.model_copy(
        update={
            "english_only": saved["english_only"],
            "metadata_language": saved.get("language", ""),
        }
    )
    return cfg.model_copy(update={"style": style})


def _apply_style(cfg: Config, store: Store) -> Config:
    style = store.get_setting("ui.style") or {}
    if not style:
        return cfg
    update: dict[str, Any] = {
        k: style[k]
        for k in ("theme", "tone", "extra_instructions", "region_focus")
        if isinstance(style.get(k), str)
    }
    if isinstance(style.get("always_include"), list):
        update["keywords"] = cfg.style.keywords.model_copy(
            update={"always_include": list(style["always_include"])}
        )
    place = {
        part: style[f"default_{part}"]
        for part in ("city", "country")
        if isinstance(style.get(f"default_{part}"), str)
    }
    if place:
        update["default_place"] = cfg.style.default_place.model_copy(update=place)
    return cfg.model_copy(update={"style": cfg.style.model_copy(update=update)})


def effective_config(cfg: Config, store: Store) -> Config:
    """Config copy with the style and the theme/rules set in the web app (theme, tone,
    always-include, extras, region focus, `ui.filters`). A key that is present wins over the
    config file, even when empty."""
    for step in (
        _apply_bedrock_profile,
        _apply_filter_overrides,
        _apply_include_gps,
        _apply_max_tries,
        _apply_language,
        _apply_style,
    ):
        cfg = step(cfg, store)
    return cfg
