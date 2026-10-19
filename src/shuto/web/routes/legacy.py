"""Legacy power-user job routes: POST /batch/{id}/{cull,describe,prepare,upload,export}.

Registered after every specific `/batch/{id}/x` POST so it only catches what is left.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from shuto.core.errors import PipelineError
from shuto.core.jobs import JobContext
from shuto.web.routes.batch import start_live
from shuto.web.state import Ctx, PostForm
from shuto.workflow import culling, delivering, describing, exporting

router = APIRouter()

BATCH_JOBS = ("cull", "describe", "prepare", "upload", "export")  # legacy / power-user routes


@router.post("/batch/{batch_id}/{action}", response_class=HTMLResponse)
def batch_job(request: Request, batch_id: str, action: str, form: PostForm, ctx: Ctx):
    cfg = ctx.cfg
    if action not in BATCH_JOBS:
        raise HTTPException(status_code=404, detail="Unknown action.")
    ctx.get_batch_or_404(batch_id)
    only = ctx.ids_from(form) or None
    store = ctx.store
    if action == "cull":

        def run(job: JobContext) -> str:
            s = culling.cull_batch(
                cfg, store, batch_id, progress=job.progress, cancel=job.cancelled
            )
            return f"Cull done: {s.ready} ready, {s.culled} culled (of {s.total} new)."

    elif action == "describe":
        block = ctx.model_block(request)
        if block:
            return ctx.message_fragment(request, block)

        def run(job: JobContext) -> str:
            try:
                s = describing.describe_batch(
                    cfg,
                    store,
                    batch_id,
                    only_ids=only,
                    progress=job.progress,
                    cancel=job.cancelled,
                    live=start_live(ctx, batch_id),
                )
            finally:
                ctx.state.live.pop(batch_id, None)
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
            s = delivering.prepare_batch(
                cfg, store, batch_id, only_ids=only, progress=job.progress, cancel=job.cancelled
            )
            text = f"Prepared {len(s.prepared)} file(s)."
            if s.problems:
                first = next(iter(s.problems.values()))[0]
                text += f" {len(s.problems)} photo(s) have problems, e.g. {first}"
            if s.warnings:
                text += " " + s.warning_text()
            return text

    elif action == "upload":
        if form.get("confirm") != "yes":
            return ctx.message_fragment(
                request, "Tick the box to confirm you want to upload to Shutterstock."
            )

        def run(job: JobContext) -> str:
            s = delivering.upload_batch(
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
            s = exporting.export_batch(cfg, store, batch_id)
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
