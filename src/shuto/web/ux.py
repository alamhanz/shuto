"""Plain-language vocabulary and batch-flow helpers for the web UI.

Every user-visible word that is not page copy lives here, so the UI can be translated later.
Nothing in this module touches the database or the network.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

LABELS: dict[str, str] = {
    "imported": "New",
    "ready": "Waiting for AI",
    "culled": "Set aside: quality",
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

STEP_LABELS: dict[str, str] = {
    "add": "Add",
    "check": "Check",
    "review": "Review",
    "upload": "Upload",
    "submit": "Submit",
}

TERMS: dict[str, str] = {
    "cull": "quality check",
    "describe": "write titles & keywords",
    "guardrails": "Shutterstock rules check",
    "ftps": "Shutterstock upload login",
    "iptc": "info saved inside the photo copy",
    "csv": "Shutterstock info file (CSV)",
    "gps": "this photo contains the place where it was taken",
}

HELP: dict[str, str] = {
    "batch": "A batch is a group of photos you handle together, usually one folder.",
    "check": "The quality check looks for blurry, dark or small photos. Then the AI writes a "
    "title, description and keywords, and shuto checks them against the Shutterstock rules.",
    "attention": "The AI or the rules found something you should look at before sending.",
    "good": "Nothing to fix. You can still change the text if you like.",
    "aside": "Left out on purpose: poor quality, copies, or photos you sent before.",
    "commercial": "Commercial photos can be used in ads and products. People and private "
    "places may need a release.",
    "editorial": "Editorial photos show real news or events and cannot be used in ads.",
    "csv": "The Shutterstock info file (CSV) holds the titles and keywords. You upload it on "
    "the Shutterstock website in the last step.",
}

STAGES = ("check", "review", "upload", "submit", "done", "empty")
_FLOW = ("add", "check", "review", "upload", "submit")
_STAGE_STEP = {
    "empty": "add",
    "check": "check",
    "review": "review",
    "upload": "upload",
    "submit": "submit",
}


def label(status: Any) -> str:
    key = getattr(status, "value", status)
    return LABELS.get(str(key), str(key))


def bucket(status: Any) -> str:
    key = getattr(status, "value", status)
    return BUCKETS.get(str(key), "stage")


def _n(counts: dict[str, int], *names: str) -> int:
    return sum(int(counts.get(name, 0) or 0) for name in names)


def stage(counts: dict[str, int]) -> str:
    """Where the batch is in the flow; first match wins."""
    if _n(counts, "imported", "ready") > 0:
        return "check"
    if _n(counts, "described", "needs_review", "blocked") > 0:
        return "review"
    if _n(counts, "approved", "prepared") > 0:
        return "upload"
    if _n(counts, "uploaded", "exported") > 0:
        return "submit"
    if _n(counts, "submitted", "accepted", "rejected") > 0:
        return "done"
    return "empty"


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


def _href(batch_id: str, page: str) -> str:
    return f"/batch/{quote(batch_id, safe='')}/{page}"


def next_action(batch_id: str, counts: dict[str, int]) -> dict[str, str]:
    """The one primary button for a batch: `{label, href}`."""
    st = stage(counts)
    if st == "check":
        n = _n(counts, "imported", "ready")
        return {"label": f"Check {_plural(n, 'new photo')}", "href": _href(batch_id, "check")}
    if st == "review":
        n = _n(counts, "needs_review", "blocked")
        if n:
            text = f"Review {_plural(n, 'photo')} that need a look"
            if n == 1:
                text = "Review 1 photo that needs a look"
        else:
            text = f"Review {_plural(_n(counts, 'described'), 'photo')}"
        return {"label": text, "href": _href(batch_id, "review")}
    if st == "upload":
        n = _n(counts, "approved", "prepared")
        return {
            "label": f"Upload {_plural(n, 'approved photo')}",
            "href": _href(batch_id, "upload"),
        }
    if st == "submit":
        return {"label": "Submit on Shutterstock", "href": _href(batch_id, "submit")}
    if st == "done":
        return {"label": "See all batches", "href": "/"}
    return {"label": "Add photos", "href": "/add"}


def steps(batch_id: str, counts: dict[str, int]) -> list[dict[str, str]]:
    """The five flow steps as `{key, label, href, state}`; state is done / current / todo."""
    st = stage(counts)
    current = _STAGE_STEP.get(st)
    idx = _FLOW.index(current) if current else len(_FLOW)
    out: list[dict[str, str]] = []
    for i, key in enumerate(_FLOW):
        state = "done" if i < idx else "current" if i == idx else "todo"
        href = "/add" if key == "add" else _href(batch_id, key)
        out.append({"key": key, "label": STEP_LABELS[key], "href": href, "state": state})
    return out


BUSY_WHAT = "shuto is busy with another task."
_BUSY_ERROR = (
    ("database is locked", "database is busy"),
    BUSY_WHAT,
    "Wait a moment, then try again. Make sure shuto is not open in two windows.",
)

_AI_ERRORS: tuple[tuple[tuple[str, ...], str, str], ...] = (
    _BUSY_ERROR,
    (
        ("ollama is not running", "not running", "connection refused", "urlopen error"),
        "The AI helper is not running.",
        "Open Ollama from the Start menu (or Applications), then press Try again.",
    ),
    (
        ("timed out", "timeout"),
        "The AI took too long to answer.",
        "Wait a minute, then press Try again. A smaller model may be faster.",
    ),
    (
        ("model not pulled", "not in the model catalog", "no local model", "choose a model"),
        "No AI model is ready yet.",
        "Open Settings, pick an AI model and download it, then press Try again.",
    ),
    (
        ("api_key", "api key", "access key"),
        "The online AI needs an access key that is missing.",
        "Open Settings, add the access key under the online AI, or choose a model on this "
        "computer.",
    ),
    (
        ("not enough free", "no space", "disk space", "disk full"),
        "There is not enough free space on this computer.",
        "Free some space or pick a smaller model, then press Try again.",
    ),
)

# Checked first for deliver / upload / login-test jobs: the same words mean something else there.
_UPLOAD_ERRORS: tuple[tuple[tuple[str, ...], str, str], ...] = (
    (
        ("login incorrect", "authentication failed", "ftps login", "credentials", "not logged in"),
        "Shutterstock did not accept the upload login.",
        "Check the username and password in Settings, use Test login, then press Try again.",
    ),
    (
        ("timed out", "timeout", "connection refused", "connection reset", "name or service"),
        "Could not reach Shutterstock.",
        "Check your internet connection, then press Try again. Photos that already went "
        "through are not sent twice.",
    ),
    (
        ("uploading is disabled", "not confirmed"),
        "The upload was not confirmed.",
        "Tick the box that says you want to upload, then press the button again.",
    ),
)

_FALLBACK_TODO = "Press Try again. If it keeps happening, open Help for common fixes."


def friendly_error(exc: BaseException | str, context: str = "ai") -> dict[str, str]:
    """Turn an internal error into `{what, todo}` for non-technical readers.

    `context` is "ai" (describe / check / model jobs) or "upload" (deliver, upload, login test).
    With "upload" the upload and login needles are checked first, so a timeout is not reported
    as a slow AI.
    """
    raw = str(exc).strip()
    low = raw.lower()
    tables = (_UPLOAD_ERRORS + _AI_ERRORS) if context == "upload" else _AI_ERRORS
    for needles, what, todo in tables:
        if any(n in low for n in needles):
            return {"what": what, "todo": todo}
    if context != "upload":
        for needles, what, todo in _UPLOAD_ERRORS:
            if any(n in low for n in needles if n not in ("timed out", "timeout")):
                return {"what": what, "todo": todo}
    return {"what": raw or "Something went wrong.", "todo": _FALLBACK_TODO}
