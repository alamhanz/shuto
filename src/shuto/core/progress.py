"""Progress and cancel callback types."""

from __future__ import annotations

from typing import Protocol


class Progress(Protocol):
    """`progress(done, total, message)`: report how far a long task is."""

    def __call__(self, done: int, total: int, message: str = "", /) -> None: ...


class Cancel(Protocol):
    """`cancel()` is True once the user asked to stop."""

    def __call__(self) -> bool: ...


def noop(done: int, total: int, message: str = "") -> None:
    return None


class ScaledProgress:
    """Maps a task's own progress onto part of a bigger bar.

    `ScaledProgress(progress, start, span, scale)(done, total, message)` reports
    `start + span * done // total` out of `scale` to `progress`, so two tasks can share one bar
    (the first uses units 0..100 of 1000, the second 100..1000).
    """

    def __init__(self, progress: Progress, start: int, span: int, scale: int):
        self._progress = progress
        self.start = start
        self.span = span
        self.scale = scale

    def __call__(self, done: int, total: int, message: str = "") -> None:
        self._progress(self.start + self.span * done // max(total, 1), self.scale, message)
