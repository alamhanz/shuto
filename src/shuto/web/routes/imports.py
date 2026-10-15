"""Add photos: the add page, the import job and "Add anyway"."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from shuto.core.jobs import JobContext
from shuto.settings import ui as settings_ui
from shuto.web.state import IMPORT_JOB_KEY, Ctx, PostForm
from shuto.workflow import importing, resend

router = APIRouter()

MAX_BATCH_NAME = 80


@router.get("/add", response_class=HTMLResponse)
def add_page(request: Request, ctx: Ctx):
    last = str(settings_ui.get_ui_setting(ctx.store, "last_folder") or "")
    return ctx.render(
        request,
        "add.html",
        last_folder=last,
        default_name=Path(last).expanduser().name[:MAX_BATCH_NAME] if last else "",
    )


@router.post("/import", response_class=HTMLResponse)
def start_import(request: Request, form: PostForm, ctx: Ctx):
    cfg = ctx.cfg
    raw = str(form.get("path") or "").strip()
    if not raw:
        return ctx.message_fragment(request, "Enter the folder that holds your photos.")
    folder = Path(raw).expanduser()
    if not folder.is_dir():
        return ctx.message_fragment(request, f'"{raw}" is not a folder on this computer.')
    notes = str(form.get("notes") or "").strip() or None
    batch_name = (str(form.get("batch_name") or "").strip() or folder.name)[:MAX_BATCH_NAME]
    recursive = form.get("recursive") == "yes"
    force = form.get("force") == "yes"
    mark_uploaded = form.get("mark_uploaded") == "yes"
    if force and mark_uploaded:
        return ctx.message_fragment(
            request, "Choose either Add anyway or Just remember them, not both."
        )
    settings_ui.set_ui_setting(ctx.store, "last_folder", str(folder))

    def run(job: JobContext) -> str:
        s = importing.import_folder(
            cfg,
            ctx.store,
            folder,
            recursive=recursive,
            notes=notes,
            progress=job.progress,
            force=force,
            mark_uploaded=mark_uploaded,
            batch_name=batch_name or None,
        )
        ctx.state.import_results[job.job_id] = list(s.batches.items())
        ctx.state.import_known[job.job_id] = [
            a.photo_id for a in s.already_uploaded if a.photo_id is not None
        ]
        parts = [
            importing.batches_text(s.batches) + "."
            if len(s.batches) > 1
            else f"{s.imported} photo(s) added."
        ]
        copies = s.duplicates + s.already_known
        if copies:
            parts.append(f"{copies} were already in shuto as exact copies.")
        if s.near_duplicates:
            parts.append(f"{s.near_duplicates} look like another photo in this folder.")
        if s.errors:
            parts.append(f"{len(s.errors)} could not be read.")
        if s.marked_uploaded:
            parts.append(f"{s.marked_uploaded} remembered as already sent.")
        if s.already_uploaded:
            shown = "; ".join(a.text() for a in s.already_uploaded[:10])
            more = len(s.already_uploaded) - 10
            parts.append(
                f"{len(s.already_uploaded)} look like photos you sent before and were "
                f"left out: {shown}" + (f"; and {more} more" if more > 0 else "") + ". "
                'To add them anyway, press "Add anyway" below.'
            )
        return " ".join(parts)

    return ctx.start_job(request, "import", IMPORT_JOB_KEY, run)


@router.post("/import/add-anyway")
def add_anyway(form: PostForm, ctx: Ctx):
    """ "Add anyway" on the import result: photos left out as sent before join after all."""
    ids = ctx.ids_from(form)
    if not ids:
        return ctx.back("/add", error="There is nothing to add.")
    s = resend.add_anyway(ctx.cfg, ctx.store, ids)
    if not s.moved:
        return ctx.back("/add", error=next(iter(s.refused.values()), "Nothing was added."))
    n = len(s.moved)
    text = f"Added {n} photo{'' if n == 1 else 's'} anyway."
    target = "/batch/" + quote(next(iter(s.batches)), safe="") if len(s.batches) == 1 else "/"
    return ctx.back(target, notice=text, error=next(iter(s.refused.values()), None))
