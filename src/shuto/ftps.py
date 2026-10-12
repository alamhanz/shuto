"""Upload originals to Shutterstock's contributor FTPS server.

Shutterstock: host ftps.shutterstock.com, port 21, explicit TLS (AUTH TLS), passive mode,
login = contributor email/username + account password.
"""

from __future__ import annotations

import ftplib
import io
from collections.abc import Callable, Iterable
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


def upload_many(
    ftp,
    items: Iterable[tuple[str, Path]],
    on_result: Callable[[str, Exception | None], None],
    reconnect: Callable[[], ftplib.FTP] | None = None,
):
    """Upload (remote_name, local_path) pairs; a failure never stops the batch.

    on_result(remote_name, None) on success or on_result(remote_name, exc) on failure.
    If the connection drops, `reconnect` is called once for that file and the file is retried.
    Returns the connection in use at the end (it may differ from `ftp`); the caller closes it.
    """
    broken = False
    for name, path in items:
        if broken:
            on_result(name, ConnectionError("FTPS connection lost and could not be re-established"))
            continue
        error: Exception | None = None
        for attempt in (1, 2):
            try:
                fh = open(path, "rb")  # noqa: SIM115 - a local read error is not a dropped link
            except OSError as exc:
                error = exc
                break
            try:
                with fh:
                    ftp.storbinary(f"STOR {name}", fh)
                error = None
                break
            except Exception as exc:  # noqa: BLE001
                error = exc
                if attempt == 2 or reconnect is None or not _is_dropped(exc):
                    break
                close(ftp)
                try:
                    ftp = reconnect()
                except Exception as rexc:  # noqa: BLE001
                    error = rexc
                    broken = True
                    break
        on_result(name, error)
    return ftp
