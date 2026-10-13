"""Deliver prepared files: FTPS upload and bulk-metadata CSV files."""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import ftps
from .csv_export import build_csv, split_by_kind
from .secrets import get_secret


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

    def reconnect():
        return connect(creds.host, creds.username, creds.password)

    ftp = reconnect()
    try:
        ftp = ftps.upload_many(ftp, items, on_result, reconnect=reconnect, **options)
    finally:
        ftps.close(ftp)


def write_csvs(batch_id: str, rows: Sequence[dict], out_dir: Path, ts: datetime) -> list[Path]:
    """Write one CSV per kind (commercial / editorial); returns the files written."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    safe_batch = re.sub(r"[^A-Za-z0-9._-]+", "-", batch_id).strip("-") or "batch"
    stamp = ts.strftime("%Y%m%d-%H%M%S")
    written: list[Path] = []
    for kind, group in split_by_kind(rows).items():
        dest = out_dir / f"shutterstock-{safe_batch}-{kind}-{stamp}.csv"
        tmp = dest.with_name(dest.name + ".tmp")
        with open(tmp, "w", encoding="utf-8", newline="") as fh:
            fh.write(build_csv(group))
        os.replace(tmp, dest)
        written.append(dest)
    return written
