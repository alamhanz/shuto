"""Upload originals to Shutterstock's contributor FTPS server.

Shutterstock: host ftps.shutterstock.com, port 21, explicit TLS (AUTH TLS), passive mode,
login = contributor email/username + account password.
"""

from __future__ import annotations

import ftplib
import io
import time
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path

DEFAULT_HOST = "ftps.shutterstock.com"


class _SessionReuseFTPS(ftplib.FTP_TLS):
    """FTP_TLS that reuses the control connection's TLS session on the data channel.

    Many FTPS servers require this ("522 session reuse required"); stdlib ftplib doesn't do it.
    """

    def ntransfercmd(self, cmd, rest=None):
        conn, size = ftplib.FTP.ntransfercmd(self, cmd, rest)
        if self._prot_p:
            conn = self.context.wrap_socket(
                conn, server_hostname=self.host, session=self.sock.session
            )
        return conn, size


def connect(host: str, username: str, password: str, timeout: int = 120) -> ftplib.FTP_TLS:
    ftp = _SessionReuseFTPS(timeout=timeout)
    ftp.connect(host, 21)
    ftp.auth()
    ftp.login(username, password)
    ftp.prot_p()  # encrypt the data channel too
    ftp.set_pasv(True)
    return ftp


def upload_file(
    host: str, username: str, password: str, filename: str, data: bytes, timeout: int = 120
) -> None:
    """Upload one file over explicit FTPS."""
    ftp = connect(host, username, password, timeout)
    try:
        ftp.storbinary(f"STOR {filename}", io.BytesIO(data))
    finally:
        close(ftp)


def close(ftp) -> None:
    """Close politely, never raising."""
    try:
        ftp.quit()
    except Exception:  # noqa: BLE001
        try:
            ftp.close()
        except Exception:  # noqa: BLE001
            pass


def _is_dropped(exc: BaseException) -> bool:
    """Connection-level failures (worth a reconnect), as opposed to server refusals."""
    if isinstance(exc, ftplib.error_perm):
        return False
    return isinstance(exc, (ftplib.error_temp, EOFError, OSError))


RECONNECT_WAITS: tuple[float, ...] = (5, 15, 30, 60, 120)  # about 4 minutes in all
_CANCEL_POLL = 0.5  # seconds between cancel checks while waiting for the internet


LOST_PREFIX = "The internet connection was lost."


def lost_message(n: int) -> str:
    """Plain words for photos left unsent after the internet stayed away."""
    photos = "1 photo was" if n == 1 else f"{n} photos were"
    return f"{LOST_PREFIX} {photos} not sent; press Upload to send them."


class _CountingReader:
    """File wrapper that reports how many bytes were read (works with any storbinary)."""

    def __init__(self, fh, on_bytes: Callable[[int], None]):
        self._fh, self._on_bytes, self._sent = fh, on_bytes, 0

    def read(self, size: int = -1) -> bytes:
        block = self._fh.read(size)
        self._sent += len(block)
        self._on_bytes(self._sent)
        return block


def _wait_for_internet(
    reconnect: Callable[[], ftplib.FTP],
    waits: Sequence[float],
    cancel: Callable[[], bool] | None,
    on_wait: Callable[[str], None] | None,
    sleep: Callable[[float], None],
):
    """Reconnect now, then again after each wait: (connection | None, cancelled, refusal).

    A server refusal (e.g. a wrong login) is returned at once as `refusal`, never retried.
    """
    attempt = 0
    while True:
        try:
            return reconnect(), False, None
        except Exception as exc:  # noqa: BLE001
            if not _is_dropped(exc):
                return None, False, exc
            if attempt >= len(waits):
                return None, False, None
        wait = waits[attempt]
        attempt += 1
        if on_wait:
            shown = f"{wait:g}"
            on_wait(
                f"Connection lost. Trying again in {shown} s (attempt {attempt} of {len(waits)})"
            )
        left = float(wait)
        while left > 0:
            if cancel and cancel():
                return None, True, None
            step = min(_CANCEL_POLL, left)
            sleep(step)
            left -= step
        if cancel and cancel():
            return None, True, None


def upload_many(
    ftp,
    items: Iterable[tuple[str, Path]],
    on_result: Callable[[str, Exception | None], None],
    reconnect: Callable[[], ftplib.FTP] | None = None,
    *,
    cancel: Callable[[], bool] | None = None,
    on_start: Callable[[str], None] | None = None,
    on_bytes: Callable[[int], None] | None = None,
    on_wait: Callable[[str], None] | None = None,
    waits: Sequence[float] = RECONNECT_WAITS,
    sleep: Callable[[float], None] = time.sleep,
):
    """Upload (remote_name, local_path) pairs; a failure never stops the batch.

    on_result(remote_name, None) on success or on_result(remote_name, exc) on failure.
    If the connection drops, `reconnect` is tried (now, then after each of `waits` seconds, with
    on_wait(text) before each wait) and the file is retried. If the internet stays away, the
    remaining files fail with a plain message. Server refusals are never retried.
    `cancel` is checked before each file and during waits: the file in transfer is finished, the
    rest get no result at all. Returns the connection in use at the end (the caller closes it).
    """
    pending = list(items)
    for index, (name, path) in enumerate(pending):
        if cancel and cancel():
            break
        if on_start:
            on_start(name)
        error: Exception | None = None
        for attempt in (1, 2):
            try:
                fh = open(path, "rb")  # noqa: SIM115 - a local read error is not a dropped link
            except OSError as exc:
                error = exc
                break
            try:
                with fh:
                    src = _CountingReader(fh, on_bytes) if on_bytes else fh
                    ftp.storbinary(f"STOR {name}", src)
                error = None
                break
            except Exception as exc:  # noqa: BLE001
                error = exc
                if attempt == 2 or reconnect is None or not _is_dropped(exc):
                    break
                close(ftp)
                new, cancelled, refusal = _wait_for_internet(
                    reconnect, waits, cancel, on_wait, sleep
                )
                if cancelled:
                    return ftp
                if new is None:
                    left = len(pending) - index
                    failure = refusal or ConnectionError(lost_message(left))
                    for rest, _ in pending[index:]:
                        on_result(rest, failure)
                    return ftp
                ftp = new
        on_result(name, error)
    return ftp
