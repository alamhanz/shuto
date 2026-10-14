"""Cull a batch (blur, size, near-duplicates)."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from shuto.core import progress as core_progress
from shuto.core.config import Config
from shuto.core.models import Status
from shuto.core.progress import Cancel, Progress
from shuto.ingest import cull as cull_mod
from shuto.store import Store
from shuto.workflow import importing, reports


@dataclass
class CullSummary:
    total: int = 0
    ready: int = 0
    culled: int = 0
    details: list[dict[str, Any]] = field(default_factory=list)  # culled photos with reasons


def cull_batch(
    cfg: Config,
    store: Store,
    batch_id: str,
    *,
    explain: bool = False,
    progress: Progress | None = None,
    cancel: Cancel | None = None,
) -> CullSummary:
    """Technical check of `imported` photos: -> `ready` or `culled` (reasons stored in cull)."""
    progress = progress or core_progress.noop
    thresholds = cull_mod.CullThresholds(**cfg.cull.model_dump())
    photos = store.list_photos(batch_id, [Status.IMPORTED])[
        : importing.MAX_BATCH_PHOTOS
    ]  # safety net
    summary = CullSummary(total=len(photos))
    for n, photo in enumerate(photos, 1):
        if cancel and cancel():
            break
        progress(n, len(photos), photo.orig_name)
        try:
            res = cull_mod.evaluate(photo.src_path, thresholds)
            data = asdict(res)
        except Exception as exc:
            data = {"ok": False, "reasons": [f"unreadable: {exc}"]}
        assert photo.id is not None
        store.update_photo(photo.id, cull=data)
        if data["ok"]:
            store.set_status(photo.id, Status.READY)
            summary.ready += 1
        else:
            store.set_status(photo.id, Status.CULLED)
            summary.culled += 1
            summary.details.append(
                {"id": photo.id, "name": photo.orig_name, "reasons": list(data["reasons"])}
            )
    if photos:
        reports.refresh_report(cfg, store, batch_id)
    return summary
