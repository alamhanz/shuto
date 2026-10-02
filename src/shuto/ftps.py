"""Upload originals to Shutterstock's contributor FTPS server."""

from __future__ import annotations

import ftplib
import io

DEFAULT_HOST = "ftps.shutterstock.com"


def upload_file(
    host: str, username: str, password: str, filename: str, data: bytes, timeout: int = 120
) -> None:
    """Upload one file over explicit FTPS (TLS on port 21, passive mode)."""
    ftp = ftplib.FTP_TLS(timeout=timeout)
    try:
        ftp.connect(host, 21)
        ftp.login(username, password)
        ftp.prot_p()  # encrypt the data channel too
        ftp.set_pasv(True)
        ftp.storbinary(f"STOR {filename}", io.BytesIO(data))
    finally:
        try:
            ftp.quit()
        except Exception:  # noqa: BLE001
            ftp.close()
