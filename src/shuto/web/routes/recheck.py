"""Check a sent photo again with the AI, then use or drop the new text (photo page)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from shuto.core.jobs import JobBusy
from shuto.core.models import Photo
from shuto.web import ux
from shuto.web.routes.batch_support import filter_of, photo_path
from shuto.web.routes.review import start_redo_job
from shuto.web.state import Ctx, PostForm, WebCtx, split_lines
from shuto.workflow import recheck

router = APIRouter()


def panel(ctx: WebCtx, photo: Photo) -> dict[str, Any]:
    """What the photo page needs to show the "Check again with AI" part (empty when not sent)."""
    can = recheck.why_not(photo) is None
    draft = photo.draft or {}
    state = draft.get("state") if can else None
    job_id = ctx.runner.active_job(photo.batch_id) if photo.batch_id else None
    new = dict(draft.get("metadata") or {}) if state == "ready" else {}
    return {
        "recheck": {
            "can": can,
            "too_late": recheck.why_not(photo) == recheck.TOO_LATE,
            "state": state,
            "busy": job_id is not None,
            "job_id": job_id,
            "new": new,
            "new_keywords": ", ".join(new.get("keywords") or []),
            "gr": draft.get("guardrail") or {},
            "error": ux.friendly_error(draft.get("error"))
            if state == "failed" and draft.get("error")
            else None,
        }
    }


@router.post("/photo/{photo_id}/recheck", response_class=HTMLResponse)
def photo_recheck(request: Request, photo_id: int, form: PostForm, ctx: Ctx):
    photo = ctx.get_photo_or_404(photo_id)
    if not photo.batch_id:
        return ctx.message_fragment(request, "This photo is not part of a batch.")
    batch_id = photo.batch_id
    block = ctx.model_block(request)
    if block:
        return ctx.message_fragment(request, block)
    s = recheck.queue(ctx.cfg, ctx.store, batch_id, [photo_id])
    if s.refused:
        why = s.refused[photo_id]
        return ctx.message_fragment(
            request, why[0].upper() + why[1:] + ("" if why[-1] == "." else ".")
        )
    running = ctx.runner.active_job(batch_id)
    if running is not None:
        # The running check or redo picks the photo up when it is done with the others.
        return ctx.message_fragment(
            request, "The AI is already working on this batch; this photo is next."
        )
    try:
        job_id = start_redo_job(ctx, batch_id)
    except JobBusy as exc:
        return ctx.message_fragment(request, str(exc))
    return ctx.job_fragment(request, job_id)


@router.post("/photo/{photo_id}/draft/use")
def draft_use(photo_id: int, form: PostForm, ctx: Ctx):
    ctx.get_photo_or_404(photo_id)
    path = photo_path(photo_id, filter_of(form.get("back")))
    edits: dict[str, Any] | None = None
    if "keywords" in form:  # the edit boxes were on the form
        edits = {
            "description": str(form.get("description") or "").strip(),
            "keywords": split_lines(str(form.get("keywords") or "")),
            "categories": [str(c) for c in form.getlist("categories")],
            "editorial": form.get("editorial") == "yes",
        }
    result = recheck.use_draft(ctx.cfg, ctx.store, photo_id, edits)
    if not result.ok:
        return ctx.back(path, error=" ".join(result.messages) or "The new text could not be used.")
    return ctx.back(
        path,
        notice=(
            f"New text saved. The Shutterstock info file was updated: {result.csv_path}. "
            "On the Shutterstock site, open Upload > CSV and upload this file again before "
            "you submit."
        ),
        error=" ".join(result.messages) or None,
    )


@router.post("/photo/{photo_id}/draft/drop")
def draft_drop(photo_id: int, form: PostForm, ctx: Ctx):
    photo = ctx.get_photo_or_404(photo_id)
    path = photo_path(photo_id, filter_of(form.get("back")))
    if (photo.draft or {}).get("state") == "waiting" and ctx.runner.active_job(
        photo.batch_id or ""
    ):
        return ctx.back(path, error="The AI is still writing the new text. Wait until it is done.")
    recheck.drop_draft(ctx.store, photo_id)
    return ctx.back(path, notice="Kept the old text.")
