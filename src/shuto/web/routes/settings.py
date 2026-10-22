"""The Settings page and the values its forms start from."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from shuto.ai import selection
from shuto.core import errors
from shuto.metadata.rules import language_policy
from shuto.ollama import OLLAMA_DOWNLOAD_URL
from shuto.settings import ui as settings_ui
from shuto.web.routes.actions import MAX_RULES
from shuto.web.routes.model_support import (
    helper_steps,
    model_context,
    storage_context,
)
from shuto.web.state import MODELS_JOB_KEY, Ctx, WebCtx
from shuto.workflow import ftps_login, reports
from shuto.workflow import models as workflow_models

router = APIRouter()


def language_warnings(language: str, local: bool) -> list[dict[str, str]]:
    """The cautions shown when metadata is not strict English. The local-model one comes first
    and is the prominent one; the Shutterstock one is phrased as a caution, not a claim."""
    out = []
    if local:
        out.append(
            {
                "kind": "local",
                "text": (
                    f"A local AI model may not write {language} well: it can answer in the "
                    "wrong language, mix languages or produce broken words. Results are best "
                    "in English. Read every description and keyword before you approve."
                ),
            }
        )
    out.append(
        {
            "kind": "shutterstock",
            "text": (
                "Shutterstock may expect English metadata. Titles and keywords in another "
                "language might be rejected or be found less often by buyers."
            ),
        }
    )
    return out


def style_values(ctx: WebCtx) -> dict:
    cfg = ctx.cfg
    return {
        "theme": cfg.style.theme,
        "tone": cfg.style.tone,
        "always_include": ", ".join(cfg.style.keywords.always_include),
        "region_focus": cfg.style.region_focus,
        "default_city": cfg.style.default_place.city,
        "default_country": cfg.style.default_place.country,
    }


def language_values(ctx: WebCtx) -> dict:
    """The metadata language setting, with the cautions to show while strict is off."""
    style = ctx.cfg.style
    try:
        local = selection.resolve_selection(ctx.cfg, ctx.store).provider == "ollama"
    except Exception:  # nothing chosen yet: the default is a local model
        local = ctx.cfg.llm.provider == "ollama"
    policy = language_policy(style)
    name = policy.name or "that language"
    return {
        "english_only": policy.strict,
        "language": policy.name,
        "local": local,
        "warnings": [] if policy.strict else language_warnings(name, local),
    }


def rules_values(ctx: WebCtx) -> dict:
    """Rule rows for the form: the saved ones plus two empty rows (up to the limit)."""
    cfg = ctx.cfg
    try:
        saved = [
            {"id": r.id, "kind": r.kind, "text": r.text, "mode": r.mode}
            for r in settings_ui.get_filter_rules(cfg)
        ]
    except errors.PipelineError:
        saved = []
    blank = {"id": "", "kind": "avoid", "text": "", "mode": "set_aside"}
    room = max(0, MAX_RULES - len(saved))
    return {
        "theme_check": cfg.filters.theme_check,
        "theme_partly": cfg.filters.theme_partly,
        "theme": cfg.style.theme,
        "rule_rows": saved + [dict(blank) for _ in range(min(2, room))],
        "rule_count": len(saved),
        "max_rules": MAX_RULES,
    }


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, ctx: Ctx):
    report = workflow_models.doctor(
        ctx.cfg, ctx.store, config_path=ctx.state.config_path, with_conflict=False
    )
    usage = {"calls": 0, "input_tokens": 0, "output_tokens": 0, "seconds": 0.0}
    for b in reports.batch_report(ctx.store):
        for k in usage:
            usage[k] += b["usage"].get(k, 0) or 0
    active = ctx.runner.active_job(MODELS_JOB_KEY)
    return ctx.render(
        request,
        "settings.html",
        report=report,
        hw=report.hardware,
        ollama=report.ollama,
        helper_steps=helper_steps(report.hardware.get("os", "")),
        ollama_url=OLLAMA_DOWNLOAD_URL,
        login=ftps_login.ftps_login_status(ctx.cfg),
        style=style_values(ctx),
        language=language_values(ctx),
        rules=rules_values(ctx),
        include_gps=ctx.cfg.prepare.include_gps,
        max_tries=ctx.cfg.llm.max_tries,
        usage=usage,
        job=ctx.runner.get(active) if active else None,
        job_active=bool(active),
        model_status=ctx.model_status(request),
        ms=storage_context(ctx),
        **model_context(ctx, report),
    )
