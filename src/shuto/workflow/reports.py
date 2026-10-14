"""Batch reports and summaries."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any

from shuto.core import paths
from shuto.core.config import Config
from shuto.core.models import Status
from shuto.deliver import report as report_mod
from shuto.store import Store


def batch_report(store: Store, batch_id: str | None = None) -> list[dict[str, Any]]:
    """Per-batch status counts and LLM usage (all batches when batch_id is None)."""
    out = []
    for batch, counts in store.list_batches():
        if batch_id is not None and batch.id != batch_id:
            continue
        out.append(
            {
                "id": batch.id,
                "notes": batch.notes,
                "created_at": batch.created_at,
                "counts": {s.value: n for s, n in counts.items()},
                "total": sum(counts.values()),
                "usage": store.usage_by_batch(batch.id),
            }
        )
    return out


def build_report(cfg: Config, store: Store, batch_id: str) -> tuple[list[str], list[list[str]]]:
    """Header and records of the batch report, built from the database."""
    from shuto.web import ux  # plain-words status names; imported late (web imports this module)

    photos = store.list_photos(batch_id)
    by_id = {p.id: p for p in photos}
    keepers: dict[int, str] = {}
    for p in photos:
        if p.near_dup_group is not None and p.status != Status.NEAR_DUPLICATE:
            keepers.setdefault(p.near_dup_group, p.orig_name)
    duplicate_of: dict[int, str] = {}
    for p in photos:
        if p.id is not None and p.status == Status.NEAR_DUPLICATE and p.near_dup_group is not None:
            if p.near_dup_group in keepers:
                duplicate_of[p.id] = keepers[p.near_dup_group]
    for ev in store.batch_events(batch_id, ["already_uploaded"]):
        other_id = (ev["detail"] or {}).get("look_alike_of")
        other = store.get_photo(other_id) if isinstance(other_id, int) else None
        if other is not None and ev["photo_id"] in by_id:
            duplicate_of[ev["photo_id"]] = other.submit_name or other.orig_name
    rejections: dict[int, tuple[str, str]] = {}
    for p in photos:
        if p.id is not None and p.status == Status.REJECTED:
            found = store.list_rejections(p.id)
            if found:
                last = found[-1]
                why = last.reason_code + (f": {last.note}" if last.note else "")
                rejections[p.id] = (last.at, why)
    return report_mod.build_rows(
        photos,
        times=store.status_times(batch_id),
        duplicate_of=duplicate_of,
        labels=ux.label,
        rejections=rejections,
        ai_times=store.ai_times(batch_id),
    )


def ai_time_summary(store: Store, batch_id: str) -> dict[str, float | int]:
    """Total and average AI seconds per photo for a batch (the figures in the report)."""
    return report_mod.ai_time_totals(store.ai_times(batch_id))


def report_bytes(cfg: Config, store: Store, batch_id: str) -> bytes:
    """The report as CSV bytes (UTF-8 with BOM), built in memory; nothing is read from disk."""
    header, records = build_report(cfg, store, batch_id)
    return report_mod.render(header, records)


def write_report(cfg: Config, store: Store, batch_id: str) -> Path:
    """Write the report file for a batch (atomic) and return its path."""
    header, records = build_report(cfg, store, batch_id)
    return report_mod.write_report(paths.report_path(cfg, batch_id), header, records)


def refresh_report(cfg: Config, store: Store, batch_id: str | None) -> None:
    """Best-effort rewrite of the batch report after a batch step. A file problem (for example
    Excel holding the file open) never fails the step; it is logged as `report_failed`."""
    if not batch_id:
        return
    try:
        write_report(cfg, store, batch_id)
    except Exception as exc:
        try:
            store.log_event("report_failed", batch_id=batch_id, detail={"error": str(exc)})
        except Exception:
            pass


def _refresh_reports(cfg: Config, store: Store, batch_ids: Iterable[str]) -> None:
    for batch_id in sorted(batch_ids):
        refresh_report(cfg, store, batch_id)


def batch_summary(store: Store, batch_id: str) -> dict[str, Any]:
    """Status counts, photo total and bytes that an upload would send (approved + prepared)."""
    counts: dict[str, int] = {}
    upload_bytes = 0
    photos = store.list_photos(batch_id)
    for p in photos:
        counts[p.status.value] = counts.get(p.status.value, 0) + 1
        if p.status in (Status.APPROVED, Status.PREPARED):
            try:
                upload_bytes += Path(p.src_path).stat().st_size
            except OSError:
                continue
    return {"counts": counts, "total": len(photos), "upload_bytes": upload_bytes}
