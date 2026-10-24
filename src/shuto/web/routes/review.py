"""The Review page and bulk actions on selected photos."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse

from shuto.ai import filters as filters_mod
from shuto.core.errors import PipelineError
from shuto.core.jobs import JobBusy, JobContext
from shuto.core.models import UPLOADED_STATES, Photo, Status
from shuto.web import ux
from shuto.web.routes.batch import ai_busy, check_action, start_live
from shuto.web.routes.batch_support import (
    FILTERS,
    ai_facts,
    batch_page,
    batch_url,
    count_by_status,
    filter_of,
    in_filter,
    is_sent,
    is_waiting,
)
from shuto.web.state import Ctx, PostForm, WebCtx, short, split_lines
from shuto.workflow import recheck, redo
from shuto.workflow import review as workflow_review

router = APIRouter()

WAITING_FOR_AI = (Status.IMPORTED, Status.READY)
RUN_NOW_KEY = "ui.send_back_run_now"  # "Run the AI right away" on Send back; remembered
WHY_FILTERS = ("quality", "copies", "theme", "rules")  # why a photo was set aside
WHY_LABELS = {
    "quality": "Quality",
    "copies": "Copies",
    "theme": "Your theme",
    "rules": "Your rules",
}


def _why(value: Any) -> str | None:
    """A "why set aside" sub-filter from user input, or None when it is not a known one."""
    text = str(value or "")
    return text if text in WHY_FILTERS else None


def _why_set(photo: Photo) -> set[str]:
    """Which reasons put a photo in the "Set aside" group (empty for other photos)."""
    if photo.status == Status.CULLED:
        return {"quality"}
    if photo.status in (Status.DUPLICATE, Status.NEAR_DUPLICATE, Status.ALREADY_UPLOADED):
        return {"copies"}
    if photo.status == Status.SET_ASIDE:
        by = (photo.filters or {}).get("set_aside_by") or []
        found = {"theme" if k == "theme" else "rules" for k in by}
        return found or {"rules"}
    return set()


def _theme_needs_look(flt: dict[str, Any] | None) -> bool:
    """True when the theme check (not one of the user's rules) is why a photo needs a look."""
    theme = (flt or {}).get("theme")
    return isinstance(theme, dict) and theme.get("effect") not in (None, "ok")


def _aside_messages(flt: dict[str, Any] | None) -> list[str]:
    """Plain-words reasons for the checks that set a photo aside (theme first)."""
    if not isinstance(flt, dict):
        return []
    by = [str(k) for k in flt.get("set_aside_by") or []]
    only = {
        "theme": flt.get("theme") if "theme" in by else None,
        "rules": [
            r for r in flt.get("rules") or [] if isinstance(r, dict) and f"rule:{r.get('id')}" in by
        ],
    }
    return filters_mod.messages(only)


def _card(ctx: WebCtx, p: Photo) -> dict[str, Any]:
    gr = p.guardrail or {}
    meta = p.metadata or {}
    reasons: list[str] = []
    layer = ""
    if p.status == Status.CULLED:
        reasons = list((p.cull or {}).get("reasons") or [])
        layer = "Quality"
    elif p.status in (Status.DUPLICATE, Status.NEAR_DUPLICATE, Status.ALREADY_UPLOADED):
        layer = "Copy"
    elif p.status == Status.SET_ASIDE:
        reasons = _aside_messages(p.filters) or filters_mod.messages(p.filters)
        layer = "Theme" if "theme" in _why_set(p) else "Your rule"
    elif p.status == Status.DESCRIBED and (p.filters or {}).get("theme", {}).get("fit") == (
        "partly"
    ):
        reasons = filters_mod.messages({"theme": p.filters["theme"]})
        layer = "Theme"
    elif p.status in (Status.NEEDS_REVIEW, Status.BLOCKED):
        reasons = list(gr.get("messages") or [])
        layer = "Shutterstock rules" if reasons else ""
        if not reasons:
            reasons = filters_mod.messages(p.filters)
            layer = "Theme" if _theme_needs_look(p.filters) else "Your rule"
    return {
        "id": p.id,
        "name": p.orig_name,
        "status": p.status.value,
        "bucket": ux.bucket(p.status),
        "group": p.near_dup_group,
        "reason": short(str(reasons[0]), 220) if reasons else "",
        "layer": layer,
        "snippet": short(str(meta.get("description") or ""), 90),
        "sent": is_sent(p),
        "new_text": is_sent(p) and recheck.has_new_text(p),
        "pickable": p.status not in UPLOADED_STATES,
        "editorial": bool(meta.get("editorial")),
        "gps": p.has_gps,
        "gps_sent": p.has_gps and ctx.cfg.prepare.include_gps,
        "default_place": bool((meta.get("place") or {}).get("default")),
    }


@router.get("/batch/{batch_id}/review", response_class=HTMLResponse)
def review_page(
    request: Request, batch_id: str, ctx: Ctx, show: str | None = None, why: str | None = None
):
    ctx.get_batch_or_404(batch_id)
    everything = ctx.store.list_photos(batch_id)
    photos = [p for p in everything if not is_waiting(p)]
    waiting = len(everything) - len(photos)
    by_bucket = {"attention": 0, "good": 0, "aside": 0, "all": len(photos)}
    for p in photos:
        b = ux.bucket(p.status)
        if b in by_bucket:
            by_bucket[b] += 1
    chosen = filter_of(show) or ("attention" if by_bucket["attention"] else "good")
    aside = [p for p in photos if ux.bucket(p.status) == "aside"]
    why_counts = {w: sum(1 for p in aside if w in _why_set(p)) for w in WHY_FILTERS}
    chosen_why = _why(why) if chosen == "aside" else None
    shown = [
        p
        for p in photos
        if in_filter(p, chosen) and (chosen_why is None or chosen_why in _why_set(p))
    ]
    # Near-duplicate groups stay together, in order of first appearance.
    groups: dict[tuple[str, int], list[Photo]] = {}
    for p in shown:
        key = ("g", p.near_dup_group) if p.near_dup_group is not None else ("p", p.id or 0)
        groups.setdefault(key, []).append(p)
    counts = count_by_status(photos)
    return batch_page(
        ctx,
        request,
        "batch_review.html",
        batch_id,
        "review",
        cards=[_card(ctx, p) for group in groups.values() for p in group],
        show=chosen,
        by_bucket=by_bucket,
        filters=FILTERS,
        why=chosen_why or "",
        why_counts=why_counts,
        why_labels=WHY_LABELS,
        waiting=waiting,
        run_now=bool(ctx.store.get_setting(RUN_NOW_KEY, True)),
        not_approved=waiting
        + counts.get("described", 0)
        + counts.get("needs_review", 0)
        + counts.get("blocked", 0),
        clean_ids=[p.id for p in photos if p.status == Status.DESCRIBED],
        ai=ai_working(ctx, batch_id, len(photos), chosen),
        report_href=batch_url(batch_id, "report.csv"),
        **ai_facts(ctx, batch_id),
        approved=counts.get("approved", 0) + counts.get("prepared", 0),
        clean=counts.get("described", 0),
        upload_href=batch_url(batch_id, "upload"),
    )


def _count_text(n: int, word: str = "photo") -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


def _running_job(ctx: WebCtx, batch_id: str) -> tuple[Any, bool]:
    """The batch's running job, and whether it picks up photos sent back (check and redo do)."""
    job = ai_busy(ctx, batch_id)
    return job, job is not None and job.kind in ("check", "redo")


def ai_working(
    ctx: WebCtx, batch_id: str, seen: int, show: str | None, note: str = ""
) -> dict[str, Any]:
    """What the "AI is working" indicator on Review shows (also its htmx poll).

    state: working (a job is writing text) | waiting (photos wait, no job) | back (photos came
    back since the page was drawn) | none. `seen` is the number of photos Review showed when it
    was drawn; more now means photos came back.
    """
    photos = ctx.store.list_photos(batch_id)
    imported = sum(1 for p in photos if p.status == Status.IMPORTED)
    ready = sum(1 for p in photos if p.status == Status.READY)
    job, drains = _running_job(ctx, batch_id)
    state = "none"
    total = done = 0
    if drains and job is not None:
        state = "working"
        total = imported + ready
        if job.kind == "redo":
            total = max(job.total, ready)
            done = min(job.done, total)
    elif imported + ready:
        state = "waiting"
    back = max(0, len(photos) - imported - ready - seen)
    if state == "none" and back:
        state = "back"
    return {
        "state": state,
        "total": total,
        "done": done,
        "imported": imported,
        "waiting": imported + ready,
        "back": back,
        "seen": seen,
        "show": show or "",
        "note": note,
        "job": job.id if job is not None else None,
        "kind": job.kind if job is not None else "",
        "other_task": job is not None and not drains,
    }


def _seen_of(value: Any) -> int:
    text = str(value or "")
    return int(text) if text.isdigit() and len(text) <= 9 else 0


def start_redo_job(ctx: WebCtx, batch_id: str) -> int:
    """Queue the background job that writes new text for every `ready` photo of the batch.

    Raises JobBusy when the batch already has a job.
    """
    cfg, store = ctx.cfg, ctx.store

    def run(job: JobContext) -> str:
        live = start_live(ctx, batch_id)
        try:
            s = redo.run_redo(
                cfg,
                store,
                batch_id,
                progress=job.progress_text,
                cancel=job.cancelled,
                live=live,
                explain=lambda err: ux.friendly_error(err)["what"].rstrip("."),
            )
        finally:
            ctx.state.live.pop(batch_id, None)
        if s.aborted and not s.done:
            fe = ux.friendly_error(s.aborted)
            raise PipelineError(f"{fe['what']} {fe['todo']}")
        text = f"The AI wrote new text for {_count_text(s.done)}."
        if s.failed:
            text += f" {s.failed} could not be done"
            if s.errors:
                text += f": {ux.friendly_error(s.errors[0][1])['what']}"
            text += "."
            if s.restored:
                text += f" The old text of {len(s.restored)} is back under Needs a look."
            if s.failed > len(s.restored):
                text += " The others wait: press Run the AI now to try again."
        return text

    total = len(store.list_photos(batch_id, [Status.READY]))
    return ctx.runner.submit("redo", batch_id, run, total=total)


def send_back_and_run(
    ctx: WebCtx, request: Request, batch_id: str, ids: list[int], run_now: bool | None = None
) -> tuple[str | None, str | None]:
    """Send photos back and, unless `run_now` is False, make sure the AI runs on them.

    `run_now` None means the saved choice (ticked until the owner unticks it).
    Returns (notice, error) texts.
    """
    if run_now is None:
        run_now = bool(ctx.store.get_setting(RUN_NOW_KEY, True))
    s = redo.send_back(ctx.cfg, ctx.store, batch_id, ids)
    left_out = len(s.on_shutterstock)
    parts: list[str] = []
    error = next(iter(s.refused.values()), None)
    if error and len(s.refused) > 1:
        error = f"{_count_text(len(s.refused))} could not be sent back. First reason: {error}"
    if s.sent:
        parts.append(f"{_count_text(len(s.sent))} sent back.")
        block = ctx.model_block(request)
        job, drains = _running_job(ctx, batch_id)
        if not run_now:
            if job is not None and drains:
                parts.append("The AI is already running for this batch and will include them.")
            else:
                waiting = sum(
                    1 for p in ctx.store.list_photos(batch_id) if p.status in WAITING_FOR_AI
                )
                parts.append(
                    "They wait for the AI: press Run the AI now here, or "
                    f"Check all {_count_text(waiting)} on the Check page."
                )
        elif block:
            parts.append(
                f"The AI cannot run now: {block} The photos wait; fix that in Settings, then "
                "press Run the AI now."
            )
        elif job is not None and drains:
            parts.append(
                "The AI is already working on this batch and should pick them up. If they are "
                "still waiting when it stops, press Run the AI now. "
                "You can keep reviewing the others."
            )
        else:
            try:
                start_redo_job(ctx, batch_id)
                parts.append(
                    "The AI is writing new text now; each one comes back here when it is done. "
                    "You can keep reviewing the others."
                )
            except JobBusy:
                parts.append(
                    "Another task is running for this batch. The photos wait: press Run the AI "
                    "now when it has finished."
                )
    elif left_out and not error:
        parts.append("Nothing to send back.")
    if left_out:
        parts.append(
            f"{_count_text(left_out)} {'is' if left_out == 1 else 'are'} already on Shutterstock "
            "and left out."
        )
    return " ".join(parts) or None, error


@router.get("/batch/{batch_id}/ai-status", response_class=HTMLResponse)
def ai_status(
    request: Request, batch_id: str, ctx: Ctx, seen: str = "0", show: str = "", place: str = ""
):
    batch = ctx.get_batch_or_404(batch_id)
    if place == "check":  # the Check page polls here to keep its button in step with the job
        return ctx.render(
            request,
            "_check_action.html",
            batch=batch,
            check=check_action(ctx, request, batch_id),
            review_href=batch_url(batch_id, "review"),
        )
    ai = ai_working(ctx, batch_id, _seen_of(seen), filter_of(show))
    return ctx.render(request, "_ai_working.html", batch=batch, ai=ai)


@router.post("/batch/{batch_id}/redo", response_class=HTMLResponse)
def redo_start(request: Request, batch_id: str, form: PostForm, ctx: Ctx):
    """Run the AI now: write new text for the photos that wait for it."""
    batch = ctx.get_batch_or_404(batch_id)
    seen, show = _seen_of(form.get("seen")), filter_of(form.get("show"))
    note = ""
    if not ctx.store.list_photos(batch_id, [Status.READY]):
        note = "No photos are waiting for the AI."
    else:
        note = ctx.model_block(request) or ""
        if not note and not _running_job(ctx, batch_id)[1]:
            try:
                start_redo_job(ctx, batch_id)
            except JobBusy:
                note = "The AI is busy with another task for this batch. Try again in a moment."
    ai = ai_working(ctx, batch_id, seen, show, note)
    return ctx.render(request, "_ai_working.html", batch=batch, ai=ai)


@router.get("/batch/{batch_id}/bulk-edit", response_class=HTMLResponse)
def bulk_edit_form(
    request: Request,
    batch_id: str,
    ctx: Ctx,
    ids: Annotated[list[str] | None, Query()] = None,
    show: str = "",
):
    ctx.get_batch_or_404(batch_id)
    picked = [int(i) for i in (ids or []) if i.isdigit()]
    return ctx.render(
        request, "_bulk_edit.html", batch_id=batch_id, ids=picked, show=filter_of(show) or ""
    )


def _edit_selected(ctx: WebCtx, form: Any, ids: list[int], path: str):
    add = split_lines(str(form.get("add_keywords") or ""))
    drop = split_lines(str(form.get("remove_keywords") or ""))
    categories = (
        [str(c) for c in form.getlist("categories")]
        if form.get("categories_mode") == "set"
        else None
    )
    editorial = {"editorial": True, "commercial": False}.get(str(form.get("kind") or ""))
    if not (add or drop or categories is not None or editorial is not None):
        return ctx.back(path, error="Choose at least one change first.")
    try:
        s = workflow_review.bulk_edit(
            ctx.cfg,
            ctx.store,
            ids,
            add_keywords=add,
            remove_keywords=drop,
            categories=categories,
            editorial=editorial,
        )
    except PipelineError as exc:
        return ctx.back(path, error=str(exc))
    refused = "; ".join(f"#{i}: {why}" for i, why in list(s.refused.items())[:3])
    return ctx.back(path, notice=f"Changed {len(s.changed)} photo(s).", error=refused or None)


@router.post("/batch/{batch_id}/bulk")
def batch_bulk(request: Request, batch_id: str, form: PostForm, ctx: Ctx):
    cfg = ctx.cfg
    ctx.get_batch_or_404(batch_id)
    action = str(form.get("action") or "")
    ids = ctx.ids_from(form)
    show = filter_of(form.get("show"))
    path = batch_url(batch_id, "review") + (f"?show={show}" if show else "")
    try:
        if action == "approve_all":
            # Only the photos that were on screen when the page was drawn, never ones that came
            # back from the AI since.
            # A photo must still be clean now: one sent back or flagged since is left alone.
            mine = {p.id for p in ctx.store.list_photos(batch_id, [Status.DESCRIBED])}
            clean = [int(v) for v in form.getlist("clean_ids") if str(v).isdigit()]
            clean = [i for i in clean if i in mine]
            if not clean:
                return ctx.back(path, error="No photos to approve here. Reload the page first.")
            s = workflow_review.approve(cfg, ctx.store, photo_ids=clean, include_needs_review=False)
        elif action in ("approve", "ready", "edit"):
            if not ids:
                return ctx.back(path, error="Select at least one photo first.")
            if action == "edit":
                return _edit_selected(ctx, form, ids, path)
            if action == "approve":
                s = workflow_review.approve(
                    cfg, ctx.store, photo_ids=ids, include_needs_review=True
                )
            else:
                run_now = None
                if form.get("run_now_shown"):
                    run_now = form.get("run_now") == "on"
                    ctx.store.set_setting(RUN_NOW_KEY, run_now)
                notice, error = send_back_and_run(ctx, request, batch_id, ids, run_now)
                return ctx.back(path, notice=notice, error=error)
        else:
            raise HTTPException(status_code=400, detail="Unknown bulk action.")
    except PipelineError as exc:
        return ctx.back(path, error=str(exc))
    refused_text = "; ".join(f"#{i}: {why}" for i, why in list(s.refused.items())[:3])
    return ctx.back(
        path, notice=f"Approved {len(s.approved)} photo(s).", error=refused_text or None
    )
