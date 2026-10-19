"""Where shuto keeps its files: model choice, benchmark cache, batch folders, ledger, report."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import platformdirs

from shuto.core.config import APP_NAME, Config, default_config_dir


def model_choice_path() -> Path:
    """Explicitly chosen local model: <user_config_dir>/model_choice.json."""
    return default_config_dir() / "model_choice.json"


def benchmark_cache_path() -> Path:
    """Cached benchmark results: <user_cache_dir>/benchmarks.json."""
    return Path(platformdirs.user_cache_dir(APP_NAME)) / "benchmarks.json"


def ledger_path(cfg: Config) -> Path:
    return cfg.workspace_path("uploaded.csv")


def batch_dir(cfg: Config, batch_id: str) -> Path:
    """The one folder that holds a batch's own files: <workspace>/batches/<safe name>/.

    The name is readable ("trip (2)") and uses only letters, digits, space, ( ) - _ and dot (a
    short hash is added when the id had to be changed or has upper-case letters; see
    `folder_name`), so it can never point outside the workspace.
    Older versions kept these files in <workspace>/reports, exports and prepared; those files
    are left where they are and still work (each photo remembers its own file paths).
    """
    return cfg.workspace_path("batches", folder_name(batch_id))


def report_path(cfg: Config, batch_id: str) -> Path:
    """Where the always-current report CSV of a batch lives: <batch folder>/report.csv."""
    return batch_dir(cfg, batch_id) / "report.csv"


_FOLDER_UNSAFE = re.compile(r"[^A-Za-z0-9 ()._-]")
_RESERVED_NAMES = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{n}" for n in range(1, 10)),
        *(f"LPT{n}" for n in range(1, 10)),
    }
)
FOLDER_NAME_MAX = 80


def folder_name(batch_id: str) -> str:
    """A readable, safe folder name for a batch: "trip (2)" stays "trip (2)".

    Keeps ASCII letters, digits, space, ( ) - _ and dot; anything else becomes `_`. Repeated
    spaces collapse, spaces and dots are stripped at both ends, the length is capped at 80 and
    Windows device names (nul, con.txt, COM1 ...) get a `_` in front. A short hash of the exact
    batch id is added when the name had to change or has upper-case letters, so ids that differ
    only by case ("Kyoto" and "kyoto") never share a folder on Windows or macOS.
    """
    name = _FOLDER_UNSAFE.sub("_", batch_id)
    name = re.sub(" {2,}", " ", name).strip(" .")[:FOLDER_NAME_MAX].strip(" .")
    changed = name != batch_id
    if name.split(".")[0].rstrip(" ").upper() in _RESERVED_NAMES:
        name, changed = "_" + name, True
    if not name:
        name, changed = "batch", True
    if changed or name != name.lower():
        name += "-" + hashlib.sha1(batch_id.encode("utf-8")).hexdigest()[:8]
    return name
