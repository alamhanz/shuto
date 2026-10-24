"""In-app updates: the panel, the banner and the update itself."""

from __future__ import annotations

import threading

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response

from shuto.settings import ui as settings_ui
from shuto.update import core as updates
from shuto.web.state import Ctx, PostForm, WebCtx

router = APIRouter()


def update_panel(ctx: WebCtx, request: Request, *, force: bool = False, problem: str | None = None):
    check_on = bool(settings_ui.get_ui_setting(ctx.store, "update_check", True))
    if check_on or force:
        latest = updates.cached_latest(ctx.store, force=force)
    else:
        latest = updates.peek_cached(ctx.store)
    current = updates.current_version()
    newer = latest is not None and updates.is_newer(latest.version, current)
    elig = updates.eligibility(
        ctx.runner, client_host=request.client.host if request.client else ""
    )
    status = updates.read_status(ctx.cfg)
    if status and status.get("state") in ("started", "waiting", "installing"):
        status = None
    if status and status.get("state") == "done" and status.get("to") != current:
        status = None
    return ctx.render(
        request,
        "_updates.html",
        current=current,
        latest=latest,
        newer=newer,
        elig=elig,
        check_on=check_on,
        checked=force,
        check_failed=updates.last_check_failed(ctx.store),
        status=status,
        log_file=str(updates.log_path(ctx.cfg)),
        problem=problem,
    )


@router.get("/updates/panel", response_class=HTMLResponse)
def updates_panel(request: Request, ctx: Ctx):
    return update_panel(ctx, request)


@router.get("/updates/banner", response_class=HTMLResponse)
def updates_banner(request: Request, ctx: Ctx):
    status = updates.read_status(ctx.cfg)
    if (
        status
        and not status.get("seen")
        and status.get("state")
        in (
            "done",
            "failed",
            "rolled_back",
        )
    ):
        updates.mark_status_seen(ctx.cfg)
        return ctx.render(request, "_update_banner.html", status=status, latest=None)
    if not settings_ui.get_ui_setting(ctx.store, "update_check", True):
        return Response("")
    latest = updates.cached_latest(ctx.store)
    if latest is None or not updates.is_newer(latest.version, updates.current_version()):
        return Response("")
    return ctx.render(request, "_update_banner.html", status=None, latest=latest)


@router.post("/updates/check", response_class=HTMLResponse)
def updates_check(request: Request, form: PostForm, ctx: Ctx):
    return update_panel(ctx, request, force=True)


@router.post("/updates/settings")
def updates_settings(form: PostForm, ctx: Ctx):
    on = str(form.get("update_check") or "") == "on"
    settings_ui.set_ui_setting(ctx.store, "update_check", on)
    notice = (
        "shuto will check for new versions when it opens and every few hours."
        if on
        else "Automatic checks are off."
    )
    return ctx.go("/settings#updates", notice, None)


@router.post("/updates/start", response_class=HTMLResponse)
def updates_start(request: Request, form: PostForm, ctx: Ctx):
    uv = updates.find_uv()
    host = request.client.host if request.client else ""
    elig = updates.eligibility(ctx.runner, client_host=host, uv=uv)
    if not elig.ok:
        return update_panel(ctx, request, problem=elig.reason)
    latest = updates.cached_latest(ctx.store)
    if (
        latest is None
        or not updates.is_newer(latest.version, updates.current_version())
        or str(form.get("version") or "") != latest.version
    ):
        return update_panel(
            ctx, request, problem="That version is no longer the newest. Check again, then update."
        )
    if not ctx.runner.close():
        return update_panel(
            ctx, request, problem="Wait until the current task finishes, then update."
        )
    started = False
    problem = None
    try:
        updates.start_update(
            ctx.cfg,
            latest,
            uv=uv or "",
            ui_args=getattr(request.app.state, "launch_args", None),
        )
        started = updates.wait_for_helper(ctx.cfg)
        if not started:
            problem = (
                "The updater did not start, so nothing was changed. shuto is still "
                "running. Try again, or run the install line again."
            )
    except updates.UpdateError as exc:
        problem = str(exc)
    except (OSError, ValueError):
        problem = "shuto could not start the update, so nothing was changed. Try again."
    finally:
        if not started:
            ctx.runner.reopen()
    if not started:
        return update_panel(ctx, request, problem=problem)
    from shuto.web.app import request_shutdown

    timer = threading.Timer(1.0, request_shutdown, args=(request.app,))
    timer.daemon = True
    timer.start()
    return ctx.render(request, "_updating.html", version=latest.version)
