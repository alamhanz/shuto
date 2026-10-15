"""Finding the newest release: GitHub URLs, version rules, the latest-version probe and the
sha256 digest GitHub publishes for the release wheel.

Stdlib only. Only validated version strings are put in any URL.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from importlib import metadata
from typing import Any

REPO_RELEASES = "https://github.com/alamhanz/shuto/releases"
LATEST_PAGE = REPO_RELEASES + "/latest"
WHEEL_PREFIX = REPO_RELEASES + "/download/v"
VERSION_RE = re.compile(r"\d+\.\d+\.\d+")  # final releases only; always use fullmatch
TAG_URL_RE = re.compile(r"https://github\.com/alamhanz/shuto/releases/tag/v(\d+\.\d+\.\d+)")
SHA256_RE = re.compile(r"[0-9a-f]{64}")
DIGEST_RE = re.compile(r"sha256:([0-9a-f]{64})")
RELEASE_API = "https://api.github.com/repos/alamhanz/shuto/releases/tags/v"
MAX_API_BYTES = 2 * 1024 * 1024
MSG_NO_DIGEST = "Could not check the update download. Try again later."
MSG_DOWNLOAD = "shuto could not download the update. Try again later."
MSG_MISMATCH = (
    "The downloaded update did not match what was published, so nothing was changed. "
    "Try again later."
)
REDIRECTS = (301, 302, 303, 307, 308)
HTTP_TIMEOUT_S = 5


class UpdateError(Exception):
    """The update could not be started; the message is plain language."""


@dataclass(frozen=True)
class Latest:
    version: str
    wheel_url: str
    notes_url: str
    checked_at: float


def wheel_url(version: str) -> str:
    if not VERSION_RE.fullmatch(version):
        raise ValueError(f"Not a release version: {version!r}")
    return f"{WHEEL_PREFIX}{version}/shuto-{version}-py3-none-any.whl"


def notes_url(version: str) -> str:
    if not VERSION_RE.fullmatch(version):
        raise ValueError(f"Not a release version: {version!r}")
    return f"{REPO_RELEASES}/tag/v{version}"


def wheel_name(version: str) -> str:
    if not VERSION_RE.fullmatch(version):
        raise ValueError(f"Not a release version: {version!r}")
    return f"shuto-{version}-py3-none-any.whl"


def current_version() -> str:
    try:
        return metadata.version("shuto")
    except metadata.PackageNotFoundError:
        return "dev"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def fetch_wheel_digest(version: str, opener: Any = None, timeout: float = HTTP_TIMEOUT_S) -> str:
    """The sha256 GitHub reports for this release's wheel. Any problem is an UpdateError.

    Redirects are not followed: api.github.com answers this URL directly.
    """
    if not VERSION_RE.fullmatch(version):
        raise UpdateError(MSG_NO_DIGEST)
    url = RELEASE_API + version  # only a validated version goes into the URL
    opener = opener or urllib.request.build_opener(_NoRedirect)
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": f"shuto/{current_version()}",
            "Accept": "application/vnd.github+json",
        },
    )
    try:
        resp = opener.open(req, timeout=timeout)
        try:
            data = json.loads(resp.read(MAX_API_BYTES))
        finally:
            close = getattr(resp, "close", None)
            if close:
                close()
        if not isinstance(data, dict) or data.get("tag_name") != f"v{version}":
            raise ValueError("wrong release")
        name = wheel_name(version)
        digests = [
            a.get("digest")
            for a in data.get("assets") or []
            if isinstance(a, dict) and a.get("name") == name
        ]
        match = DIGEST_RE.fullmatch(digests[0]) if len(digests) == 1 else None
        if not match:
            raise ValueError("no usable digest")
        return match.group(1)
    except Exception:
        raise UpdateError(MSG_NO_DIGEST) from None


def _parts(version: str) -> tuple[int, ...] | None:
    if not VERSION_RE.fullmatch(version):
        return None
    return tuple(int(p) for p in version.split("."))


def is_newer(latest: str, current: str) -> bool:
    a, b = _parts(latest), _parts(current)
    return a is not None and b is not None and a > b


def fetch_latest(
    opener: Any = None, timeout: float = HTTP_TIMEOUT_S, wheel_opener: Any = None
) -> Latest | None:
    """Ask GitHub which release is the latest. Any problem (offline, odd answer) gives None.

    The wheel is only probed: a redirect from the release URL proves it exists, and it is not
    followed, so nothing is downloaded.
    """
    if opener is None:
        opener = urllib.request.build_opener()
        wheel_opener = wheel_opener or urllib.request.build_opener(_NoRedirect)
    wheel_opener = wheel_opener or opener
    headers = {"User-Agent": f"shuto/{current_version()}"}

    def head(client: Any, url: str) -> tuple[str, int]:
        req = urllib.request.Request(url, method="HEAD", headers=headers)
        try:
            resp = client.open(req, timeout=timeout)
        except urllib.error.HTTPError as err:
            location = err.headers.get("Location", "") if err.headers else ""
            if err.code in REDIRECTS and location.startswith("https://"):
                return url, 200
            raise
        try:
            return resp.geturl(), resp.getcode()
        finally:
            close = getattr(resp, "close", None)
            if close:
                close()

    try:
        final, _ = head(opener, LATEST_PAGE)
        match = TAG_URL_RE.fullmatch(final)
        if not match:
            return None
        version = match.group(1)
        _, code = head(wheel_opener, wheel_url(version))
        if code != 200:
            return None
        return Latest(version, wheel_url(version), notes_url(version), time.time())
    except Exception:
        return None


def _latest_from_cache(cache: Any) -> Latest | None:
    if not isinstance(cache, dict) or not cache.get("ok"):
        return None
    version = cache.get("version")
    if not isinstance(version, str) or not VERSION_RE.fullmatch(version):
        return None
    return Latest(version, wheel_url(version), notes_url(version), float(cache["checked_at"]))
