"""The CLI surface: every command, flag, default, and the help texts."""

from __future__ import annotations

import argparse
import re
import sys

import pytest

from . import check_json, check_text


def _seam():
    from shuto import cli

    return cli.build_parser


def _describe(action: argparse.Action) -> dict:
    default = action.default
    return {
        "flags": list(action.option_strings),
        "dest": action.dest,
        "kind": type(action).__name__,
        "default": None if default is argparse.SUPPRESS else default,
        "suppressed_default": default is argparse.SUPPRESS,
        "choices": list(action.choices) if action.choices and action.option_strings else None,
        "nargs": action.nargs,
        "required": action.required,
        "type": getattr(action.type, "__name__", None),
    }


def _walk(parser: argparse.ArgumentParser) -> dict:
    out = {
        "options": [
            _describe(a)
            for a in parser._actions
            if not isinstance(a, argparse._SubParsersAction | argparse._HelpAction)
        ],
        "commands": {},
    }
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for name, sub in action.choices.items():
                out["commands"][name] = _walk(sub)
    return out


def _help(parser: argparse.ArgumentParser, monkeypatch) -> str:
    monkeypatch.setenv("COLUMNS", "100")
    text = parser.format_help()
    return re.sub(r"shuto \d[\w.+-]*", "shuto <version>", text).replace("\r\n", "\n")


def test_cli_options_golden():
    parser = _seam()()
    check_json("cli.json", _walk(parser))


def _all_parsers(parser, prefix=()):
    yield prefix, parser
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for name, sub in action.choices.items():
                yield from _all_parsers(sub, (*prefix, name))


def _names():
    return [p for p, _ in _all_parsers(_seam()())]


# argparse help layout changes between Python versions; the goldens are recorded on 3.12.
# The command structure itself is pinned on every version by test_cli_options_golden.
@pytest.mark.skipif(sys.version_info[:2] != (3, 12), reason="help goldens are recorded on 3.12")
@pytest.mark.parametrize("path", _names(), ids=lambda p: " ".join(p) or "shuto")
def test_cli_help_golden(path, monkeypatch):
    parser = dict(_all_parsers(_seam()()))[path]
    name = "-".join(path) or "shuto"
    check_text(f"help/{name}.txt", _help(parser, monkeypatch))


def test_ui_options_the_updater_relaunch_passes():
    """The update helper relaunches `shuto ui <ui_args> --no-browser`."""
    args = _seam()().parse_args(["ui", "--host", "127.0.0.1", "--port", "8765", "--no-browser"])
    assert (args.command, args.host, args.port, args.no_browser) == (
        "ui",
        "127.0.0.1",
        8765,
        True,
    )


def test_no_command_means_ui():
    assert _seam()().parse_args([]).command is None


def test_version_exits_zero(capsys):
    with pytest.raises(SystemExit) as exc:
        _seam()().parse_args(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.startswith("shuto ")
