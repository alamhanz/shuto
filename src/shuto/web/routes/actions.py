"""Settings actions shared by the welcome wizard and the Settings page."""

from __future__ import annotations

import re

from starlette.datastructures import FormData

from shuto.ai import filters as filters_mod
from shuto.core import errors
from shuto.core import secrets as shuto_secrets
from shuto.settings import ui as settings_ui
from shuto.web.state import WebCtx
from shuto.workflow import ftps_login
from shuto.workflow import models as workflow_models

API_PROVIDERS = ("anthropic", "gemini", "bedrock")
API_KEY_ENV = {
    "anthropic": "ANTHROPIC_API_KEY",
    "gemini": "GEMINI_API_KEY",
}
TAG_RE = re.compile(r"^[A-Za-z0-9._:/-]{1,100}$")
RETURN_PAGES = {"/settings", "/welcome?step=3"}
MAX_RULES = filters_mod.MAX_RULES
MAX_RULE_ROWS = 40  # form rows read at most; the real limit is MAX_RULES


def apply_model(ctx: WebCtx, spec: str) -> tuple[str | None, str | None]:
    """Returns (notice, error) after trying to use a model or an online provider."""
    cfg = ctx.cfg
    spec = spec.strip()
    if not TAG_RE.match(spec):
        return None, "That model name is not valid."
    prov = spec.partition(":")[0]
    if ":" in spec and prov in API_PROVIDERS:
        pass  # provider choice, validated by pipeline.use_model against the price table
    elif workflow_models.model_disk_advice(cfg, spec) is None:
        return None, f"{spec} is not in the model catalog."
    try:
        sel = workflow_models.use_model(cfg, ctx.store, spec)
    except errors.PipelineError as exc:
        return None, str(exc)
    return f"Now using {sel.provider}: {sel.model}.", None


def save_login(ctx: WebCtx, form: FormData) -> tuple[str | None, str | None]:
    user = str(form.get("username") or "").strip()
    password = str(form.get("password") or "")
    saved = ftps_login.ftps_login_status(ctx.cfg)
    if not user and not password:
        return None, None
    if not user:
        return None, "Enter the email or username you use on Shutterstock."
    if not password and not saved["password_set"]:
        return None, "Enter your Shutterstock password."
    try:
        shuto_secrets.set_secret("SHUTO_FTPS_USER", user)
        if password:
            shuto_secrets.set_secret("SHUTO_FTPS_PASSWORD", password)
    except RuntimeError as exc:
        return None, str(exc)
    return "Login saved on this computer.", None


def save_style(ctx: WebCtx, form: FormData) -> tuple[str | None, str | None]:
    current = dict(settings_ui.get_ui_setting(ctx.store, "style") or {})
    current["theme"] = str(form.get("theme") or "")
    current["tone"] = str(form.get("tone") or "")
    current["always_include"] = str(form.get("always_include") or "")
    if "region_focus" in form:
        current["region_focus"] = str(form.get("region_focus") or "")
    for key in ("default_city", "default_country"):
        if key in form:
            current[key] = str(form.get(key) or "")
    try:
        settings_ui.set_ui_setting(ctx.store, "style", current)
    except errors.PipelineError as exc:
        return None, str(exc)
    ctx.state.reload_settings()
    return "Your style is saved.", None


def save_rules(ctx: WebCtx, form: FormData) -> tuple[str | None, str | None]:
    """Save the theme check and the rule rows of the "Your photo rules" form."""
    rows = []
    for n in range(MAX_RULE_ROWS):
        text = str(form.get(f"rule_text_{n}") or "").strip()
        if not text or form.get(f"rule_remove_{n}"):
            continue
        rows.append(
            {
                "id": str(form.get(f"rule_id_{n}") or ""),
                "kind": str(form.get(f"rule_kind_{n}") or ""),
                "text": text,
                "mode": str(form.get(f"rule_mode_{n}") or ""),
            }
        )
    try:
        settings_ui.set_ui_setting(
            ctx.store,
            "filters",
            {
                "theme_check": form.get("theme_check") == "yes",
                "theme_partly": str(form.get("theme_partly") or "pass"),
                "rules": rows,
            },
        )
    except errors.PipelineError as exc:
        return None, str(exc)
    ctx.state.reload_settings()
    return "Your photo rules are saved. They apply to photos you check from now on.", None
