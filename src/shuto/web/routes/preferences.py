"""Settings routes for preferences: style, language, photo rules, tries and privacy."""

from __future__ import annotations

from fastapi import APIRouter

from shuto.core import errors
from shuto.settings import ui as settings_ui
from shuto.web.routes.actions import save_rules, save_style
from shuto.web.routes.settings import language_values
from shuto.web.state import Ctx, PostForm

router = APIRouter()


@router.post("/settings/style")
def settings_style(form: PostForm, ctx: Ctx):
    notice, error = save_style(ctx, form)
    return ctx.go("/settings", notice, error)


@router.post("/settings/language")
def settings_language(form: PostForm, ctx: Ctx):
    strict = str(form.get("english_only") or "yes") != "no"
    value: dict = {"english_only": strict}
    if not strict:
        value["language"] = str(form.get("language") or "")
    try:
        settings_ui.set_ui_setting(ctx.store, "language", value)
    except errors.PipelineError as exc:
        return ctx.go("/settings", None, str(exc))
    ctx.state.reload_settings()
    if strict:
        return ctx.go("/settings", "Descriptions and keywords are written in English only.", None)
    info = language_values(ctx)
    cautions = []
    if info["local"]:
        cautions.append(f"a local AI model may write {info['language']} badly")
    cautions.append("Shutterstock may expect English")
    return ctx.go(
        "/settings",
        f"Saved. Descriptions and keywords will be written in {info['language']}. "
        f"Be careful: {'; '.join(cautions)}. Details are under Metadata language.",
        None,
    )


@router.post("/settings/rules")
def settings_rules(form: PostForm, ctx: Ctx):
    notice, error = save_rules(ctx, form)
    return ctx.go("/settings", notice, error)


@router.post("/settings/tries")
def settings_tries(form: PostForm, ctx: Ctx):
    try:
        tries = settings_ui.set_ui_setting(ctx.store, "max_tries", form.get("max_tries"))
    except errors.PipelineError as exc:
        return ctx.go("/settings", None, str(exc))
    ctx.state.reload_settings()
    return ctx.go(
        "/settings",
        f"shuto will try up to {tries} {'time' if tries == 1 else 'times'} per photo.",
        None,
    )


@router.post("/settings/privacy")
def settings_privacy(form: PostForm, ctx: Ctx):
    on = str(form.get("include_gps") or "") == "yes"
    settings_ui.set_ui_setting(ctx.store, "include_gps", on)
    ctx.state.reload_settings()
    note = (
        "Photos you upload will include the GPS location. Photos already prepared keep "
        "what they had; prepare them again to add it."
        if on
        else "The GPS location is removed from photos you upload, also from photos "
        "already prepared."
    )
    return ctx.go("/settings", note, None)
