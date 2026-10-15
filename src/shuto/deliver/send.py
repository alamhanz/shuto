"""Deliver prepared files: FTPS upload and bulk-metadata CSV files."""

from __future__ import annotations

import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from shuto.core.paths import folder_name
from shuto.core.secrets import get_secret
from shuto.deliver import ftps
from shuto.deliver.csv_export import build_csv


@dataclass(frozen=True)
class FtpsCreds:
    host: str
    username: str = field(repr=False)
    password: str = field(repr=False)


def creds_from_secrets(host: str) -> FtpsCreds:
    user = get_secret("SHUTO_FTPS_USER")
    password = get_secret("SHUTO_FTPS_PASSWORD")
    missing = [
        name
        for name, value in (("SHUTO_FTPS_USER", user), ("SHUTO_FTPS_PASSWORD", password))
        if not value
    ]
    if missing:
        raise RuntimeError(
            "Missing FTPS credentials: set "
            + " and ".join(missing)
            + " (environment, .env file or OS keyring)."
        )
    return FtpsCreds(host=host, username=user or "", password=password or "")


def upload_files(
    creds: FtpsCreds,
    files: Sequence[Path],
    on_result: Callable[[str, Exception | None], None],
    connect: Callable[..., object] = ftps.connect,
    **options,
) -> None:
    """Upload files under their own names. Per-file results go to on_result(name, error).

    `options` (cancel, on_start, on_bytes, on_wait, waits, sleep) go to ftps.upload_many.
    """
    items = [(Path(f).name, Path(f)) for f in files]
    if not items:
        return

    live: list[object] = []  # the newest connection, so it is closed even if on_result raises

    def reconnect():
        conn = connect(creds.host, creds.username, creds.password)
        live.append(conn)
        return conn

    ftp = reconnect()
    try:
        ftps.upload_many(ftp, items, on_result, reconnect=reconnect, **options)
    finally:
        ftps.close(live[-1])


def csv_filename(batch_id: str) -> str:
    """`<safe batch name>-shutterstock.csv`, the one CSV a batch needs."""
    return f"{folder_name(batch_id)}-shutterstock.csv"


def write_csv(batch_id: str, rows: Sequence[dict], out_dir: Path, strict: bool = True) -> Path:
    """Write the batch's single Shutterstock CSV (replacing an earlier one); returns its path."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / csv_filename(batch_id)
    tmp = dest.with_name(dest.name + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="") as fh:
        fh.write(build_csv(rows, strict))
    os.replace(tmp, dest)
    return dest
