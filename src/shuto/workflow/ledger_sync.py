"""Export and import the uploaded-photo ledger."""

from __future__ import annotations

import csv
from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from shuto.core import paths
from shuto.core import text as core_text
from shuto.core.config import Config
from shuto.core.errors import PipelineError
from shuto.core.models import UPLOADED_STATES, Status
from shuto.deliver import ledger as ledger_mod
from shuto.store import Store


def _ledger_rows(store: Store) -> list[ledger_mod.LedgerRow]:
    return [
        ledger_mod.LedgerRow(
            p.sha256,
            p.dhash,
            p.orig_name,
            p.submit_name or "",
            p.uploaded_at or "",
            p.batch_id or "",
        )
        for p in store.list_photos(statuses=UPLOADED_STATES)
    ]


def export_ledger(
    store: Store,
    path: str | Path,
    *,
    keep_existing: bool = False,
    drop: Collection[str] = (),
) -> int:
    """Write the uploaded photos to a ledger CSV; returns the number of rows written.

    With `keep_existing`, rows already in the file but not in the database are kept (so a ledger
    restored from backup is never shrunk by a workspace that is missing those photos), except
    the sha256 values in `drop` (photos the user deleted on Shutterstock and will send again).
    """
    rows = _ledger_rows(store)
    if keep_existing:
        try:
            old = ledger_mod.read_ledger(path)
        except (UnicodeDecodeError, csv.Error):
            ledger_mod.back_up_corrupt(path)
            old = []
        rows = ledger_mod.merge(old, rows)
    if drop:
        keep = {p.sha256 for p in store.list_photos(statuses=UPLOADED_STATES)}
        rows = [r for r in rows if r.sha256 not in drop or r.sha256 in keep]
    ledger_mod.write_ledger(path, rows)
    return len(rows)


def refresh_ledger(cfg: Config, store: Store) -> None:
    """Rewrite <workspace>/uploaded.csv after an upload. A file problem never fails the upload."""
    try:
        export_ledger(store, paths.ledger_path(cfg), keep_existing=True)
    except (OSError, ValueError, csv.Error) as exc:
        store.log_event("ledger_failed", detail={"error": str(exc)})


@dataclass
class LedgerImportSummary:
    added: int = 0
    already_known: int = 0
    marked: int = 0


def import_ledger(cfg: Config, store: Store, path: str | Path) -> LedgerImportSummary:
    """Merge a ledger CSV into the database as uploaded photos (idempotent, keyed by sha256)."""
    p = Path(path).expanduser()
    if not p.is_file():
        raise PipelineError(f"Ledger file not found: {p}")
    try:
        rows = ledger_mod.read_ledger(p)
    except (OSError, UnicodeDecodeError, csv.Error) as exc:
        raise PipelineError(f"Could not read the ledger {p}: {exc}") from exc
    summary = LedgerImportSummary()
    for row in rows:
        known = store.find_by_sha(row.sha256)
        if known and known.status in UPLOADED_STATES:
            summary.already_known += 1
            continue
        if known:
            assert known.id is not None
            store.update_photo(known.id, uploaded_at=row.uploaded_at or core_text.now_iso())
            store.set_status(known.id, Status.UPLOADED, force=True, detail={"ledger": True})
            summary.marked += 1
            continue
        fields: dict[str, Any] = {
            "sha256": row.sha256,
            "src_path": "(from ledger)",
            "orig_name": row.orig_name or row.sha256[:12],
            "batch_id": row.batch_id or "uploaded-before",
            "status": Status.UPLOADED,
            "uploaded_at": row.uploaded_at or core_text.now_iso(),
        }
        if row.dhash is not None:
            fields["dhash"] = row.dhash
        if row.submit_name and not store.find_by_submit_name(row.submit_name):
            fields["submit_name"] = row.submit_name
        store.add_photo(**fields)
        summary.added += 1
    if summary.added or summary.marked:
        refresh_ledger(cfg, store)
    return summary
