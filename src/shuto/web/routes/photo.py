"""The photo page: view, edit, approve, send back to the AI, describe again, record a result."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from shuto.ai import filters as filters_mod
from shuto.core.errors import PipelineError
from shuto.core.jobs import JobContext
from shuto.core.models import ALLOWED, REASON_CODES, REASON_LABELS, Photo, Status
from shuto.web import ux
from shuto.web.routes.batch_support import (
    batch_url,
    filter_of,
    in_filter,
    is_sent,
    photo_path,
)
from shuto.web.routes.recheck import panel as recheck_panel
from shuto.web.routes.review import send_back_and_run
from shuto.web.state import Ctx, PostForm, WebCtx, split_lines
from shuto.workflow import describing, resend
from shuto.workflow import review as workflow_review

router = APIRouter()

RESULT_STATUSES = (Status.EXPORTED, Status.SUBMITTED, Status.REJECTED)
# Reasons offered as tick boxes; "quality_mild" is set by the "Data licensing only" button.
RESULT_REASONS = [(c, REASON_LABELS[c]) for c in REASON_CODES if c != "quality_mild"]
# Flip to True to make the web forms ask for a reason when a photo is marked Rejected.
RESULT_NEEDS_REASON = False
OUTCOME_VALUES = {"accept": "accepted", "data_licensing": "data_licensing", "reject": "rejected"}
OUTCOME_NOTICES = {
    "accepted": "Accepted.",
    "data_licensing": "Noted: accepted for data licensing only.",
    "rejected": "Rejection recorded.",
}


def _check_rows(flt: dict[str, Any] | None) -> list[dict[str, str]]:
    """One row per check for the photo page: what was asked, the result, the AI's reason."""
    if not isinstance(flt, dict):
        return []
    rows: list[dict[str, str]] = []
    theme = flt.get("theme")
    if isinstance(theme, dict):
        result, tone = {
            "yes": ("Fits", "ok"),
            "partly": ("Partly fits", "review"),
            "no": ("Doesn't fit", "blocked"),
        }.get(str(theme.get("fit")), ("Not sure", "review"))
        rows.append(
            {
                "what": f'Fits your theme "{theme.get("text", "")}"',
                "result": result,
                "tone": tone,
                "reason": str(theme.get("reason") or ""),
            }
        )
    for r in flt.get("rules") or []:
        if not isinstance(r, dict):
            continue
        res = r.get("result")
        tone = "ok" if res == "pass" else "review"
        if res == "fail" and r.get("mode") == "set_aside":
            tone = "blocked"
        label = filters_mod.UserRule(
            id="", kind=str(r.get("kind", "")), text=str(r.get("text", "")), mode=""
        ).label()
        rows.append(
            {
                "what": label,
                "result": {"pass": "Passed", "fail": "Not met"}.get(str(res), "Not sure"),
                "tone": tone,
                "reason": str(r.get("reason") or ""),
            }
        )
    return rows


def _older_rules(ctx: WebCtx, photo: Photo) -> bool:
    """True when the photo was checked with a different theme or rules than today's."""
    old = (photo.filters or {}).get("fingerprint")
    if not old:
        return False
    try:
        return old != filters_mod.filter_spec(ctx.cfg).fingerprint()
    except PipelineError:
        return False


def _save_edits(ctx: WebCtx, photo_id: int, form: Any) -> None:
    """Save the edit form (guardrails re-run); raises PipelineError with a plain reason."""
    categories = [str(c) for c in form.getlist("categories")]
    if len(categories) > 2:
        raise PipelineError("Choose at most 2 categories.")
    workflow_review.update_metadata(
        ctx.cfg,
        ctx.store,
        photo_id,
        description=str(form.get("description") or "").strip(),
        title=str(form.get("title") or "").strip(),
        keywords=split_lines(str(form.get("keywords") or "")),
        categories=categories,
        editorial=form.get("editorial") == "yes",
        mature=form.get("mature") == "yes",
        release_ids=split_lines(str(form.get("release_ids") or "")),
    )


@router.get("/photo/{photo_id}", response_class=HTMLResponse)
def photo_page(request: Request, photo_id: int, ctx: Ctx, back: str | None = None):
    photo = ctx.get_photo_or_404(photo_id)
    show = filter_of(back)
    meta = photo.metadata or {}
    siblings: list[int] = []
    if photo.batch_id:
        siblings = [
            p.id
            for p in ctx.store.list_photos(photo.batch_id)
            if p.id == photo_id or in_filter(p, show)
        ]
    idx = siblings.index(photo_id) if photo_id in siblings else -1
    prev_id = siblings[idx - 1] if idx > 0 else None
    next_id = siblings[idx + 1] if 0 <= idx < len(siblings) - 1 else None
    back_href = (
        batch_url(photo.batch_id, "review") + (f"?show={show}" if show else "")
        if photo.batch_id
        else "/"
    )
    return ctx.render(
        request,
        "photo.html",
        photo=photo,
        meta=meta,
        keywords_text=", ".join(meta.get("keywords") or []),
        keyword_count=len(meta.get("keywords") or []),
        gr=photo.guardrail or {},
        cull=photo.cull or {},
        detected=photo.detected or {},
        location=meta.get("location") or {},
        events=ctx.store.events_for(photo_id),
        rejections=ctx.store.list_rejections(photo_id),
        show=show or "",
        sent=is_sent(photo),
        sent_when=(photo.uploaded_at or photo.exported_at or photo.submitted_at or "")[:10],
        back_href=back_href,
        prev_href=photo_path(prev_id, show) if prev_id else None,
        next_href=photo_path(next_id, show) if next_id else None,
        next_id=next_id,
        editable=photo.status in workflow_review.PRE_UPLOAD_EDITABLE,
        can_approve=photo.status in (Status.DESCRIBED, Status.NEEDS_REVIEW, Status.SET_ASIDE),
        checks=_check_rows(photo.filters),
        checks_overridden=bool((photo.filters or {}).get("overridden")),
        checked_with_older_rules=_older_rules(ctx, photo),
        can_ready=photo.status != Status.READY and Status.READY in ALLOWED.get(photo.status, ()),
        can_result=photo.status in RESULT_STATUSES,
        result_needs_reason=RESULT_NEEDS_REASON,
        result_reasons=RESULT_REASONS,
        reason_labels=REASON_LABELS,
        releases_text="\n".join(photo.release_ids),
        include_gps=ctx.cfg.prepare.include_gps,
        batch_job=ctx.runner.active_job(photo.batch_id) if photo.batch_id else None,
        **recheck_panel(ctx, photo),
    )


@router.post("/photo/{photo_id}")
def photo_save(photo_id: int, form: PostForm, ctx: Ctx):
    ctx.get_photo_or_404(photo_id)
    path = photo_path(photo_id, filter_of(form.get("back")))
    try:
        _save_edits(ctx, photo_id, form)
    except PipelineError as exc:
        return ctx.back(path, error=str(exc))
    return ctx.back(path, notice="Saved. The Shutterstock rules were checked again.")


@router.post("/photo/{photo_id}/approve")
def photo_approve(photo_id: int, form: PostForm, ctx: Ctx):
    cfg = ctx.cfg
    photo = ctx.get_photo_or_404(photo_id)
    show = filter_of(form.get("back"))
    path = photo_path(photo_id, show)
    try:
        if "keywords" in form:  # the edit form was submitted: save it first, then approve
            _save_edits(ctx, photo_id, form)
            after = ctx.get_photo_or_404(photo_id)
            if photo.status == Status.DESCRIBED and after.status != Status.DESCRIBED:
                why = " ".join((after.guardrail or {}).get("messages") or [])
                raise PipelineError(f"Saved, but not approved. Fix this first: {why}".strip())
        s = workflow_review.approve(cfg, ctx.store, photo_ids=[photo_id], include_needs_review=True)
    except PipelineError as exc:
        return ctx.back(path, error=str(exc))
    if s.refused:
        return ctx.back(path, error=next(iter(s.refused.values())))
    # `next` must be a plain photo id from the same batch; anything else is ignored.
    raw = str(form.get("next") or "")
    target = ctx.store.get_photo(int(raw)) if raw.isdigit() and len(raw) < 12 else None
    if target is not None and target.batch_id == photo.batch_id:
        return ctx.back(photo_path(int(raw), show), notice="Approved.")
    if photo.batch_id:
        review = batch_url(photo.batch_id, "review") + (f"?show={show}" if show else "")
        return ctx.back(review, notice="Approved. That was the last photo here.")
    return ctx.back(path, notice="Approved.")


@router.post("/photo/{photo_id}/ready")
def photo_ready(request: Request, photo_id: int, form: PostForm, ctx: Ctx):
    photo = ctx.get_photo_or_404(photo_id)
    path = photo_path(photo_id, filter_of(form.get("back")))
    if not photo.batch_id:
        return ctx.back(path, error="This photo is not part of a batch.")
    notice, error = send_back_and_run(ctx, request, photo.batch_id, [photo_id])
    return ctx.back(path, notice=notice, error=error)


@router.post("/photo/{photo_id}/regenerate", response_class=HTMLResponse)
def photo_regenerate(request: Request, photo_id: int, form: PostForm, ctx: Ctx):
    cfg = ctx.cfg
    photo = ctx.get_photo_or_404(photo_id)
    if not photo.batch_id:
        return ctx.message_fragment(request, "This photo is not part of a batch.")
    batch_id = photo.batch_id
    block = ctx.model_block(request)
    if block:
        return ctx.message_fragment(request, block)

    def run(job: JobContext) -> str:
        s = describing.describe_batch(
            cfg, ctx.store, batch_id, only_ids=[photo_id], cancel=job.cancelled
        )
        if s.aborted or s.errors:
            fe = ux.friendly_error(s.aborted or s.errors[0][1])
            raise PipelineError(f"{fe['what']} {fe['todo']}")
        return "Described again. Reload the page to see the new text."

    return ctx.start_job(request, "describe", batch_id, run, total=1)


@router.post("/photo/{photo_id}/result")
def photo_result(photo_id: int, form: PostForm, ctx: Ctx):
    ctx.get_photo_or_404(photo_id)
    path = photo_path(photo_id, filter_of(form.get("back")))
    outcome = OUTCOME_VALUES.get(str(form.get("outcome")))
    if outcome is None:
        return ctx.back(path, error="Choose Accepted, Data licensing only or Rejected.")
    try:
        resend.record_result(
            ctx.store,
            photo_id,
            outcome,
            [str(v) for v in form.getlist("reasons")],
            str(form.get("note") or "").strip() or None,
            cfg=ctx.cfg,
            require_reason=RESULT_NEEDS_REASON,
        )
    except PipelineError as exc:
        return ctx.back(path, error=str(exc))
    return ctx.back(path, notice=OUTCOME_NOTICES[outcome])
