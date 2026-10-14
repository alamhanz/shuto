"""Re-upload, add anyway and record results."""

from __future__ import annotations

import csv
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from shuto.core import paths
from shuto.core import text as core_text
from shuto.core.config import Config
from shuto.core.errors import PipelineError
from shuto.core.models import REASON_CODES, REUPLOADABLE, Photo, Status
from shuto.store import InvalidTransition, Store
from shuto.workflow import ledger_sync, reports, review


@dataclass
class ReuploadSummary:
    moved: dict[int, Status] = field(default_factory=dict)  # photo id -> its new status
    refused: dict[int, str] = field(default_factory=dict)
    batches: set[str] = field(default_factory=set)


def reupload_photos(
    cfg: Config, store: Store, photo_ids: Iterable[int], *, batch_id: str | None = None
) -> ReuploadSummary:
    """The user deleted these photos on Shutterstock: make them ready to be uploaded again.

    Works for uploaded, exported and submitted photos (never accepted or rejected ones). Each
    goes back to `prepared` when its upload copy still exists, else to `approved` (a new copy is
    made) or, if it was never described here, to `ready` (it needs Check). `uploaded_at` and the
    other send dates are cleared, a `reupload_requested` event records the time, and the
    uploaded.csv ledger and the batch reports are rewritten so they no longer count as sent.
    With `batch_id`, photos of other batches are refused.
    """
    summary = ReuploadSummary()
    shas: set[str] = set()
    for pid in dict.fromkeys(photo_ids):
        photo = store.get_photo(pid)
        if photo is None or (batch_id is not None and photo.batch_id != batch_id):
            summary.refused[pid] = "That photo is not in this batch."
            continue
        if photo.status not in REUPLOADABLE:
            answered = photo.status in (Status.ACCEPTED, Status.REJECTED)
            summary.refused[pid] = (
                f"{photo.orig_name}: Shutterstock already gave an answer, so it is not sent again."
                if answered
                else f"{photo.orig_name} is not marked as sent, so there is nothing to send again."
            )
            continue
        prepared = bool(photo.prepared_path) and Path(str(photo.prepared_path)).is_file()
        has_original = Path(photo.src_path).is_file()
        if prepared:
            target = Status.PREPARED
        elif has_original and (photo.metadata or {}).get("description"):  # facts alone are not text
            target = Status.APPROVED
        elif has_original:
            target = Status.READY
        else:
            summary.refused[pid] = (
                f"{photo.orig_name}: shuto no longer has a copy or the original to send. "
                "Add the original photo again from its folder."
            )
            continue
        was = photo.status
        store.update_photo(
            pid, uploaded_at=None, exported_at=None, export_file=None, submitted_at=None
        )
        # `ready` (never described here) is not a normal way back after upload, so it is forced
        store.set_status(pid, target, force=target == Status.READY, detail={"reupload": True})
        store.log_event(
            "reupload_requested",
            photo_id=pid,
            batch_id=photo.batch_id,
            detail={"at": core_text.now_iso(), "from": was.value, "to": target.value},
        )
        summary.moved[pid] = target
        shas.add(photo.sha256)
        if photo.batch_id:
            summary.batches.add(photo.batch_id)
    if summary.moved:
        try:
            ledger_sync.export_ledger(store, paths.ledger_path(cfg), keep_existing=True, drop=shas)
        except (OSError, ValueError, csv.Error) as exc:
            store.log_event("ledger_failed", detail={"error": str(exc)})
        reports._refresh_reports(cfg, store, summary.batches)
    return summary


def add_anyway(cfg: Config, store: Store, photo_ids: Iterable[int]) -> ReuploadSummary:
    """Import's "Add anyway": photos left out as 'sent before' join their batch after all.

    A look-alike that was set aside as `already_uploaded` becomes a normal imported photo (it
    still needs Check); an exact copy of a remembered photo is sent again (`reupload_photos`).
    """
    summary = ReuploadSummary()
    exact: list[int] = []
    for pid in dict.fromkeys(photo_ids):
        photo = store.get_photo(pid)
        if photo is None:
            summary.refused[pid] = "That photo is no longer in shuto."
        elif photo.status == Status.ALREADY_UPLOADED:
            store.set_status(pid, Status.IMPORTED, force=True, detail={"forced": True})
            summary.moved[pid] = Status.IMPORTED
            if photo.batch_id:
                summary.batches.add(photo.batch_id)
        elif photo.status in REUPLOADABLE:
            exact.append(pid)
        else:
            summary.refused[pid] = f"{photo.orig_name} was already added."
    if exact:
        again = reupload_photos(cfg, store, exact)
        summary.moved.update(again.moved)
        summary.refused.update(again.refused)
        summary.batches |= again.batches
    reports._refresh_reports(cfg, store, summary.batches)
    return summary


def mark_submitted(store: Store, batch_id: str, *, cfg: Config | None = None) -> list[int]:
    """After the manual Submit on the Shutterstock portal: exported -> submitted.

    Pass `cfg` to refresh the batch report afterwards.
    """
    ids = []
    for photo in store.list_photos(batch_id, [Status.EXPORTED]):
        assert photo.id is not None
        store.update_photo(photo.id, submitted_at=core_text.now_iso())
        store.set_status(photo.id, Status.SUBMITTED)
        ids.append(photo.id)
    if cfg is not None and ids:
        reports.refresh_report(cfg, store, batch_id)
    return ids


def record_result(
    store: Store,
    photo_id: int,
    accepted: bool,
    reason_code: str | None = None,
    note: str | None = None,
    *,
    cfg: Config | None = None,
) -> Photo:
    """Record Shutterstock's verdict. Rejection reasons feed later describe prompts.

    Pass `cfg` to refresh the batch report afterwards.
    """
    photo = review.get_photo(store, photo_id)
    if not accepted:
        if reason_code not in REASON_CODES:
            raise PipelineError("Reason must be one of: " + ", ".join(REASON_CODES))
    if photo.status == Status.EXPORTED:
        store.update_photo(photo_id, submitted_at=core_text.now_iso())
        store.set_status(photo_id, Status.SUBMITTED)
    try:
        store.set_status(photo_id, Status.ACCEPTED if accepted else Status.REJECTED)
    except InvalidTransition as exc:
        raise PipelineError(
            f"Photo {photo_id} is {photo.status.value}; only submitted photos get a result."
        ) from exc
    if not accepted and reason_code:
        store.add_rejection(photo_id, reason_code, note)
    if cfg is not None:
        reports.refresh_report(cfg, store, photo.batch_id)
    return review.get_photo(store, photo_id)
