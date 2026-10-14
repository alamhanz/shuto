"""Where shuto keeps its files: model choice, benchmark cache, batch folders, ledger, report."""

from __future__ import annotations

from pathlib import Path

import platformdirs

from shuto.core.config import APP_NAME, Config, default_config_dir
from shuto.deliver import report as report_mod


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
    `report.folder_name`), so it can never point outside the workspace.
    Older versions kept these files in <workspace>/reports, exports and prepared; those files
    are left where they are and still work (each photo remembers its own file paths).
    """
    return cfg.workspace_path("batches", report_mod.folder_name(batch_id))


def report_path(cfg: Config, batch_id: str) -> Path:
    """Where the always-current report CSV of a batch lives: <batch folder>/report.csv."""
    return batch_dir(cfg, batch_id) / "report.csv"
