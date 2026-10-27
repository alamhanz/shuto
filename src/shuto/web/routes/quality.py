"""Quality check extras: the 100% crop of a photo and "Measure earlier photos"."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse

from shuto.core.jobs import JobContext
from shuto.web.state import Ctx, PostForm
from shuto.workflow import quality

router = APIRouter()

QUALITY_JOB_KEY = "quality"  # pseudo batch id for a run over every batch


@router.get("/photo/{photo_id}/crop")
def photo_crop(photo_id: int, ctx: Ctx):
    """The photo at 100% around its sharpest spot. 404 when there are no numbers or no file."""
    photo = ctx.get_photo_or_404(photo_id)
    path = quality.crop_best_tile(ctx.cfg, photo)
    if path is None:
        raise HTTPException(status_code=404, detail="No close-up for this photo.")
    return FileResponse(path, media_type="image/jpeg")


@router.post("/quality/measure", response_class=HTMLResponse)
def measure_earlier(request: Request, form: PostForm, ctx: Ctx):
    """ "Measure earlier photos": the full-size check for photos checked before it existed."""
    batch_id = str(form.get("batch_id") or "").strip() or None
    if batch_id is not None:
        ctx.get_batch_or_404(batch_id)
    todo = len(quality.missing(ctx.store, batch_id))
    if not todo:
        return ctx.message_fragment(request, "Every photo has already been measured.")
    cfg = ctx.cfg

    def run(job: JobContext) -> str:
        done = quality.measure_missing(
            cfg, ctx.store, batch_id, progress=job.progress_text, cancel=job.cancelled
        )
        skipped = todo - done
        text = f"Measured {done} photo{'' if done == 1 else 's'}."
        if skipped and not job.cancelled():
            text += f" {skipped} could not be measured (the file is missing or unreadable)."
        return text

    return ctx.start_job(request, "quality", batch_id or QUALITY_JOB_KEY, run, total=todo)
