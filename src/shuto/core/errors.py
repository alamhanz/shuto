"""Errors the user can fix; shown as friendly messages."""

from __future__ import annotations


class PipelineError(Exception):
    """A problem the user can fix (bad input, missing key, refused action). Message is friendly."""


class UploadRefused(PipelineError):
    pass


class SettingsError(PipelineError):
    """A saved-setting value that is not valid (the message says what to fix)."""
