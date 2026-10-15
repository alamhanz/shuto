"""Guardrails and rules applied to described photos."""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from shuto.ai import filters as filters_mod
from shuto.core.config import Config
from shuto.core.errors import PipelineError
from shuto.core.models import Photo, Status
from shuto.metadata import guardrails
from shuto.metadata import rules as metadata_rules
from shuto.metadata.model import PhotoMetadata, normalize
from shuto.store import Store


@lru_cache(maxsize=8)
def _restrictions(
    builtin: bool, extra: tuple[str, ...], disabled: tuple[str, ...], house: bool
) -> guardrails.Restrictions:
    return guardrails.load_restrictions(builtin, extra, disabled, house_rules=house)


def clear_cache() -> None:
    """Forget the loaded restriction sets (the next check loads them again)."""
    _restrictions.cache_clear()


def load_restrictions(cfg: Config) -> guardrails.Restrictions:
    r = cfg.restrictions
    return _restrictions(
        r.builtin,
        tuple(str(p) for p in r.extra_files),
        tuple(r.disabled_rules),
        cfg.style.region_focus == "japan",
    )


def _release_flags(release_ids: list[str]) -> tuple[bool, bool]:
    has_property = any(r.startswith("property:") for r in release_ids)
    has_model = any(not r.startswith("property:") for r in release_ids)
    return has_model, has_property


def active_checks(cfg: Config) -> list[str]:
    """Plain-English lines for the checks that new photos get (empty when there are none)."""
    try:
        spec = filters_mod.filter_spec(cfg)
    except PipelineError as exc:
        return [f"Your rules are not usable: {exc}"]
    lines: list[str] = []
    if spec.theme_active:
        partly = "needs a look" if spec.theme_partly == "flag" else "let through"
        lines.append(f'Theme "{spec.theme.strip()}": set aside if no, {partly} if partly')
    for rule in spec.rules:
        action = "set aside" if rule.mode == "set_aside" else "just flag"
        lines.append(f"{rule.label()} ({action})")
    return lines


def checks_summary(cfg: Config) -> str:
    """One line for `doctor`: 'theme "X" (set aside if no) + 2 rules', or 'none'."""
    try:
        spec = filters_mod.filter_spec(cfg)
    except PipelineError:
        return "your rules are not usable"
    parts = []
    if spec.theme_active:
        parts.append(f'theme "{spec.theme.strip()}" (set aside if no)')
    if spec.rules:
        parts.append(f"{len(spec.rules)} rule{'s' if len(spec.rules) != 1 else ''}")
    return " + ".join(parts) or "none"


def _filter_counts(cfg: Config) -> tuple[int, int]:
    """(number of rules, 1 if the theme is checked else 0), tolerant of unusable rules."""
    n_rules = len(cfg.filters.rules)
    theme = 1 if cfg.filters.theme_check and (cfg.style.theme or "").strip() else 0
    return n_rules, theme


def _estimate_tokens(cfg: Config) -> dict[str, int]:
    rules, theme = _filter_counts(cfg)
    return {
        "prompt_tokens": 1200 + 40 * rules + 60 * theme,
        "output_tokens": 600 + 50 * (rules + theme),
    }


NOT_ENGLISH_TEXT = metadata_rules.reason("non_english")


def not_english(data: dict[str, Any], strict: bool = True) -> bool:
    """True when the title, description or a keyword has non-Latin letters (never sent on).

    Always False when strict English is switched off (the owner chose another language)."""
    if not strict:
        return False
    return any(
        metadata_rules.has_foreign_script(str(data.get(k) or "")) for k in ("title", "description")
    ) or any(metadata_rules.has_foreign_script(str(k)) for k in data.get("keywords") or [])


def _status_for(
    meta: PhotoMetadata,
    gr: guardrails.GuardrailResult,
    filters: dict[str, Any] | None = None,
) -> Status:
    """Shutterstock-rule block wins, then set aside, then needs a look, then looks good."""
    if gr.action == "block":
        return Status.BLOCKED
    effect = filters_mod.status_effect(filters)
    if effect == "set_aside":
        return Status.SET_ASIDE
    extra = [w for w in meta.warnings if w not in gr.messages]
    if gr.action == "review" or extra or effect == "flag":
        return Status.NEEDS_REVIEW
    return Status.DESCRIBED


def _evaluate(
    cfg: Config, store: Store, photo: Photo, data: dict[str, Any]
) -> tuple[PhotoMetadata, guardrails.GuardrailResult, Status]:
    """Normalize hand-edited metadata and re-run the deterministic guardrails."""
    batch = store.get_batch(photo.batch_id) if photo.batch_id else None
    has_model, has_property = _release_flags(photo.release_ids)
    meta = normalize(photo.orig_name, data)
    meta, gr = guardrails.check(
        meta,
        photo.detected,
        load_restrictions(cfg),
        notes=batch.notes if batch else "",
        has_model_release=has_model,
        has_property_release=has_property,
    )
    meta.warnings.extend(m for m in gr.messages if m not in meta.warnings)
    if not_english(data, metadata_rules.language_policy(cfg.style).strict):
        meta.warnings.append(NOT_ENGLISH_TEXT)
    return meta, gr, _status_for(meta, gr, photo.filters)
