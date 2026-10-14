"""Review: edit, approve, set aside and bulk-edit photos."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from shuto.ai import filters as filters_mod
from shuto.core import text as core_text
from shuto.core.config import Config
from shuto.core.errors import PipelineError
from shuto.core.models import (
    FILE_FACT_KEYS,
    Photo,
    Status,
)
from shuto.store import InvalidTransition, Store
from shuto.workflow import checks, describing, reports
from shuto.workflow.checks import NOT_ENGLISH_TEXT

PRE_UPLOAD_EDITABLE = (
    Status.DESCRIBED,
    Status.NEEDS_REVIEW,
    Status.BLOCKED,
    Status.SET_ASIDE,
    Status.APPROVED,
    Status.PREPARED,
)


def get_photo(store: Store, photo_id: int) -> Photo:
    photo = store.get_photo(photo_id)
    if photo is None:
        raise PipelineError(f"No photo with id {photo_id}.")
    return photo


def find_photo(store: Store, ref: str | int) -> Photo:
    """Photo by id, prepared (submit) name or original file name (must be unambiguous)."""
    text = str(ref).strip()
    if text.isdigit():
        return get_photo(store, int(text))
    by_name = store.find_by_submit_name(text)
    if by_name:
        return by_name
    matches = [p for p in store.list_photos() if p.orig_name.lower() == text.lower()]
    if not matches:
        raise PipelineError(f"No photo matches {text!r}.")
    if len(matches) > 1:
        ids = ", ".join(str(p.id) for p in matches)
        raise PipelineError(f"{text!r} is ambiguous (photo ids: {ids}); use an id.")
    return matches[0]


def update_metadata(
    cfg: Config,
    store: Store,
    photo_id: int,
    *,
    description: str | None = None,
    title: str | None = None,
    keywords: list[str] | None = None,
    categories: list[str] | None = None,
    editorial: bool | None = None,
    mature: bool | None = None,
    release_ids: list[str] | None = None,
    refresh: bool = True,
) -> Photo:
    """Edit metadata; guardrails re-run on every save and decide the new status.

    A prepared photo goes back to approved (its stale copy is deleted); an approved photo that
    now violates a guardrail drops to needs_review/blocked. The AI is never asked again: a
    set-aside photo stays set aside (unless a guardrail now blocks it). `refresh=False` skips
    rewriting the batch report (bulk callers refresh once).
    """
    photo = get_photo(store, photo_id)
    if photo.status not in PRE_UPLOAD_EDITABLE:
        raise PipelineError(f"Photo {photo_id} is {photo.status.value}; it cannot be edited now.")
    if release_ids is not None:
        store.update_photo(photo_id, release_ids=list(release_ids))
        photo = get_photo(store, photo_id)
    data = dict(photo.metadata or {})
    if description is not None:  # the user has looked at the caption: no "Default place" note
        data.pop("place", None)
    for key, value in (
        ("description", description),
        ("title", title),
        ("keywords", keywords),
        ("categories", categories),
        ("editorial", editorial),
        ("mature", mature),
    ):
        if value is not None:
            data[key] = value
    meta, gr, status = checks._evaluate(cfg, store, photo, data)
    stored = meta.to_dict()
    for extra in ("title", "location", "place", "language", *FILE_FACT_KEYS):
        if extra in data:
            stored[extra] = data[extra]
    was = photo.status
    if was in (Status.APPROVED, Status.PREPARED):
        # An edit never moves an approved photo because of the theme or rules (only guardrails).
        status = checks._status_for(meta, gr)
    if was == Status.PREPARED:
        describing._drop_prepared(store, photo)
        store.set_status(photo_id, Status.APPROVED)
        was = Status.APPROVED
    store.update_photo(photo_id, metadata=stored, guardrail=gr.to_dict(), metadata_source="manual")
    if was == Status.APPROVED:
        if status in (Status.NEEDS_REVIEW, Status.BLOCKED):
            store.set_status(photo_id, status, force=True, detail={"reason": "edited"})
    elif status != was:
        store.set_status(photo_id, status)
    if refresh:
        reports.refresh_report(cfg, store, photo.batch_id)
    return get_photo(store, photo_id)


@dataclass
class ApproveSummary:
    approved: list[int] = field(default_factory=list)
    refused: dict[int, str] = field(default_factory=dict)


def approve(
    cfg: Config,
    store: Store,
    *,
    batch_id: str | None = None,
    photo_ids: Iterable[int] | None = None,
    include_needs_review: bool = False,
) -> ApproveSummary:
    """Manual approval. Guardrails are re-checked; blocked photos are always refused.

    A photo that was set aside (or flagged) by the theme/rules check can be approved by id
    ("Approve anyway"); that is recorded in `Photo.filters["overridden"]` and as a
    `filters_overridden` event. Approving without ids never includes set-aside photos.
    """
    summary = ApproveSummary()
    if photo_ids is not None:
        photos = [get_photo(store, i) for i in photo_ids]
    else:
        statuses = [Status.DESCRIBED] + ([Status.NEEDS_REVIEW] if include_needs_review else [])
        photos = store.list_photos(batch_id, statuses)
    for photo in photos:
        pid = photo.id
        assert pid is not None
        if photo.status == Status.APPROVED:
            continue
        if photo.status not in (Status.DESCRIBED, Status.NEEDS_REVIEW, Status.SET_ASIDE):
            summary.refused[pid] = f"status is {photo.status.value}"
            continue
        if checks.not_english(photo.metadata or {}, cfg.style.english_only):
            summary.refused[pid] = NOT_ENGLISH_TEXT + ": edit it to English first"
            continue
        meta, gr, _ = checks._evaluate(cfg, store, photo, photo.metadata or {})
        if gr.action == "block":
            store.update_photo(pid, guardrail=gr.to_dict())
            store.set_status(pid, Status.BLOCKED)
            summary.refused[pid] = "blocked by guardrails: " + "; ".join(gr.messages[:2])
            continue
        if photo.filters and filters_mod.status_effect(photo.filters) != "ok":
            at = core_text.now_iso()
            overridden = {"at": at, "status_was": photo.status.value}
            store.update_photo(pid, filters={**photo.filters, "overridden": overridden})
            store.log_event(
                "filters_overridden", photo_id=pid, batch_id=photo.batch_id, detail=overridden
            )
        store.set_status(pid, Status.APPROVED)
        summary.approved.append(pid)
    reports._refresh_reports(cfg, store, {p.batch_id for p in photos if p.batch_id})
    return summary


_KEEPS_PREPARED = {
    Status.PREPARED,
    Status.UPLOADED,
    Status.EXPORTED,
    Status.SUBMITTED,
    Status.ACCEPTED,
    Status.REJECTED,
}


def override_status(
    store: Store, photo_id: int, status: Status | str, *, cfg: Config | None = None
) -> Photo:
    """Manual status change that follows models.ALLOWED (e.g. culled -> ready, any -> ready).

    Pass `cfg` to refresh the batch report afterwards.
    """
    before = get_photo(store, photo_id)
    try:
        store.set_status(photo_id, Status(status))
    except InvalidTransition as exc:
        raise PipelineError(str(exc)) from exc
    if Status(status) not in _KEEPS_PREPARED:
        describing._drop_prepared(store, before)
    if cfg is not None:
        reports.refresh_report(cfg, store, before.batch_id)
    return get_photo(store, photo_id)


@dataclass
class BulkEditSummary:
    changed: list[int] = field(default_factory=list)
    refused: dict[int, str] = field(default_factory=dict)


def _fold(words: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for w in words:
        w = str(w).strip()
        if w and w.lower() not in seen:
            seen.add(w.lower())
            out.append(w)
    return out


def bulk_edit(
    cfg: Config,
    store: Store,
    photo_ids: Iterable[int],
    *,
    add_keywords: Iterable[str] = (),
    remove_keywords: Iterable[str] = (),
    categories: list[str] | None = None,
    editorial: bool | None = None,
) -> BulkEditSummary:
    """Apply one keyword/category/editorial change to many photos (guardrails re-run on each)."""
    if categories is not None and len(categories) > 2:
        raise PipelineError("Pick at most 2 categories.")
    add = _fold(add_keywords)
    drop = {w.lower() for w in _fold(remove_keywords)}
    from shuto.web import ux  # plain-words status names; imported late (web imports this module)

    summary = BulkEditSummary()
    batches: set[str] = set()
    for pid in photo_ids:
        photo = store.get_photo(pid)
        if photo is None:
            summary.refused[pid] = "photo not found"
            continue
        if photo.status not in PRE_UPLOAD_EDITABLE:
            summary.refused[pid] = (
                f'it is marked "{ux.label(photo.status)}" and cannot be edited now'
            )
            continue
        current = [str(k) for k in (photo.metadata or {}).get("keywords") or []]
        keywords = _fold([k for k in current if k.lower() not in drop] + add)
        try:
            update_metadata(
                cfg,
                store,
                pid,
                keywords=keywords if (add or drop) else None,
                categories=list(categories) if categories is not None else None,
                editorial=editorial,
                refresh=False,
            )
        except PipelineError as exc:
            summary.refused[pid] = str(exc)
            continue
        summary.changed.append(pid)
        if photo.batch_id:
            batches.add(photo.batch_id)
    reports._refresh_reports(cfg, store, batches)
    return summary
