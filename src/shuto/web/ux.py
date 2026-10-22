"""Plain-language vocabulary and batch-flow helpers for the web UI.

Every user-visible word that is not page copy lives here, so the UI can be translated later.
Nothing in this module touches the database or the network.
"""

from __future__ import annotations

import re
from urllib.parse import quote

from shuto.core.status_text import BUCKET_LABELS, BUCKETS, LABELS, bucket, label  # noqa: F401 (re-exported)

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
    "aside": "Left out on purpose: poor quality, copies, photos you sent before, or photos that "
    "don't fit your theme or your own rules. Open one and press Approve anyway if the AI got it "
    "wrong.",
    "rules": "The AI checks each photo against your theme and your rules while it writes the "
    "title. Small AI models can be wrong, so read the reason and approve by hand when it missed.",
    "commercial": "Commercial photos can be used in ads and products. People and private "
    "places may need a release.",
    "editorial": "Editorial photos show real news or events and cannot be used in ads.",
    "csv": "The Shutterstock info file (CSV) holds the titles and keywords. In the last step you "
    "upload it with the Upload CSV button on the Shutterstock website (Portfolio, Submit "
    "content, Not submitted tab).",
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


def ai_time_text(seconds: float) -> str:
    """'4.2 s', '45 s', '3 min 12 s' or '1 h 5 min' for a number of seconds."""
    if 0 < seconds < 10:
        return f"{seconds:.1f} s"
    total = max(0, round(seconds))
    if total < 60:
        return f"{total} s"
    minutes, secs = divmod(total, 60)
    if minutes < 60:
        return f"{minutes} min {secs} s" if secs else f"{minutes} min"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes} min" if minutes else f"{hours} h"


def _n(counts: dict[str, int], *names: str) -> int:
    return sum(int(counts.get(name, 0) or 0) for name in names)


def stage(counts: dict[str, int]) -> str:
    """Where the batch is in the flow; first match wins."""
    if _n(counts, "imported") > 0:
        return "check"
    if _n(counts, "ready", "described", "needs_review", "blocked") > 0:
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
        elif _n(counts, "described"):
            text = f"Review {_plural(_n(counts, 'described'), 'photo')}"
        else:
            text = f"The AI is writing text for {_plural(_n(counts, 'ready'), 'photo')}"
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
    aside = _n(counts, "set_aside")
    if aside:  # every photo was set aside by the theme or your rules: keep a way back to them
        label_ = f"Review {_plural(aside, 'set-aside photo')}"
        return {"label": label_, "href": _href(batch_id, "review") + "?show=aside"}
    return {"label": "Add photos", "href": "/add"}


def _reachable(counts: dict[str, int]) -> dict[str, bool]:
    """Which steps have something to show, so their link works even before they are current."""
    return {
        "add": True,
        "check": _n(counts, "imported", "ready") > 0,
        "review": sum(int(v or 0) for v in counts.values()) - _n(counts, "imported") > 0,
        "upload": _n(counts, "approved", "prepared") > 0,
        "submit": _n(counts, "uploaded", "exported") > 0,
    }


def steps(batch_id: str, counts: dict[str, int]) -> list[dict[str, str]]:
    """The five flow steps as `{key, label, href, state, link}`.

    state is done / current / todo. `link` is "1" when the step has a working link whatever its
    state: a step with photos can be opened before it is the current one.
    """
    st = stage(counts)
    current = _STAGE_STEP.get(st)
    idx = _FLOW.index(current) if current else len(_FLOW)
    reach = _reachable(counts)
    out: list[dict[str, str]] = []
    for i, key in enumerate(_FLOW):
        state = "done" if i < idx else "current" if i == idx else "todo"
        href = "/add" if key == "add" else _href(batch_id, key)
        link = "1" if state != "todo" or reach[key] else ""
        out.append(
            {"key": key, "label": STEP_LABELS[key], "href": href, "state": state, "link": link}
        )
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
        ("download stalled",),
        "The download stalled.",
        "Check your internet connection, then press Try again. What was already downloaded "
        "is kept, so it continues where it stopped.",
    ),
    (
        ("download connection was lost",),
        "The download was interrupted.",
        "Check your internet connection and that the Ollama app is still open, then press "
        "Try again. What was already downloaded is kept, so it continues where it stopped.",
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


ERROR_PREFIX_RE = re.compile(r"^[A-Za-z_.]*(?:Error|Exception|Busy|Refused): ")
UPLOAD_JOB_KINDS = ("deliver", "ftps_test", "upload")


def friendly(error: str | None, kind: str = "") -> dict[str, str]:
    """Plain-words error; upload-type jobs get upload advice instead of AI advice."""
    context = "upload" if kind in UPLOAD_JOB_KINDS else "ai"
    return friendly_error(ERROR_PREFIX_RE.sub("", error or ""), context=context)
