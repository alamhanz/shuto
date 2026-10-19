"""SQLite persistence: the Store, its schema and the workspace opener."""

from __future__ import annotations

from shuto.core.config import Config
from shuto.store.db import InvalidTransition, Store

__all__ = ["InvalidTransition", "Store", "open_store"]


def open_store(cfg: Config) -> Store:
    cfg.workspace_dir.mkdir(parents=True, exist_ok=True)
    return Store(cfg.workspace_path("shuto.db"))
