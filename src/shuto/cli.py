"""Command line interface. Plain text output (no ANSI, ASCII only) so any console works."""

from __future__ import annotations

import argparse
import getpass
import sys
import traceback
from collections.abc import Sequence
from pathlib import Path

from . import deliver, ftps, guardrails, local_models, pipeline
from .config import Config, ConfigError, find_config_path, load_config
from .llm.base import LLMError
from .metadata import PhotoMetadata
from .models import REASON_CODES, Status
from .pipeline import PipelineError
from .secrets import KNOWN_SECRETS, set_secret
from .store import Store

OLLAMA_URL = "https://ollama.com/download"


def _input(prompt: str) -> str:
    """Single place for interactive input (tests replace it)."""
    return input(prompt)


def _confirm(prompt: str) -> bool:
    try:
        return _input(f"{prompt} [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def out(text: str = "") -> None:
    print(text)


def table(headers: Sequence[str], rows: Sequence[Sequence[object]]) -> str:
    cells = [[str(c) for c in r] for r in rows]
    widths = [
        max(len(h), *(len(r[i]) for r in cells)) if cells else len(h) for i, h in enumerate(headers)
    ]
    lines = ["  ".join(h.ljust(w) for h, w in zip(headers, widths, strict=True)).rstrip()]
    lines.append("  ".join("-" * w for w in widths))
    lines += ["  ".join(c.ljust(w) for c, w in zip(r, widths, strict=True)).rstrip() for r in cells]
    return "\n".join(lines)


def _yn(flag: bool) -> str:
    return "yes" if flag else "no"


def _short(text: str, n: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[: n - 3] + "..."


# --------------------------------------------------------------------------- shared output


def print_models_table(rows: Sequence[local_models.ModelRow]) -> None:
    body = [
        (
            r.rank,
            r.tag + (" *" if r.recommended else ""),
            f"{r.size_gb:g} GB",
            f"{r.est_seconds_per_image:g} s ({r.basis})",
            r.tier_label,
            _yn(r.fits_hardware),
            _yn(r.fits_disk),
            _yn(r.pulled),
        )
        for r in rows
    ]
    out(
        table(
            [
                "#",
                "model",
                "download",
                "est. per image",
                "quality / speed",
                "fits hw",
                "fits disk",
                "pulled",
            ],
            body,
        )
    )
    out("* = best score for this machine. Estimates are heuristics until `shuto models bench`.")
    out("Pick one with: shuto models use <model>   (nothing is downloaded until you confirm).")


def print_disk_advice(adv: local_models.DiskAdvice) -> None:
    out(adv.message)
    for v in adv.volumes:
        out(f"  {v.mount}  {v.free_gb:g} GB free -> suggested OLLAMA_MODELS={v.suggested_path}")
        out(f"    {v.env_hint}")
    if adv.api_options:
        out("API options (cost per image):")
        for a in adv.api_options:
            out(
                f"  {a.provider}/{a.model}: {a.text}   use: shuto models use {a.provider}:{a.model}"
            )


def ollama_guidance(os_name: str) -> str:
    steps = {
        "windows": "Run the installer from the link, then start Ollama from the Start menu.",
        "macos": "Download the app from the link, open it, and keep it running.",
    }.get(
        os_name, "Use the install command from the link, then run `ollama serve` (or the service)."
    )
    return f"Ollama is needed for local models. Download: {OLLAMA_URL}\n  {steps}"


def no_model_message() -> str:
    return (
        "No local model chosen yet. shuto never picks one silently.\n"
        "See the top 8 for this machine with `shuto models list`, then choose with "
        "`shuto models use <model>`."
    )


def show_model_choice(cfg: Config) -> None:
    out(no_model_message())
    out()
    try:
        print_models_table(pipeline.list_models(cfg))
    except Exception as exc:
        out(f"(could not detect hardware: {exc})")


# --------------------------------------------------------------------------- context


class Ctx:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.config_path = find_config_path(getattr(args, "config", None))
        self.cfg = load_config(getattr(args, "config", None))
        self._store: Store | None = None

    @property
    def store(self) -> Store:
        if self._store is None:
            self._store = pipeline.open_store(self.cfg)
        return self._store


def _progress_printer(label: str):
    state = {"last": -1}

    def report(done: int, total: int, message: str = "") -> None:
        if total and done != state["last"]:
            state["last"] = done
            out(f"{label} {done}/{total} {message}".rstrip())

    return report


# --------------------------------------------------------------------------- setup commands


def cmd_ui(ctx: Ctx) -> int:
    try:
        from .web import serve
    except ImportError as exc:
        out(f"The web UI is not available in this install ({exc}).")
        return 1
    a = ctx.args
    web = ctx.cfg.web
    serve(
        ctx.cfg,
        host=getattr(a, "host", None) or web.host,
        port=getattr(a, "port", None) or web.port,
        open_browser=web.open_browser and not getattr(a, "no_browser", False),
        config_path=str(ctx.config_path) if ctx.config_path else None,
    )
    return 0


def _print_hardware(report: pipeline.DoctorReport) -> None:
    hw = report.hardware
    out(f"OS:        {hw['os']} ({hw['arch']})")
    out(f"CPU:       {hw['cpu']} ({hw['cores']} cores)")
    out(f"RAM:       {hw['ram_gb']:g} GB")
    if hw["gpus"]:
        for g in hw["gpus"]:
            vram = f", {g['vram_total_gb']:g} GB" if g.get("vram_total_gb") else ""
            out(f"GPU:       {g['name']} ({g['vendor']}{vram})")
    else:
        out("GPU:       none detected (CPU mode)")
    out(f"Tier:      {hw['tier']} (usable GPU memory {hw['usable_gpu_gb']:g} GB)")
    for note in hw.get("notes", []):
        out(f"Note:      {note}")


def cmd_doctor(ctx: Ctx) -> int:
    report = pipeline.doctor(ctx.cfg, ctx.store, config_path=ctx.config_path)
    out(f"Config:    {report.config_path or '(none, using defaults)'}")
    out(f"Workspace: {report.workspace}")
    _print_hardware(report)
    st = report.ollama
    if st.running:
        out(f"Ollama:    running {st.version or ''} at {st.url}; {len(st.pulled)} model(s) pulled")
        for tag in st.pulled:
            out(f"           - {tag}")
    elif st.installed:
        out(f"Ollama:    installed but not running at {st.url} (start it)")
    else:
        out("Ollama:    not found")
        out("  " + ollama_guidance(report.hardware["os"]).replace("\n", "\n  "))
    free = (
        f"{report.disk_free_gb:g} GB free"
        if report.disk_free_gb is not None
        else "free space unknown"
    )
    out(f"Models in: {report.models_dir} ({free})")
    out(f"Provider:  {report.provider}")
    out(f"Model:     {report.selected or '(not chosen)'}")
    out(f"Model choice file: {pipeline.model_choice_path()}")
    out(f"Benchmark cache:   {pipeline.benchmark_cache_path()}")
    if report.selected is None and report.provider == "ollama":
        out()
        out(no_model_message())
        out()
        print_models_table(report.rows)
    return 0


def _pull_with_confirmation(ctx: Ctx, tag: str, assume_yes: bool) -> int:
    cfg = ctx.cfg
    adv = pipeline.model_disk_advice(cfg, tag)
    if adv is not None and not adv.fits:
        print_disk_advice(adv)
        out("Not downloading: the model does not fit where Ollama stores models.")
        return 1
    size = f" (about {adv.need_gb - local_models.DISK_HEADROOM_GB:g} GB)" if adv else ""
    if not assume_yes and not _confirm(f"Download {tag}{size} with Ollama?"):
        out("Skipped. Nothing was downloaded.")
        return 0
    last = {"pct": -10}

    def on_progress(ev: dict) -> None:
        total, done = ev.get("total"), ev.get("completed")
        if total and done is not None:
            pct = int(done * 100 / total)
            if pct >= last["pct"] + 10 or pct == 100:
                last["pct"] = pct
                out(f"  {ev.get('status', 'pulling')}: {pct}%")
        elif ev.get("status") and ev["status"] != last.get("status"):
            last["status"] = ev["status"]
            out(f"  {ev['status']}")

    pipeline.pull_model(cfg, tag, on_progress)
    out(f"Downloaded {tag}.")
    return 0


def cmd_init(ctx: Ctx) -> int:
    cfg = ctx.cfg
    _ = ctx.store  # creates the workspace and database
    out(f"Workspace ready: {cfg.workspace_dir}")
    if ctx.config_path is None:
        out("No shuto.yaml found; using defaults (see shuto.example.yaml to customise).")
    report = pipeline.doctor(cfg, ctx.store, config_path=ctx.config_path)
    _print_hardware(report)
    if cfg.llm.provider != "ollama" and report.selected:
        out(f"Provider: {report.selected} (no local model needed)")
        return 0
    if not report.ollama.running:
        out(ollama_guidance(report.hardware["os"]))
    tag = report.selected.split(":", 1)[1] if report.selected else None
    if report.selected is None:
        out()
        out("Choose a local model (nothing is downloaded yet).")
        print_models_table(report.rows)
        answer = _answer("Number or model tag (Enter to skip): ")
        if answer:
            by_rank = {str(r.rank): r.tag for r in report.rows}
            tag = by_rank.get(answer, answer)
            sel = pipeline.use_model(cfg, ctx.store, tag)
            out(f"Saved choice: {sel.model}")
        else:
            out("No model chosen. Run `shuto models list` / `shuto models use <model>` later.")
            return 0
    assert tag is not None
    if tag in report.ollama.pulled or f"{tag}:latest" in report.ollama.pulled:
        out(f"{tag} is already downloaded.")
    elif report.ollama.running:
        _pull_with_confirmation(ctx, tag, assume_yes=False)
    else:
        adv = pipeline.model_disk_advice(cfg, tag)
        if adv is not None and not adv.fits:
            print_disk_advice(adv)
        out(f"Start Ollama, then run `shuto models pull {tag}`.")
    out("Next: shuto import <folder>   (or `shuto` to open the web UI)")
    return 0


def _answer(prompt: str) -> str:
    try:
        return _input(prompt).strip()
    except EOFError:
        return ""


def cmd_models(ctx: Ctx) -> int:
    a = ctx.args
    sub = getattr(a, "models_cmd", None) or "list"
    cfg = ctx.cfg
    if sub == "list":
        rows = pipeline.list_models(cfg)
        print_models_table(rows)
        sel = _selected_or_none(ctx)
        out(f"Current choice: {sel or '(none)'}")
        return 0
    if sub == "use":
        sel = pipeline.use_model(cfg, ctx.store, a.spec)
        out(f"Saved choice: {sel.provider}:{sel.model}")
        if sel.provider == "ollama":
            adv = pipeline.model_disk_advice(cfg, sel.model)
            if adv is None:
                out("Note: this tag is not in the catalog, so no estimate is available.")
            elif not adv.fits:
                print_disk_advice(adv)
            else:
                out(adv.message)
            out(f"Download it with: shuto models pull {sel.model}")
        return 0
    if sub == "pull":
        return _pull_with_confirmation(ctx, a.tag, a.yes)
    if sub == "bench":
        model = a.model or pipeline.resolve_selection(cfg, ctx.store, provider="ollama").model
        out(f"Benchmarking {model} (two calls on a small sample image)...")
        rec = pipeline.bench_model(cfg, ctx.store, model)
        out(f"cold {rec['cold_s']} s, warm {rec['warm_s']} s, valid JSON: {_yn(rec['ok_json'])}")
        out(f"Saved to {pipeline.benchmark_cache_path()}")
        return 0
    raise AssertionError(sub)


def _selected_or_none(ctx: Ctx) -> str | None:
    try:
        sel = pipeline.resolve_selection(ctx.cfg, ctx.store)
    except local_models.ModelChoiceRequired:
        return None
    return f"{sel.provider}:{sel.model}"


# --------------------------------------------------------------------------- pipeline commands


def _print_import(summary: pipeline.ImportSummary) -> None:
    out(
        f"Imported {summary.imported} photo(s); {summary.duplicates} exact duplicate(s) skipped, "
        f"{summary.already_known} already known, {summary.near_duplicates} near duplicate(s)."
    )
    if summary.marked_uploaded:
        out(f"Registered {summary.marked_uploaded} photo(s) as already uploaded.")
    if summary.already_uploaded:
        out(
            f"{len(summary.already_uploaded)} photo(s) were uploaded before and were not "
            "processed again (use --force to import them anyway):"
        )
        for item in summary.already_uploaded[:20]:
            out(f"  {item.text()}")
        if len(summary.already_uploaded) > 20:
            out(f"  ... and {len(summary.already_uploaded) - 20} more")
    for batch, n in sorted(summary.batches.items()):
        out(f"  batch {batch}: {n}")
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
    summary = pipeline.import_folder(
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
            count = pipeline.export_ledger(ctx.store, path)
        else:
            path = pipeline.ledger_path(ctx.cfg)
            count = pipeline.export_ledger(ctx.store, path, keep_existing=True)
        out(f"Wrote {count} uploaded photo(s) to {path}")
        return 0
    res = pipeline.import_ledger(ctx.cfg, ctx.store, a.path)
    out(
        f"Ledger merged: {res.added} added, {res.marked} marked uploaded, "
        f"{res.already_known} already known."
    )
    return 0


def _print_cull(summary: pipeline.CullSummary, explain: bool) -> None:
    out(f"Checked {summary.total}: {summary.ready} ready, {summary.culled} culled.")
    if explain or summary.culled:
        for d in summary.details:
            out(f"  [{d['id']}] {d['name']}: " + "; ".join(d["reasons"]))


def cmd_cull(ctx: Ctx) -> int:
    summary = pipeline.cull_batch(ctx.cfg, ctx.store, ctx.args.batch, explain=ctx.args.explain)
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
    cfg, store = ctx.cfg, ctx.store
    try:
        sel = pipeline.resolve_selection(cfg, store, provider, model)
    except local_models.ModelChoiceRequired:
        show_model_choice(cfg)
        return 2
    problems = pipeline.describe_preflight(cfg, sel)
    if problems:
        for p in problems:
            out(p)
        if sel.provider == "ollama":
            adv = pipeline.model_disk_advice(cfg, sel.model)
            if adv is not None and not adv.fits:
                print_disk_advice(adv)
        return 1
    est = pipeline.estimate_describe(
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
    llm = pipeline.resolve_llm(cfg, store, provider, model)
    summary = pipeline.describe_batch(
        cfg, store, batch, ids, llm, progress=_progress_printer("describe")
    )
    out(
        f"Described {summary.described}, needs review {summary.needs_review}, "
        f"blocked {summary.blocked}, failed {summary.failed}."
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
    summary = pipeline.import_folder(
        ctx.cfg, ctx.store, a.path, notes=a.notes, progress=_progress_printer("scan")
    )
    _print_import(summary)
    code = 0
    for batch in sorted(summary.batches):
        out(f"== batch {batch} ==")
        _print_cull(pipeline.cull_batch(ctx.cfg, ctx.store, batch), explain=False)
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
            f"editorial={_yn(bool(m.get('editorial')))}"
        )
        out(f"  description: {m.get('description', '')}")
        out(f"  keywords ({len(m.get('keywords') or [])}): {', '.join(m.get('keywords') or [])}")
        out(f"  categories: {', '.join(m.get('categories') or [])}")
        for w in m.get("warnings") or []:
            out(f"  ! {w}")
        if p.cull and not p.cull.get("ok", True):
            out("  cull: " + "; ".join(p.cull.get("reasons", [])))
        out()
    return 0


def _split_csv(text: str | None) -> list[str] | None:
    if text is None:
        return None
    return [t.strip() for t in text.split(",") if t.strip()]


def cmd_edit(ctx: Ctx) -> int:
    a = ctx.args
    photo = pipeline.update_metadata(
        ctx.cfg,
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
    summary = pipeline.approve(
        ctx.cfg,
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
    return 0


def cmd_prepare(ctx: Ctx) -> int:
    a = ctx.args
    summary = pipeline.prepare_batch(
        ctx.cfg, ctx.store, a.batch, only_ids=a.ids, progress=_progress_printer("prepare")
    )
    out(f"Prepared {len(summary.prepared)} file(s) in {ctx.cfg.workspace_path('prepared')}.")
    for pid, problems in summary.problems.items():
        out(f"  photo {pid} skipped: " + " ".join(problems))
    return 0


def cmd_upload(ctx: Ctx) -> int:
    a = ctx.args
    try:
        summary = pipeline.upload_batch(
            ctx.cfg,
            ctx.store,
            a.batch,
            confirm=a.yes,
            only_ids=a.ids,
            progress=_progress_printer("upload"),
        )
    except pipeline.UploadRefused as exc:
        out(str(exc))
        return 1
    out(f"Uploaded {len(summary.uploaded)}, failed {len(summary.failed)}.")
    for name, err in summary.failed.items():
        out(f"  {name}: {err}")
    out(f"Next: shuto export {a.batch}")
    return 1 if summary.failed else 0


def cmd_export(ctx: Ctx) -> int:
    a = ctx.args
    summary = pipeline.export_batch(ctx.cfg, ctx.store, a.batch, dry_run=a.dry_run)
    if a.dry_run:
        out(f"Dry run: {len(summary.rows)} row(s) would be exported.")
        for row in summary.rows:
            out(f"  {row['filename']}  {'editorial' if row['editorial'] else 'commercial'}")
        return 0
    for f in summary.files:
        out(f"Wrote {f}")
    if not summary.files:
        out("Nothing to export (photos must be uploaded first).")
        return 0
    out("Final step is manual: on shutterstock.com choose Submit and upload this CSV there.")
    out(f"Afterwards: shuto submitted {a.batch}")
    return 0


def cmd_submitted(ctx: Ctx) -> int:
    ids = pipeline.mark_submitted(ctx.store, ctx.args.batch)
    out(f"Marked {len(ids)} photo(s) as submitted.")
    return 0


def cmd_status(ctx: Ctx) -> int:
    rows = pipeline.batch_report(ctx.store, ctx.args.batch)
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
    photo = pipeline.find_photo(ctx.store, a.file)
    assert photo.id is not None
    pipeline.record_result(ctx.store, photo.id, False, a.reason, a.note)
    out(
        f"Recorded rejection ({a.reason}) for {photo.orig_name}; later prompts will account for it."
    )
    return 0


def cmd_accept(ctx: Ctx) -> int:
    photo = pipeline.find_photo(ctx.store, ctx.args.file)
    assert photo.id is not None
    pipeline.record_result(ctx.store, photo.id, True)
    out(f"Recorded acceptance for {photo.orig_name}.")
    return 0


def cmd_secrets(ctx: Ctx) -> int:
    name = ctx.args.name
    if name not in KNOWN_SECRETS:
        out(f"Unknown secret {name!r}. Known: {', '.join(KNOWN_SECRETS)}")
        return 2
    value = getpass.getpass(f"{name}: ")
    if not value:
        out("Empty value; nothing saved.")
        return 1
    set_secret(name, value)
    out(f"Saved {name} in the OS keyring.")
    return 0


def cmd_check_ftps(ctx: Ctx) -> int:
    creds = deliver.creds_from_secrets(ctx.cfg.deliver.ftps_host)
    ftp = ftps.connect(creds.host, creds.username, creds.password)
    first = [ftp]  # hand the login we already have to upload_files once (one login, not two)

    def connect(*args, **kwargs):
        return first.pop() if first else ftps.connect(*args, **kwargs)

    try:
        out(f"Connected and logged in to {creds.host}.")
        if ctx.args.upload:
            path = Path(ctx.args.upload)
            errors: list[str] = []
            deliver.upload_files(
                creds,
                [path],
                lambda name, exc: (
                    errors.append(f"{name}: {exc}") if exc else out(f"Uploaded {name}")
                ),
                connect=connect,
            )
            if errors:
                out("\n".join(errors))
                return 1
    finally:
        ftps.close(ftp)
    return 0


def cmd_restrictions(ctx: Ctx) -> int:
    r = pipeline.load_restrictions(ctx.cfg)
    if ctx.args.restrictions_cmd == "list":
        rules = [x for x in r.rules if not ctx.args.level or x.level == ctx.args.level]
        out(
            table(
                ["id", "level", "category", "name"],
                [(x.id, x.level, x.category, x.name) for x in rules],
            )
        )
        out(f"{len(rules)} rule(s), {len(r.trademark_terms)} trademark term(s).")
        return 0
    meta = PhotoMetadata(filename="check", description=ctx.args.text, keywords=[], categories=[])
    _, result = guardrails.check(meta, {}, r)
    out(f"Result: {result.action}")
    for m in result.messages:
        out(f"  {m}")
    return 0


HANDLERS = {
    "ui": cmd_ui,
    "init": cmd_init,
    "doctor": cmd_doctor,
    "models": cmd_models,
    "import": cmd_import,
    "ledger": cmd_ledger,
    "cull": cmd_cull,
    "describe": cmd_describe,
    "run": cmd_run,
    "review": cmd_review,
    "edit": cmd_edit,
    "approve": cmd_approve,
    "prepare": cmd_prepare,
    "upload": cmd_upload,
    "export": cmd_export,
    "submitted": cmd_submitted,
    "status": cmd_status,
    "reject": cmd_reject,
    "accept": cmd_accept,
    "secrets": cmd_secrets,
    "check-ftps": cmd_check_ftps,
    "restrictions": cmd_restrictions,
}


# --------------------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", default=argparse.SUPPRESS, help="path to shuto.yaml")
    common.add_argument("-v", "--verbose", action="count", default=argparse.SUPPRESS)

    p = argparse.ArgumentParser(
        prog="shuto",
        parents=[common],
        description="Local-first pipeline for Shutterstock contributors. "
        "With no command, starts the web UI.",
    )
    sub = p.add_subparsers(dest="command")

    def add(name: str, help_: str) -> argparse.ArgumentParser:
        return sub.add_parser(name, parents=[common], help=help_, description=help_)

    s = add("ui", "start the local web UI")
    s.add_argument("--host")
    s.add_argument("--port", type=int)
    s.add_argument("--no-browser", action="store_true")

    add("init", "create the workspace and choose a model (asks before any download)")
    add("doctor", "show hardware, Ollama and model status")

    s = add("models", "list, choose, download and benchmark local models")
    ms = s.add_subparsers(dest="models_cmd")
    ms.add_parser("list", parents=[common], help="top 8 local models for this machine")
    m = ms.add_parser("pull", parents=[common], help="download a model with Ollama (asks first)")
    m.add_argument("tag")
    m.add_argument("--yes", action="store_true", help="skip the confirmation")
    m = ms.add_parser("use", parents=[common], help="remember your choice: TAG or PROVIDER:MODEL")
    m.add_argument("spec")
    m = ms.add_parser("bench", parents=[common], help="time one model on a sample image")
    m.add_argument("model", nargs="?")

    s = add("import", "register photos from a folder")
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

    s = add("ledger", "export or import the list of already uploaded photos (uploaded.csv)")
    ls = s.add_subparsers(dest="ledger_cmd", required=True)
    m = ls.add_parser("export", parents=[common], help="write the ledger CSV from the database")
    m.add_argument("path", nargs="?", help="default: <workspace>/uploaded.csv")
    m = ls.add_parser("import", parents=[common], help="merge a ledger CSV into the database")
    m.add_argument("path")

    s = add("cull", "technical check: size, sharpness, exposure")
    s.add_argument("batch")
    s.add_argument("--explain", action="store_true")

    s = add("describe", "generate title, description and keywords (shows an estimate first)")
    s.add_argument("batch")
    s.add_argument("--ids", type=int, nargs="+")
    s.add_argument("--yes", action="store_true", help="skip the estimate confirmation")
    s.add_argument("--provider")
    s.add_argument("--model")

    s = add("run", "import, cull and describe in one go")
    s.add_argument("path")
    s.add_argument("--notes")
    s.add_argument("--yes", action="store_true", help="skip the estimate confirmation")
    s.add_argument("--provider")
    s.add_argument("--model")

    s = add("review", "list described photos")
    s.add_argument("batch")
    s.add_argument("--status", choices=[x.value for x in Status])
    s.add_argument("--full", action="store_true")

    s = add("edit", "edit metadata of one photo (guardrails re-run)")
    s.add_argument("id", type=int)
    s.add_argument("--description")
    s.add_argument("--title")
    s.add_argument("--keywords", help="comma separated")
    s.add_argument("--categories", help="comma separated")
    s.add_argument("--editorial", action=argparse.BooleanOptionalAction, default=None)
    s.add_argument("--mature", action=argparse.BooleanOptionalAction, default=None)
    s.add_argument("--release", action="append", help="release id; prefix property: for property")

    s = add("approve", "approve reviewed photos")
    s.add_argument("batch", nargs="?")
    s.add_argument("--ids", type=int, nargs="+")
    s.add_argument("--include-review", action="store_true", help="also approve needs_review")

    s = add("prepare", "write upload-ready JPEG copies with embedded metadata")
    s.add_argument("batch")
    s.add_argument("--ids", type=int, nargs="+")

    s = add("upload", "FTPS upload of prepared photos (needs deliver.upload_enabled or --yes)")
    s.add_argument("batch")
    s.add_argument("--ids", type=int, nargs="+")
    s.add_argument("--yes", action="store_true", help="confirm this upload")

    s = add("export", "write the Shutterstock CSV files")
    s.add_argument("batch")
    s.add_argument("--dry-run", action="store_true")

    s = add("submitted", "mark an exported batch as submitted on the portal")
    s.add_argument("batch")

    s = add("status", "batches and photo counts")
    s.add_argument("batch", nargs="?")

    s = add("reject", "record a rejection and its reason")
    s.add_argument("file", help="photo id, original file name or prepared name")
    s.add_argument("--reason", required=True, choices=REASON_CODES)
    s.add_argument("--note")

    s = add("accept", "record an acceptance")
    s.add_argument("file")

    s = add("secrets", "store a secret in the OS keyring")
    ss = s.add_subparsers(dest="secrets_cmd", required=True)
    m = ss.add_parser("set", parents=[common], help="prompt for a value and save it")
    m.add_argument("name")

    s = add("check-ftps", "test the Shutterstock FTPS login")
    s.add_argument("--upload", metavar="FILE", help="also upload this file")

    s = add("restrictions", "inspect the content restriction rules")
    rs = s.add_subparsers(dest="restrictions_cmd", required=True)
    m = rs.add_parser("list", parents=[common], help="list rules")
    m.add_argument("--level")
    m = rs.add_parser("check", parents=[common], help="check a piece of text")
    m.add_argument("text")
    return p


def _setup_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass


def main(argv: Sequence[str] | None = None) -> int:
    _setup_console()
    args = build_parser().parse_args(argv)
    verbose = getattr(args, "verbose", 0) or 0
    command = args.command or "ui"
    try:
        return HANDLERS[command](Ctx(args))
    except local_models.ModelChoiceRequired:
        out(no_model_message())
        return 2
    except (ConfigError, PipelineError, LLMError, RuntimeError) as exc:
        if verbose:
            traceback.print_exc()
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
