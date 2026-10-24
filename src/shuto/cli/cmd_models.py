"""Model commands: models list/use/pull/bench, plus the shared model and Ollama messages."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from typing import TYPE_CHECKING

from shuto.ai import catalog as local_models
from shuto.ai import hardware, selection
from shuto.ai import model_choice as ai_choice
from shuto.ai import recommend as ai_recommend
from shuto.cli._io import (
    COMMON,
    _confirm,
    add_command,
    out,
    table,
)
from shuto.core import paths as core_paths
from shuto.core.config import Config
from shuto.core.text import yes_no
from shuto.ollama import OLLAMA_DOWNLOAD_URL
from shuto.workflow import models as workflow_models

if TYPE_CHECKING:
    from shuto.cli import Ctx
    from shuto.store import Store


def print_models_table(
    rows: Sequence[ai_recommend.ModelRow],
    *,
    show_all: bool = False,
    selected: str | None = None,
) -> None:
    """The models rated for this computer. Without `show_all` only Good ones (or the best OK
    ones when none is Good) and the model in use are listed."""
    shown, _ = ai_recommend.visible_rows(rows, selected, show_all)
    body = [
        (
            r.rank,
            r.tag + (" *" if r.recommended else ""),
            f"{r.size_gb:g} GB",
            "not measured"
            if r.basis == "unknown"
            else f"{r.est_seconds_per_image:g} s ({r.basis})",
            r.tier_label,
            r.rating_label.removesuffix(" for this computer"),
            yes_no(r.fits_disk),
            yes_no(r.pulled),
        )
        for r in shown
    ]
    out(
        table(
            [
                "#",
                "model",
                "download",
                "est. per image",
                "quality / speed",
                "rating",
                "fits disk",
                "pulled",
            ],
            body,
        )
    )
    out("* = best match for this machine. Estimates are heuristics until `shuto models bench`.")
    for r in shown:
        if r.rating == "not_recommended":
            out(f"{r.tag} is not recommended for this computer: {r.rating_reason}")
    hidden = len(rows) - len(shown)
    if hidden:
        out(
            f"Hidden: {hidden} model{'' if hidden == 1 else 's'} not recommended for this "
            "computer (see --all)."
        )
    out("Pick one with: shuto models use <model>   (nothing is downloaded until you confirm).")


def print_disk_advice(adv: ai_recommend.DiskAdvice) -> None:
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
    return f"Ollama is needed for local models. Download: {OLLAMA_DOWNLOAD_URL}\n  {steps}"


def no_model_message() -> str:
    return (
        "No local model chosen yet. shuto never picks one silently.\n"
        "See the models that suit this machine with `shuto models list`, then choose with "
        "`shuto models use <model>`."
    )


def show_model_choice(cfg: Config, store: Store | None = None) -> None:
    out(no_model_message())
    out()
    try:
        print_models_table(workflow_models.list_models(cfg, store))
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
        rows = workflow_models.list_models(cfg, ctx.store)
        sel = _selected_or_none(ctx)
        print_models_table(
            rows,
            show_all=a.all,
            selected=hardware.normalize_tag(sel[7:]) if sel and sel.startswith("ollama:") else None,
        )
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
        out(f"cold {rec['cold_s']} s, warm {rec['warm_s']} s, valid JSON: {yes_no(rec['ok_json'])}")
        out(f"Saved to {core_paths.benchmark_cache_path()}")
        return 0
    raise AssertionError(sub)


def _selected_or_none(ctx: Ctx) -> str | None:
    try:
        sel = selection.resolve_selection(ctx.cfg, ctx.store)
    except ai_choice.ModelChoiceRequired:
        return None
    return f"{sel.provider}:{sel.model}"


def _add_models(sub: argparse._SubParsersAction) -> None:
    s = add_command(sub, "models", "list, choose, download and benchmark local models")
    ms = s.add_subparsers(dest="models_cmd")
    m = ms.add_parser("list", parents=[COMMON], help="local models rated for this machine")
    m.add_argument("--all", action="store_true", help="also show models not recommended")
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
