"""FTPS login status and test."""

from __future__ import annotations

import ftplib
from collections.abc import Callable

from shuto.core.config import Config
from shuto.core.errors import PipelineError
from shuto.core.secrets import get_secret, keyring_available
from shuto.deliver import ftps, send


def ftps_login_status(cfg: Config) -> dict[str, bool]:
    """Which parts of the Shutterstock login are saved (never the values)."""
    return {
        "user_set": bool(get_secret("SHUTO_FTPS_USER")),
        "password_set": bool(get_secret("SHUTO_FTPS_PASSWORD")),
        "keyring_ok": keyring_available(),
    }


def test_ftps_login(cfg: Config, *, connect: Callable[..., object] | None = None) -> None:
    """Log in to the upload server and log out again; PipelineError with plain-words advice."""
    try:
        creds = send.creds_from_secrets(cfg.deliver.ftps_host)
    except RuntimeError as exc:
        raise PipelineError(
            "Your Shutterstock upload login is not saved yet. Enter it in Settings."
        ) from exc
    try:
        ftp = (connect or ftps.connect)(creds.host, creds.username, creds.password)
    except ftplib.error_perm as exc:
        raise PipelineError(
            "The upload server refused this email or password. Check them and try again."
        ) from exc
    except Exception as exc:
        raise PipelineError(
            f"Could not reach {creds.host}. Check your internet connection and try again "
            f"({type(exc).__name__})."
        ) from exc
    ftps.close(ftp)
