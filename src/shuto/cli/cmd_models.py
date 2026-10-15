"""Model commands: models list/use/pull/bench, plus the shared model and Ollama messages."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from typing import TYPE_CHECKING

from shuto.ai import catalog as local_models
from shuto.ai import selection
from shuto.cli._io import (
    COMMON,
    _confirm,
    _yn,
    add_command,
    out,
    table,
)
from shuto.core import paths as core_paths
from shuto.core.config import Config
from shuto.workflow import models as workflow_models

if TYPE_CHECKING:
    from shuto.cli import Ctx


OLLAMA_URL = "https://ollama.com/download"


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
        print_models_table(workflow_models.list_models(cfg))
    except Exception as exc:
        out(f"(could not detect hardware: {exc})")


def _pull_with_confirmation(ctx: Ctx, tag: str, assume_yes: bool) -> int:
    cfg = ctx.cfg
    adv = workflow_models.model_disk_advice(cfg, tag)
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

    workflow_models.pull_model(cfg, tag, on_progress)
    out(f"Downloaded {tag}.")
    return 0


def cmd_models(ctx: Ctx) -> int:
    a = ctx.args
    sub = getattr(a, "models_cmd", None) or "list"
    cfg = ctx.cfg
    if sub == "list":
        rows = workflow_models.list_models(cfg)
        print_models_table(rows)
        sel = _selected_or_none(ctx)
        out(f"Current choice: {sel or '(none)'}")
        return 0
    if sub == "use":
        sel = workflow_models.use_model(cfg, ctx.store, a.spec)
        out(f"Saved choice: {sel.provider}:{sel.model}")
        if sel.provider == "ollama":
            adv = workflow_models.model_disk_advice(cfg, sel.model)
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
        model = a.model or selection.resolve_selection(cfg, ctx.store, provider="ollama").model
        out(f"Benchmarking {model} (two calls on a small sample image)...")
        rec = workflow_models.bench_model(cfg, ctx.store, model)
        out(f"cold {rec['cold_s']} s, warm {rec['warm_s']} s, valid JSON: {_yn(rec['ok_json'])}")
        out(f"Saved to {core_paths.benchmark_cache_path()}")
        return 0
    raise AssertionError(sub)


def _selected_or_none(ctx: Ctx) -> str | None:
    try:
        sel = selection.resolve_selection(ctx.cfg, ctx.store)
    except local_models.ModelChoiceRequired:
        return None
    return f"{sel.provider}:{sel.model}"


def _add_models(sub: argparse._SubParsersAction) -> None:
    s = add_command(sub, "models", "list, choose, download and benchmark local models")
    ms = s.add_subparsers(dest="models_cmd")
    ms.add_parser("list", parents=[COMMON], help="top 8 local models for this machine")
    m = ms.add_parser("pull", parents=[COMMON], help="download a model with Ollama (asks first)")
    m.add_argument("tag")
    m.add_argument("--yes", action="store_true", help="skip the confirmation")
    m = ms.add_parser("use", parents=[COMMON], help="remember your choice: TAG or PROVIDER:MODEL")
    m.add_argument("spec")
    m = ms.add_parser("bench", parents=[COMMON], help="time one model on a sample image")
    m.add_argument("model", nargs="?")


PARSERS = {
    "models": _add_models,
}
