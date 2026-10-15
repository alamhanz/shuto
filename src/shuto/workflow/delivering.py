"""Prepare files, upload by FTPS and deliver a batch."""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from shuto.core import paths
from shuto.core import progress as core_progress
from shuto.core import text as core_text
from shuto.core.config import Config
from shuto.core.errors import UploadRefused
from shuto.core.models import Photo, Status
from shuto.core.progress import Cancel, Progress
from shuto.deliver import send
from shuto.deliver.prepare import prepare_file, release_check, safe_filename
from shuto.metadata import embed
from shuto.metadata import rules as metadata_rules
from shuto.store import Store
from shuto.workflow import check_batch, checks, exporting, importing, ledger_sync, reports
from shuto.workflow.checks import NOT_ENGLISH_TEXT
from shuto.workflow.exporting import ExportSummary


@dataclass
class PrepareSummary:
    prepared: list[str] = field(default_factory=list)
    problems: dict[int, list[str]] = field(default_factory=dict)
    # prepared anyway (the owner approved them) although Shutterstock would reject the text
    warnings: dict[int, list[str]] = field(default_factory=dict)

    def warning_text(self) -> str:
        """One plain sentence naming those photos (empty when there are none)."""
        if not self.warnings:
            return ""
        first = next(iter(self.warnings.values()))[0]
        return (
            f"Warning: {len(self.warnings)} prepared photo(s) still have a problem Shutterstock "
            f"will reject, e.g. {first}. Fix them in Review before you send them."
        )


def _text_problems(meta: dict[str, Any]) -> list[str]:
    """Hard problems in the stored text that Shutterstock itself would reject."""
    found = []
    keywords = [k for k in meta.get("keywords") or [] if str(k).strip()]
    if len(keywords) < metadata_rules.KEYWORD_FLOOR:
        found.append(metadata_rules.reason("few_keywords", count=len(keywords)))
    if len(metadata_rules.words_of(str(meta.get("description") or ""))) < metadata_rules.MIN_WORDS:
        found.append(metadata_rules.reason("too_few_words"))
    return found


def prepare_batch(
    cfg: Config,
    store: Store,
    batch_id: str,
    *,
    only_ids: Iterable[int] | None = None,
    progress: Progress | None = None,
    cancel: Cancel | None = None,
) -> PrepareSummary:
    """Approved photos -> safe-named JPEG copies in <batch folder>/prepared with IPTC/XMP."""
    progress = progress or core_progress.noop
    photos = store.list_photos(batch_id, [Status.APPROVED])
    if only_ids is not None:
        wanted = set(only_ids)
        photos = [p for p in photos if p.id in wanted]
    taken = {p.submit_name.lower() for p in store.list_photos() if p.submit_name}
    dest_dir = paths.batch_dir(cfg, batch_id) / "prepared"
    for existing in (dest_dir, cfg.workspace_path("prepared", _safe_dir(batch_id))):  # + 0.4.0
        if existing.is_dir():
            taken |= {f.name.lower() for f in existing.iterdir()}
    n = sum(1 for p in store.list_photos(batch_id) if p.submit_name)
    summary = PrepareSummary()
    for i, photo in enumerate(photos, 1):
        if cancel and cancel():
            break
        pid = photo.id
        assert pid is not None
        progress(i, len(photos), photo.orig_name)
        meta = dict(photo.metadata or {})
        gr = (photo.guardrail or {}).get("action")
        problems = release_check(
            meta, photo.detected, photo.release_ids, strict=cfg.style.english_only
        )
        if gr == "block":
            problems.append("Blocked by guardrails.")
        if problems:
            summary.problems[pid] = problems
            continue
        n += 1
        title = str(meta.get("title") or meta.get("description") or photo.orig_name)
        name = safe_filename(
            core_text.parse_dt(photo.captured_at),
            title,
            n,
            cfg.prepare.filename_pattern,
            cfg.prepare.slug_words,
            taken,
        )
        try:
            dest = prepare_file(
                Path(photo.src_path),
                dest_dir,
                name,
                meta,
                cfg.prepare.jpeg_quality,
                cfg.prepare.include_gps,
            )
        except Exception as exc:
            summary.problems[pid] = [f"prepare failed: {exc}"]
            continue
        store.update_photo(pid, prepared_path=str(dest), submit_name=name)
        store.set_status(pid, Status.PREPARED)
        summary.prepared.append(name)
        if unfixed := _text_problems(meta):
            summary.warnings[pid] = unfixed
    if summary.prepared:
        reports.refresh_report(cfg, store, batch_id)
    return summary


def _safe_dir(batch_id: str) -> str:
    return "".join(c if c.isalnum() or c in "._-" else "-" for c in batch_id) or "batch"


@dataclass
class UploadSummary:
    uploaded: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)
    skipped_blocked: list[str] = field(default_factory=list)
    stopped: int = 0  # photos left unsent because the upload was cancelled
    over_limit: int = 0  # photos left for the next run (more than MAX_BATCH_PHOTOS)

    def stopped_text(self) -> str:
        """Truthful words for an upload that was stopped early."""
        total = len(self.uploaded) + len(self.failed) + self.stopped
        return (
            f"Upload stopped: {len(self.uploaded)} of {total} sent. "
            f"The other {total - len(self.uploaded)} are ready to send later."
        )


def _strip_gps_file(path: Path) -> bool:
    """Remove the GPS location from a prepared copy in place; True when something changed."""
    data = path.read_bytes()
    clean = embed.strip_gps(data)
    if clean == data:
        return False
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(clean)
    os.replace(tmp, path)
    return True


def _megabytes(n: int) -> str:
    return f"{n / 1_000_000:.1f}"


def _uploadable(
    cfg: Config, photos: list[Photo], summary: UploadSummary
) -> tuple[dict[str, Photo], list[Path]]:
    """The prepared copies that may be sent; the others are recorded in `summary`."""
    by_name: dict[str, Photo] = {}
    files: list[Path] = []
    for photo in photos:
        if (photo.guardrail or {}).get("action") == "block" or not photo.prepared_path:
            summary.skipped_blocked.append(photo.orig_name)
            continue
        if checks.not_english(photo.metadata or {}, cfg.style.english_only):  # e.g. a 0.4.0 copy
            summary.failed[Path(photo.prepared_path).name] = (
                NOT_ENGLISH_TEXT + ": edit it in Review"
            )
            continue
        if not cfg.prepare.include_gps:  # copies prepared before 0.5 may still hold a location
            try:
                _strip_gps_file(Path(photo.prepared_path))
            except (OSError, ValueError) as exc:
                summary.failed[Path(photo.prepared_path).name] = (
                    f"could not remove the GPS location, so it was not sent: {exc}"
                )
                continue
        by_name[Path(photo.prepared_path).name] = photo
        files.append(Path(photo.prepared_path))
    return by_name, files


class _UploadReport:
    """The progress text of one upload run."""

    def __init__(
        self, progress: Progress, count: int, text_prefix: str, by_name: dict[str, Photo]
    ) -> None:
        self.progress = progress
        self.count = count
        self.text_prefix = text_prefix
        self.by_name = by_name
        self.done = 0
        self.began = check_batch._mono()
        self.last_bytes = 0.0
        self.current = ""

    def say(self, extra: str = "", name: str = "") -> None:
        text = f"{self.text_prefix}Uploading photo {min(self.done + 1, self.count)} of {self.count}"
        if name:
            text += f" · {self.by_name[name].orig_name}"
        if extra:
            text += f" · {extra}"
        if 0 < self.done < self.count:
            left = (check_batch._mono() - self.began) / self.done * (self.count - self.done)
            text += f" · {check_batch._time_left(left)}"
        self.progress(self.done, self.count, text)

    def on_start(self, name: str) -> None:
        self.current = name
        self.last_bytes = 0.0
        self.say(name=name)

    def on_bytes(self, sent: int) -> None:
        now = check_batch._mono()
        if now - self.last_bytes < 0.5:  # the callback fires per block; keep the database calm
            return
        self.last_bytes = now
        self.say(f"{_megabytes(sent)} MB sent", name=self.current)

    def on_wait(self, text: str) -> None:
        self.progress(self.done, self.count, self.text_prefix + text)


def upload_batch(
    cfg: Config,
    store: Store,
    batch_id: str,
    *,
    confirm: bool = False,
    only_ids: Iterable[int] | None = None,
    progress: Progress | None = None,
    cancel: Cancel | None = None,
    creds: send.FtpsCreds | None = None,
    connect: Callable[..., object] | None = None,
    text_prefix: str = "",
    waits: Sequence[float] | None = None,
    sleep: Callable[[float], None] | None = None,
) -> UploadSummary:
    """FTPS-upload prepared photos. Refused unless deliver.upload_enabled or confirm=True.

    A cancel stops after the photo in transfer: the sent ones are UPLOADED, the rest stay
    PREPARED. At most MAX_BATCH_PHOTOS go in one run (`over_limit` counts the others).
    `text_prefix` goes in front of the progress text (e.g. "Step 2 of 2 · ").
    """
    if not (cfg.deliver.upload_enabled or confirm):
        raise UploadRefused(
            "Uploading is disabled. Set deliver.upload_enabled: true in the config, or confirm "
            "this upload explicitly (CLI: --yes)."
        )
    progress = progress or core_progress.noop
    photos = store.list_photos(batch_id, [Status.PREPARED])
    if only_ids is not None:
        wanted = set(only_ids)
        photos = [p for p in photos if p.id in wanted]
    summary = UploadSummary()
    by_name, files = _uploadable(cfg, photos, summary)
    if not files:
        return summary
    if len(files) > importing.MAX_BATCH_PHOTOS:
        summary.over_limit = len(files) - importing.MAX_BATCH_PHOTOS
        files = files[: importing.MAX_BATCH_PHOTOS]
    creds = creds or send.creds_from_secrets(cfg.deliver.ftps_host)
    count = len(files)
    rep = _UploadReport(progress, count, text_prefix, by_name)

    def on_result(name: str, exc: Exception | None) -> None:
        rep.done += 1
        rep.say()
        photo = by_name[name]
        assert photo.id is not None
        if exc is None:
            store.update_photo(photo.id, uploaded_at=core_text.now_iso())
            store.set_status(photo.id, Status.UPLOADED)
            summary.uploaded.append(name)
        else:
            summary.failed[name] = str(exc) or type(exc).__name__

    options: dict[str, Any] = {
        "cancel": cancel,
        "on_start": rep.on_start,
        "on_bytes": rep.on_bytes,
        "on_wait": rep.on_wait,
    }
    if waits is not None:
        options["waits"] = waits
    if sleep is not None:
        options["sleep"] = sleep
    kwargs = {"connect": connect} if connect is not None else {}
    send.upload_files(creds, files, on_result, **kwargs, **options)
    summary.stopped = max(count - rep.done, 0)
    if summary.uploaded:
        ledger_sync.refresh_ledger(cfg, store)
        reports.refresh_report(cfg, store, batch_id)
    return summary


@dataclass
class DeliverSummary:
    prepare: PrepareSummary
    upload: UploadSummary | None = None
    export: ExportSummary | None = None


def _offset_progress(progress: Progress, total: int, offset: int, limit: int) -> Progress:
    """Report `offset + done` (at most `limit`) of `total` on the outer bar. Local until the shared
    helper exists in core.progress."""

    def report(done: int, _count: int, message: str = "") -> None:
        progress(min(offset + done, limit), total, message)

    return report


def deliver_batch(
    cfg: Config,
    store: Store,
    batch_id: str,
    *,
    confirm: bool,
    progress: Progress | None = None,
    cancel: Cancel | None = None,
    creds: send.FtpsCreds | None = None,
    connect: Callable[..., object] | None = None,
    waits: Sequence[float] | None = None,
    sleep: Callable[[float], None] | None = None,
) -> DeliverSummary:
    """Prepare approved photos, upload them (needs `confirm`), then write the batch CSV.

    One progress range of 2n+1 (n = approved + prepared photos). Stops after upload failures
    or a cancel and returns what was done so far; uploaded photos can be exported later.
    """
    if not (cfg.deliver.upload_enabled or confirm):
        raise UploadRefused(
            "Uploading is disabled. Set deliver.upload_enabled: true in the config, or confirm "
            "this upload explicitly."
        )
    progress = progress or core_progress.noop
    n = min(
        len(store.list_photos(batch_id, [Status.APPROVED, Status.PREPARED])),
        importing.MAX_BATCH_PHOTOS,
    )
    total = 2 * n + 1

    prepare_window = _offset_progress(progress, total, 0, n)

    def prepare_progress(done: int, _total: int, message: str = "") -> None:
        prepare_window(done, _total, f"Step 1 of 2 · Preparing photos {min(done, n)}/{n}")

    upload_progress = _offset_progress(progress, total, n, 2 * n)

    summary = DeliverSummary(
        prepare_batch(cfg, store, batch_id, progress=prepare_progress, cancel=cancel)
    )
    if cancel and cancel():
        return summary
    summary.upload = upload_batch(
        cfg,
        store,
        batch_id,
        confirm=confirm,
        progress=upload_progress,
        cancel=cancel,
        creds=creds,
        connect=connect,
        text_prefix="Step 2 of 2 · ",
        waits=waits,
        sleep=sleep,
    )
    if summary.upload.failed or summary.upload.stopped:
        return summary
    summary.export = exporting.export_batch(cfg, store, batch_id)
    progress(total, total, "Writing the Shutterstock CSV")
    return summary
