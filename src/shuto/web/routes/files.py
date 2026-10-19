"""Files served by id: exports, thumbnails and page previews."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from shuto.ingest import imaging
from shuto.web.state import Ctx

router = APIRouter()

PREVIEW_SIDE = 2048  # page preview size; originals can be tens of MB


@router.get("/batch/{batch_id}/exports/{filename}")
def export_download(batch_id: str, filename: str, ctx: Ctx):
    ctx.get_batch_or_404(batch_id)
    for p in ctx.store.list_photos(batch_id):
        if p.export_file and Path(p.export_file).name == filename:
            path = Path(p.export_file)
            if path.is_file():
                return FileResponse(path, media_type="text/csv", filename=filename)
    raise HTTPException(status_code=404, detail="Unknown export file.")


@router.get("/thumb/{photo_id}")
def thumb(photo_id: int, ctx: Ctx):
    cfg = ctx.cfg
    photo = ctx.get_photo_or_404(photo_id)
    path = cfg.workspace_path("thumbs", f"{photo.sha256}.jpg")
    if not path.is_file():
        raise HTTPException(status_code=404, detail="No thumbnail.")
    return FileResponse(path, media_type="image/jpeg")


@router.get("/image/{photo_id}")
def image(photo_id: int, ctx: Ctx):
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
    return thumb(photo_id, ctx)
