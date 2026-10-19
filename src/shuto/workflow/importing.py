"""Import a folder of photos into a batch."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from shuto.core import progress as core_progress
from shuto.core import text as core_text
from shuto.core.config import Config
from shuto.core.errors import PipelineError
from shuto.core.models import UPLOADED_STATES, Photo, Status
from shuto.core.progress import Progress
from shuto.ingest import cull as cull_mod
from shuto.ingest import imaging
from shuto.ingest.collect import (
    ScanItem,
    _safe_batch_name,
    batch_id_for,
    group_near_duplicates,
    scan_folder,
)
from shuto.ingest.imaging import hamming
from shuto.places.context import compute_file_facts
from shuto.store import Store
from shuto.workflow import ledger_sync, reports

MAX_BATCH_PHOTOS = 250  # Shutterstock takes at most this many photos in one submission


@dataclass
class AlreadyUploaded:
    """A photo skipped because shuto remembers sending it (exactly or as a look-alike)."""

    name: str
    uploaded_at: str | None
    submit_name: str | None
    exact: bool = False
    rejected: bool = False
    photo_id: int | None = None  # the remembered photo (exact) or the new look-alike row

    def text(self) -> str:
        day = (self.uploaded_at or "")[:10] or "an earlier date"
        what = "previously submitted and rejected" if self.rejected else "already uploaded"
        kind = what if self.exact else f"looks like a photo {what}"
        return f"{self.name}: {kind} on {day} as {self.submit_name or 'an unknown name'}"


@dataclass
class ImportSummary:
    imported: int = 0
    duplicates: int = 0
    already_known: int = 0
    near_duplicates: int = 0
    marked_uploaded: int = 0
    already_uploaded: list[AlreadyUploaded] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    batches: dict[str, int] = field(default_factory=dict)
    photo_ids: list[int] = field(default_factory=list)


def _split_base(batch: str) -> str:
    """`trip (3)` -> `trip`: the name a folder was split from (the other names stay as they are)."""
    m = re.fullmatch(r"(.+) \(\d+\)", batch)
    return m.group(1) if m else batch


def batches_text(batches: dict[str, int]) -> str:
    """'600 photos added in 3 batches: trip: 250, trip (2): 250, trip (3): 100'."""
    total = sum(batches.values())
    listing = ", ".join(f"{name}: {n}" for name, n in batches.items())
    return f"{total} photos added in {len(batches)} batches: {listing}"


class Decision(Enum):
    DUPLICATE = "duplicate"
    ALREADY_UPLOADED_EXACT = "already_uploaded_exact"
    FORCE_REIMPORT = "force_reimport"
    MARK_UPLOADED_KNOWN = "mark_uploaded_known"
    MOVED = "moved"
    KNOWN = "known"
    NEW = "new"


@dataclass
class ImportRun:
    """State of one `import_folder` call."""

    cfg: Config
    store: Store
    summary: ImportSummary = field(default_factory=ImportSummary)
    path: str | Path | None = None
    recursive: bool = True
    notes: str | None = None
    force: bool = False
    mark_uploaded: bool = False
    batch_name: str | None = None
    room: dict[str, int] = field(default_factory=dict)  # photos per batch, kept up to date
    seen: set[str] = field(default_factory=set)
    history: list[tuple[Any, Photo]] = field(default_factory=list)
    new_items: dict[str, list[tuple[int, ScanItem]]] = field(default_factory=dict)

    @property
    def check_history(self) -> bool:
        return not (self.force or self.mark_uploaded)

    def load_history(self) -> None:
        if self.check_history:
            self.history = [
                (p.dhash, p)
                for p in self.store.list_photos(statuses=UPLOADED_STATES)
                if p.dhash is not None
            ]

    def _roots(self) -> list[tuple[Path, bool]]:
        roots: list[tuple[Path, bool]] = (
            [(Path(self.path).expanduser(), self.recursive)]
            if self.path is not None
            else [(s.path, s.recursive) for s in self.cfg.sources]
        )
        if not roots:
            raise PipelineError("No folder given and no `sources` in the config.")
        for root, _ in roots:
            if not root.is_dir():
                raise PipelineError(f"Not a folder: {root}")
        return roots

    def _on_error(self, p: Path, exc: Exception) -> None:
        self.summary.errors.append(f"{p}: {imaging.unreadable_message(p, exc) or exc}")

    def _place_in_batch(self, base: str) -> str:
        """The batch for the next new photo: `base`, or `base (2)`, `base (3)` once one is full."""
        n = 1
        while True:
            name = base if n == 1 else f"{base} ({n})"
            if name not in self.room:
                self.room[name] = len(self.store.list_photos(name))
            if self.room[name] < MAX_BATCH_PHOTOS:
                self.room[name] += 1
                return name
            n += 1

    def _classify(self, item: ScanItem) -> tuple[Decision, Photo | None]:
        known = self.store.find_by_sha(item.sha256)
        if item.sha256 in self.seen:
            return Decision.DUPLICATE, known
        if (
            known
            and self.check_history
            and known.status in UPLOADED_STATES
            and known.status != Status.REJECTED
        ):
            return Decision.ALREADY_UPLOADED_EXACT, known
        if known and self.force and known.status == Status.ALREADY_UPLOADED:
            return Decision.FORCE_REIMPORT, known
        if known and self.mark_uploaded and known.status not in UPLOADED_STATES:
            return Decision.MARK_UPLOADED_KNOWN, known
        if known and known.src_path != str(item.path):
            return Decision.MOVED, known
        if known:
            return Decision.KNOWN, known
        return Decision.NEW, None

    def _apply_known(self, decision: Decision, item: ScanItem, known: Photo | None) -> None:
        """Book a photo the store already has (or a duplicate within this import)."""
        summary, store = self.summary, self.store
        if decision is Decision.DUPLICATE:
            summary.duplicates += 1
        elif decision is Decision.ALREADY_UPLOADED_EXACT:
            assert known is not None
            self.seen.add(item.sha256)
            summary.already_uploaded.append(
                AlreadyUploaded(
                    item.path.name,
                    known.uploaded_at,
                    known.submit_name,
                    exact=True,
                    photo_id=known.id,
                )
            )
        elif decision is Decision.FORCE_REIMPORT:
            assert known is not None and known.id is not None and known.batch_id is not None
            self.seen.add(item.sha256)
            store.set_status(known.id, Status.IMPORTED, force=True, detail={"forced": True})
            summary.imported += 1
            summary.photo_ids.append(known.id)
            summary.batches[known.batch_id] = summary.batches.get(known.batch_id, 0) + 1
        elif decision is Decision.MARK_UPLOADED_KNOWN:
            assert known is not None and known.id is not None
            store.update_photo(known.id, uploaded_at=core_text.now_iso())
            store.set_status(known.id, Status.UPLOADED, force=True, detail={"marked": True})
            summary.marked_uploaded += 1
        elif decision is Decision.MOVED:
            assert known is not None and known.id is not None
            if Path(known.src_path).exists():
                summary.duplicates += 1
                return
            # The original was moved: follow it so prepare finds the file.
            store.update_photo(known.id, src_path=str(item.path))
            summary.already_known += 1
        else:
            summary.already_known += 1

    def _near_match(self, item: ScanItem) -> Photo | None:
        """The remembered upload this photo looks like (by dHash distance), if any."""
        if not self.history:
            return None
        best = min(self.history, key=lambda h: hamming(h[0], item.dhash))
        return (
            best[1]
            if hamming(best[0], item.dhash) <= self.cfg.import_.near_duplicate_distance
            else None
        )

    def _register_new(self, item: ScanItem, root: Path) -> None:
        cfg, store, summary = self.cfg, self.store, self.summary
        self.seen.add(item.sha256)
        batch = self._place_in_batch(
            _safe_batch_name(self.batch_name)
            if self.batch_name
            else batch_id_for(item, cfg.import_.batch_by, root)
        )
        if self.notes is not None:
            store.upsert_batch(batch, notes=self.notes, source=str(root))
        match = self._near_match(item)
        status = (
            Status.UPLOADED
            if self.mark_uploaded
            else Status.ALREADY_UPLOADED
            if match
            else Status.IMPORTED
        )
        pid = store.add_photo(
            status=status,
            uploaded_at=core_text.now_iso() if self.mark_uploaded else None,
            sha256=item.sha256,
            src_path=str(item.path),
            orig_name=item.path.name,
            batch_id=batch,
            dhash=item.dhash,
            width=item.info.width,
            height=item.info.height,
            captured_at=item.info.captured.isoformat() if item.info.captured else None,
            camera=item.info.camera,
            has_gps=item.info.has_gps,
        )
        facts = compute_file_facts(item.path)  # file EXIF/IPTC/XMP + offline place, local only
        if facts:
            store.update_photo(pid, metadata=facts)
        if self.mark_uploaded:
            summary.marked_uploaded += 1
            return
        try:
            imaging.make_thumbnail(item.path, cfg.workspace_path("thumbs", f"{item.sha256}.jpg"))
        except Exception as exc:
            summary.errors.append(f"{item.path}: thumbnail failed: {exc}")
        if match:
            store.log_event(
                "already_uploaded",
                photo_id=pid,
                batch_id=batch,
                detail={"look_alike_of": match.id, "submit_name": match.submit_name},
            )
            summary.already_uploaded.append(
                AlreadyUploaded(
                    item.path.name,
                    match.uploaded_at,
                    match.submit_name,
                    rejected=match.status == Status.REJECTED,
                    photo_id=pid,
                )
            )
            return
        self.new_items.setdefault(_split_base(batch), []).append((pid, item))
        summary.imported += 1
        summary.photo_ids.append(pid)
        summary.batches[batch] = summary.batches.get(batch, 0) + 1

    def scan(self, roots: list[tuple[Path, bool]], progress: Progress) -> None:
        for root, rec in roots:
            progress(0, 0, f"scanning {root}")
            items = scan_folder(root, self.cfg.import_.extensions, rec, on_error=self._on_error)
            for n, item in enumerate(items, 1):
                progress(n, len(items), item.path.name)
                decision, known = self._classify(item)
                if decision is Decision.NEW:
                    self._register_new(item, root)
                else:
                    self._apply_known(decision, item, known)

    def _group_near_dups(self) -> None:
        store, summary = self.store, self.summary
        distance = self.cfg.import_.near_duplicate_distance
        for entries in self.new_items.values():
            scan = [item for _, item in entries]
            for group in group_near_duplicates(scan, distance):
                scores = []
                for i in group:
                    try:
                        scores.append(cull_mod.evaluate(scan[i].path))
                    except Exception:
                        scores.append(cull_mod.CullResult(ok=False))
                best = cull_mod.pick_best(scores)
                group_id = entries[group[0]][0]
                for pos, i in enumerate(group):
                    pid = entries[i][0]
                    store.update_photo(pid, near_dup_group=group_id)
                    if pos != best:
                        store.set_status(pid, Status.NEAR_DUPLICATE)
                        summary.near_duplicates += 1


def import_folder(
    cfg: Config,
    store: Store,
    path: str | Path | None = None,
    *,
    recursive: bool = True,
    notes: str | None = None,
    progress: Progress | None = None,
    force: bool = False,
    mark_uploaded: bool = False,
    batch_name: str | None = None,
) -> ImportSummary:
    """Scan a folder (or cfg.sources), register new photos, thumbnails, exact and near dupes.

    Originals are only read. Exact duplicates (same sha256 as a known photo) are counted and not
    stored again; near duplicates keep the sharpest member and mark the rest `near_duplicate`.
    Photos that are (or look like, by dHash distance) ones already uploaded are recorded as
    `already_uploaded` and not processed again unless `force`. With `mark_uploaded` the folder is
    registered as already uploaded instead (no thumbnails; nothing else runs on it). `force` also
    sends look-alikes stored as `already_uploaded` by an earlier import back to `imported`.
    With `batch_name`, every new photo goes into that one batch (instead of `import.batch_by`).
    """
    if force and mark_uploaded:
        raise PipelineError("--force and --mark-uploaded cannot be combined.")
    progress = progress or core_progress.noop
    run = ImportRun(
        cfg,
        store,
        path=path,
        recursive=recursive,
        notes=notes,
        force=force,
        mark_uploaded=mark_uploaded,
        batch_name=batch_name,
    )
    roots = run._roots()
    run.load_history()
    run.scan(roots, progress)
    if run.summary.marked_uploaded:
        ledger_sync.refresh_ledger(cfg, store)
    run._group_near_dups()
    reports._refresh_reports(cfg, store, set(run.summary.batches))
    return run.summary
