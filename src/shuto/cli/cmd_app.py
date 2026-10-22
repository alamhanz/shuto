"""App commands: ui, shortcut, uninstall, init, doctor, secrets, check-ftps, restrictions."""

from __future__ import annotations

import argparse
import getpass
import os
import shutil
import subprocess
import webbrowser
from pathlib import Path
from typing import TYPE_CHECKING

from shuto import cli
from shuto.cli._io import (
    COMMON,
    _answer,
    _confirm,
    add_command,
    out,
    table,
)
from shuto.cli.cmd_models import (
    _pull_with_confirmation,
    no_model_message,
    ollama_guidance,
    print_disk_advice,
    print_models_table,
)
from shuto.core import paths as core_paths
from shuto.core.config import Config
from shuto.core.secrets import KNOWN_SECRETS, set_secret
from shuto.deliver import ftps, send
from shuto.metadata import guardrails
from shuto.metadata.model import PhotoMetadata
from shuto.system import launcher
from shuto.workflow import checks as workflow_checks
from shuto.workflow import models as workflow_models

if TYPE_CHECKING:
    from shuto.cli import Ctx


def _installed_version() -> str:
    from shuto.update.core import current_version

    return current_version()


def cmd_ui(ctx: Ctx) -> int:
    try:
        from shuto.web import serve
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
        d
        for d in (cli.default_config_dir(), core_paths.benchmark_cache_path().parent)
        if d.is_dir()
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
        if not cli.keyring_available():
            out("No keyring on this computer, so no saved login to remove.")
        else:
            failed = False
            for name in _LOGIN_SECRETS:
                try:
                    cli.delete_secret(name)
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


def _print_hardware(report: workflow_models.DoctorReport) -> None:
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
    report = workflow_models.doctor(ctx.run_cfg, ctx.store, config_path=ctx.config_path)
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
    if report.conflict:
        out(f"Warning:   {report.conflict.message.removesuffix(' Open Help for the steps.')}")
        for i, step in enumerate(report.conflict.steps, 1):
            out(f"           {i}. {step}")
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
    out(f"Model choice file: {core_paths.model_choice_path()}")
    out(f"Benchmark cache:   {core_paths.benchmark_cache_path()}")
    if report.selected is None and report.provider == "ollama" and not report.selection_unreadable:
        out()
        out(no_model_message())
        out()
        print_models_table(report.rows)
    return 0


def cmd_init(ctx: Ctx) -> int:
    cfg = ctx.cfg
    _ = ctx.store  # creates the workspace and database
    out(f"Workspace ready: {cfg.workspace_dir}")
    if ctx.config_path is None:
        out("No shuto.yaml found; using defaults (see shuto.example.yaml to customise).")
    report = workflow_models.doctor(cfg, ctx.store, config_path=ctx.config_path)
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
            sel = workflow_models.use_model(cfg, ctx.store, tag)
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
        adv = workflow_models.model_disk_advice(cfg, tag)
        if adv is not None and not adv.fits:
            print_disk_advice(adv)
        out(f"Start Ollama, then run `shuto models pull {tag}`.")
    out("Next: shuto import <folder>   (or `shuto` to open the web UI)")
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
    creds = send.creds_from_secrets(ctx.cfg.deliver.ftps_host)
    ftp = ftps.connect(creds.host, creds.username, creds.password)
    first = [ftp]  # hand the login we already have to upload_files once (one login, not two)

    def connect(*args, **kwargs):
        return first.pop() if first else ftps.connect(*args, **kwargs)

    try:
        out(f"Connected and logged in to {creds.host}.")
        if ctx.args.upload:
            path = Path(ctx.args.upload)
            errors: list[str] = []
            send.upload_files(
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
    r = workflow_checks.load_restrictions(ctx.cfg)
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


def _add_ui(sub: argparse._SubParsersAction) -> None:
    s = add_command(sub, "ui", "start the local web UI")
    s.add_argument("--host")
    s.add_argument("--port", type=int)
    s.add_argument("--no-browser", action="store_true")


def _add_shortcut(sub: argparse._SubParsersAction) -> None:
    s = add_command(sub, "shortcut", "create (or remove) the desktop and menu launchers")
    s.add_argument("--remove", action="store_true", help="delete the launchers")
    s.add_argument("--no-desktop", action="store_true", help="skip the Desktop launcher")
    s.add_argument("--no-menu", action="store_true", help="skip the Start menu / app menu launcher")


def _add_uninstall(sub: argparse._SubParsersAction) -> None:
    s = add_command(
        sub, "uninstall", "remove shortcuts and saved login (asks first); optionally shuto's data"
    )
    s.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    s.add_argument(
        "--delete-data", action="store_true", help="also delete shuto's database and settings"
    )
    s.add_argument("--keep-login", action="store_true", help="keep the saved Shutterstock login")


def _add_init(sub: argparse._SubParsersAction) -> None:
    add_command(sub, "init", "create the workspace and choose a model (asks before any download)")


def _add_doctor(sub: argparse._SubParsersAction) -> None:
    add_command(sub, "doctor", "show hardware, Ollama and model status")


def _add_secrets(sub: argparse._SubParsersAction) -> None:
    s = add_command(sub, "secrets", "store a secret in the OS keyring")
    ss = s.add_subparsers(dest="secrets_cmd", required=True)
    m = ss.add_parser("set", parents=[COMMON], help="prompt for a value and save it")
    m.add_argument("name")


def _add_check_ftps(sub: argparse._SubParsersAction) -> None:
    s = add_command(sub, "check-ftps", "test the Shutterstock FTPS login")
    s.add_argument("--upload", metavar="FILE", help="also upload this file")


def _add_restrictions(sub: argparse._SubParsersAction) -> None:
    s = add_command(sub, "restrictions", "inspect the content restriction rules")
    rs = s.add_subparsers(dest="restrictions_cmd", required=True)
    m = rs.add_parser("list", parents=[COMMON], help="list rules")
    m.add_argument("--level")
    m = rs.add_parser("check", parents=[COMMON], help="check a piece of text")
    m.add_argument("text")


PARSERS = {
    "ui": _add_ui,
    "shortcut": _add_shortcut,
    "uninstall": _add_uninstall,
    "init": _add_init,
    "doctor": _add_doctor,
    "secrets": _add_secrets,
    "check-ftps": _add_check_ftps,
    "restrictions": _add_restrictions,
}
