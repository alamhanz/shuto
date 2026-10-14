"""Progress and cancel callback types."""

from __future__ import annotations

from collections.abc import Callable

Progress = Callable[[int, int, str], None]


Cancel = Callable[[], bool]


def noop(done: int, total: int, message: str = "") -> None:
    return None
