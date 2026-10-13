"""Command line interface. Plain text output (no ANSI, ASCII only) so any console works."""

from __future__ import annotations

import argparse
import getpass
import os
import shutil
import subprocess
import sys
import traceback
import webbrowser
from collections.abc import Sequence
from pathlib import Path

from . import deliver, ftps, guardrails, launcher, local_models, pipeline
from . import filters as filters_mod
from . import report as report_mod
from .config import Config, ConfigError, default_config_dir, find_config_path, load_config
from .llm.base import LLMError
from .metadata import PhotoMetadata
from .models import REASON_CODES, Status
from .pipeline import PipelineError
from .secrets import KNOWN_SECRETS, delete_secret, keyring_available, set_secret
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
        try:
            self.cfg = load_config(getattr(args, "config", None))
        except ConfigError:
            if getattr(args, "command", None) != "uninstall":
                raise
            self.cfg = Config()  # a broken config must not block uninstalling
        self._store: Store | None = None
        self._run_cfg: Config | None = None

    @property
    def store(self) -> Store:
        if self._store is None:
            self._store = pipeline.open_store(self.cfg)
        return self._store

    @property
    def run_cfg(self) -> Config:
        """The config plus the style, theme and rules saved in the web app (shared with it)."""
        if self._run_cfg is None:
            self._run_cfg = pipeline.apply_style_overrides(self.cfg, self.store)
        return self._run_cfg


def _progress_printer(label: str):
    state = {"last": -1}

    def report(done: int, total: int, message: str = "") -> None:
        if total and done != state["last"]:
            state["last"] = done
            out(f"{label} {done}/{total} {message}".rstrip())

    return report


# --------------------------------------------------------------------------- setup commands


def _installed_version() -> str:
    from .updates import current_version

    return current_version()


def cmd_ui(ctx: Ctx) -> int:
    try:
        from .web import serve
    except ImportError as exc:
        out(f"The web UI is not available in this install ({exc}).")
        return 1
    a = ctx.args
    web = ctx.cfg.web
    host = getattr(a, "host", None) or web.host
    port = getattr(a, "port", None) or web.port
    no_browser = getattr(a, "no_browser", False)
    launch_args = ["ui", "--host", host, "--port", str(port)]
    if ctx.config_path:
        launch_args += ["--config", str(ctx.config_path)]
    launch_args += ["-v"] * (getattr(a, "verbose", 0) or 0)
    if launcher.is_running(host, port):
        out("shuto is already running.")
        if not no_browser:
            webbrowser.open(f"http://{host}:{port}/")
        return 0
    serve(
        ctx.cfg,
        host=host,
        port=port,
        open_browser=web.open_browser and not no_browser,
        config_path=str(ctx.config_path) if ctx.config_path else None,
        launch_args=launch_args,
    )
    return 0


def cmd_shortcut(ctx: Ctx) -> int:
    a = ctx.args
    if a.remove:
        removed = launcher.remove_shortcuts()
        for path in removed:
            out(f"Removed {path}")
        if not removed:
            out("No shuto shortcuts found.")
        return 0
    try:
        made = launcher.create_shortcuts(desktop=not a.no_desktop, menu=not a.no_menu)
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"could not create the shortcut ({exc})") from exc
    for path in made:
        out(f"Created {path}")
    return 0


_WORKSPACE_ENTRIES = (
    "shuto.db",
    "shuto.db-wal",
    "shuto.db-shm",
    "shuto.db-journal",
    "thumbs",
    "previews",
    "prepared",
    "exports",
    "batches",
    "logs",
    "uploaded.csv",
)
_LOGIN_SECRETS = ("SHUTO_FTPS_USER", "SHUTO_FTPS_PASSWORD")


def data_targets(cfg: Config) -> list[Path]:
    """What `--delete-data` removes. Only shuto's own files: in the workspace just the known
    entries (a custom workspace may be a folder the user also keeps other things in)."""
    found = [p for p in (cfg.workspace_dir / n for n in _WORKSPACE_ENTRIES) if p.exists()]
    found += [
        d for d in (default_config_dir(), pipeline.benchmark_cache_path().parent) if d.is_dir()
    ]
    return _drop_nested(found)


def _drop_nested(paths: list[Path]) -> list[Path]:
    """Remove duplicates and entries that live inside another entry (it goes away with its
    parent). Symlinks are compared by their own location, never followed."""
    keys = [Path(os.path.abspath(p)) for p in paths]
    kept: list[Path] = []
    for path, key in zip(paths, keys, strict=True):
        nested = any(other != key and other in key.parents for other in keys)
        if not nested and path not in kept:
            kept.append(path)
    return kept


def _remove_path(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()


def _delete_data(cfg: Config) -> None:
    for path in data_targets(cfg):
        if not (path.exists() or path.is_symlink()):
            continue
        try:
            _remove_path(path)
            out(f"Removed {path}")
        except OSError as exc:
            out(f"Could not remove {path} ({exc}).")
    workspace = cfg.workspace_dir
    if workspace.is_dir() and not any(workspace.iterdir()):
        try:
            workspace.rmdir()
        except OSError:
            pass


def cmd_uninstall(ctx: Ctx) -> int:
    a = ctx.args
    cfg = ctx.cfg
    host, port = cfg.web.host, cfg.web.port
    if launcher.is_running(host, port):
        out(f"shuto is still open (http://{host}:{port}/). Close it first, then run this again.")
        return 1
    out("This will remove shuto from this computer:")
    out("  - the shuto icons on your Desktop and in your menu")
    for path in launcher.list_shortcuts():
        out(f"      {path}")
    if a.keep_login:
        out("  - (your saved Shutterstock login stays: --keep-login)")
    else:
        out("  - your saved Shutterstock login (stored in this computer's keyring)")
    out("Kept unless you choose to delete them: shuto's own data (database, thumbnails,")
    out("settings, saved model choice), in these places:")
    for path in data_targets(cfg):
        out(f"      {path}")
    out("Never touched: your photos, your source folders, Ollama and its downloaded models.")
    if not a.yes and not _confirm("Remove shuto now?"):
        out("Cancelled. Nothing was changed.")
        return 1
    delete_data = a.delete_data
    if not delete_data and not a.yes:
        delete_data = _confirm("Also delete shuto's own data (database, thumbnails, settings)?")

    removed = launcher.remove_shortcuts()
    for path in removed:
        out(f"Removed {path}")
    if not removed:
        out("No shuto shortcuts found.")

    if not a.keep_login:
        if not keyring_available():
            out("No keyring on this computer, so no saved login to remove.")
        else:
            failed = False
            for name in _LOGIN_SECRETS:
                try:
                    delete_secret(name)
                except RuntimeError as exc:
                    failed = True
                    out(str(exc))
            if not failed:
                out("Removed the saved Shutterstock login.")

    if delete_data:
        _delete_data(cfg)
    else:
        out("Kept shuto's data. Run `shuto uninstall --delete-data` to delete it too.")

    out()
    out("Last step: remove the shuto program itself (a program cannot delete itself while")
    out("it is running, so run this yourself):")
    out("    uv tool uninstall shuto")
    out("Ollama and its models are not touched. To remove a model: ollama rm <model>.")
    out("To remove Ollama, use your system's own uninstaller (Settings > Apps on Windows).")
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
    report = pipeline.doctor(ctx.run_cfg, ctx.store, config_path=ctx.config_path)
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
    if report.selection_unreadable:
        out("Model:     (the AI model choice could not be read)")
    else:
        out(f"Model:     {report.selected or '(not chosen)'}")
    if report.checks:
        out("Checks:    " + report.checks[0])
        for line in report.checks[1:]:
            out(f"           {line}")
    else:
        out("Checks:    none (no theme check and no rules of your own)")
    out(f"Place:     {report.default_place or 'no default place (editorial captions say UNKNOWN)'}")
    out(f"Model choice file: {pipeline.model_choice_path()}")
    out(f"Benchmark cache:   {pipeline.benchmark_cache_path()}")
    if report.selected is None and report.provider == "ollama" and not report.selection_unreadable:
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
    if len(summary.batches) > 1:
        out(pipeline.batches_text(summary.batches))
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
    cfg, store = ctx.run_cfg, ctx.store
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
    summary = pipeline.import_folder(
        ctx.cfg, ctx.store, a.path, notes=a.notes, progress=_progress_printer("scan")
    )
    _print_import(summary)
    code = 0
    for batch in sorted(summary.batches):
        out(f"== batch {batch} ==")
        _print_cull(pipeline.cull_batch(ctx.run_cfg, ctx.store, batch), explain=False)
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
        for line in filters_mod.messages(p.filters):
            out(f"  ! {line}")
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
    summary = pipeline.approve(
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


def cmd_prepare(ctx: Ctx) -> int:
    a = ctx.args
    summary = pipeline.prepare_batch(
        ctx.cfg, ctx.store, a.batch, only_ids=a.ids, progress=_progress_printer("prepare")
    )
    out(
        f"Prepared {len(summary.prepared)} file(s) in "
        f"{pipeline.batch_dir(ctx.cfg, a.batch) / 'prepared'}."
    )
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
    out(f"Batch folder: {pipeline.batch_dir(ctx.cfg, a.batch)}")
    out("Final step is manual: on shutterstock.com choose Submit and upload this CSV there.")
    out(f"Afterwards: shuto submitted {a.batch}")
    return 0


def cmd_submitted(ctx: Ctx) -> int:
    ids = pipeline.mark_submitted(ctx.store, ctx.args.batch, cfg=ctx.cfg)
    out(f"Marked {len(ids)} photo(s) as submitted.")
    return 0


def cmd_report(ctx: Ctx) -> int:
    a = ctx.args
    if not ctx.store.list_photos(a.batch):
        raise PipelineError(f"There are no photos in batch {a.batch!r}.")
    if a.out:
        header, records = pipeline.build_report(ctx.cfg, ctx.store, a.batch)
        path = report_mod.write_report(Path(a.out).expanduser(), header, records)
    else:
        path = pipeline.write_report(ctx.cfg, ctx.store, a.batch)
    out(f"Report for {a.batch}: {path}")
    out(f"Batch folder: {pipeline.batch_dir(ctx.cfg, a.batch)}")
    return 0


def _rule_dicts(rules: Sequence[filters_mod.UserRule]) -> list[dict[str, str]]:
    return [{"id": r.id, "kind": r.kind, "text": r.text, "mode": r.mode} for r in rules]


def _mode_words(mode: str) -> str:
    return "set aside" if mode == "set_aside" else "just flag"


def _print_rules(cfg: Config) -> None:
    checks = pipeline.active_checks(cfg)
    spec_theme = cfg.style.theme.strip()
    out(
        f"Theme check: {'on' if cfg.filters.theme_check else 'off'}"
        + (f' (theme "{spec_theme}")' if spec_theme else " (no theme set, nothing to check)")
    )
    partly = "needs a look" if cfg.filters.theme_partly == "flag" else "let through"
    out(f"Theme fits partly: {partly}")
    rules = pipeline.get_filter_rules(cfg)
    if rules:
        out(
            table(
                ["id", "rule", "when it fails"],
                [[r.id, r.label(), _mode_words(r.mode)] for r in rules],
            )
        )
    else:
        out("No rules of your own yet. Add one: shuto rules add --avoid people")
    out(f"Rules apply to photos described from now on ({len(checks)} active check(s)).")


def cmd_rules(ctx: Ctx) -> int:
    a = ctx.args
    cmd = a.rules_cmd or "list"
    store = ctx.store
    rules = list(pipeline.get_filter_rules(ctx.run_cfg))
    if cmd == "list":
        _print_rules(ctx.run_cfg)
        return 0
    if cmd == "add":
        kind = "avoid" if a.avoid is not None else "include"
        text = a.avoid if a.avoid is not None else a.include
        mode = "flag" if a.flag else "set_aside"
        added = {"kind": kind, "text": text, "mode": mode}
        stored = pipeline.set_ui_setting(store, "filters", {"rules": [*_rule_dicts(rules), added]})
        new = stored["rules"][-1] if stored["rules"] else added
        out(f"Added rule {new.get('id', '')}: {kind} {new['text']!r} ({_mode_words(mode)}).")
    elif cmd == "remove":
        kept = [r for r in rules if r.id != a.id]
        if len(kept) == len(rules):
            raise PipelineError(f"There is no rule with id {a.id!r}. See: shuto rules list")
        pipeline.set_ui_setting(store, "filters", {"rules": _rule_dicts(kept)})
        out(f"Removed rule {a.id}.")
    elif cmd == "clear":
        pipeline.set_ui_setting(store, "filters", {"rules": []})
        out("Removed all rules.")
    elif cmd == "mode":
        mode = a.mode.replace("-", "_")
        found = [r for r in rules if r.id == a.id]
        if not found:
            raise PipelineError(f"There is no rule with id {a.id!r}. See: shuto rules list")
        changed = [{**d, "mode": mode} if d["id"] == a.id else d for d in _rule_dicts(rules)]
        pipeline.set_ui_setting(store, "filters", {"rules": changed})
        out(f"Rule {a.id} will now {'set photos aside' if mode == 'set_aside' else 'just flag'}.")
    elif cmd == "theme":
        pipeline.set_ui_setting(store, "filters", {"theme_check": a.state == "on"})
        out(f"Theme check is {a.state}.")
    elif cmd == "partly":
        pipeline.set_ui_setting(store, "filters", {"theme_partly": a.how})
        out(
            "Photos that fit the theme partly are "
            + ("let through." if a.how == "pass" else "flagged.")
        )
    out(
        "This applies to photos described from now on; photos already described keep their results."
    )
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
    pipeline.record_result(ctx.store, photo.id, False, a.reason, a.note, cfg=ctx.cfg)
    out(
        f"Recorded rejection ({a.reason}) for {photo.orig_name}; later prompts will account for it."
    )
    return 0


def cmd_accept(ctx: Ctx) -> int:
    photo = pipeline.find_photo(ctx.store, ctx.args.file)
    assert photo.id is not None
    pipeline.record_result(ctx.store, photo.id, True, cfg=ctx.cfg)
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
    "shortcut": cmd_shortcut,
    "uninstall": cmd_uninstall,
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
    "rules": cmd_rules,
    "report": cmd_report,
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
    p.add_argument("--version", action="version", version=f"shuto {_installed_version()}")
    sub = p.add_subparsers(dest="command")

    def add(name: str, help_: str) -> argparse.ArgumentParser:
        return sub.add_parser(name, parents=[common], help=help_, description=help_)

    s = add("ui", "start the local web UI")
    s.add_argument("--host")
    s.add_argument("--port", type=int)
    s.add_argument("--no-browser", action="store_true")

    s = add("shortcut", "create (or remove) the desktop and menu launchers")
    s.add_argument("--remove", action="store_true", help="delete the launchers")
    s.add_argument("--no-desktop", action="store_true", help="skip the Desktop launcher")
    s.add_argument("--no-menu", action="store_true", help="skip the Start menu / app menu launcher")

    s = add("uninstall", "remove shortcuts and saved login (asks first); optionally shuto's data")
    s.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    s.add_argument(
        "--delete-data", action="store_true", help="also delete shuto's database and settings"
    )
    s.add_argument("--keep-login", action="store_true", help="keep the saved Shutterstock login")

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

    s = add("rules", "your own photo rules and the theme check (shared with the web app)")
    rl = s.add_subparsers(dest="rules_cmd")
    rl.add_parser("list", parents=[common], help="show the theme check and your rules")
    m = rl.add_parser("add", parents=[common], help="add a rule (up to 10)")
    g = m.add_mutually_exclusive_group(required=True)
    g.add_argument(
        "--avoid", metavar="TEXT", help='photos showing this are a problem, e.g. "people"'
    )
    g.add_argument("--include", metavar="TEXT", help="photos must show this")
    m.add_argument(
        "--flag", action="store_true", help='only flag ("Needs a look"), do not set aside'
    )
    m = rl.add_parser("remove", parents=[common], help="remove one rule by its id")
    m.add_argument("id")
    rl.add_parser("clear", parents=[common], help="remove all your rules")
    m = rl.add_parser("mode", parents=[common], help="what happens when a rule fails")
    m.add_argument("id")
    m.add_argument("mode", choices=["set-aside", "flag"])
    m = rl.add_parser("theme", parents=[common], help="turn the theme check on or off")
    m.add_argument("state", choices=["on", "off"])
    m = rl.add_parser("partly", parents=[common], help='photos that fit the theme "partly"')
    m.add_argument("how", choices=["pass", "flag"], help="pass = let through, flag = needs a look")

    s = add("report", "write the report CSV of a batch (every check and its reason)")
    s.add_argument("batch")
    s.add_argument("--out", metavar="PATH", help="write it here instead of the workspace")

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
