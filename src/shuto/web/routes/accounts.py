"""Settings routes for logins and keys: the Shutterstock login and the online AI keys."""

from __future__ import annotations

import re

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from starlette.datastructures import FormData

from shuto.core import errors
from shuto.core import secrets as shuto_secrets
from shuto.core.jobs import JobContext
from shuto.settings import ui as settings_ui
from shuto.web.routes.actions import API_KEY_ENV, save_login
from shuto.web.state import Ctx, PostForm
from shuto.workflow import ftps_login

router = APIRouter()

FTPS_JOB_KEY = "ftps"
LOCAL_PATH_RE = re.compile(r"^/(?!/)[A-Za-z0-9._~%/-]*$")
MAX_KEY_LEN = 500
MIN_KEY_LEN = 8


def _key_return(form: FormData) -> str:
    back = str(form.get("return_to") or "")
    if back == "/welcome?step=3":
        return "/welcome?step=3&online=1"
    return "/settings"


@router.post("/settings/ftps")
def settings_ftps(form: PostForm, ctx: Ctx):
    notice, error = save_login(ctx, form)
    if not notice and not error:
        error = "Enter your Shutterstock email (or username) and password."
    back = str(form.get("back") or "")
    if not LOCAL_PATH_RE.match(back):
        back = "/settings"
    return ctx.go(back, notice, error)


@router.post("/settings/ftps/test", response_class=HTMLResponse)
def settings_ftps_test(request: Request, form: PostForm, ctx: Ctx):
    cfg = ctx.cfg

    def run(job: JobContext) -> str:
        ftps_login.test_ftps_login(cfg)
        return "The login works. Shutterstock accepted it."

    return ctx.start_job(request, "ftps_test", FTPS_JOB_KEY, run)


@router.post("/settings/ftps/forget")
def settings_ftps_forget(request: Request, form: PostForm, ctx: Ctx):
    try:
        shuto_secrets.delete_secret("SHUTO_FTPS_USER")
        shuto_secrets.delete_secret("SHUTO_FTPS_PASSWORD")
    except RuntimeError as exc:
        return ctx.go("/settings", None, str(exc), request)
    left = ftps_login.ftps_login_status(ctx.cfg)
    if left["user_set"] or left["password_set"]:
        return ctx.go(
            "/settings",
            "Removed from the keyring.",
            "A login is still found in another place on this computer "
            "(a settings file). Remove it there too.",
            request,
        )
    return ctx.go("/settings", "The saved login was removed.", None, request)


@router.post("/settings/key")
def settings_key(form: PostForm, ctx: Ctx):
    """Save an online AI access key (keyring) or the AWS profile name. Never echoed back."""
    prov = str(form.get("provider") or "")
    here = _key_return(form)
    if prov == "bedrock":
        name = str(form.get("profile") or "").strip()
        try:
            settings_ui.set_ui_setting(ctx.store, "bedrock_profile", name)
        except errors.PipelineError as exc:
            return ctx.go(here, None, str(exc))
        ctx.state.reload_settings()
        return ctx.go(here, "AWS profile saved." if name else "AWS profile cleared.", None)
    env = API_KEY_ENV.get(prov)
    if env is None:
        raise HTTPException(status_code=404, detail="Unknown provider.")
    key = str(form.get("key") or "").strip()
    if not key:
        return ctx.go(here, None, "Paste the access key first.")
    if not MIN_KEY_LEN <= len(key) <= MAX_KEY_LEN or re.search(r"\s", key):
        return ctx.go(here, None, "That does not look like an access key. Paste it again.")
    try:
        shuto_secrets.set_secret(env, key)
    except RuntimeError as exc:
        return ctx.go(here, None, str(exc).replace(key, ""))
    return ctx.go(here, f"Access key saved for {prov.capitalize()}.", None)


@router.post("/settings/key/forget")
def settings_key_forget(request: Request, form: PostForm, ctx: Ctx):
    prov = str(form.get("provider") or "")
    here = _key_return(form)
    if prov == "bedrock":
        settings_ui.set_ui_setting(ctx.store, "bedrock_profile", "")
        ctx.state.reload_settings()
        return ctx.go(here, "AWS profile cleared.", None, request)
    env = API_KEY_ENV.get(prov)
    if env is None:
        raise HTTPException(status_code=404, detail="Unknown provider.")
    try:
        shuto_secrets.delete_secret(env)
    except RuntimeError as exc:
        return ctx.go(here, None, str(exc), request)
    if shuto_secrets.get_secret(env):
        return ctx.go(
            here,
            "Removed from the secure password store.",
            "An access key is still found in another place on this computer "
            "(a settings file). Remove it there too.",
            request,
        )
    return ctx.go(here, f"The saved access key for {prov.capitalize()} was removed.", None, request)
