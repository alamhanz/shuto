"""Command line interface. Plain text output (no ANSI, ASCII only) so any console works.

The commands live in the `cmd_*` modules; this file holds the entry point, the shared context and
the names tests replace through the `shuto.cli` namespace (`_input`, `load_config`,
`find_config_path`, `delete_secret`, `keyring_available`, `default_config_dir`).
"""

from __future__ import annotations

import argparse
import sys
import traceback
import webbrowser
from collections.abc import Sequence

from shuto import store as store_pkg
from shuto.ai import catalog as local_models
from shuto.ai.providers.base import LLMError
from shuto.cli._io import COMMON, _short, _yn, out, table
from shuto.cli.cmd_app import (
    PARSERS as _APP_PARSERS,
)
from shuto.cli.cmd_app import (
    _installed_version,
    cmd_check_ftps,
    cmd_doctor,
    cmd_init,
    cmd_restrictions,
    cmd_secrets,
    cmd_shortcut,
    cmd_ui,
    cmd_uninstall,
    data_targets,
)
from shuto.cli.cmd_deliver import (
    PARSERS as _DELIVER_PARSERS,
)
from shuto.cli.cmd_deliver import (
    cmd_export,
    cmd_prepare,
    cmd_report,
    cmd_reupload,
    cmd_submitted,
    cmd_upload,
)
from shuto.cli.cmd_models import (
    OLLAMA_URL,
    cmd_models,
    no_model_message,
    ollama_guidance,
    print_disk_advice,
    print_models_table,
    show_model_choice,
)
from shuto.cli.cmd_models import (
    PARSERS as _MODELS_PARSERS,
)
from shuto.cli.cmd_photos import (
    PARSERS as _PHOTOS_PARSERS,
)
from shuto.cli.cmd_photos import (
    cmd_accept,
    cmd_approve,
    cmd_cull,
    cmd_describe,
    cmd_edit,
    cmd_import,
    cmd_ledger,
    cmd_reject,
    cmd_review,
    cmd_run,
    cmd_status,
)
from shuto.cli.cmd_rules import PARSERS as _RULES_PARSERS
from shuto.cli.cmd_rules import cmd_rules
from shuto.core.config import (
    Config,
    ConfigError,
    default_config_dir,
    find_config_path,
    load_config,
)
from shuto.core.errors import PipelineError
from shuto.core.secrets import delete_secret, keyring_available
from shuto.settings import effective
from shuto.store import Store
from shuto.system import launcher

__all__ = [
    "COMMON",
    "Config",
    "Ctx",
    "HANDLERS",
    "OLLAMA_URL",
    "build_parser",
    "cmd_accept",
    "cmd_approve",
    "cmd_check_ftps",
    "cmd_cull",
    "cmd_describe",
    "cmd_doctor",
    "cmd_edit",
    "cmd_export",
    "cmd_import",
    "cmd_init",
    "cmd_ledger",
    "cmd_models",
    "cmd_prepare",
    "cmd_reject",
    "cmd_report",
    "cmd_restrictions",
    "cmd_reupload",
    "cmd_review",
    "cmd_rules",
    "cmd_run",
    "cmd_secrets",
    "cmd_shortcut",
    "cmd_status",
    "cmd_submitted",
    "cmd_ui",
    "cmd_uninstall",
    "cmd_upload",
    "data_targets",
    "default_config_dir",
    "delete_secret",
    "find_config_path",
    "keyring_available",
    "launcher",
    "load_config",
    "main",
    "no_model_message",
    "ollama_guidance",
    "out",
    "print_disk_advice",
    "print_models_table",
    "show_model_choice",
    "table",
    "webbrowser",
    "_installed_version",
    "_short",
    "_yn",
    "_input",
]


def _input(prompt: str) -> str:
    """Single place for interactive input (tests replace it)."""
    return input(prompt)


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
            self._store = store_pkg.open_store(self.cfg)
        return self._store

    @property
    def run_cfg(self) -> Config:
        """The config plus the style, theme and rules saved in the web app (shared with it)."""
        if self._run_cfg is None:
            self._run_cfg = effective.apply_style_overrides(self.cfg, self.store)
        return self._run_cfg


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
    "reupload": cmd_reupload,
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

# Sub-commands in the order `shuto --help` lists them.
_ORDER = (
    "ui",
    "shortcut",
    "uninstall",
    "init",
    "doctor",
    "models",
    "import",
    "ledger",
    "cull",
    "describe",
    "run",
    "review",
    "edit",
    "approve",
    "prepare",
    "upload",
    "reupload",
    "export",
    "submitted",
    "status",
    "rules",
    "report",
    "reject",
    "accept",
    "secrets",
    "check-ftps",
    "restrictions",
)
_PARSERS = {
    **_APP_PARSERS,
    **_MODELS_PARSERS,
    **_PHOTOS_PARSERS,
    **_DELIVER_PARSERS,
    **_RULES_PARSERS,
}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="shuto",
        parents=[COMMON],
        description="Local-first pipeline for Shutterstock contributors. "
        "With no command, starts the web UI.",
    )
    p.add_argument("--version", action="version", version=f"shuto {_installed_version()}")
    sub = p.add_subparsers(dest="command")
    for name in _ORDER:
        _PARSERS[name](sub)
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
