"""Console helpers shared by the command modules (plain text, ASCII only)."""

from __future__ import annotations

import argparse
from collections.abc import Sequence

COMMON = argparse.ArgumentParser(add_help=False)
COMMON.add_argument("--config", default=argparse.SUPPRESS, help="path to shuto.yaml")
COMMON.add_argument("-v", "--verbose", action="count", default=argparse.SUPPRESS)


def add_command(sub: argparse._SubParsersAction, name: str, help_: str) -> argparse.ArgumentParser:
    return sub.add_parser(name, parents=[COMMON], help=help_, description=help_)


def _confirm(prompt: str) -> bool:
    from shuto import cli  # tests replace `shuto.cli._input`

    try:
        return cli._input(f"{prompt} [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def _answer(prompt: str) -> str:
    from shuto import cli  # tests replace `shuto.cli._input`

    try:
        return cli._input(prompt).strip()
    except EOFError:
        return ""


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


def _progress_printer(label: str):
    state = {"last": -1}

    def report(done: int, total: int, message: str = "") -> None:
        if total and done != state["last"]:
            state["last"] = done
            out(f"{label} {done}/{total} {message}".rstrip())

    return report


def _split_csv(text: str | None) -> list[str] | None:
    if text is None:
        return None
    return [t.strip() for t in text.split(",") if t.strip()]
