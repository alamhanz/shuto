"""Export a batch as CSV plus files."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from shuto.core import paths
from shuto.core import text as core_text
from shuto.core.config import Config
from shuto.core.models import Photo, Status
from shuto.deliver import send
from shuto.store import Store
from shuto.workflow import checks, reports
from shuto.workflow.checks import NOT_ENGLISH_TEXT


@dataclass
class ExportSummary:
    files: list[Path] = field(default_factory=list)
    rows: list[dict[str, Any]] = field(default_factory=list)
    dry_run: bool = False
    skipped: dict[int, str] = field(default_factory=dict)  # photo id -> why it is not in the CSV


def export_rows(photos: Iterable[Photo]) -> list[dict[str, Any]]:
    rows = []
    for p in photos:
        m = p.metadata or {}
        rows.append(
            {
                "filename": p.submit_name,
                "description": m.get("description", ""),
                "keywords": list(m.get("keywords") or []),
                "categories": list(m.get("categories") or []),
                "illustration": bool(m.get("illustration")),
                "mature": bool(m.get("mature")),
                "editorial": bool(m.get("editorial")),
            }
        )
    return rows


def export_batch(
    cfg: Config,
    store: Store,
    batch_id: str,
    *,
    dry_run: bool = False,
) -> ExportSummary:
    """Write the batch's one Shutterstock CSV for uploaded photos (filenames = uploaded names).

    The file is rewritten on every export, so it always lists every photo that is uploaded or
    exported and not yet submitted.
    """
    photos = [p for p in store.list_photos(batch_id, [Status.UPLOADED, Status.EXPORTED])]
    photos = [p for p in photos if p.submit_name]
    skipped: dict[int, str] = {}
    for p in photos:  # last guard: the CSV never carries non-Latin text (flagged, not altered)
        if checks.not_english(p.metadata or {}, cfg.style.english_only):
            assert p.id is not None
            skipped[p.id] = NOT_ENGLISH_TEXT
    if skipped and not dry_run:
        for pid, why in skipped.items():
            store.log_event("export_skipped", photo_id=pid, batch_id=batch_id, detail={"why": why})
    photos = [p for p in photos if p.id not in skipped]
    rows = export_rows(photos)
    summary = ExportSummary(rows=rows, dry_run=dry_run, skipped=skipped)
    if dry_run or not rows:
        return summary
    dest = send.write_csv(batch_id, rows, paths.batch_dir(cfg, batch_id), cfg.style.english_only)
    summary.files = [dest]
    for photo in photos:
        assert photo.id is not None
        store.update_photo(photo.id, exported_at=core_text.now_iso(), export_file=str(dest))
        if photo.status == Status.UPLOADED:
            store.set_status(photo.id, Status.EXPORTED)
    reports.refresh_report(cfg, store, batch_id)
    return summary
