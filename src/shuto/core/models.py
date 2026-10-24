"""Domain models for the shuto pipeline (statuses, reason codes, row dataclasses)."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, TypedDict


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
    SET_ASIDE = "set_aside"
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
    S.SET_ASIDE,
    S.APPROVED,
    S.PREPARED,
)

_DESCRIBE_RESULTS = frozenset({S.DESCRIBED, S.NEEDS_REVIEW, S.BLOCKED, S.SET_ASIDE})

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
    # Set aside by the theme/rules check; approving by hand is allowed (guardrails still apply).
    S.SET_ASIDE: frozenset(_DESCRIBE_RESULTS | {S.APPROVED}),
    S.APPROVED: frozenset({S.PREPARED}),
    # Editing a prepared photo sends it back to approved (stale copy deleted by the caller).
    S.PREPARED: frozenset({S.UPLOADED, S.APPROVED}),
    # Photos deleted on Shutterstock can be sent again (`reupload_photos`).
    S.UPLOADED: frozenset({S.EXPORTED, S.PREPARED, S.APPROVED}),
    S.EXPORTED: frozenset({S.SUBMITTED, S.PREPARED, S.APPROVED}),
    S.SUBMITTED: frozenset({S.ACCEPTED, S.REJECTED, S.PREPARED, S.APPROVED}),
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

# Photos the user may send again after deleting them on Shutterstock. Accepted photos are live
# in the library and rejected ones were refused, so neither is offered.
REUPLOADABLE: tuple[Status, ...] = (S.UPLOADED, S.EXPORTED, S.SUBMITTED)

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
    filters: dict[str, Any] | None = None
    draft: dict[str, Any] | None = None  # new text from "Check again with AI", for a sent photo
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


# read from the photo at import, plus the AI model and max tries that wrote the text; kept on edits
FILE_FACT_KEYS = ("file_meta", "gps_place", "ai_run")


def _file_facts(metadata: dict | None) -> dict[str, Any]:
    """The stored facts read from the photo file, to carry over when metadata is rewritten."""
    return {k: metadata[k] for k in FILE_FACT_KEYS if metadata and k in metadata}


class StoredPlace(TypedDict, total=False):
    """`metadata["place"]`: the place used for the text and which source decided it."""

    city: str
    region: str  # prefecture or state; empty when unknown
    country: str
    source: str  # gps, file, ai, "gps + AI", default, "partly default" or unknown
    default: bool


class AiRun(TypedDict, total=False):
    """`metadata["ai_run"]`: the model (`provider/model`) and max tries that wrote the text."""

    model: str
    max_tries: int


class StoredMetadata(TypedDict, total=False):
    """`Photo.metadata` (column `metadata_json`): every key written today."""

    filename: str
    title: str
    description: str
    keywords: list[str]
    categories: list[str]
    editorial: bool
    mature: bool
    illustration: bool
    warnings: list[str]
    location: dict[str, Any]
    place: StoredPlace
    file_meta: dict[str, Any]
    gps_place: dict[str, Any]
    ai_run: AiRun
    language: str
    metadata_issues: list[dict[str, Any]]
    caption_sentence: str  # the factual sentence an editorial caption is built from
    commercial_description: str  # the commercial headline, restored when editorial is switched off


class StoredFilters(TypedDict, total=False):
    """`Photo.filters` (column `filters_json`): the theme/rules check result."""

    v: int
    at: str
    model: str
    fingerprint: str
    verdict: str
    theme: dict[str, Any] | None
    rules: list[dict[str, Any]]
    set_aside_by: list[str]
    overridden: dict[str, Any] | None


class StoredGuardrail(TypedDict, total=False):
    """`Photo.guardrail` (column `guardrail_json`): the deterministic guardrail result."""

    action: str
    hits: list[dict[str, Any]]
    removed_keywords: list[str]
    messages: list[str]
