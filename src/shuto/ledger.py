"""The uploaded.csv ledger: a plain, portable list of photos already sent to Shutterstock.

UTF-8 CSV, one row per photo, keyed by sha256. Rewritten atomically (temp file + replace) so a
crash never leaves half a file. Pure file handling; the database side lives in `pipeline`.
"""

from __future__ import annotations

import csv
import os
import tempfile
from dataclasses import dataclass, fields
from datetime import datetime
from pathlib import Path

_FORMULA_START = ("=", "+", "-", "@")

COLUMNS = ("sha256", "dhash", "orig_name", "submit_name", "uploaded_at", "batch_id")


def _safe(value: str) -> str:
    """Keep spreadsheet apps from reading a cell as a formula."""
    return "'" + value if value.startswith(_FORMULA_START) else value


def _unsafe(value: str) -> str:
    return value[1:] if value.startswith("'") and value[1:].startswith(_FORMULA_START) else value


@dataclass
class LedgerRow:
    sha256: str
    dhash: int | None = None
    orig_name: str = ""
    submit_name: str = ""
    uploaded_at: str = ""
    batch_id: str = ""

    def to_record(self) -> list[str]:
        return [
            _safe(self.sha256),
            "" if self.dhash is None else str(self.dhash),
            _safe(self.orig_name),
            _safe(self.submit_name),
            _safe(self.uploaded_at),
            _safe(self.batch_id),
        ]


def read_ledger(path: str | Path) -> list[LedgerRow]:
    """Rows of a ledger file (missing file = empty). Rows without a sha256 are ignored."""
    p = Path(path)
    if not p.is_file():
        return []
    rows: list[LedgerRow] = []
    with p.open("r", encoding="utf-8-sig", newline="") as fh:
        for rec in csv.DictReader(fh):
            sha = (rec.get("sha256") or "").strip().lower()
            if not sha:
                continue
            raw = (rec.get("dhash") or "").strip()
            rows.append(
                LedgerRow(
                    sha256=sha,
                    dhash=int(raw) if raw.isdigit() else None,
                    orig_name=_unsafe(rec.get("orig_name") or ""),
                    submit_name=_unsafe(rec.get("submit_name") or ""),
                    uploaded_at=_unsafe(rec.get("uploaded_at") or ""),
                    batch_id=_unsafe(rec.get("batch_id") or ""),
                )
            )
    return rows


def write_ledger(path: str | Path, rows: list[LedgerRow]) -> Path:
    """Write the whole ledger atomically; returns the path."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=p.name + ".", suffix=".tmp", dir=p.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(COLUMNS)
            writer.writerows(r.to_record() for r in rows)
        os.replace(tmp, p)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return p


def merge(existing: list[LedgerRow], newer: list[LedgerRow]) -> list[LedgerRow]:
    """Union by sha256; `newer` wins but never blanks a value the older row had."""
    merged = {r.sha256: r for r in existing}
    for r in newer:
        old = merged.get(r.sha256)
        if old is not None:
            for f in fields(LedgerRow):
                if getattr(r, f.name) in (None, ""):
                    setattr(r, f.name, getattr(old, f.name))
        merged[r.sha256] = r
    return list(merged.values())


def back_up_corrupt(path: str | Path) -> Path:
    """Move an unreadable ledger aside (uploaded.csv.bad-<timestamp>) so it can be rebuilt."""
    p = Path(path)
    dest = p.with_name(f"{p.name}.bad-{datetime.now():%Y%m%d-%H%M%S}")
    os.replace(p, dest)
    return dest
