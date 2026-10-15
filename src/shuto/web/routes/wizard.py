"""The welcome wizard."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from shuto.settings import ui as settings_ui
from shuto.web.routes.actions import apply_model, save_login, save_style
from shuto.web.routes.model_support import (
    OLLAMA_URL,
    current_model,
    helper_steps,
    model_context,
    storage_context,
)
from shuto.web.routes.settings import style_values
from shuto.web.state import MODELS_JOB_KEY, Ctx, PostForm
from shuto.workflow import ftps_login
from shuto.workflow import models as workflow_models

router = APIRouter()

WIZARD_STEPS = (
    ("welcome", "Welcome"),
    ("helper", "AI helper"),
    ("model", "AI model"),
    ("login", "Upload login"),
    ("style", "Your style"),
    ("done", "All set"),
)


@router.get("/welcome", response_class=HTMLResponse)
def welcome(request: Request, ctx: Ctx, step: int = 1):
    step = min(max(step, 1), len(WIZARD_STEPS))
    extra: dict = {}
    if step in (2, 3, 6):
        report = workflow_models.doctor(ctx.cfg, ctx.store, config_path=ctx.state.config_path)
        extra.update(report=report, ollama=report.ollama, os_name=report.hardware.get("os"))
        extra["helper_steps"] = helper_steps(report.hardware.get("os", ""))
        if step in (3, 6):
            extra.update(model_context(ctx, report))
        if step == 3:
            extra["model_status"] = ctx.model_status(request)
            extra["later"] = bool(request.query_params.get("later"))
            extra["ms"] = storage_context(ctx)
            active = ctx.runner.active_job(MODELS_JOB_KEY)
            extra.update(job=ctx.runner.get(active) if active else None, job_active=bool(active))
    if step in (4, 6):
        extra["login"] = ftps_login.ftps_login_status(ctx.cfg)
    if step == 5:
        extra["style"] = style_values(ctx)
    if step == 6:
        extra["model_name"] = current_model(ctx)
    return ctx.render(
        request,
        "welcome.html",
        step=step,
        wizard=WIZARD_STEPS,
        ollama_url=OLLAMA_URL,
        **extra,
    )


@router.get("/welcome/nav", response_class=HTMLResponse)
def welcome_nav(request: Request, ctx: Ctx):
    """The buttons under the model step, from the current model state (read-only)."""
    return ctx.render(request, "_wizard_model_nav.html", model_status=ctx.fresh_model_status())


@router.post("/welcome/{step}")
def welcome_post(step: int, form: PostForm, ctx: Ctx):
    if not 1 <= step <= len(WIZARD_STEPS):
        raise HTTPException(status_code=404, detail="Unknown step.")
    here, nxt = f"/welcome?step={step}", f"/welcome?step={step + 1}"
    notice = error = None
    if step == 3:
        spec = str(form.get("spec") or "")
        if spec:
            notice, error = apply_model(ctx, spec)
        elif form.get("later") == "yes":
            return ctx.go(nxt, "You can choose the AI model later in Settings.", None)
        else:
            return ctx.go(here, None, "Choose an AI model first, or pick Set up later.")
    elif step == 4:
        notice, error = save_login(ctx, form)
    elif step == 5:
        notice, error = save_style(ctx, form)
    elif step == 6:
        settings_ui.set_ui_setting(ctx.store, "onboarded", True)
        target = "/add" if form.get("then") == "add" else "/"
        return ctx.go(target, None, None)
    return ctx.go(here if error else nxt, notice, error)
