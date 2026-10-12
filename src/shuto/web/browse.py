"""Read-only folder browser for the web UI.

Returns directory names and photo counts only (never file names or contents), and only for
folders inside the allowed roots: the home folder and the drives / mounted media.
"""

from __future__ import annotations

import getpass
import os
import stat
import string
import sys
from dataclasses import dataclass, field
from pathlib import Path

MAX_DIRS = 300
MAX_COUNT = 1000
DEFAULT_EXTENSIONS = (".jpg", ".jpeg", ".heic", ".png")


class BrowseError(Exception):
    """The folder cannot be shown. The message is safe to display."""


@dataclass(frozen=True)
class Root:
    label: str
    path: str


@dataclass
class Listing:
    path: str
    parent: str | None
    crumbs: list[dict[str, str]]
    dirs: list[dict[str, object]] = field(default_factory=list)
    truncated: bool = False


def _user() -> str:
    try:
        return os.environ.get("USER") or os.environ.get("USERNAME") or getpass.getuser()
    except Exception:
        return ""


def _children(base: str) -> list[Path]:
    try:
        return sorted(p for p in Path(base).iterdir() if p.is_dir())
    except OSError:
        return []


def _drives() -> list[str]:
    listdrives = getattr(os, "listdrives", None)  # Python 3.12+ on Windows
    if listdrives is not None:
        try:
            return list(listdrives())
        except OSError:
            pass
    return [f"{c}:\\" for c in string.ascii_uppercase if os.path.exists(f"{c}:\\")]


def roots() -> list[Root]:
    """Home first, then drives (Windows) or mounted media (macOS, Linux)."""
    found: list[Root] = [Root("Home", str(Path.home()))]
    if sys.platform == "win32":
        found += [Root(f"Drive {d[0]}:", d) for d in _drives()]
    elif sys.platform == "darwin":
        found += [Root(p.name, str(p)) for p in _children("/Volumes")]
    else:
        user = _user()
        bases = [f"/media/{user}", f"/run/media/{user}", "/mnt"] if user else ["/mnt"]
        for base in bases:
            found += [Root(p.name, str(p)) for p in _children(base)]
    seen: set[str] = set()
    unique: list[Root] = []
    for r in found:
        key = os.path.normcase(r.path)
        if key not in seen:
            seen.add(key)
            unique.append(r)
    return unique


def _resolved_roots() -> list[Path]:
    out: list[Path] = []
    for r in roots():
        try:
            out.append(Path(r.path).resolve(strict=True))
        except OSError:
            continue
    return out


def _inside(path: Path, bases: list[Path]) -> bool:
    return any(path == b or path.is_relative_to(b) for b in bases)


def _hidden(entry: os.DirEntry) -> bool:
    if entry.name.startswith("."):
        return True
    try:
        attrs = getattr(entry.stat(follow_symlinks=False), "st_file_attributes", 0)
    except OSError:
        return False
    return (
        bool(attrs & (stat.FILE_ATTRIBUTE_HIDDEN | stat.FILE_ATTRIBUTE_SYSTEM)) if attrs else False
    )


def _photo_count(folder: Path, exts: tuple[str, ...]) -> int | None:
    """Image files directly inside `folder`, counted up to MAX_COUNT; None if unreadable."""
    count = 0
    try:
        with os.scandir(folder) as it:
            for e in it:
                if e.name.lower().endswith(exts) and e.is_file():
                    count += 1
                    if count >= MAX_COUNT:
                        break
    except OSError:
        return None
    return count


def list_dir(path: str | None, extensions: tuple[str, ...] | list[str] = DEFAULT_EXTENSIONS):
    """List sub-folders of `path` (default: home). Raises BrowseError outside the roots."""
    bases = _resolved_roots()
    if not bases:
        raise BrowseError("No folders are available to browse.")
    raw = (path or "").strip()
    if not raw:
        target = bases[0]
    else:
        if "\x00" in raw or ".." in Path(raw).parts:
            raise BrowseError("That folder is not available.")
        try:
            target = Path(raw).expanduser().resolve(strict=True)
        except (OSError, ValueError):
            raise BrowseError("That folder does not exist.") from None
        if not target.is_dir():
            raise BrowseError("That is not a folder.")
        if not _inside(target, bases):
            raise BrowseError("That folder is outside the places shuto may look in.")
    exts = tuple(e.lower() for e in extensions)

    # Breadcrumbs start at the deepest root that contains the target.
    holders = [b for b in bases if _inside(target, [b])]
    top = max(holders, key=lambda b: len(b.parts))
    crumbs = [{"name": top.name or str(top), "path": str(top)}]
    cur = top
    for part in target.relative_to(top).parts:
        cur = cur / part
        crumbs.append({"name": part, "path": str(cur)})
    parent = None if target == top else str(target.parent)

    listing = Listing(path=str(target), parent=parent, crumbs=crumbs)
    try:
        with os.scandir(target) as it:
            entries = sorted(it, key=lambda e: e.name.lower())
    except OSError:
        raise BrowseError("shuto is not allowed to open that folder.") from None
    for entry in entries:
        try:
            if not entry.is_dir() or _hidden(entry):
                continue
            real = Path(entry.path).resolve(strict=True)
        except OSError:
            continue
        if not _inside(real, bases):
            continue  # a link that leads out of the allowed places
        if len(listing.dirs) >= MAX_DIRS:
            listing.truncated = True
            break
        count = _photo_count(real, exts)
        listing.dirs.append(
            {
                "name": entry.name,
                "path": str(real),
                "photo_count": count or 0,
                "unreadable": count is None,
            }
        )
    return listing
