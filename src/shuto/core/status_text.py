"""Plain-language status names and review buckets (shared by the web UI and the workflow)."""

from __future__ import annotations

from typing import Any

LABELS: dict[str, str] = {
    "imported": "New",
    "ready": "Waiting for AI",
    "culled": "Set aside: quality",
    "set_aside": "Set aside: doesn't fit",
    "duplicate": "Exact copy",
    "near_duplicate": "Looks like another photo",
    "already_uploaded": "Sent before",
    "described": "Looks good",
    "needs_review": "Needs a look",
    "blocked": "Can't send as is",
    "approved": "Approved",
    "prepared": "Ready to upload",
    "uploaded": "Uploaded",
    "exported": "Uploaded",
    "submitted": "Submitted",
    "accepted": "Accepted",
    "rejected": "Rejected",
}

# Review filters: attention / good / aside. "stage" and "done" statuses are not shown as filters.
BUCKETS: dict[str, str] = {
    "imported": "stage",
    "ready": "stage",
    "culled": "aside",
    "set_aside": "aside",
    "duplicate": "aside",
    "near_duplicate": "aside",
    "already_uploaded": "aside",
    "described": "good",
    "needs_review": "attention",
    "blocked": "attention",
    "approved": "good",
    "prepared": "stage",
    "uploaded": "stage",
    "exported": "stage",
    "submitted": "done",
    "accepted": "done",
    "rejected": "done",
}

BUCKET_LABELS: dict[str, str] = {
    "attention": "Needs a look",
    "good": "Looks good",
    "aside": "Set aside",
    "all": "All",
}


def label(status: Any) -> str:
    key = getattr(status, "value", status)
    return LABELS.get(str(key), str(key))


def bucket(status: Any) -> str:
    key = getattr(status, "value", status)
    return BUCKETS.get(str(key), "stage")
