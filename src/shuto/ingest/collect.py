"""Scan folders for photos, assign batches and find near duplicates."""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from shuto.core.files import sha256_file
from shuto.ingest.imaging import ImageInfo, dhash, open_image, read_info, small_gray


@dataclass(frozen=True)
class ScanItem:
    path: Path
    sha256: str
    dhash: int
    info: ImageInfo


def _norm_ext(ext: str) -> str:
    ext = ext.strip().lower()
    return ext if ext.startswith(".") else "." + ext


def scan_folder(
    path: str | Path,
    extensions: Iterable[str] = (".jpg", ".jpeg"),
    recursive: bool = True,
    on_error: Callable[[Path, Exception], None] | None = None,
) -> list[ScanItem]:
    """Hash and describe every matching image under `path`.

    Sorted by path, skips hidden files/folders, matches extensions case-insensitively and does
    not follow symlinked directories. Unreadable files are reported to `on_error` and skipped.
    """
    root = Path(path).expanduser()
    exts = {_norm_ext(e) for e in extensions}
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        if not recursive:
            dirnames[:] = []
        for name in filenames:
            if name.startswith(".") or Path(name).suffix.lower() not in exts:
                continue
            found.append(Path(dirpath) / name)
    items: list[ScanItem] = []
    for p in sorted(found, key=lambda x: str(x).lower()):
        try:
            info = read_info(p)
            with open_image(p) as im:
                h = dhash(small_gray(im, 256))
            items.append(ScanItem(p, sha256_file(p), h, info))
        except Exception as exc:
            if on_error is not None:
                on_error(p, exc)
    return items


def _safe_batch_name(name: str) -> str:
    """Folder-derived batch ids: letters, digits, dot, underscore, hyphen (URL and path safe)."""
    safe = "".join(ch if ch.isalnum() or ch in "._-" else "-" for ch in name).strip("-.")
    return safe or "root"


def batch_id_for(item: ScanItem, mode: str, root: str | Path | None = None) -> str:
    """Batch label: iso_week (2025-W01), month (2025-01), day (2025-01-05) or folder name."""
    if mode == "folder":
        parent = item.path.parent
        if root is not None:
            base = Path(root).expanduser()
            try:
                rel = parent.relative_to(base)
            except ValueError:
                return _safe_batch_name(parent.name)
            return _safe_batch_name("-".join(rel.parts) if rel.parts else base.name)
        return _safe_batch_name(parent.name)
    when = item.info.captured
    if when is None:
        when = datetime.fromtimestamp(item.path.stat().st_mtime)
    if mode == "iso_week":
        year, week, _ = when.isocalendar()
        return f"{year}-W{week:02d}"
    if mode == "month":
        return when.strftime("%Y-%m")
    if mode == "day":
        return when.strftime("%Y-%m-%d")
    raise ValueError(f"unknown batch mode: {mode!r}")


def group_near_duplicates(items: list[ScanItem], max_distance: int = 6) -> list[list[int]]:
    """Groups (as index lists, ascending) of items within `max_distance` dHash bits.

    Union-find, so similarity is transitive. Only groups with 2+ members are returned.
    """
    parent = list(range(len(items)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            if (items[i].dhash ^ items[j].dhash).bit_count() <= max_distance:
                ri, rj = find(i), find(j)
                if ri != rj:
                    parent[max(ri, rj)] = min(ri, rj)
    groups: dict[int, list[int]] = {}
    for i in range(len(items)):
        groups.setdefault(find(i), []).append(i)
    return sorted((g for g in groups.values() if len(g) > 1), key=lambda g: g[0])
