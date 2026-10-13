"""Batch-side routes: the Check -> Review -> Upload -> Submit pages, bulk actions and photos."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response

from .. import filters as filters_mod
from .. import ftps, imaging, openfolder, pipeline
from ..jobs import JobContext
from ..local_models import ModelChoiceRequired
from ..models import ALLOWED, Photo, Status
from ..pipeline import PipelineError
from ..report import report_filename
from . import ux
from .common import PostForm, WebCtx, short, split_lines

BATCH_JOBS = ("cull", "describe", "prepare", "upload", "export")  # legacy / power-user routes
OPEN_FOLDER_PAGES = ("review", "submit", "upload")
PREVIEW_SIDE = 2048  # page preview size; originals can be tens of MB
FILTERS = ("attention", "good", "aside", "all")
WHY_FILTERS = ("quality", "copies", "theme", "rules")  # why a photo was set aside
WHY_LABELS = {
    "quality": "Quality",
    "copies": "Copies",
    "theme": "Your theme",
    "rules": "Your rules",
}


def _url(batch_id: str, page: str = "") -> str:
    return f"/batch/{quote(batch_id, safe='')}" + (f"/{page}" if page else "")


def _filter(value: Any) -> str | None:
    """A review filter from user input, or None when it is not one of the known four."""
    text = str(value or "")
    return text if text in FILTERS else None


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


def _counts(photos: list[Photo]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for p in photos:
        counts[p.status.value] = counts.get(p.status.value, 0) + 1
    return counts


def _in_filter(photo: Photo, show: str | None) -> bool:
    return show in (None, "all") or ux.bucket(photo.status) == show


def _photo_path(photo_id: int, show: str | None) -> str:
    return f"/photo/{photo_id}" + (f"?back={show}" if show else "")


def _mb(n: int) -> str:
    mb = n / (1024 * 1024)
    return f"{mb:.0f} MB" if mb >= 10 else f"{mb:.1f} MB"


def register_batch_routes(app: FastAPI, ctx: WebCtx) -> None:
    def page(request: Request, name: str, batch_id: str, current: str, **extra: Any):
        """Render a batch-flow page with its stepper."""
        batch = ctx.get_batch_or_404(batch_id)
        photos = ctx.store.list_photos(batch_id)
        counts = _counts(photos)
        return ctx.render(
            request,
            name,
            batch=batch,
            counts=counts,
            total=len(photos),
            photos=photos,
            steps=ux.steps(batch_id, counts),
            step_current=current,
            active_job=ctx.runner.active_job(batch_id),
            batch_folder=str(pipeline.batch_dir(ctx.cfg, batch_id)),
            **extra,
        )

    def ai_facts(batch_id: str) -> dict[str, Any]:
        """Total and average AI time for the batch, as plain text (None until the AI has run)."""
        t = pipeline.ai_time_summary(ctx.store, batch_id)
        if not t["photos"]:
            return {"ai_time": None}
        return {
            "ai_time": {
                "total": ux.ai_time_text(float(t["total"])),
                "average": ux.ai_time_text(float(t["average"])),
                "photos": int(t["photos"]),
            }
        }

    @app.get("/batch/{batch_id}")
    def batch_page(batch_id: str):
        ctx.get_batch_or_404(batch_id)
        stage = ux.stage(_counts(ctx.store.list_photos(batch_id)))
        target = {"empty": "review", "done": "submit"}.get(stage, stage)
        return RedirectResponse(_url(batch_id, target), status_code=303)

    # ------------------------------------------------------------------ check

    def check_facts(request: Request, batch_id: str, counts: dict[str, int]) -> dict[str, Any]:
        """Estimate, model name and plain-words problems for the Check page."""
        facts: dict[str, Any] = {
            "estimate": None,
            "model": None,
            "problems": [],
            "checks": pipeline.active_checks(ctx.cfg),
        }
        if not (counts.get("imported", 0) + counts.get("ready", 0)):
            return facts
        try:
            sel = pipeline.resolve_selection(ctx.cfg, ctx.store)
            facts["model"] = f"{sel.provider} / {sel.model}"
            facts["estimate"] = pipeline.estimate_check(ctx.cfg, ctx.store, batch_id).text
        except ModelChoiceRequired:
            pass
        except PipelineError as exc:
            facts["problems"] = [ux.friendly_error(exc)["what"]]
        if not facts["problems"]:
            block = ctx.model_block(request)
            facts["problems"] = [block] if block else []
        return facts

    @app.get("/batch/{batch_id}/check", response_class=HTMLResponse)
    def check_page(request: Request, batch_id: str):
        ctx.get_batch_or_404(batch_id)
        counts = _counts(ctx.store.list_photos(batch_id))
        facts = check_facts(request, batch_id, counts)
        stage = ux.stage(counts)
        return page(
            request,
            "batch_check.html",
            batch_id,
            "check",
            to_check=counts.get("imported", 0) + counts.get("ready", 0),
            next_step=ux.next_action(batch_id, counts) if stage != "check" else None,
            review_href=_url(batch_id, "review"),
            **facts,
        )

    @app.post("/batch/{batch_id}/check", response_class=HTMLResponse)
    def check_start(request: Request, batch_id: str, form: PostForm):
        cfg, store = ctx.cfg, ctx.store
        ctx.get_batch_or_404(batch_id)
        n = len(store.list_photos(batch_id, [Status.IMPORTED, Status.READY]))
        if not n:
            return ctx.message_fragment(request, "There are no new photos to check.")
        block = ctx.model_block(request)
        if block:
            return ctx.message_fragment(request, block)

        def run(job: JobContext) -> str:
            s = pipeline.check_batch(
                cfg, store, batch_id, progress=job.progress_text, cancel=job.cancelled
            )
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

        return ctx.start_job(request, "check", batch_id, run, total=pipeline.CHECK_SCALE)

    # ------------------------------------------------------------------ review

    def _card(p: Photo) -> dict[str, Any]:
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
            "editorial": bool(meta.get("editorial")),
            "gps": p.has_gps,
            "gps_sent": p.has_gps and ctx.cfg.prepare.include_gps,
            "default_place": bool((meta.get("place") or {}).get("default")),
        }

    @app.get("/batch/{batch_id}/review", response_class=HTMLResponse)
    def review_page(
        request: Request, batch_id: str, show: str | None = None, why: str | None = None
    ):
        ctx.get_batch_or_404(batch_id)
        photos = ctx.store.list_photos(batch_id)
        by_bucket = {"attention": 0, "good": 0, "aside": 0, "all": len(photos)}
        for p in photos:
            b = ux.bucket(p.status)
            if b in by_bucket:
                by_bucket[b] += 1
        chosen = _filter(show) or ("attention" if by_bucket["attention"] else "good")
        aside = [p for p in photos if ux.bucket(p.status) == "aside"]
        why_counts = {w: sum(1 for p in aside if w in _why_set(p)) for w in WHY_FILTERS}
        chosen_why = _why(why) if chosen == "aside" else None
        shown = [
            p
            for p in photos
            if _in_filter(p, chosen) and (chosen_why is None or chosen_why in _why_set(p))
        ]
        # Near-duplicate groups stay together, in order of first appearance.
        groups: dict[tuple[str, int], list[Photo]] = {}
        for p in shown:
            key = ("g", p.near_dup_group) if p.near_dup_group is not None else ("p", p.id or 0)
            groups.setdefault(key, []).append(p)
        counts = _counts(photos)
        return page(
            request,
            "batch_review.html",
            batch_id,
            "review",
            cards=[_card(p) for group in groups.values() for p in group],
            show=chosen,
            by_bucket=by_bucket,
            filters=FILTERS,
            why=chosen_why or "",
            why_counts=why_counts,
            why_labels=WHY_LABELS,
            report_href=_url(batch_id, "report.csv"),
            **ai_facts(batch_id),
            approved=counts.get("approved", 0) + counts.get("prepared", 0),
            clean=counts.get("described", 0),
            upload_href=_url(batch_id, "upload"),
        )

    @app.get("/batch/{batch_id}/bulk-edit", response_class=HTMLResponse)
    def bulk_edit_form(
        request: Request,
        batch_id: str,
        ids: Annotated[list[str] | None, Query()] = None,
        show: str = "",
    ):
        ctx.get_batch_or_404(batch_id)
        picked = [int(i) for i in (ids or []) if i.isdigit()]
        return ctx.render(
            request, "_bulk_edit.html", batch_id=batch_id, ids=picked, show=_filter(show) or ""
        )

    def _edit_selected(form: Any, ids: list[int], path: str):
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
            s = pipeline.bulk_edit(
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

    @app.post("/batch/{batch_id}/bulk")
    def batch_bulk(batch_id: str, form: PostForm):
        cfg = ctx.cfg
        ctx.get_batch_or_404(batch_id)
        action = str(form.get("action") or "")
        ids = ctx.ids_from(form)
        show = _filter(form.get("show"))
        path = _url(batch_id, "review") + (f"?show={show}" if show else "")
        try:
            if action == "approve_all":
                s = pipeline.approve(cfg, ctx.store, batch_id=batch_id)
            elif action in ("approve", "ready", "edit"):
                if not ids:
                    return ctx.back(path, error="Select at least one photo first.")
                if action == "edit":
                    return _edit_selected(form, ids, path)
                if action == "approve":
                    s = pipeline.approve(cfg, ctx.store, photo_ids=ids, include_needs_review=True)
                else:
                    changed = 0
                    refused = []
                    for pid in ids:
                        try:
                            pipeline.override_status(ctx.store, pid, Status.READY, cfg=cfg)
                            changed += 1
                        except PipelineError as exc:
                            refused.append(str(exc))
                    return ctx.back(
                        path,
                        notice=f"{changed} photo(s) sent back to the AI. Press Check to run it.",
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

    # ------------------------------------------------------------------ upload

    def _export_names(photos: list[Photo]) -> list[str]:
        return [Path(f).name for f in sorted({p.export_file for p in photos if p.export_file})]

    def _resend_choices(photos: list[Photo]) -> dict[str, list[Photo]]:
        """Photos the user may tell shuto were deleted on Shutterstock (accepted ones are not)."""
        return {
            "sent": [p for p in photos if p.status in (Status.UPLOADED, Status.EXPORTED)],
            "submitted": [p for p in photos if p.status == Status.SUBMITTED],
        }

    @app.get("/batch/{batch_id}/upload", response_class=HTMLResponse)
    def upload_page(request: Request, batch_id: str):
        ctx.get_batch_or_404(batch_id)
        summary = pipeline.batch_summary(ctx.store, batch_id)
        counts = summary["counts"]
        photos = ctx.store.list_photos(batch_id)
        login = pipeline.ftps_login_status(ctx.cfg)
        return page(
            request,
            "batch_upload.html",
            batch_id,
            "upload",
            n=min(counts.get("approved", 0) + counts.get("prepared", 0), pipeline.MAX_BATCH_PHOTOS),
            size=_mb(summary["upload_bytes"]),
            login_ok=bool(login["user_set"] and login["password_set"]),
            exports=_export_names(photos),
            resend=_resend_choices(photos),
            resend_page="upload",
            sent=counts.get("uploaded", 0) + counts.get("exported", 0),
            back_path=_url(batch_id, "upload"),
            submit_href=_url(batch_id, "submit"),
        )

    @app.post("/batch/{batch_id}/deliver", response_class=HTMLResponse)
    def deliver(request: Request, batch_id: str, form: PostForm):
        cfg, store = ctx.cfg, ctx.store
        ctx.get_batch_or_404(batch_id)
        if form.get("confirm") != "yes":
            return ctx.message_fragment(
                request, "Tick the box to confirm you want to upload to Shutterstock."
            )
        n = len(store.list_photos(batch_id, [Status.APPROVED, Status.PREPARED]))
        if not n:
            return ctx.message_fragment(request, "No approved photos to upload yet. Review first.")

        def run(job: JobContext) -> str:
            s = pipeline.deliver_batch(
                cfg,
                store,
                batch_id,
                confirm=True,
                progress=job.progress_text,
                cancel=job.cancelled,
            )
            sent = len(s.upload.uploaded) if s.upload else 0
            if s.prepare.problems and not sent:
                first = next(iter(s.prepare.problems.values()))[0]
                raise PipelineError(
                    f"{len(s.prepare.problems)} photo(s) are not ready to send: {first} "
                    "Fix them in Review, then press Try again."
                )
            if s.upload is None:
                return "Stopped before uploading."
            if s.upload.stopped:
                return s.upload.stopped_text()
            if job.cancelled() and s.export is not None:
                job.mark_completed()  # the cancel came too late: everything was sent
            lost = [e for e in s.upload.failed.values() if e.startswith(ftps.LOST_PREFIX)]
            if lost and not sent:
                raise PipelineError(lost[0])
            if s.upload.failed and not sent:
                err = next(iter(s.upload.failed.values()))
                fe = ux.friendly_error(err, context="upload")
                raise PipelineError(
                    f"{len(s.upload.failed)} photo(s) did not upload. {fe['what']} {fe['todo']}"
                )
            fixes = len(s.prepare.problems) + len(s.upload.failed) - len(lost)
            if fixes or lost:
                # Partial success: keep what was sent and still write its CSV.
                if s.export is None:
                    pipeline.export_batch(cfg, store, batch_id)
                text = f"Uploaded {sent}."
                if fixes:
                    text = f"Uploaded {sent}; {fixes} need a fix in Review."
                if lost:
                    text += " " + lost[0]
                return f"{text} Wrote the Shutterstock info file."
            if s.upload.over_limit:
                return (
                    f"Uploaded {sent} photo(s). {s.upload.over_limit} more are ready: "
                    "press Upload again to send them."
                )
            if s.export is None:
                return f"Uploaded {len(s.upload.uploaded)} photo(s). Stopped before the info file."
            return (
                f"Uploaded {len(s.upload.uploaded)} photo(s) and wrote the Shutterstock info file."
            )

        return ctx.start_job(request, "deliver", batch_id, run, total=2 * n + 1)

    # ------------------------------------------------------------------ submit

    @app.get("/batch/{batch_id}/submit", response_class=HTMLResponse)
    def submit_page(request: Request, batch_id: str):
        ctx.get_batch_or_404(batch_id)
        photos = ctx.store.list_photos(batch_id)
        counts = _counts(photos)
        return page(
            request,
            "batch_submit.html",
            batch_id,
            "submit",
            exports=_export_names(photos),
            resend=_resend_choices(photos),
            resend_page="submit",
            waiting=counts.get("exported", 0),
            finished=sum(counts.get(k, 0) for k in ("submitted", "accepted", "rejected")),
            report_href=_url(batch_id, "report.csv"),
            **ai_facts(batch_id),
        )

    @app.post("/batch/{batch_id}/submitted")
    def batch_submitted(batch_id: str, form: PostForm):
        ctx.get_batch_or_404(batch_id)
        ids = pipeline.mark_submitted(ctx.store, batch_id, cfg=ctx.cfg)
        if not ids:
            return ctx.back(
                _url(batch_id, "submit"), error="Nothing is waiting to be marked as submitted."
            )
        return ctx.back(
            "/", notice=f"Marked {len(ids)} photo(s) as submitted. Open a photo to note the result."
        )

    @app.get("/batch/{batch_id}/report.csv")
    def report_download(batch_id: str):
        ctx.get_batch_or_404(batch_id)  # only an existing batch id is accepted; no file path
        data = pipeline.report_bytes(ctx.cfg, ctx.store, batch_id)
        name = "shuto-report-" + report_filename(batch_id)
        return Response(
            data,
            media_type="text/csv; charset=utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="{name}"',
                "X-Content-Type-Options": "nosniff",
                "Cache-Control": "no-store",
            },
        )

    @app.post("/batch/{batch_id}/resend")
    def resend_photos(batch_id: str, form: PostForm):
        """ "These are no longer on Shutterstock": put the ticked photos back in the upload queue.

        shuto cannot see the Shutterstock account, so this is the user's word. The confirm box
        works without JavaScript."""
        ctx.get_batch_or_404(batch_id)
        page_name = str(form.get("page"))
        here = _url(batch_id, page_name if page_name in ("upload", "submit") else "upload")
        if form.get("confirm") != "yes":
            return ctx.back(
                here, error="Tick the box to confirm these photos are no longer on Shutterstock."
            )
        ids = ctx.ids_from(form)
        if not ids:
            return ctx.back(here, error="Tick the photos that are no longer on Shutterstock.")
        s = pipeline.reupload_photos(ctx.cfg, ctx.store, ids, batch_id=batch_id)
        if not s.moved:
            return ctx.back(here, error=next(iter(s.refused.values())))
        n = len(s.moved)
        return ctx.back(
            _url(batch_id, "upload"),
            notice=f"{n} photo{'' if n == 1 else 's'} will be uploaded again. "
            "Press Upload when you are ready.",
            error=next(iter(s.refused.values()), None),
        )

    @app.post("/batch/{batch_id}/open-folder")
    def open_batch_folder(batch_id: str, form: PostForm):
        ctx.get_batch_or_404(batch_id)
        page_name = str(form.get("page"))
        back = _url(batch_id, page_name if page_name in OPEN_FOLDER_PAGES else "")
        folder = pipeline.batch_dir(ctx.cfg, batch_id)  # always the batch's own folder
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError:
            return ctx.back(back, error=openfolder.NO_DESKTOP)
        problem = openfolder.open_folder(folder)
        if problem:
            return ctx.back(back, error=problem)
        return ctx.back(back, notice="Opened the folder on this computer.")

    # ------------------------------------------------------------------ legacy job routes

    @app.post("/batch/{batch_id}/{action}", response_class=HTMLResponse)
    def batch_job(request: Request, batch_id: str, action: str, form: PostForm):
        cfg = ctx.cfg
        if action not in BATCH_JOBS:
            raise HTTPException(status_code=404, detail="Unknown action.")
        ctx.get_batch_or_404(batch_id)
        only = ctx.ids_from(form) or None
        store = ctx.store
        if action == "cull":

            def run(job: JobContext) -> str:
                s = pipeline.cull_batch(
                    cfg, store, batch_id, progress=job.progress, cancel=job.cancelled
                )
                return f"Cull done: {s.ready} ready, {s.culled} culled (of {s.total} new)."

        elif action == "describe":
            block = ctx.model_block(request)
            if block:
                return ctx.message_fragment(request, block)

            def run(job: JobContext) -> str:
                s = pipeline.describe_batch(
                    cfg, store, batch_id, only_ids=only, progress=job.progress, cancel=job.cancelled
                )
                text = (
                    f"Described {s.described + s.needs_review + s.blocked}: {s.described} clean, "
                    f"{s.needs_review} need review, {s.blocked} blocked, {s.failed} failed."
                )
                if s.aborted:
                    raise PipelineError(f"Stopped early: {s.aborted}. {text}")
                if s.errors:
                    text += f" First error: {s.errors[0][1]}"
                return text

        elif action == "prepare":

            def run(job: JobContext) -> str:
                s = pipeline.prepare_batch(
                    cfg, store, batch_id, only_ids=only, progress=job.progress, cancel=job.cancelled
                )
                text = f"Prepared {len(s.prepared)} file(s)."
                if s.problems:
                    first = next(iter(s.problems.values()))[0]
                    text += f" {len(s.problems)} photo(s) have problems, e.g. {first}"
                return text

        elif action == "upload":
            if form.get("confirm") != "yes":
                return ctx.message_fragment(
                    request, "Tick the box to confirm you want to upload to Shutterstock."
                )

            def run(job: JobContext) -> str:
                s = pipeline.upload_batch(
                    cfg,
                    store,
                    batch_id,
                    confirm=True,
                    only_ids=only,
                    progress=job.progress_text,
                    cancel=job.cancelled,
                )
                if s.stopped:
                    return s.stopped_text()
                if job.cancelled():
                    job.mark_completed()  # the cancel came too late: everything was sent
                text = f"Uploaded {len(s.uploaded)} file(s)."
                if s.failed:
                    name, err = next(iter(s.failed.items()))
                    text += f" {len(s.failed)} failed, e.g. {name}: {err}"
                if s.skipped_blocked:
                    text += f" Skipped {len(s.skipped_blocked)} blocked."
                return text

        else:

            def run(job: JobContext) -> str:
                s = pipeline.export_batch(cfg, store, batch_id)
                if not s.files:
                    return "Nothing to export: upload prepared photos first."
                text = f"Wrote the Shutterstock CSV for {len(s.rows)} photo(s)."
                if s.skipped:
                    text += (
                        f" {len(s.skipped)} left out because the text is not in English;"
                        " edit them in Review."
                    )
                return text

        return ctx.start_job(request, action, batch_id, run)

    @app.get("/batch/{batch_id}/exports/{filename}")
    def export_download(batch_id: str, filename: str):
        ctx.get_batch_or_404(batch_id)
        for p in ctx.store.list_photos(batch_id):
            if p.export_file and Path(p.export_file).name == filename:
                path = Path(p.export_file)
                if path.is_file():
                    return FileResponse(path, media_type="text/csv", filename=filename)
        raise HTTPException(status_code=404, detail="Unknown export file.")

    # ------------------------------------------------------------------ photos

    @app.get("/thumb/{photo_id}")
    def thumb(photo_id: int):
        cfg = ctx.cfg
        photo = ctx.get_photo_or_404(photo_id)
        path = cfg.workspace_path("thumbs", f"{photo.sha256}.jpg")
        if not path.is_file():
            raise HTTPException(status_code=404, detail="No thumbnail.")
        return FileResponse(path, media_type="image/jpeg")

    @app.get("/image/{photo_id}")
    def image(photo_id: int):
        cfg = ctx.cfg
        photo = ctx.get_photo_or_404(photo_id)
        src = Path(photo.src_path)
        dest = cfg.workspace_path("previews", f"{photo.sha256}.jpg")
        if not dest.is_file() and src.is_file():
            try:  # cached ~2048 px copy; the original is only read, never served or touched
                imaging.make_thumbnail(src, dest, max_side=PREVIEW_SIDE)
            except Exception:  # unreadable or unsupported file: fall back to the thumbnail
                pass
        if dest.is_file():
            return FileResponse(dest, media_type="image/jpeg")
        return thumb(photo_id)

    @app.get("/photo/{photo_id}", response_class=HTMLResponse)
    def photo_page(request: Request, photo_id: int, back: str | None = None):
        photo = ctx.get_photo_or_404(photo_id)
        show = _filter(back)
        meta = photo.metadata or {}
        siblings: list[int] = []
        if photo.batch_id:
            siblings = [
                p.id
                for p in ctx.store.list_photos(photo.batch_id)
                if p.id == photo_id or _in_filter(p, show)
            ]
        idx = siblings.index(photo_id) if photo_id in siblings else -1
        prev_id = siblings[idx - 1] if idx > 0 else None
        next_id = siblings[idx + 1] if 0 <= idx < len(siblings) - 1 else None
        back_href = (
            _url(photo.batch_id, "review") + (f"?show={show}" if show else "")
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
            back_href=back_href,
            prev_href=_photo_path(prev_id, show) if prev_id else None,
            next_href=_photo_path(next_id, show) if next_id else None,
            next_id=next_id,
            editable=photo.status in pipeline.PRE_UPLOAD_EDITABLE,
            can_approve=photo.status in (Status.DESCRIBED, Status.NEEDS_REVIEW, Status.SET_ASIDE),
            checks=_check_rows(photo.filters),
            checks_overridden=bool((photo.filters or {}).get("overridden")),
            checked_with_older_rules=_older_rules(photo),
            can_ready=photo.status != Status.READY
            and Status.READY in ALLOWED.get(photo.status, ()),
            can_result=photo.status in (Status.EXPORTED, Status.SUBMITTED),
            releases_text="\n".join(photo.release_ids),
            include_gps=ctx.cfg.prepare.include_gps,
        )

    def _older_rules(photo: Photo) -> bool:
        """True when the photo was checked with a different theme or rules than today's."""
        old = (photo.filters or {}).get("fingerprint")
        if not old:
            return False
        try:
            return old != pipeline.filter_spec(ctx.cfg).fingerprint()
        except PipelineError:
            return False

    def _save_edits(photo_id: int, form: Any) -> None:
        """Save the edit form (guardrails re-run); raises PipelineError with a plain reason."""
        categories = [str(c) for c in form.getlist("categories")]
        if len(categories) > 2:
            raise PipelineError("Choose at most 2 categories.")
        pipeline.update_metadata(
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

    @app.post("/photo/{photo_id}")
    def photo_save(photo_id: int, form: PostForm):
        ctx.get_photo_or_404(photo_id)
        path = _photo_path(photo_id, _filter(form.get("back")))
        try:
            _save_edits(photo_id, form)
        except PipelineError as exc:
            return ctx.back(path, error=str(exc))
        return ctx.back(path, notice="Saved. The Shutterstock rules were checked again.")

    @app.post("/photo/{photo_id}/approve")
    def photo_approve(photo_id: int, form: PostForm):
        cfg = ctx.cfg
        photo = ctx.get_photo_or_404(photo_id)
        show = _filter(form.get("back"))
        path = _photo_path(photo_id, show)
        try:
            if "keywords" in form:  # the edit form was submitted: save it first, then approve
                _save_edits(photo_id, form)
                after = ctx.get_photo_or_404(photo_id)
                if photo.status == Status.DESCRIBED and after.status != Status.DESCRIBED:
                    why = " ".join((after.guardrail or {}).get("messages") or [])
                    raise PipelineError(f"Saved, but not approved. Fix this first: {why}".strip())
            s = pipeline.approve(cfg, ctx.store, photo_ids=[photo_id], include_needs_review=True)
        except PipelineError as exc:
            return ctx.back(path, error=str(exc))
        if s.refused:
            return ctx.back(path, error=next(iter(s.refused.values())))
        # `next` must be a plain photo id from the same batch; anything else is ignored.
        raw = str(form.get("next") or "")
        target = ctx.store.get_photo(int(raw)) if raw.isdigit() and len(raw) < 12 else None
        if target is not None and target.batch_id == photo.batch_id:
            return ctx.back(_photo_path(int(raw), show), notice="Approved.")
        if photo.batch_id:
            review = _url(photo.batch_id, "review") + (f"?show={show}" if show else "")
            return ctx.back(review, notice="Approved. That was the last photo here.")
        return ctx.back(path, notice="Approved.")

    @app.post("/photo/{photo_id}/ready")
    def photo_ready(photo_id: int, form: PostForm):
        ctx.get_photo_or_404(photo_id)
        path = _photo_path(photo_id, _filter(form.get("back")))
        try:
            pipeline.override_status(ctx.store, photo_id, Status.READY, cfg=ctx.cfg)
        except PipelineError as exc:
            return ctx.back(path, error=str(exc))
        return ctx.back(path, notice="Sent back to the AI. Press Check on the batch to run it.")

    @app.post("/photo/{photo_id}/regenerate", response_class=HTMLResponse)
    def photo_regenerate(request: Request, photo_id: int, form: PostForm):
        cfg = ctx.cfg
        photo = ctx.get_photo_or_404(photo_id)
        if not photo.batch_id:
            return ctx.message_fragment(request, "This photo is not part of a batch.")
        batch_id = photo.batch_id
        block = ctx.model_block(request)
        if block:
            return ctx.message_fragment(request, block)

        def run(job: JobContext) -> str:
            s = pipeline.describe_batch(
                cfg, ctx.store, batch_id, only_ids=[photo_id], cancel=job.cancelled
            )
            if s.aborted or s.errors:
                fe = ux.friendly_error(s.aborted or s.errors[0][1])
                raise PipelineError(f"{fe['what']} {fe['todo']}")
            return "Described again. Reload the page to see the new text."

        return ctx.start_job(request, "describe", batch_id, run, total=1)

    @app.post("/photo/{photo_id}/result")
    def photo_result(photo_id: int, form: PostForm):
        ctx.get_photo_or_404(photo_id)
        path = _photo_path(photo_id, _filter(form.get("back")))
        accepted = form.get("outcome") == "accept"
        try:
            pipeline.record_result(
                ctx.store,
                photo_id,
                accepted,
                reason_code=str(form.get("reason") or "") or None,
                note=str(form.get("note") or "").strip() or None,
                cfg=ctx.cfg,
            )
        except PipelineError as exc:
            return ctx.back(path, error=str(exc))
        return ctx.back(path, notice="Accepted." if accepted else "Rejection recorded.")
