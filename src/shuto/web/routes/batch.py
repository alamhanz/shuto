"""Batch entry and the Check page: where a batch goes next, and the check job."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from shuto.ai import selection
from shuto.ai.model_choice import ModelChoiceRequired
from shuto.core.errors import PipelineError
from shuto.core.jobs import JobContext
from shuto.core.models import Status
from shuto.web import ux
from shuto.web.routes.batch_support import batch_page, batch_url, count_by_status
from shuto.web.state import Ctx, PostForm, WebCtx
from shuto.workflow import check_batch, checks, speed

router = APIRouter()

SPEED_CHOICES = ("tries", "model", "keep")


def start_live(ctx: WebCtx, batch_id: str):
    """Live speed check of a local run (None for online AI), shared with the Speed routes."""
    try:
        live = speed.make_live_run(ctx.cfg, ctx.store)
    except Exception:  # the speed check is optional: the job goes on without it
        logging.getLogger(__name__).exception("speed check unavailable")
        return None
    if live is not None:
        ctx.state.live[batch_id] = live
    return live


def check_facts(ctx: WebCtx, request: Request, batch_id: str, counts: dict[str, int]):
    """Estimate, model name and plain-words problems for the Check page."""
    facts: dict[str, Any] = {
        "estimate": None,
        "model": None,
        "problems": [],
        "checks": checks.active_checks(ctx.cfg),
    }
    if not (counts.get("imported", 0) + counts.get("ready", 0)):
        return facts
    try:
        sel = selection.resolve_selection(ctx.cfg, ctx.store)
        facts["model"] = f"{sel.provider} / {sel.model}"
        facts["estimate"] = check_batch.estimate_check(ctx.cfg, ctx.store, batch_id).text
    except ModelChoiceRequired:
        pass
    except PipelineError as exc:
        facts["problems"] = [ux.friendly_error(exc)["what"]]
    if not facts["problems"]:
        block = ctx.model_block(request)
        facts["problems"] = [block] if block else []
    return facts


@router.get("/batch/{batch_id}")
def batch_entry(batch_id: str, ctx: Ctx):
    ctx.get_batch_or_404(batch_id)
    stage = ux.stage(count_by_status(ctx.store.list_photos(batch_id)))
    target = {"empty": "review", "done": "submit"}.get(stage, stage)
    return RedirectResponse(batch_url(batch_id, target), status_code=303)


@router.get("/batch/{batch_id}/check", response_class=HTMLResponse)
def check_page(request: Request, batch_id: str, ctx: Ctx):
    ctx.get_batch_or_404(batch_id)
    counts = count_by_status(ctx.store.list_photos(batch_id))
    facts = check_facts(ctx, request, batch_id, counts)
    stage = ux.stage(counts)
    return batch_page(
        ctx,
        request,
        "batch_check.html",
        batch_id,
        "check",
        to_check=counts.get("imported", 0) + counts.get("ready", 0),
        next_step=ux.next_action(batch_id, counts) if stage != "check" else None,
        review_href=batch_url(batch_id, "review"),
        **facts,
    )


@router.post("/batch/{batch_id}/speed/{choice}", response_class=HTMLResponse)
def speed_choice(request: Request, batch_id: str, choice: str, form: PostForm, ctx: Ctx):
    """One click on the 'this is slow' suggestion; applies to the photos not started yet."""
    job_id = ctx.runner.active_job(batch_id)
    live = ctx.state.live.get(batch_id)
    if choice not in SPEED_CHOICES:
        raise HTTPException(status_code=404, detail="Unknown choice.")
    if live is not None:
        if choice == "tries":
            suggestion = live.suggestion()
            if suggestion is not None and suggestion.tries is not None:
                live.set_max_tries(suggestion.tries)
        elif choice == "model":
            if not live.switch_model(str(form.get("model") or "")):
                live.notice = "That model cannot be used now. Keeping the current one."
        else:
            live.dismiss()
    if job_id is None:
        return ctx.message_fragment(request, "The check has finished.")
    return ctx.job_fragment(request, job_id)


@router.post("/batch/{batch_id}/check", response_class=HTMLResponse)
def check_start(request: Request, batch_id: str, form: PostForm, ctx: Ctx):
    cfg, store = ctx.cfg, ctx.store
    ctx.get_batch_or_404(batch_id)
    n = len(store.list_photos(batch_id, [Status.IMPORTED, Status.READY]))
    if not n:
        return ctx.message_fragment(request, "There are no new photos to check.")
    block = ctx.model_block(request)
    if block:
        return ctx.message_fragment(request, block)

    def run(job: JobContext) -> str:
        live = start_live(ctx, batch_id)
        try:
            s = check_batch.check_batch(
                cfg,
                store,
                batch_id,
                progress=job.progress_text,
                cancel=job.cancelled,
                live=live,
            )
        finally:
            ctx.state.live.pop(batch_id, None)
        text = f"Quality check set aside {s.cull.culled} of {s.cull.total} new photos."
        d = s.describe
        if d is None:
            return text + " Stopped before the AI started."
        if d.aborted:
            fe = ux.friendly_error(d.aborted)
            raise PipelineError(f"{fe['what']} {fe['todo']}")
        wrote = d.described + d.needs_review + d.blocked + d.set_aside
        text += (
            f" The AI wrote text for {wrote} photos: "
            f"{d.described} look good, {d.needs_review + d.blocked} need a look."
        )
        if d.set_aside:
            text += f" {d.set_aside} were set aside because they don't fit your theme or rules."
        if d.failed:
            text += f" {d.failed} could not be written; press Check again to retry them."
        return text

    return ctx.start_job(request, "check", batch_id, run, total=check_batch.CHECK_SCALE)
