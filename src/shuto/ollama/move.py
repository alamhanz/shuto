"""Moving the models: plan, copy with size checks, delete the originals only when verified."""

from __future__ import annotations

import errno
import filecmp
import hashlib
import json
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from shuto.ai import hardware
from shuto.ollama.errors import MoveCancelled, StoreError

SUBDIRS = ("blobs", "manifests")  # the only parts of a models folder that are copied or deleted
HEADROOM_GB = 1.0  # free space kept on the target on top of the files being copied
CHUNK = 8 * 1024 * 1024
SMALL_FILE = 1024 * 1024  # files under this are compared byte by byte, larger ones by size
PART_SUFFIX = ".shuto-part"


@dataclass
class MovePlan:
    old: Path
    new: Path
    files: list[tuple[str, int]]  # (path relative to old, size) of every model file
    todo_bytes: int  # bytes still to copy (identical files already in the new folder are skipped)
    conflicts: list[str] = field(default_factory=list)  # files that exist there but differ

    @property
    def total_bytes(self) -> int:
        return sum(s for _, s in self.files)


def _real(p: Path | str) -> Path:
    """The folder as the file system sees it: links, junctions, subst drives and short names
    resolved (as far as the folder exists)."""
    return Path(os.path.abspath(os.path.expanduser(str(p)))).resolve(strict=False)


def same_dir(a: Path | str, b: Path | str) -> bool:
    """Are these two spellings the same folder? Compares real identity, not text."""
    ra, rb = _real(a), _real(b)
    if os.path.normcase(str(ra)) == os.path.normcase(str(rb)):
        return True
    try:
        return os.path.samefile(ra, rb)
    except OSError:
        return False  # one of them does not exist, so they cannot be the same folder


def is_inside(child: Path | str, parent: Path | str) -> bool:
    """Is `child` the folder `parent` or somewhere below it (by real identity)?"""
    cur = _real(child)
    while True:
        if same_dir(cur, parent):
            return True
        if cur.parent == cur:
            return False
        cur = cur.parent


def check_distinct(old: Path | str, new: Path | str) -> None:
    """Raise a plain-language StoreError unless the two folders are really different places."""
    if same_dir(new, old):
        raise StoreError("Ollama already keeps its models in that folder.")
    if is_inside(new, old) or is_inside(old, new):
        raise StoreError(
            "Choose a folder that is not inside the current models folder, and does not contain it."
        )


@dataclass
class Created:
    """What one run created in the new folder, so a failed or stopped run can undo exactly that."""

    files: list[Path] = field(default_factory=list)
    dirs: list[Path] = field(default_factory=list)

    def remove(self) -> int:
        """Delete only what this run created. Returns how many items could not be removed."""
        left = 0
        for f in reversed(self.files):
            try:
                f.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                left += 1
        for d in reversed(self.dirs):  # deepest first (they were recorded parent first)
            try:
                d.rmdir()  # only succeeds when empty
            except FileNotFoundError:
                pass
            except OSError:
                left += 1
        return left


def _missing_dirs(path: Path) -> list[Path]:
    """The folders in path (parent first) that do not exist yet."""
    out: list[Path] = []
    cur = path
    while not cur.exists() and cur.parent != cur:
        out.append(cur)
        cur = cur.parent
    return out[::-1]


def prepare_folder(path: Path, created: Created) -> None:
    """Make the chosen folder and prove shuto can write there. Records what it made."""
    try:
        created.dirs.extend(_missing_dirs(path))
        path.mkdir(parents=True, exist_ok=True)
        probe = path / f".shuto-write-test{PART_SUFFIX}"
        probe.write_bytes(b"ok")
        probe.unlink()
    except OSError:
        raise StoreError(
            "shuto cannot save files in that folder. Choose another folder or drive."
        ) from None


def _model_files(root: Path) -> list[tuple[str, int]]:
    out: list[tuple[str, int]] = []
    for sub in SUBDIRS:
        base = root / sub
        if not base.is_dir():
            continue
        for dirpath, _dirs, names in os.walk(base):
            for name in names:
                p = Path(dirpath) / name
                if "-partial" in name or name.endswith(PART_SUFFIX) or p.is_symlink():
                    continue  # unfinished downloads and links are left alone
                try:
                    out.append((str(p.relative_to(root)), p.stat().st_size))
                except OSError:
                    continue
    return out


def models_size_bytes(root: Path) -> int:
    return sum(s for _, s in _model_files(root))


def _manifest_digests(path: Path) -> set[str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    items = [data.get("config") or {}, *(data.get("layers") or [])]
    return {str(i["digest"]).replace(":", "-") for i in items if i.get("digest")}


def freeable_bytes(root: Path, name: str) -> int | None:
    """Bytes Ollama would really free by deleting model `name`: only the blobs that no other
    model uses (models often share layers). None when the manifests cannot be read."""
    name = hardware.normalize_tag(name)
    model, _, tag = name.rpartition(":")
    parts = model.split("/")
    if len(parts) == 1:
        parts = ["library", model]
    wanted = Path("manifests", "registry.ollama.ai", *parts, tag)
    try:
        mine: set[str] = set()
        others: set[str] = set()
        found = False
        for rel, _size in _model_files(root):
            if not rel.startswith("manifests"):
                continue
            digests = _manifest_digests(root / rel)
            if Path(rel) == wanted:
                mine, found = digests, True
            else:
                others |= digests
        if not found:
            return None
        return sum(
            (root / "blobs" / d).stat().st_size
            for d in mine - others
            if (root / "blobs" / d).is_file()
        )
    except (OSError, ValueError, AttributeError, TypeError):
        return None


def _same(src: Path, dst: Path, size: int) -> bool:
    try:
        if not dst.is_file() or dst.stat().st_size != size:
            return False
        return size >= SMALL_FILE or filecmp.cmp(src, dst, shallow=False)
    except OSError:
        return False


_BLOB_NAME = re.compile(r"^sha256-([0-9a-f]{64})$")
_hash_cache: dict[tuple[str, int, int], bool] = {}


def _blob_matches_name(path: Path) -> bool:
    """Does a model blob hold the data its name promises (blobs are named by their sha256)?
    Files that are not named that way are trusted."""
    m = _BLOB_NAME.match(path.name)
    if not m:
        return True
    st = path.stat()
    key = (str(path), st.st_size, st.st_mtime_ns)
    if key not in _hash_cache:
        digest = hashlib.sha256()
        with path.open("rb") as fh:
            while chunk := fh.read(CHUNK):
                digest.update(chunk)
        _hash_cache[key] = digest.hexdigest() == m.group(1)
    return _hash_cache[key]


def plan_move(old: Path, new: Path) -> MovePlan:
    files = _model_files(old)
    todo = 0
    conflicts: list[str] = []
    for rel, size in files:
        dst = new / rel
        if _same(old / rel, dst, size):
            # a big file already there was not copied by us: prove it is intact before the
            # original can be deleted; a damaged one is never overwritten, so it is a conflict
            try:
                intact = size < SMALL_FILE or _blob_matches_name(dst)
            except OSError:
                intact = False
            if not intact:
                conflicts.append(rel)
            continue
        if dst.exists():
            conflicts.append(rel)  # same name, different content: never overwritten
        else:
            todo += size
    return MovePlan(old, new, files, todo, conflicts)


def conflict_message(plan: MovePlan) -> str:
    n = len(plan.conflicts)
    return (
        f"That folder already holds models, and {n} of its files have the same name as yours "
        "but are different. shuto will not overwrite them, so nothing was changed. "
        "Choose an empty folder, or choose the folder without moving the models."
    )


def manifest_names(files: list[tuple[str, int]]) -> set[str]:
    """Model names (as Ollama lists them) for the manifest files of a models folder."""
    names: set[str] = set()
    for rel, _size in files:
        parts = Path(rel).parts
        if len(parts) < 5 or parts[0] != "manifests":
            continue
        host, ns, tail = parts[1], parts[2], parts[3:]
        model, tag = "/".join(tail[:-1]), tail[-1]
        name = f"{model}:{tag}"
        if ns != "library":
            name = f"{ns}/{name}"
        if host not in ("registry.ollama.ai", "registry"):
            name = f"{host}/{name}"
        names.add(hardware.normalize_tag(name))
    return names


Progress = Callable[[int, int, str], None]


def _copy_file(
    src: Path, dst: Path, on_bytes: Callable[[int], None], cancelled: Callable[[], bool]
):
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + PART_SUFFIX)
    try:
        with src.open("rb") as fin, tmp.open("wb") as fout:
            while chunk := fin.read(CHUNK):
                fout.write(chunk)
                on_bytes(len(chunk))
                if cancelled():
                    raise MoveCancelled("stopped")
        if dst.exists():
            raise FileExistsError(errno.EEXIST, "already exists")  # never overwrite
        os.replace(tmp, dst)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def copy_models(
    plan: MovePlan,
    progress: Progress | None = None,
    cancelled: Callable[[], bool] = lambda: False,
    created: Created | None = None,
) -> int:
    """Copy every model file not already identical in the new folder, then check all sizes.

    Returns the number of files copied. Never touches the old folder and never overwrites a
    file in the new one; what it creates is recorded in `created`.
    """
    created = created if created is not None else Created()
    if plan.conflicts:
        raise StoreError(conflict_message(plan))
    total = max(plan.todo_bytes, 1)
    done = 0
    copied = 0

    def on_bytes(n: int) -> None:
        nonlocal done
        done += n
        if progress:
            progress(done // 1_000_000, total // 1_000_000, "Copying models")

    for rel, size in plan.files:
        src, dst = plan.old / rel, plan.new / rel
        if _same(src, dst, size):
            continue
        if cancelled():
            raise MoveCancelled("stopped")
        created.dirs.extend(_missing_dirs(dst.parent))
        try:
            _copy_file(src, dst, on_bytes, cancelled)
        except OSError as exc:
            raise StoreError(
                f"Copying the models failed ({exc.strerror or 'disk error'}). "
                "Your models were not changed."
            ) from None
        created.files.append(dst)
        copied += 1
    bad = [rel for rel, size in plan.files if not _same(plan.old / rel, plan.new / rel, size)]
    if bad:
        raise StoreError(
            f"{len(bad)} copied file(s) did not match the originals. Your models were not changed."
        )
    return copied


def delete_old_models(plan: MovePlan) -> int:
    """Remove the old model files that were copied and verified. Returns bytes freed.

    Refuses (and deletes nothing) unless every copy exists, has the same size and is a
    different file from the original. Manifests go first, so an interrupted delete never
    leaves a model that points at blobs that are gone.
    """
    if same_dir(plan.old, plan.new):
        raise StoreError("The old and the new folder are the same place.")
    for rel, size in plan.files:
        src, dst = plan.old / rel, plan.new / rel
        try:
            if not dst.is_file() or dst.stat().st_size != size:
                raise StoreError(f"The copy of {rel} is missing or incomplete.")
            if src.exists() and os.path.samefile(src, dst):
                raise StoreError(f"{rel} in the old folder is the same file as its copy.")
        except OSError:
            raise StoreError(f"shuto could not check the copy of {rel}.") from None
    freed = 0
    for rel, size in sorted(plan.files, key=lambda f: not f[0].startswith("manifests")):
        try:
            (plan.old / rel).unlink()
            freed += size
        except OSError:
            continue
    for sub in SUBDIRS:
        base = plan.old / sub
        if not base.is_dir():
            continue
        for dirpath, _dirs, _names in os.walk(base, topdown=False):
            try:
                Path(dirpath).rmdir()  # only succeeds when empty
            except OSError:
                pass
    return freed


def _gb(n: int) -> str:
    return f"{n / 1024**3:.1f} GB"


def _check_room(plan: MovePlan, new: Path) -> None:
    if plan.conflicts:
        raise StoreError(conflict_message(plan))
    if not plan.files:
        return
    free = hardware.free_gb(new)
    if free is not None and free * 1024**3 < plan.todo_bytes + HEADROOM_GB * 1024**3:
        raise StoreError(
            f"There is not enough room: the models need {_gb(plan.todo_bytes)} and "
            f"{free:g} GB is free there. Choose a drive with more space, "
            "or do not move the models."
        )
