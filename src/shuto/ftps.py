"""Upload originals to Shutterstock's contributor FTPS server.

Shutterstock: host ftps.shutterstock.com, port 21, explicit TLS (AUTH TLS), passive mode,
login = contributor email/username + account password.
"""

from __future__ import annotations

import ftplib
import io

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
        try:
            ftp.quit()
        except Exception:  # noqa: BLE001
            ftp.close()
