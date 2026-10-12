"""Domain models for the shuto pipeline (statuses, reason codes, row dataclasses)."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class Status(StrEnum):
    IMPORTED = "imported"
    DUPLICATE = "duplicate"
    NEAR_DUPLICATE = "near_duplicate"
    ALREADY_UPLOADED = "already_uploaded"
    CULLED = "culled"
    READY = "ready"
    DESCRIBED = "described"
    NEEDS_REVIEW = "needs_review"
    BLOCKED = "blocked"
    APPROVED = "approved"
    PREPARED = "prepared"
    UPLOADED = "uploaded"
    EXPORTED = "exported"
    SUBMITTED = "submitted"
    ACCEPTED = "accepted"
    REJECTED = "rejected"


S = Status

# States from which a photo can be sent back to `ready` for a fresh describe (before upload).
_PRE_UPLOAD = (
    S.IMPORTED,
    S.DUPLICATE,
    S.NEAR_DUPLICATE,
    S.ALREADY_UPLOADED,
    S.CULLED,
    S.READY,
    S.DESCRIBED,
    S.NEEDS_REVIEW,
    S.BLOCKED,
    S.APPROVED,
    S.PREPARED,
)

_DESCRIBE_RESULTS = frozenset({S.DESCRIBED, S.NEEDS_REVIEW, S.BLOCKED})

ALLOWED: dict[Status, frozenset[Status]] = {
    S.IMPORTED: frozenset({S.DUPLICATE, S.NEAR_DUPLICATE, S.CULLED, S.READY}),
    S.DUPLICATE: frozenset({S.READY}),
    S.NEAR_DUPLICATE: frozenset({S.READY}),
    S.ALREADY_UPLOADED: frozenset({S.READY}),
    S.CULLED: frozenset({S.READY}),
    S.READY: _DESCRIBE_RESULTS,
    # Guardrails re-run on every edit, so describe results can move between each other.
    S.DESCRIBED: frozenset(_DESCRIBE_RESULTS | {S.APPROVED}),
    S.NEEDS_REVIEW: frozenset(_DESCRIBE_RESULTS | {S.APPROVED}),
    S.BLOCKED: frozenset(_DESCRIBE_RESULTS),
    S.APPROVED: frozenset({S.PREPARED}),
    # Editing a prepared photo sends it back to approved (stale copy deleted by the caller).
    S.PREPARED: frozenset({S.UPLOADED, S.APPROVED}),
    S.UPLOADED: frozenset({S.EXPORTED}),
    S.EXPORTED: frozenset({S.SUBMITTED}),
    S.SUBMITTED: frozenset({S.ACCEPTED, S.REJECTED}),
    S.ACCEPTED: frozenset(),
    S.REJECTED: frozenset(),
}
for _src in _PRE_UPLOAD:
    ALLOWED[_src] = frozenset(ALLOWED[_src] | {S.READY})
del _src

# Photos shuto remembers as sent to Shutterstock (the history that new imports are compared to).
UPLOADED_STATES: tuple[Status, ...] = (
    S.UPLOADED,
    S.EXPORTED,
    S.SUBMITTED,
    S.ACCEPTED,
    S.REJECTED,
)

REASON_CODES: tuple[str, ...] = (
    "focus",
    "noise",
    "lighting",
    "composition",
    "trademark",
    "property_release",
    "model_release",
    "title",
    "keywords",
    "editorial_caption",
    "similar",
    "limited_value",
    "ip",
    "other",
)


@dataclass
class Batch:
    id: str
    created_at: str = ""
    notes: str = ""
    source: str | None = None


@dataclass
class Photo:
    sha256: str
    src_path: str
    orig_name: str
    batch_id: str | None = None
    status: Status = Status.IMPORTED
    id: int | None = None
    dhash: int | None = None
    width: int | None = None
    height: int | None = None
    captured_at: str | None = None
    camera: str | None = None
    has_gps: bool = False
    near_dup_group: int | None = None
    cull: dict[str, Any] | None = None
    metadata: dict[str, Any] | None = None
    metadata_source: str | None = None
    detected: dict[str, Any] | None = None
    guardrail: dict[str, Any] | None = None
    release_ids: list[str] = field(default_factory=list)
    submit_name: str | None = None
    prepared_path: str | None = None
    uploaded_at: str | None = None
    exported_at: str | None = None
    export_file: str | None = None
    submitted_at: str | None = None
    created_at: str = ""
    updated_at: str = ""


@dataclass
class Rejection:
    photo_id: int
    reason_code: str
    note: str | None = None
    at: str = ""
    id: int | None = None


@dataclass
class Job:
    kind: str
    batch_id: str | None = None
    state: str = "queued"
    done: int = 0
    total: int = 0
    error: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    id: int | None = None


@dataclass
class LLMCall:
    provider: str
    model: str
    photo_id: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    seconds: float | None = None
    repaired: bool = False
    retries: int = 0
    ok: bool = True
    error: str | None = None
    at: str = ""
    id: int | None = None
