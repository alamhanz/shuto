"""Small text and time helpers."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def yes_no(value: object) -> str:
    return "yes" if bool(value) else "no"


def fold_words(words: Iterable[str]) -> list[str]:
    """The words trimmed, empty ones dropped and repeats (ignoring case) removed, in order."""
    seen: set[str] = set()
    out: list[str] = []
    for w in words:
        w = str(w).strip()
        if w and w.lower() not in seen:
            seen.add(w.lower())
            out.append(w)
    return out
