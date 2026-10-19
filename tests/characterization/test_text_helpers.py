"""Small text helpers that exist in several copies today. 0.6.0 may merge two copies only when
the golden shows they give identical output for every input here (see IDENTICAL below)."""

from __future__ import annotations

import hashlib

from . import check_json


def _seam():
    from shuto import cli
    from shuto.ai import estimate
    from shuto.core import files as core_files
    from shuto.core import text as core_text
    from shuto.ollama import move as ollama_move
    from shuto.web import state, ux
    from shuto.web.routes import home, model_support, upload
    from shuto.workflow import check_batch, delivering

    return {
        "time_left": check_batch._time_left,
        "plain_seconds": model_support._plain_seconds,
        "ai_time_text": ux.ai_time_text,
        "format_duration": estimate.format_duration,
        "mb": upload._mb,
        "megabytes": delivering._megabytes,
        "gb": ollama_move._gb,
        "cli_short": cli._short,
        "web_short": state.short,
        "cli_yn": core_text.yes_no,
        "csv_yn": core_text.yes_no,
        "ux_n": ux._n,
        "setup_n": home._n,
        "sha256_file": core_files.sha256_file,
    }


SECONDS = [0, 0.4, 1, 9.99, 10, 45, 59.4, 59.6, 60, 89, 90, 119, 125, 600, 3599, 5399, 5400, 7200]
BYTES = [0, 1, 1000, 1_000_000, 5_242_880, 10_485_760, 1_073_741_824, 5 * 1024**3]
TEXTS = ["", "short", "a  b\n c", "x" * 20, "x" * 21, "x" * 300, "x" * 301, "é" * 12]
FLAGS = [True, False, 0, 1, None, "", "x", [], [0]]
COUNTS = [{}, {"a": 1}, {"a": 2, "b": 3}, {"a": None, "b": "4"}, {"a": 0}]
NAMES = [(), ("a",), ("a", "b"), ("missing",), ("a", "missing")]


def _call(fn, *args):
    try:
        return fn(*args)
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"


def test_text_helpers_golden():
    s = _seam()
    out = {
        "time_left": {str(v): _call(s["time_left"], v) for v in SECONDS},
        "plain_seconds": {str(v): _call(s["plain_seconds"], v) for v in SECONDS},
        "ai_time_text": {str(v): _call(s["ai_time_text"], v) for v in SECONDS},
        "format_duration": {str(v): _call(s["format_duration"], v) for v in SECONDS},
        "web_mb": {str(v): _call(s["mb"], v) for v in BYTES},
        "pipeline_megabytes": {str(v): _call(s["megabytes"], v) for v in BYTES},
        "ollama_gb": {str(v): _call(s["gb"], v) for v in BYTES},
        "cli_short_20": {repr(t): _call(s["cli_short"], t, 20) for t in TEXTS},
        "web_short_20": {repr(t): _call(s["web_short"], t, 20) for t in TEXTS},
        "web_short_default": {repr(t[:5]): _call(s["web_short"], t) for t in TEXTS},
        "cli_yn": {repr(v): _call(s["cli_yn"], v) for v in FLAGS},
        "csv_yn": {repr(v): _call(s["csv_yn"], v) for v in FLAGS},
        "ux_n": {f"{c} {n}": _call(s["ux_n"], c, *n) for c in COUNTS for n in NAMES},
        "routes_setup_n": {f"{c} {n}": _call(s["setup_n"], c, *n) for c in COUNTS for n in NAMES},
    }
    check_json("text_helpers.json", out)


def test_which_copies_agree():
    """Pairs that give the same answer for every input above may be merged; the rest may not."""
    s = _seam()

    def same(a, b, inputs):
        return all(_call(s[a], *i) == _call(s[b], *i) for i in inputs)

    seconds = [(v,) for v in SECONDS]
    found = {
        "plain_seconds == format_duration": same("plain_seconds", "format_duration", seconds),
        "ai_time_text == format_duration": same("ai_time_text", "format_duration", seconds),
        "time_left == format_duration": same("time_left", "format_duration", seconds),
        "web_mb == pipeline_megabytes": same("mb", "megabytes", [(v,) for v in BYTES]),
        "cli_yn == csv_yn": same("cli_yn", "csv_yn", [(v,) for v in FLAGS]),
        "cli_short == web_short": same("cli_short", "web_short", [(t, 20) for t in TEXTS]),
        "ux_n == routes_setup_n": same("ux_n", "setup_n", [(c, *n) for c in COUNTS for n in NAMES]),
    }
    check_json("text_helpers_identical.json", found)


def test_sha256_file(tmp_path):
    s = _seam()
    path = tmp_path / "blob.bin"
    path.write_bytes(b"shuto" * 100_000)
    expected = hashlib.sha256(path.read_bytes()).hexdigest()
    assert s["sha256_file"](path) == expected
    empty = tmp_path / "empty.bin"
    empty.write_bytes(b"")
    assert s["sha256_file"](empty) == hashlib.sha256(b"").hexdigest()
