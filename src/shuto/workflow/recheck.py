"""Check a sent photo again with the AI: the new text is a draft the owner accepts or drops.

A sent photo (uploaded, or its CSV made, but not yet submitted) keeps its status, its place in
the sent history and its old text while the AI works. Using the draft rewrites the batch CSV;
the owner uploads that CSV on the Shutterstock site by hand.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from shuto.core.config import Config
from shuto.core.models import FILE_FACT_KEYS, Photo, Status
from shuto.deliver import csv_export
from shuto.metadata import rules as metadata_rules
from shuto.store import Store
from shuto.workflow import checks, exporting, reports
from shuto.workflow import editorial as editorial_text
from shuto.workflow.checks import NOT_ENGLISH_TEXT

RECHECKABLE = (Status.UPLOADED, Status.EXPORTED)

TOO_LATE = (
    "Shutterstock is already reviewing (or has) this photo, so its text can only be changed on "
    "the Shutterstock site."
)
NOT_SENT = "this photo has not been sent to Shutterstock yet; use Send back to AI instead"


@dataclass
class QueueSummary:
    queued: list[int] = field(default_factory=list)
    refused: dict[int, str] = field(default_factory=dict)  # photo id -> why, in plain words


@dataclass
class UseDraftResult:
    ok: bool
    messages: list[str] = field(default_factory=list)
    csv_path: Path | None = None


def why_not(photo: Photo) -> str | None:
    """Plain reason a photo cannot be checked again, or None when it can."""
    if photo.status in RECHECKABLE:
        return None if photo.submit_name else "this photo has no upload name yet"
    if photo.status in (Status.SUBMITTED, Status.ACCEPTED, Status.REJECTED):
        return TOO_LATE
    return NOT_SENT


def queue(cfg: Config, store: Store, batch_id: str, ids: list[int]) -> QueueSummary:
    """Mark sent photos as waiting for new text. The photos themselves do not change."""
    summary = QueueSummary()
    for pid in dict.fromkeys(ids):
        photo = store.get_photo(pid)
        if photo is None or photo.batch_id != batch_id:
            summary.refused[pid] = "this photo is not in this batch"
            continue
        why = why_not(photo)
        if why:
            summary.refused[pid] = why
            continue
        store.update_photo(pid, draft={"state": "waiting"})
        summary.queued.append(pid)
    return summary


def drop_draft(store: Store, pid: int) -> None:
    """Keep the old text: the draft is thrown away."""
    store.update_photo(pid, draft=None)


def has_new_text(photo: Photo) -> bool:
    return (photo.draft or {}).get("state") == "ready"


def _differs(a: dict[str, Any], b: dict[str, Any]) -> bool:
    return any(a.get(k) != b.get(k) for k in ("description", "keywords", "categories", "editorial"))


def use_draft(
    cfg: Config, store: Store, pid: int, edits: dict[str, Any] | None = None
) -> UseDraftResult:
    """Replace the old text with the draft (plus the owner's edits) and rewrite the batch CSV."""
    photo = store.get_photo(pid)
    if photo is None:
        return UseDraftResult(False, ["This photo no longer exists."])
    why = why_not(photo)
    if why:
        return UseDraftResult(False, [why[0].upper() + why[1:]])
    draft = photo.draft or {}
    if draft.get("state") != "ready" or not photo.batch_id:
        return UseDraftResult(False, ["There is no new text to use."])

    data = dict(draft.get("metadata") or {})
    for key in ("description", "keywords", "categories", "editorial"):
        if edits and edits.get(key) is not None:
            data[key] = edits[key]
    if len(data.get("categories") or []) > 2:
        return UseDraftResult(False, ["Choose at most 2 categories."])
    if edits and edits.get("description") is not None:
        data.pop("place", None)  # the owner looked at the caption: no "Default place" note
    # Judge the new text with the facts the AI saw (what it detected), not the old text's.
    work = dataclasses.replace(
        photo,
        metadata=dict(draft.get("metadata") or {}),
        detected=draft.get("detected"),
        filters=draft.get("filters"),
    )
    place = cfg.style.default_place
    strict = metadata_rules.language_policy(cfg.style).strict
    data = editorial_text.apply_editorial(
        work,
        data,
        default_city=place.city,
        default_country=place.country,
        english=strict,
        include_gps=cfg.prepare.include_gps,
    )
    meta, gr, _status = checks._evaluate(cfg, store, work, data)
    problems: list[str] = []
    if gr.action == "block":
        problems.extend(gr.messages or ["The Shutterstock rules do not allow this text."])
    if checks.not_english(data, strict):
        problems.append(NOT_ENGLISH_TEXT)
    if meta.editorial:
        caption = csv_export.editorial_problem(meta.description)
        if caption:
            problems.append(f"The editorial caption needs a fix: {caption}")
    if problems:
        return UseDraftResult(False, list(dict.fromkeys(problems)))

    stored = meta.to_dict()
    for extra in (
        "title",
        "location",
        "place",
        "language",
        "caption_sentence",
        "commercial_description",
        "ai_run",
        "metadata_issues",
        *FILE_FACT_KEYS,
    ):
        if extra in data:
            stored[extra] = data[extra]
    edited = bool(edits) and _differs(stored, draft.get("metadata") or {})
    store.log_event(
        "recheck_applied",
        photo_id=pid,
        batch_id=photo.batch_id,
        detail={"old_metadata": photo.metadata, "old_source": photo.metadata_source},
    )
    store.update_photo(
        pid,
        metadata=stored,
        guardrail=gr.to_dict(),
        detected=draft.get("detected"),
        filters=draft.get("filters"),
        metadata_source="manual" if edited else draft.get("metadata_source"),
        draft=None,
    )
    if photo.prepared_path:  # its IPTC text is the old text; a resend makes a fresh copy
        try:
            Path(photo.prepared_path).unlink(missing_ok=True)
        except OSError:
            pass
        store.update_photo(pid, prepared_path=None)
    summary = exporting.export_batch(cfg, store, photo.batch_id)
    reports.refresh_report(cfg, store, photo.batch_id)
    csv_path = summary.files[0] if summary.files else None
    messages = [summary.skipped[pid]] if pid in summary.skipped else []
    return UseDraftResult(csv_path is not None, messages, csv_path)
