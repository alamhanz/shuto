"""Job status, cancel, the app-wide model banner and the download bar."""

from __future__ import annotations

from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from shuto.web.routes.model_support import BAR_KINDS, bar_response
from shuto.web.state import MODELS_JOB_KEY, Ctx, PostForm, WebCtx

router = APIRouter()


def job_extras(ctx: WebCtx, job_id: int) -> dict:
    job = ctx.runner.get(job_id)
    extra: dict = {}
    if job is not None and job.kind == "import":
        extra["batches"] = ctx.state.import_results.get(job_id, [])
        extra["already"] = ctx.state.import_known.get(job_id, [])
    if job is not None and job.kind in ("pull", "models_dir"):
        extra["next_href"] = ctx.state.pull_return
    return extra


@router.get("/jobs/{job_id}", response_class=HTMLResponse)
def job_status(request: Request, job_id: int, ctx: Ctx):
    return ctx.job_fragment(request, job_id, **job_extras(ctx, job_id))


@router.get("/model/banner", response_class=HTMLResponse)
def model_banner(request: Request, ctx: Ctx):
    """App-wide 'No AI model set' note. Quiet while the download bar shows, in the setup
    wizard, and when the local AI simply can't be reached (read-only)."""
    if urlparse(request.headers.get("hx-current-url", "")).path == "/welcome":
        return HTMLResponse("")
    if ctx.runner.active_job(MODELS_JOB_KEY):
        return HTMLResponse("")
    if ctx.model_status(request).state != "missing":
        return HTMLResponse("")
    return ctx.render(request, "_model_banner.html", conflict=ctx.ollama_conflict())


@router.get("/downloads/bar", response_class=HTMLResponse)
def download_bar(request: Request, ctx: Ctx, job: int = 0):
    """Small progress bar for the running model download, shown on every page (read-only)."""
    job_id = ctx.runner.active_job(MODELS_JOB_KEY) or job
    found = ctx.runner.get(job_id) if job_id else None
    if found is None or found.kind not in BAR_KINDS:
        return HTMLResponse("")
    if found.state not in ("queued", "running") and job != found.id:
        return HTMLResponse("")  # finished earlier: only the page that saw it run says so
    return bar_response(ctx, request, found.id)


@router.post("/jobs/{job_id}/cancel", response_class=HTMLResponse)
def job_cancel(request: Request, job_id: int, form: PostForm, ctx: Ctx):
    if ctx.runner.get(job_id) is None:
        raise HTTPException(status_code=404, detail="Unknown job.")
    ctx.runner.cancel(job_id)
    if ctx.runner.get(job_id).kind in BAR_KINDS:
        return bar_response(ctx, request, job_id)
    return ctx.job_fragment(request, job_id, **job_extras(ctx, job_id))
