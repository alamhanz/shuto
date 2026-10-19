"""Delivery commands: prepare, upload, reupload, export, submitted, report."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import TYPE_CHECKING

from shuto.cli._io import (
    _progress_printer,
    add_command,
    out,
)
from shuto.core import errors as core_errors
from shuto.core import paths as core_paths
from shuto.core.errors import PipelineError
from shuto.core.models import Status
from shuto.deliver import report as report_mod
from shuto.workflow import (
    delivering,
    exporting,
    reports,
    resend,
)

if TYPE_CHECKING:
    from shuto.cli import Ctx


def cmd_prepare(ctx: Ctx) -> int:
    a = ctx.args
    summary = delivering.prepare_batch(
        ctx.run_cfg, ctx.store, a.batch, only_ids=a.ids, progress=_progress_printer("prepare")
    )
    out(
        f"Prepared {len(summary.prepared)} file(s) in "
        f"{core_paths.batch_dir(ctx.cfg, a.batch) / 'prepared'}."
    )
    for pid, problems in summary.problems.items():
        out(f"  photo {pid} skipped: " + " ".join(problems))
    for pid, problems in summary.warnings.items():
        out(
            f"  WARNING photo {pid} prepared with a problem Shutterstock will reject: "
            + "; ".join(problems)
        )
    for pid, notes in summary.notes.items():
        out(f"  note photo {pid}: " + "; ".join(notes))
    return 0


def cmd_upload(ctx: Ctx) -> int:
    a = ctx.args
    try:
        summary = delivering.upload_batch(
            ctx.run_cfg,
            ctx.store,
            a.batch,
            confirm=a.yes,
            only_ids=a.ids,
            progress=_progress_printer("upload"),
        )
    except core_errors.UploadRefused as exc:
        out(str(exc))
        return 1
    out(f"Uploaded {len(summary.uploaded)}, failed {len(summary.failed)}.")
    for name, err in summary.failed.items():
        out(f"  {name}: {err}")
    out(f"Next: shuto export {a.batch}")
    return 1 if summary.failed else 0


def cmd_reupload(ctx: Ctx) -> int:
    """Photos deleted on Shutterstock: make them ready to upload again."""
    a = ctx.args
    if bool(a.ids) == bool(a.all):
        out("error: give --ids ID [ID ...] or --all.")
        return 2
    if a.ids:
        ids = list(a.ids)
    else:
        kinds = [Status.UPLOADED, Status.EXPORTED] + (
            [Status.SUBMITTED] if a.include_submitted else []
        )
        ids = [p.id for p in ctx.store.list_photos(a.batch, kinds) if p.id is not None]
    if not ids:
        out("Nothing to send again: no photo of this batch is marked as sent.")
        return 0
    if not a.yes:
        out(f"{len(ids)} photo(s) of batch {a.batch} would be uploaded again.")
        out("Only do this for photos that are no longer on Shutterstock. Add --yes to confirm.")
        return 1
    summary = resend.reupload_photos(ctx.run_cfg, ctx.store, ids, batch_id=a.batch)
    out(f"{len(summary.moved)} photo(s) will be uploaded again; {len(summary.refused)} refused.")
    for why in summary.refused.values():
        out(f"  {why}")
    if summary.moved:
        out(f"Next: shuto prepare {a.batch} (if needed), then shuto upload {a.batch} --yes")
    return 1 if summary.refused else 0


def cmd_export(ctx: Ctx) -> int:
    a = ctx.args
    summary = exporting.export_batch(ctx.run_cfg, ctx.store, a.batch, dry_run=a.dry_run)
    if a.dry_run:
        out(f"Dry run: {len(summary.rows)} row(s) would be exported.")
        for row in summary.rows:
            out(f"  {row['filename']}  {'editorial' if row['editorial'] else 'commercial'}")
        return 0
    for f in summary.files:
        out(f"Wrote {f}")
    if summary.skipped:
        out(summary.skipped_text())
    if not summary.files:
        out("Nothing to export (photos must be uploaded first).")
        return 0
    out(f"Batch folder: {core_paths.batch_dir(ctx.cfg, a.batch)}")
    out("Final step is manual: on submit.shutterstock.com open Portfolio > Submit content,")
    out("(Not submitted tab), press Upload CSV and choose this file, check the photos and Submit.")
    out(f"Afterwards: shuto submitted {a.batch}")
    return 0


def cmd_submitted(ctx: Ctx) -> int:
    ids = resend.mark_submitted(ctx.store, ctx.args.batch, cfg=ctx.cfg)
    out(f"Marked {len(ids)} photo(s) as submitted.")
    return 0


def cmd_report(ctx: Ctx) -> int:
    a = ctx.args
    if not ctx.store.list_photos(a.batch):
        raise PipelineError(f"There are no photos in batch {a.batch!r}.")
    if a.out:
        header, records = reports.build_report(ctx.cfg, ctx.store, a.batch)
        path = report_mod.write_report(Path(a.out).expanduser(), header, records)
    else:
        path = reports.write_report(ctx.cfg, ctx.store, a.batch)
    out(f"Report for {a.batch}: {path}")
    out(f"Batch folder: {core_paths.batch_dir(ctx.cfg, a.batch)}")
    return 0


def _add_prepare(sub: argparse._SubParsersAction) -> None:
    s = add_command(sub, "prepare", "write upload-ready JPEG copies with embedded metadata")
    s.add_argument("batch")
    s.add_argument("--ids", type=int, nargs="+")


def _add_upload(sub: argparse._SubParsersAction) -> None:
    s = add_command(
        sub, "upload", "FTPS upload of prepared photos (needs deliver.upload_enabled or --yes)"
    )
    s.add_argument("batch")
    s.add_argument("--ids", type=int, nargs="+")
    s.add_argument("--yes", action="store_true", help="confirm this upload")


def _add_reupload(sub: argparse._SubParsersAction) -> None:
    s = add_command(
        sub, "reupload", "send photos again that you deleted on Shutterstock (needs --yes)"
    )
    s.add_argument("batch")
    s.add_argument("--ids", type=int, nargs="+", help="photo ids (see `shuto review`)")
    s.add_argument("--all", action="store_true", help="every uploaded photo of the batch")
    s.add_argument(
        "--include-submitted",
        action="store_true",
        help="with --all: also photos noted as submitted",
    )
    s.add_argument("--yes", action="store_true", help="confirm they are gone from Shutterstock")


def _add_export(sub: argparse._SubParsersAction) -> None:
    s = add_command(sub, "export", "write the batch's Shutterstock CSV")
    s.add_argument("batch")
    s.add_argument("--dry-run", action="store_true")


def _add_submitted(sub: argparse._SubParsersAction) -> None:
    s = add_command(sub, "submitted", "mark an exported batch as submitted on the portal")
    s.add_argument("batch")


def _add_report(sub: argparse._SubParsersAction) -> None:
    s = add_command(sub, "report", "write the report CSV of a batch (every check and its reason)")
    s.add_argument("batch")
    s.add_argument("--out", metavar="PATH", help="write it here instead of the workspace")


PARSERS = {
    "prepare": _add_prepare,
    "upload": _add_upload,
    "reupload": _add_reupload,
    "export": _add_export,
    "submitted": _add_submitted,
    "report": _add_report,
}
