"""Send photos back to the AI and write their text again in the background."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from shuto.ai import selection, speedcheck
from shuto.ai.providers.base import VisionLLM
from shuto.core import progress as core_progress
from shuto.core import status_text
from shuto.core.config import Config
from shuto.core.errors import PipelineError
from shuto.core.models import UPLOADED_STATES, Status
from shuto.core.progress import Cancel, Progress
from shuto.store import Store
from shuto.workflow import checks, describing, reports
from shuto.workflow import review as workflow_review

NOTE_FIRST = "The AI could not write new text this time ({why}). The old text is kept: "
NOTE_LAST = "edit it, or send it back again."


@dataclass
class SendBackSummary:
    sent: list[int] = field(default_factory=list)  # now `ready` (already-ready ones count too)
    on_shutterstock: list[int] = field(default_factory=list)  # left out: Shutterstock has them
    refused: dict[int, str] = field(default_factory=dict)  # anything else, in plain words


@dataclass
class RedoSummary:
    done: int = 0
    failed: int = 0
    restored: list[int] = field(default_factory=list)
    errors: list[tuple[int, str]] = field(default_factory=list)
    aborted: str | None = None


def send_back(cfg: Config, store: Store, batch_id: str, ids: Iterable[int]) -> SendBackSummary:
    """Put photos back to `ready` so the AI writes them again.

    Photos Shutterstock already has are skipped (not an error). Sending the same photo twice, or
    a photo that is already `ready`, changes nothing.
    """
    summary = SendBackSummary()
    for pid in dict.fromkeys(ids):
        photo = store.get_photo(pid)
        if photo is None or photo.batch_id != batch_id:
            summary.refused[pid] = "this photo is not in this batch"
            continue
        if photo.status in UPLOADED_STATES:
            summary.on_shutterstock.append(pid)
            continue
        if photo.status == Status.READY:
            summary.sent.append(pid)
            continue
        try:
            workflow_review.override_status(
                store, pid, Status.READY, detail={"reason": "sent_back"}
            )
        except PipelineError:
            summary.refused[pid] = (
                f'it is marked "{status_text.label(photo.status)}" and cannot be sent back now'
            )
            continue
        summary.sent.append(pid)
    reports.refresh_report(cfg, store, batch_id)
    return summary


def restore_failed(cfg: Config, store: Store, ids: Iterable[int], why: str) -> list[int]:
    """Give `ready` photos that have old text back to Review, with a note saying why.

    Photos with no old text stay `ready` (they wait for the next run). Returns the restored ids.
    """
    restored: list[int] = []
    for pid in ids:
        photo = store.get_photo(pid)
        if photo is None or photo.status != Status.READY:
            continue
        data = dict(photo.metadata or {})
        if not str(data.get("description") or "").strip():
            continue
        _meta, gr, _status = checks._evaluate(cfg, store, photo, dict(data))
        note = NOTE_FIRST.format(why=why.rstrip(". ") or "unknown reason") + NOTE_LAST
        gr.messages.insert(0, note)
        store.update_photo(pid, guardrail=gr.to_dict())
        store.set_status(
            pid,
            Status.BLOCKED if gr.action == "block" else Status.NEEDS_REVIEW,
            detail={"reason": "redo_failed"},
        )
        restored.append(pid)
    return restored


def run_redo(
    cfg: Config,
    store: Store,
    batch_id: str,
    *,
    progress: Progress | None = None,
    cancel: Cancel | None = None,
    llm: VisionLLM | None = None,
    live: speedcheck.LiveRun | None = None,
    explain: Callable[[str], str] | None = None,
) -> RedoSummary:
    """Write text for every `ready` photo (and the ones sent back meanwhile), one by one.

    Each photo is saved when it is done, so it is back in Review at once. A photo the AI failed
    on gets its old text back under "needs a look". `explain` turns an error into plain words.
    """
    progress = progress or core_progress.noop
    if llm is None:
        problems = selection.describe_preflight(cfg, selection.resolve_selection(cfg, store))
        if problems:
            raise PipelineError(" ".join(problems))
        llm = selection.resolve_llm(cfg, store)
    s = describing.describe_waiting(
        cfg, store, batch_id, llm=llm, progress=progress, cancel=cancel, live=live
    )
    out = RedoSummary(
        done=s.described + s.needs_review + s.blocked + s.set_aside,
        failed=s.failed,
        errors=list(s.errors),
        aborted=s.aborted,
    )
    say = explain or (lambda text: text)
    for pid, err in s.errors:
        out.restored += restore_failed(cfg, store, [pid], say(err))
    if out.restored:
        reports.refresh_report(cfg, store, batch_id)
    return out
