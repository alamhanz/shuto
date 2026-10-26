"""Upload and Submit pages: deliver to Shutterstock, mark submitted, resend, the report."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response

from shuto.core import paths
from shuto.core.errors import PipelineError
from shuto.core.jobs import JobContext
from shuto.core.models import Photo, Status
from shuto.deliver import ftps
from shuto.deliver.report import report_filename
from shuto.system import openfolder
from shuto.web import ux
from shuto.web.routes.batch_support import ai_facts, batch_page, batch_url, count_by_status
from shuto.web.routes.photo import OUTCOME_VALUES, RESULT_REASONS, RESULT_STATUSES
from shuto.web.state import Ctx, PostForm
from shuto.workflow import delivering, exporting, ftps_login, importing, recheck, reports, resend

router = APIRouter()

OPEN_FOLDER_PAGES = ("review", "submit", "upload")


def _mb(n: int) -> str:
    mb = n / (1024 * 1024)
    return f"{mb:.0f} MB" if mb >= 10 else f"{mb:.1f} MB"


def _export_names(photos: list[Photo]) -> list[str]:
    return [Path(f).name for f in sorted({p.export_file for p in photos if p.export_file})]


def _new_text(photos: list[Photo]) -> list[Photo]:
    """Sent photos whose new text (Check again with AI) waits for the owner's choice."""
    return [p for p in photos if p.status in recheck.RECHECKABLE and recheck.has_new_text(p)]


def _resend_choices(photos: list[Photo]) -> dict[str, list[Photo]]:
    """Photos the user may tell shuto were deleted on Shutterstock (accepted ones are not)."""
    return {
        "sent": [p for p in photos if p.status in (Status.UPLOADED, Status.EXPORTED)],
        "submitted": [p for p in photos if p.status == Status.SUBMITTED],
    }


@router.get("/batch/{batch_id}/upload", response_class=HTMLResponse)
def upload_page(request: Request, batch_id: str, ctx: Ctx):
    ctx.get_batch_or_404(batch_id)
    summary = reports.batch_summary(ctx.store, batch_id)
    counts = summary["counts"]
    photos = ctx.store.list_photos(batch_id)
    login = ftps_login.ftps_login_status(ctx.cfg)
    return batch_page(
        ctx,
        request,
        "batch_upload.html",
        batch_id,
        "upload",
        n=min(counts.get("approved", 0) + counts.get("prepared", 0), importing.MAX_BATCH_PHOTOS),
        size=_mb(summary["upload_bytes"]),
        login_ok=bool(login["user_set"] and login["password_set"]),
        exports=_export_names(photos),
        resend=_resend_choices(photos),
        new_text=_new_text(photos),
        resend_page="upload",
        sent=counts.get("uploaded", 0) + counts.get("exported", 0),
        waiting=counts.get("imported", 0) + counts.get("ready", 0),
        unapproved=sum(counts.get(k, 0) for k in ("described", "needs_review", "blocked")),
        back_path=batch_url(batch_id, "upload"),
        submit_href=batch_url(batch_id, "submit"),
    )


@router.post("/batch/{batch_id}/deliver", response_class=HTMLResponse)
def deliver(request: Request, batch_id: str, form: PostForm, ctx: Ctx):
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
        s = delivering.deliver_batch(
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
        warn = " " + s.prepare.warning_text() if s.prepare.warnings else ""
        if s.prepare.notes:
            warn += " " + s.prepare.note_text()
        fixes = len(s.prepare.problems) + len(s.upload.failed) - len(lost)
        if fixes or lost:
            # Partial success: keep what was sent and still write its CSV.
            if s.export is None:
                exporting.export_batch(cfg, store, batch_id)
            text = f"Uploaded {sent}."
            if fixes:
                text = f"Uploaded {sent}; {fixes} need a fix in Review."
            if lost:
                text += " " + lost[0]
            return f"{text} Wrote the Shutterstock info file.{warn}"
        if s.upload.over_limit:
            return (
                f"Uploaded {sent} photo(s). {s.upload.over_limit} more are ready: "
                f"press Upload again to send them.{warn}"
            )
        if s.export is None:
            return (
                f"Uploaded {len(s.upload.uploaded)} photo(s). Stopped before the info file.{warn}"
            )
        left_out = " " + s.export.skipped_text() if s.export.skipped else ""
        return (
            f"Uploaded {len(s.upload.uploaded)} photo(s) and wrote the Shutterstock info file."
            f"{left_out}{warn}"
        )

    return ctx.start_job(request, "deliver", batch_id, run, total=2 * n + 1)


@router.get("/batch/{batch_id}/submit", response_class=HTMLResponse)
def submit_page(request: Request, batch_id: str, ctx: Ctx):
    ctx.get_batch_or_404(batch_id)
    photos = ctx.store.list_photos(batch_id)
    counts = count_by_status(photos)
    return batch_page(
        ctx,
        request,
        "batch_submit.html",
        batch_id,
        "submit",
        exports=_export_names(photos),
        resend=_resend_choices(photos),
        new_text=_new_text(photos),
        resend_page="submit",
        result_photos=[p for p in photos if p.status in RESULT_STATUSES[:2]],
        result_reasons=RESULT_REASONS,
        waiting=counts.get("exported", 0),
        finished=sum(counts.get(k, 0) for k in ("submitted", "accepted", "rejected")),
        report_href=batch_url(batch_id, "report.csv"),
        **ai_facts(ctx, batch_id),
    )


@router.post("/batch/{batch_id}/submitted")
def batch_submitted(batch_id: str, form: PostForm, ctx: Ctx):
    ctx.get_batch_or_404(batch_id)
    ids = resend.mark_submitted(ctx.store, batch_id, cfg=ctx.cfg)
    if not ids:
        return ctx.back(
            batch_url(batch_id, "submit"), error="Nothing is waiting to be marked as submitted."
        )
    return ctx.back(
        "/", notice=f"Marked {len(ids)} photo(s) as submitted. Open a photo to note the result."
    )


@router.post("/batch/{batch_id}/results")
def batch_results(batch_id: str, form: PostForm, ctx: Ctx):
    """Record what Shutterstock said for several photos in one go (rows left blank are skipped)."""
    ctx.get_batch_or_404(batch_id)
    here = batch_url(batch_id, "submit")
    rows: list[tuple[int, str, list[str]]] = []
    for pid in (int(v) for v in form.getlist("result_ids") if str(v).isdigit()):
        outcome = OUTCOME_VALUES.get(str(form.get(f"outcome_{pid}") or ""))
        if outcome is not None:
            rows.append((pid, outcome, [str(v) for v in form.getlist(f"reasons_{pid}")]))
    if not rows:
        return ctx.back(here, error="Choose a result for at least one photo.")
    done, refused = resend.record_results(ctx.store, rows, batch_id=batch_id, cfg=ctx.cfg)
    error = next(iter(refused.values()), None)
    if not done:
        return ctx.back(here, error=error)
    return ctx.back(here, notice=f"Recorded {done} result{'' if done == 1 else 's'}.", error=error)


@router.get("/batch/{batch_id}/report.csv")
def report_download(batch_id: str, ctx: Ctx):
    ctx.get_batch_or_404(batch_id)  # only an existing batch id is accepted; no file path
    data = reports.report_bytes(ctx.cfg, ctx.store, batch_id)
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


@router.post("/batch/{batch_id}/resend")
def resend_photos(batch_id: str, form: PostForm, ctx: Ctx):
    """ "These are no longer on Shutterstock": put the ticked photos back in the upload queue.

    shuto cannot see the Shutterstock account, so this is the user's word. The confirm box
    works without JavaScript."""
    ctx.get_batch_or_404(batch_id)
    page_name = str(form.get("page"))
    here = batch_url(batch_id, page_name if page_name in ("upload", "submit") else "upload")
    if form.get("confirm") != "yes":
        return ctx.back(
            here, error="Tick the box to confirm these photos are no longer on Shutterstock."
        )
    ids = ctx.ids_from(form)
    if not ids:
        return ctx.back(here, error="Tick the photos that are no longer on Shutterstock.")
    s = resend.reupload_photos(ctx.cfg, ctx.store, ids, batch_id=batch_id)
    if not s.moved:
        return ctx.back(here, error=next(iter(s.refused.values())))
    n = len(s.moved)
    return ctx.back(
        batch_url(batch_id, "upload"),
        notice=f"{n} photo{'' if n == 1 else 's'} will be uploaded again. "
        "Press Upload when you are ready.",
        error=next(iter(s.refused.values()), None),
    )


@router.post("/batch/{batch_id}/open-folder")
def open_batch_folder(batch_id: str, form: PostForm, ctx: Ctx):
    ctx.get_batch_or_404(batch_id)
    page_name = str(form.get("page"))
    back = batch_url(batch_id, page_name if page_name in OPEN_FOLDER_PAGES else "")
    folder = paths.batch_dir(ctx.cfg, batch_id)  # always the batch's own folder
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError:
        return ctx.back(back, error=openfolder.NO_DESKTOP)
    problem = openfolder.open_folder(folder)
    if problem:
        return ctx.back(back, error=problem)
    return ctx.back(back, notice="Opened the folder on this computer.")
