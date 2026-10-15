"""The per-batch report CSV: one row per photo with every check and its reason.

UTF-8 with BOM (so Excel reads Japanese file names), formula-safe cells, written atomically.
Pure data shaping and file handling: no database and no config here; `workflow` feeds it.
"""

from __future__ import annotations

import csv
import hashlib
import io
import os
import re
import tempfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from shuto.core.models import UPLOADED_STATES, Photo
from shuto.deliver.ledger import safe_cell
from shuto.places.resolve import default_flag, short_place_text

FIXED_BEFORE = (
    "file",
    "batch",
    "sha256",
    "duplicate_of",
    "quality",
    "quality_reasons",
    "shutterstock_rules",
    "rules_hit",
    "theme",
    "theme_fit",
    "theme_reason",
)
FIXED_AFTER = (
    "your_rules_reasons",
    "set_aside_by",
    "approved_by_hand",
    "status",
    "status_label",
    "title",
    "place",
    "place_source",
    "default_place",
    "keyword_count",
    "metadata_issues",
    "metadata_language",
    "ai_model",
    "ai_seconds",
    "ai_tries",
    "ai_max_tries",
    "checked_at",
    "approved_at",
    "uploaded_at",
    "submitted_at",
    "accepted_at",
    "rejected_at",
    "rejection_reason",
)

_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


def rule_label(rule: dict[str, Any]) -> str:
    """'Must include: a car' / 'Must avoid: people' from a stored rule snapshot."""
    head = "Must include" if rule.get("kind") == "include" else "Must avoid"
    return f"{head}: {rule.get('text', '')}"


@dataclass
class ReportRow:
    cells: dict[str, str] = field(default_factory=dict)  # fixed columns by name
    rules: dict[str, str] = field(default_factory=dict)  # per-rule columns by label


def _described_at(p: Photo, times: dict[str, str]) -> str:
    """When the AI last wrote this photo's text, for photos checked with no theme or rules."""
    if not p.metadata_source:
        return ""
    found = [times[s] for s in ("described", "needs_review", "blocked", "set_aside") if s in times]
    return max(found, default="")


def _row(
    p: Photo,
    times: dict[str, str],
    duplicate_of: str,
    labels: Callable[[str], str],
    rejection: tuple[str, str] | None,
    ai_time: tuple[float, int] | None = None,
) -> ReportRow:
    cull = p.cull or {}
    guard = p.guardrail or {}
    flt = p.filters or {}
    meta = p.metadata or {}
    theme = flt.get("theme") or {}
    rules = [r for r in flt.get("rules") or [] if isinstance(r, dict)]
    by_id = {r.get("id"): r for r in rules}

    aside = []
    for key in flt.get("set_aside_by") or []:
        if key == "theme":
            aside.append("Theme")
        elif str(key).startswith("rule:") and by_id.get(str(key)[5:]):
            aside.append(rule_label(by_id[str(key)[5:]]))
    reasons = [
        f"{rule_label(r)}: {r.get('reason') or r.get('result')}"
        for r in rules
        if r.get("result") in ("fail", "unknown")
    ]
    hits: list[str] = []
    for h in guard.get("hits") or []:
        name = h.get("name") or h.get("matched") or h.get("kind") or ""
        if name and name not in hits:
            hits.append(name)
    status = str(p.status)
    sent = p.status in UPLOADED_STATES  # a photo sent again has no earlier upload time
    rejected_at, rejected_why = rejection or ("", "")
    keywords = meta.get("keywords") or []

    cells = {
        "file": p.orig_name,
        "batch": p.batch_id or "",
        "sha256": p.sha256,
        "duplicate_of": duplicate_of,
        "quality": "" if not cull else ("ok" if cull.get("ok") else "failed"),
        "quality_reasons": "; ".join(str(r) for r in cull.get("reasons") or []),
        "shutterstock_rules": str(guard.get("action") or ""),
        "rules_hit": "; ".join(hits),
        "theme": str(theme.get("text") or ""),
        "theme_fit": str(theme.get("fit") or ""),
        "theme_reason": str(theme.get("reason") or ""),
        "your_rules_reasons": " | ".join(reasons),
        "set_aside_by": "; ".join(aside),
        "approved_by_hand": "yes" if flt.get("overridden") else "",
        "status": status,
        "status_label": labels(status),
        "title": str(meta.get("title") or ""),
        "place": short_place_text(meta.get("place")),
        "place_source": str((meta.get("place") or {}).get("source") or ""),
        "default_place": default_flag(meta.get("place")),
        "keyword_count": str(len(keywords)) if keywords else "",
        "metadata_issues": "; ".join(
            str(i.get("text") or "")
            for i in meta.get("metadata_issues") or []
            if isinstance(i, dict)
        ),
        "metadata_language": str(meta.get("language") or ""),
        "ai_model": str(flt.get("model") or ""),
        "ai_seconds": f"{ai_time[0]:.1f}" if ai_time else "",
        "ai_tries": str(ai_time[1]) if ai_time else "",
        "ai_max_tries": str((meta.get("ai_run") or {}).get("max_tries") or ""),
        "checked_at": str(flt.get("at") or _described_at(p, times)),
        "approved_at": times.get("approved", ""),
        "uploaded_at": p.uploaded_at or (times.get("uploaded", "") if sent else ""),
        "submitted_at": p.submitted_at or (times.get("submitted", "") if sent else ""),
        "accepted_at": times.get("accepted", ""),
        "rejected_at": rejected_at or times.get("rejected", ""),
        "rejection_reason": rejected_why,
    }
    if not cells["ai_model"] and str(p.metadata_source or "").startswith("llm:"):
        cells["ai_model"] = str(p.metadata_source)[4:]
    return ReportRow(cells, {rule_label(r): str(r.get("result") or "") for r in rules})


def build_rows(
    photos: Iterable[Photo],
    *,
    times: dict[int, dict[str, str]],
    duplicate_of: dict[int, str],
    labels: Callable[[str], str],
    rejections: dict[int, tuple[str, str]],
    ai_times: dict[int, tuple[float, int]] | None = None,
) -> tuple[list[str], list[list[str]]]:
    """Header and records for a batch. Per-rule columns are the union of the photos' rule
    snapshots in first-seen order; a cell is pass / fail / unknown, or blank when the photo was
    not checked with that rule."""
    rows = [
        _row(
            p,
            times.get(p.id or 0, {}),
            duplicate_of.get(p.id or 0, ""),
            labels,
            rejections.get(p.id or 0),
            (ai_times or {}).get(p.id or 0),
        )
        for p in photos
    ]
    rule_cols: list[str] = []
    for r in rows:
        for label in r.rules:
            if label not in rule_cols:
                rule_cols.append(label)
    header = [*FIXED_BEFORE, *rule_cols, *FIXED_AFTER]
    records = []
    for r in rows:
        values = {**r.cells, **r.rules}
        records.append([values.get(col, "") for col in header])
    return header, records


def ai_time_totals(ai_times: dict[int, tuple[float, int]]) -> dict[str, float | int]:
    """Total seconds, number of photos with an AI time and the average per photo."""
    n = len(ai_times)
    total = sum(sec for sec, _ in ai_times.values())
    return {"total": total, "photos": n, "average": total / n if n else 0.0}


def render(header: list[str], records: list[list[str]]) -> bytes:
    """The whole CSV as bytes: BOM, CRLF rows, every text cell formula-safe."""
    buf = io.StringIO(newline="")
    writer = csv.writer(buf, lineterminator="\r\n")
    writer.writerow([safe_cell(str(h)) for h in header])
    writer.writerows([safe_cell(str(c)) for c in rec] for rec in records)
    return buf.getvalue().encode("utf-8-sig")


def write_report(path: str | Path, header: list[str], records: list[list[str]]) -> Path:
    """Write the report atomically (temp file + replace); returns the path."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    data = render(header, records)
    fd, tmp = tempfile.mkstemp(prefix=p.name + ".", suffix=".tmp", dir=p.parent)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, p)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return p


def report_filename(batch_id: str) -> str:
    """`<safe>.csv`; a short hash is added when making the name safe changed it."""
    safe = _UNSAFE_NAME.sub("_", batch_id).strip("._") or "batch"
    if safe != batch_id:
        safe += "-" + hashlib.sha1(batch_id.encode("utf-8")).hexdigest()[:8]
    return safe + ".csv"
