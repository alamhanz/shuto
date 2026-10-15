"""The rules command: your own photo rules and the theme check."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from typing import TYPE_CHECKING

from shuto.ai import filters as filters_mod
from shuto.cli._io import (
    COMMON,
    add_command,
    out,
    table,
)
from shuto.core.config import Config
from shuto.core.errors import PipelineError
from shuto.settings import ui as settings_ui
from shuto.workflow import checks as workflow_checks

if TYPE_CHECKING:
    from shuto.cli import Ctx


def _rule_dicts(rules: Sequence[filters_mod.UserRule]) -> list[dict[str, str]]:
    return [{"id": r.id, "kind": r.kind, "text": r.text, "mode": r.mode} for r in rules]


def _mode_words(mode: str) -> str:
    return "set aside" if mode == "set_aside" else "just flag"


def _print_rules(cfg: Config) -> None:
    checks = workflow_checks.active_checks(cfg)
    spec_theme = cfg.style.theme.strip()
    out(
        f"Theme check: {'on' if cfg.filters.theme_check else 'off'}"
        + (f' (theme "{spec_theme}")' if spec_theme else " (no theme set, nothing to check)")
    )
    partly = "needs a look" if cfg.filters.theme_partly == "flag" else "let through"
    out(f"Theme fits partly: {partly}")
    rules = settings_ui.get_filter_rules(cfg)
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
    rules = list(settings_ui.get_filter_rules(ctx.run_cfg))
    if cmd == "list":
        _print_rules(ctx.run_cfg)
        return 0
    if cmd == "add":
        kind = "avoid" if a.avoid is not None else "include"
        text = a.avoid if a.avoid is not None else a.include
        mode = "flag" if a.flag else "set_aside"
        added = {"kind": kind, "text": text, "mode": mode}
        stored = settings_ui.set_ui_setting(
            store, "filters", {"rules": [*_rule_dicts(rules), added]}
        )
        new = stored["rules"][-1] if stored["rules"] else added
        out(f"Added rule {new.get('id', '')}: {kind} {new['text']!r} ({_mode_words(mode)}).")
    elif cmd == "remove":
        kept = [r for r in rules if r.id != a.id]
        if len(kept) == len(rules):
            raise PipelineError(f"There is no rule with id {a.id!r}. See: shuto rules list")
        settings_ui.set_ui_setting(store, "filters", {"rules": _rule_dicts(kept)})
        out(f"Removed rule {a.id}.")
    elif cmd == "clear":
        settings_ui.set_ui_setting(store, "filters", {"rules": []})
        out("Removed all rules.")
    elif cmd == "mode":
        mode = a.mode.replace("-", "_")
        found = [r for r in rules if r.id == a.id]
        if not found:
            raise PipelineError(f"There is no rule with id {a.id!r}. See: shuto rules list")
        changed = [{**d, "mode": mode} if d["id"] == a.id else d for d in _rule_dicts(rules)]
        settings_ui.set_ui_setting(store, "filters", {"rules": changed})
        out(f"Rule {a.id} will now {'set photos aside' if mode == 'set_aside' else 'just flag'}.")
    elif cmd == "theme":
        settings_ui.set_ui_setting(store, "filters", {"theme_check": a.state == "on"})
        out(f"Theme check is {a.state}.")
    elif cmd == "partly":
        settings_ui.set_ui_setting(store, "filters", {"theme_partly": a.how})
        out(
            "Photos that fit the theme partly are "
            + ("let through." if a.how == "pass" else "flagged.")
        )
    out(
        "This applies to photos described from now on; photos already described keep their results."
    )
    return 0


def _add_rules(sub: argparse._SubParsersAction) -> None:
    s = add_command(
        sub, "rules", "your own photo rules and the theme check (shared with the web app)"
    )
    rl = s.add_subparsers(dest="rules_cmd")
    rl.add_parser("list", parents=[COMMON], help="show the theme check and your rules")
    m = rl.add_parser("add", parents=[COMMON], help="add a rule (up to 10)")
    g = m.add_mutually_exclusive_group(required=True)
    g.add_argument(
        "--avoid", metavar="TEXT", help='photos showing this are a problem, e.g. "people"'
    )
    g.add_argument("--include", metavar="TEXT", help="photos must show this")
    m.add_argument(
        "--flag", action="store_true", help='only flag ("Needs a look"), do not set aside'
    )
    m = rl.add_parser("remove", parents=[COMMON], help="remove one rule by its id")
    m.add_argument("id")
    rl.add_parser("clear", parents=[COMMON], help="remove all your rules")
    m = rl.add_parser("mode", parents=[COMMON], help="what happens when a rule fails")
    m.add_argument("id")
    m.add_argument("mode", choices=["set-aside", "flag"])
    m = rl.add_parser("theme", parents=[COMMON], help="turn the theme check on or off")
    m.add_argument("state", choices=["on", "off"])
    m = rl.add_parser("partly", parents=[COMMON], help='photos that fit the theme "partly"')
    m.add_argument("how", choices=["pass", "flag"], help="pass = let through, flag = needs a look")


PARSERS = {
    "rules": _add_rules,
}
