"""Photo commands: import, ledger, cull, describe, run, review, edit, approve, status, etc."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import TYPE_CHECKING

from shuto.ai import filters as filters_mod
from shuto.ai import model_choice as ai_choice
from shuto.ai import selection
from shuto.cli._io import (
    COMMON,
    _confirm,
    _progress_printer,
    _short,
    _split_csv,
    add_command,
    out,
    table,
)
from shuto.cli.cmd_models import print_disk_advice, show_model_choice
from shuto.core import paths as core_paths
from shuto.core.models import REASON_CODES, Status
from shuto.core.text import yes_no
from shuto.workflow import (
    culling,
    describing,
    importing,
    ledger_sync,
    reports,
    resend,
    review,
)
from shuto.workflow import models as workflow_models

if TYPE_CHECKING:
    from shuto.cli import Ctx


def _print_import(summary: importing.ImportSummary) -> None:
    out(
        f"Imported {summary.imported} photo(s); {summary.duplicates} exact duplicate(s) skipped, "
        f"{summary.already_known} already known, {summary.near_duplicates} near duplicate(s)."
    )
    if summary.marked_uploaded:
        out(f"Registered {summary.marked_uploaded} photo(s) as already uploaded.")
    if summary.already_uploaded:
        out(
            f"{len(summary.already_uploaded)} photo(s) were uploaded before and were not "
            "processed again (--force imports look-alikes anyway; for photos you deleted on "
            "Shutterstock use `shuto reupload BATCH --ids ...`):"
        )
        for item in summary.already_uploaded[:20]:
            out(f"  {item.text()}")
        if len(summary.already_uploaded) > 20:
            out(f"  ... and {len(summary.already_uploaded) - 20} more")
    for batch, n in sorted(summary.batches.items()):
        out(f"  batch {batch}: {n}")
    if len(summary.batches) > 1:
        out(importing.batches_text(summary.batches))
    for err in summary.errors[:10]:
        out(f"  problem: {err}")


def cmd_import(ctx: Ctx) -> int:
    a = ctx.args
    if a.force and a.mark_uploaded:
        out("error: --force and --mark-uploaded cannot be combined.")
        return 2
    if a.mark_uploaded and not a.yes:
        out(
            "This marks the photos as uploaded for good, including any already being processed. "
            "Nothing is sent anywhere."
        )
        if not _confirm("Register these photos as already uploaded?"):
            out("Cancelled. Nothing was changed.")
            return 1
    summary = importing.import_folder(
        ctx.cfg,
        ctx.store,
        a.path,
        recursive=not a.no_recursive,
        notes=a.notes,
        progress=_progress_printer("scan"),
        force=a.force,
        mark_uploaded=a.mark_uploaded,
    )
    _print_import(summary)
    if summary.batches:
        out("Next: shuto cull <batch>")
    return 0


def cmd_ledger(ctx: Ctx) -> int:
    a = ctx.args
    if a.ledger_cmd == "export":
        if a.path:
            path = Path(a.path).expanduser()
            count = ledger_sync.export_ledger(ctx.store, path)
        else:
            path = core_paths.ledger_path(ctx.cfg)
            count = ledger_sync.export_ledger(ctx.store, path, keep_existing=True)
        out(f"Wrote {count} uploaded photo(s) to {path}")
        return 0
    res = ledger_sync.import_ledger(ctx.cfg, ctx.store, a.path)
    out(
        f"Ledger merged: {res.added} added, {res.marked} marked uploaded, "
        f"{res.already_known} already known."
    )
    return 0


def _print_cull(summary: culling.CullSummary, explain: bool) -> None:
    out(f"Checked {summary.total}: {summary.ready} ready, {summary.culled} culled.")
    if explain or summary.culled:
        for d in summary.details:
            out(f"  [{d['id']}] {d['name']}: " + "; ".join(d["reasons"]))


def cmd_cull(ctx: Ctx) -> int:
    summary = culling.cull_batch(ctx.cfg, ctx.store, ctx.args.batch, explain=ctx.args.explain)
    _print_cull(summary, ctx.args.explain)
    return 0


def _describe_flow(
    ctx: Ctx,
    batch: str,
    ids: list[int] | None,
    assume_yes: bool,
    provider: str | None,
    model: str | None,
) -> int:
    cfg, store = ctx.run_cfg, ctx.store
    try:
        sel = selection.resolve_selection(cfg, store, provider, model)
    except ai_choice.ModelChoiceRequired:
        show_model_choice(cfg, store)
        return 2
    problems = selection.describe_preflight(cfg, sel)
    if problems:
        for p in problems:
            out(p)
        if sel.provider == "ollama":
            adv = workflow_models.model_disk_advice(cfg, sel.model)
            if adv is not None and not adv.fits:
                print_disk_advice(adv)
        return 1
    est = describing.estimate_describe(
        cfg, store, batch, only_ids=ids, provider=provider, model=model
    )
    out(f"Model: {sel.provider}:{sel.model}")
    out(f"Estimate: {est.text}")
    if est.n == 0:
        out("Nothing to describe (only photos in status `ready` are sent to the model).")
        return 0
    if not assume_yes and not _confirm("Continue?"):
        out("Cancelled. Nothing was sent to the model.")
        return 1
    llm = selection.resolve_llm(cfg, store, provider, model)
    summary = describing.describe_batch(
        cfg, store, batch, ids, llm, progress=_progress_printer("describe")
    )
    out(
        f"Described {summary.described}, needs review {summary.needs_review}, "
        f"set aside {summary.set_aside}, blocked {summary.blocked}, failed {summary.failed}."
    )
    for pid, err in summary.errors[:10]:
        out(f"  photo {pid}: {err}")
    if summary.aborted:
        out(f"Stopped early: {summary.aborted}")
        return 1
    out(f"Next: shuto review {batch}")
    return 0


def cmd_describe(ctx: Ctx) -> int:
    a = ctx.args
    return _describe_flow(ctx, a.batch, a.ids, a.yes, a.provider, a.model)


def cmd_run(ctx: Ctx) -> int:
    a = ctx.args
    summary = importing.import_folder(
        ctx.cfg, ctx.store, a.path, notes=a.notes, progress=_progress_printer("scan")
    )
    _print_import(summary)
    code = 0
    for batch in sorted(summary.batches):
        out(f"== batch {batch} ==")
        _print_cull(culling.cull_batch(ctx.run_cfg, ctx.store, batch), explain=False)
        code = max(code, _describe_flow(ctx, batch, None, a.yes, a.provider, a.model))
    return code


def _photo_row(p) -> tuple:
    m = p.metadata or {}
    return (
        p.id,
        p.status.value,
        _short(p.orig_name, 28),
        _short(m.get("description", ""), 60),
        len(m.get("keywords") or []),
    )


def cmd_review(ctx: Ctx) -> int:
    a = ctx.args
    statuses = [Status(a.status)] if a.status else None
    photos = ctx.store.list_photos(a.batch, statuses)
    if not photos:
        out("No photos.")
        return 0
    if not a.full:
        out(table(["id", "status", "file", "description", "kw"], [_photo_row(p) for p in photos]))
        out(f"Edit: shuto edit <id> --description ...   Approve: shuto approve {a.batch}")
        return 0
    for p in photos:
        m = p.metadata or {}
        out(
            f"[{p.id}] {p.orig_name}  status={p.status.value}  "
            f"editorial={yes_no(bool(m.get('editorial')))}"
        )
        out(f"  description: {m.get('description', '')}")
        out(f"  keywords ({len(m.get('keywords') or [])}): {', '.join(m.get('keywords') or [])}")
        out(f"  categories: {', '.join(m.get('categories') or [])}")
        for w in m.get("warnings") or []:
            out(f"  ! {w}")
        for line in filters_mod.messages(p.filters):
            out(f"  ! {line}")
        if p.cull and not p.cull.get("ok", True):
            out("  cull: " + "; ".join(p.cull.get("reasons", [])))
        out()
    return 0


def cmd_edit(ctx: Ctx) -> int:
    a = ctx.args
    photo = review.update_metadata(
        ctx.run_cfg,
        ctx.store,
        a.id,
        description=a.description,
        title=a.title,
        keywords=_split_csv(a.keywords),
        categories=_split_csv(a.categories),
        editorial=a.editorial,
        mature=a.mature,
        release_ids=a.release,
    )
    out(f"Photo {photo.id} is now {photo.status.value}.")
    for w in (photo.metadata or {}).get("warnings") or []:
        out(f"  ! {w}")
    return 0


def cmd_approve(ctx: Ctx) -> int:
    a = ctx.args
    summary = review.approve(
        ctx.run_cfg,
        ctx.store,
        batch_id=a.batch,
        photo_ids=a.ids,
        include_needs_review=a.include_review,
    )
    out(f"Approved {len(summary.approved)} photo(s).")
    for pid, why in summary.refused.items():
        out(f"  photo {pid} not approved: {why}")
    if not a.include_review and a.ids is None:
        out("Photos in `needs_review` are only approved with --include-review (or --ids).")
        out("Photos that were set aside are only approved by hand: --ids ... (Approve anyway).")
    return 0


def cmd_status(ctx: Ctx) -> int:
    rows = reports.batch_report(ctx.store, ctx.args.batch)
    if not rows:
        out("No batches yet. Start with `shuto import <folder>`.")
        return 0
    for r in rows:
        counts = ", ".join(f"{k}={v}" for k, v in sorted(r["counts"].items())) or "empty"
        u = r["usage"]
        out(f"{r['id']}  ({r['total']} photos)  {counts}")
        if u["calls"]:
            out(
                f"    LLM: {u['calls']} calls, {u['input_tokens']} in / "
                f"{u['output_tokens']} out tokens, {u['seconds']:.0f} s"
            )
    rejections = ctx.store.rejection_stats()
    if rejections:
        out("Rejections: " + ", ".join(f"{k}={v}" for k, v in sorted(rejections.items())))
    return 0


def cmd_reject(ctx: Ctx) -> int:
    a = ctx.args
    photo = review.find_photo(ctx.store, a.file)
    assert photo.id is not None
    resend.record_result(ctx.store, photo.id, False, a.reason, a.note, cfg=ctx.cfg)
    out(
        f"Recorded rejection ({a.reason}) for {photo.orig_name}; later prompts will account for it."
    )
    return 0


def cmd_accept(ctx: Ctx) -> int:
    photo = review.find_photo(ctx.store, ctx.args.file)
    assert photo.id is not None
    resend.record_result(ctx.store, photo.id, True, cfg=ctx.cfg)
    out(f"Recorded acceptance for {photo.orig_name}.")
    return 0


def _add_import(sub: argparse._SubParsersAction) -> None:
    s = add_command(sub, "import", "register photos from a folder")
    s.add_argument("path", nargs="?", help="folder (default: sources in the config)")
    s.add_argument("--no-recursive", action="store_true")
    s.add_argument("--notes", help="batch notes (location, event) sent with the prompt")
    s.add_argument(
        "--force", action="store_true", help="import even photos that look already uploaded"
    )
    s.add_argument("--yes", action="store_true", help="skip the --mark-uploaded confirmation")
    s.add_argument(
        "--mark-uploaded",
        action="store_true",
        help="just remember these photos as already uploaded (nothing else is done to them)",
    )


def _add_ledger(sub: argparse._SubParsersAction) -> None:
    s = add_command(
        sub, "ledger", "export or import the list of already uploaded photos (uploaded.csv)"
    )
    ls = s.add_subparsers(dest="ledger_cmd", required=True)
    m = ls.add_parser("export", parents=[COMMON], help="write the ledger CSV from the database")
    m.add_argument("path", nargs="?", help="default: <workspace>/uploaded.csv")
    m = ls.add_parser("import", parents=[COMMON], help="merge a ledger CSV into the database")
    m.add_argument("path")


def _add_cull(sub: argparse._SubParsersAction) -> None:
    s = add_command(sub, "cull", "technical check: size, sharpness, exposure")
    s.add_argument("batch")
    s.add_argument("--explain", action="store_true")


def _add_describe(sub: argparse._SubParsersAction) -> None:
    s = add_command(
        sub, "describe", "generate title, description and keywords (shows an estimate first)"
    )
    s.add_argument("batch")
    s.add_argument("--ids", type=int, nargs="+")
    s.add_argument("--yes", action="store_true", help="skip the estimate confirmation")
    s.add_argument("--provider")
    s.add_argument("--model")


def _add_run(sub: argparse._SubParsersAction) -> None:
    s = add_command(sub, "run", "import, cull and describe in one go")
    s.add_argument("path")
    s.add_argument("--notes")
    s.add_argument("--yes", action="store_true", help="skip the estimate confirmation")
    s.add_argument("--provider")
    s.add_argument("--model")


def _add_review(sub: argparse._SubParsersAction) -> None:
    s = add_command(sub, "review", "list described photos")
    s.add_argument("batch")
    s.add_argument("--status", choices=[x.value for x in Status])
    s.add_argument("--full", action="store_true")


def _add_edit(sub: argparse._SubParsersAction) -> None:
    s = add_command(sub, "edit", "edit metadata of one photo (guardrails re-run)")
    s.add_argument("id", type=int)
    s.add_argument("--description")
    s.add_argument("--title")
    s.add_argument("--keywords", help="comma separated")
    s.add_argument("--categories", help="comma separated")
    s.add_argument("--editorial", action=argparse.BooleanOptionalAction, default=None)
    s.add_argument("--mature", action=argparse.BooleanOptionalAction, default=None)
    s.add_argument("--release", action="append", help="release id; prefix property: for property")


def _add_approve(sub: argparse._SubParsersAction) -> None:
    s = add_command(sub, "approve", "approve reviewed photos")
    s.add_argument("batch", nargs="?")
    s.add_argument("--ids", type=int, nargs="+")
    s.add_argument("--include-review", action="store_true", help="also approve needs_review")


def _add_status(sub: argparse._SubParsersAction) -> None:
    s = add_command(sub, "status", "batches and photo counts")
    s.add_argument("batch", nargs="?")


def _add_reject(sub: argparse._SubParsersAction) -> None:
    s = add_command(sub, "reject", "record a rejection and its reason")
    s.add_argument("file", help="photo id, original file name or prepared name")
    s.add_argument("--reason", required=True, choices=REASON_CODES)
    s.add_argument("--note")


def _add_accept(sub: argparse._SubParsersAction) -> None:
    s = add_command(sub, "accept", "record an acceptance")
    s.add_argument("file")


PARSERS = {
    "import": _add_import,
    "ledger": _add_ledger,
    "cull": _add_cull,
    "describe": _add_describe,
    "run": _add_run,
    "review": _add_review,
    "edit": _add_edit,
    "approve": _add_approve,
    "status": _add_status,
    "reject": _add_reject,
    "accept": _add_accept,
}
