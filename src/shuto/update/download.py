"""Downloading the release wheel: GitHub hosts only, size cap, sha256 checked against the
digest from the release API before the file is kept.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from shuto.core.config import Config
from shuto.core.files import sha256_file
from shuto.update.release import (
    MSG_DOWNLOAD,
    MSG_MISMATCH,
    MSG_NO_DIGEST,
    SHA256_RE,
    UpdateError,
    current_version,
    wheel_name,
    wheel_url,
)

# The release download redirects to GitHub's file storage; nothing else is followed.
WHEEL_HOSTS = frozenset(
    {"github.com", "objects.githubusercontent.com", "release-assets.githubusercontent.com"}
)
MAX_WHEEL_BYTES = 200 * 1024 * 1024
DOWNLOAD_TIMEOUT_S = 30

sha256_of = sha256_file  # alias until T9


def wheels_dir(cfg: Config) -> Path:
    """Where verified wheels are kept (the installed tool's receipt points here)."""
    return cfg.workspace_path("updates", "wheels")


def _is_wheel_host(url: str) -> bool:
    parts = urllib.parse.urlsplit(url)
    try:
        port = parts.port
    except ValueError:
        return False
    return parts.scheme == "https" and parts.hostname in WHEEL_HOSTS and port in (None, 443)


class _GithubOnlyRedirect(urllib.request.HTTPRedirectHandler):
    """Follow redirects only to https URLs on GitHub's own download hosts."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _is_wheel_host(newurl):
            raise urllib.error.URLError("redirect to a foreign host refused")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def download_wheel(
    version: str,
    sha256: str,
    folder: Path,
    opener: Any = None,
    timeout: float = DOWNLOAD_TIMEOUT_S,
) -> Path:
    """Download the release wheel into `folder`, keep it only if its sha256 matches.

    A file already there with the right hash is reused. Any problem is an UpdateError.
    """
    if not SHA256_RE.fullmatch(sha256):
        raise UpdateError(MSG_NO_DIGEST)
    dest = folder / wheel_name(version)
    try:
        if dest.is_file() and hmac.compare_digest(sha256_of(dest), sha256):
            return dest
        folder.mkdir(parents=True, exist_ok=True)
    except OSError:
        raise UpdateError(MSG_DOWNLOAD) from None
    opener = opener or urllib.request.build_opener(_GithubOnlyRedirect)
    part = dest.with_name(dest.name + ".part")
    req = urllib.request.Request(
        wheel_url(version), headers={"User-Agent": f"shuto/{current_version()}"}
    )
    h = hashlib.sha256()
    try:
        resp = opener.open(req, timeout=timeout)
        try:
            if not _is_wheel_host(resp.geturl()):
                raise ValueError("final address is not on GitHub")
            size = 0
            with part.open("wb") as out:
                while chunk := resp.read(1024 * 1024):
                    size += len(chunk)
                    if size > MAX_WHEEL_BYTES:
                        raise ValueError("download too large")
                    h.update(chunk)
                    out.write(chunk)
        finally:
            close = getattr(resp, "close", None)
            if close:
                close()
        if not hmac.compare_digest(h.hexdigest(), sha256):
            raise UpdateError(MSG_MISMATCH)
        os.replace(part, dest)
    except UpdateError:
        raise
    except Exception:
        raise UpdateError(MSG_DOWNLOAD) from None
    finally:
        part.unlink(missing_ok=True)
    return dest


def prune_wheels(folder: Path, keep: set[str]) -> None:
    """Delete every stored wheel (and half download) except the named files."""
    try:
        entries = list(folder.iterdir())
    except OSError:
        return
    for entry in entries:
        if entry.name not in keep and entry.name.startswith("shuto-"):
            try:
                entry.unlink()
            except OSError:
                pass
