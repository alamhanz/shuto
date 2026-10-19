"""The Review page and bulk actions on selected photos."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse

from shuto.ai import filters as filters_mod
from shuto.core.errors import PipelineError
from shuto.core.models import Photo, Status
from shuto.web import ux
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
from shuto.workflow import review as workflow_review

router = APIRouter()

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
    waiting = sum(1 for p in everything if is_waiting(p))
    photos = [p for p in everything if not is_waiting(p)]
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
        check_href=batch_url(batch_id, "check"),
        report_href=batch_url(batch_id, "report.csv"),
        **ai_facts(ctx, batch_id),
        approved=counts.get("approved", 0) + counts.get("prepared", 0),
        clean=counts.get("described", 0),
        upload_href=batch_url(batch_id, "upload"),
    )


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
def batch_bulk(batch_id: str, form: PostForm, ctx: Ctx):
    cfg = ctx.cfg
    ctx.get_batch_or_404(batch_id)
    action = str(form.get("action") or "")
    ids = ctx.ids_from(form)
    show = filter_of(form.get("show"))
    path = batch_url(batch_id, "review") + (f"?show={show}" if show else "")
    try:
        if action == "approve_all":
            s = workflow_review.approve(cfg, ctx.store, batch_id=batch_id)
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
                changed = 0
                refused = []
                for pid in ids:
                    try:
                        workflow_review.override_status(ctx.store, pid, Status.READY, cfg=cfg)
                        changed += 1
                    except PipelineError as exc:
                        refused.append(str(exc))
                return ctx.back(
                    path,
                    notice=(
                        f"{changed} photo(s) sent back to the AI. They are hidden here until "
                        "the AI has run: press Check to run it."
                    ),
                    error=refused[0] if refused else None,
                )
        else:
            raise HTTPException(status_code=400, detail="Unknown bulk action.")
    except PipelineError as exc:
        return ctx.back(path, error=str(exc))
    refused_text = "; ".join(f"#{i}: {why}" for i, why in list(s.refused.items())[:3])
    return ctx.back(
        path, notice=f"Approved {len(s.approved)} photo(s).", error=refused_text or None
    )
