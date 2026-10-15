"""Plain-language errors of the models-folder change."""

from __future__ import annotations


class StoreError(Exception):
    """Something failed. The message is plain language and safe to show."""


class StoreUnsupported(StoreError):
    """Not available on this system (Linux): the Help page has the steps."""


class MoveCancelled(StoreError):
    """The user stopped the move."""
