"""Describe a batch with the AI."""

from __future__ import annotations

import threading
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from shuto.ai import benchmark as ai_benchmark
from shuto.ai import catalog as local_models
from shuto.ai import describe, estimate, hardware, machine, prompts, selection, speedcheck
from shuto.ai import filters as filters_mod
from shuto.ai import recommend as ai_recommend
from shuto.ai.providers.base import LLMError, VisionLLM
from shuto.core import models, paths
from shuto.core import progress as core_progress
from shuto.core import text as core_text
from shuto.core.config import Config
from shuto.core.models import LLMCall, Photo, Status
from shuto.core.progress import Cancel, Progress
from shuto.ingest import imaging
from shuto.metadata import rules as metadata_rules
from shuto.places.context import photo_context
from shuto.store import Store
from shuto.workflow import checks, importing, reports


def _count(store: Store, batch_id: str, only_ids: Iterable[int] | None) -> int:
    return len(_describe_targets(store, batch_id, only_ids))


def _describe_targets(store: Store, batch_id: str, only_ids: Iterable[int] | None) -> list[Photo]:
    """The photos for the AI; never more than MAX_BATCH_PHOTOS in one run (safety net)."""
    if only_ids is None:
        return store.list_photos(batch_id, [Status.READY])[: importing.MAX_BATCH_PHOTOS]
    wanted = set(only_ids)
    found = [p for p in store.list_photos(batch_id) if p.id in wanted and p.status in _REDESCRIBE]
    return found[: importing.MAX_BATCH_PHOTOS]


_REDESCRIBE = {
    Status.READY,
    Status.CULLED,
    Status.NEAR_DUPLICATE,
    Status.ALREADY_UPLOADED,
    Status.DUPLICATE,
    Status.DESCRIBED,
    Status.NEEDS_REVIEW,
    Status.BLOCKED,
    Status.SET_ASIDE,
    Status.APPROVED,
    Status.PREPARED,
}


def estimate_describe(
    cfg: Config,
    store: Store,
    batch_id: str,
    *,
    only_ids: Iterable[int] | None = None,
    provider: str | None = None,
    model: str | None = None,
    profile: hardware.HardwareProfile | None = None,
) -> estimate.Estimate:
    """Time (local) or cost (API) for the photos describe would process."""
    sel = selection.resolve_selection(cfg, store, provider, model)
    n = _count(store, batch_id, only_ids)
    llm_cls = selection.get_provider_class(sel.provider)
    side = cfg.llm.image_max_side or llm_cls.default_image_side
    conc = cfg.llm.concurrency or llm_cls.default_concurrency
    if sel.provider == "ollama":
        profile = profile or machine.hardware_profile()
        local_catalog = local_models.load_catalog()
        recs = ai_recommend.recommend(
            profile,
            local_catalog,
            [],
            ai_benchmark.load_benchmarks(paths.benchmark_cache_path()),
        )
        rec = next((r for r in recs if r.model.tag == sel.model), None)
        return estimate.for_batch(
            n,
            "ollama",
            sel.model,
            recommendation=rec,
            concurrency=conc,
            **checks._estimate_tokens(cfg),
        )
    return estimate.for_batch(
        n,
        sel.provider,
        sel.model,
        api_model=selection.api_model(sel.provider, sel.model),
        concurrency=conc,
        image_side=side,
        **checks._estimate_tokens(cfg),
    )


@dataclass
class DescribeSummary:
    described: int = 0
    needs_review: int = 0
    blocked: int = 0
    set_aside: int = 0
    failed: int = 0
    approved: int = 0
    errors: list[tuple[int, str]] = field(default_factory=list)
    aborted: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0


def _drop_prepared(store: Store, photo: Photo) -> None:
    if photo.prepared_path:
        try:
            Path(photo.prepared_path).unlink(missing_ok=True)
        except OSError:
            pass
        assert photo.id is not None
        store.update_photo(photo.id, prepared_path=None, submit_name=None)


class AbortPolicy:
    """Stop the batch when the provider is down or several calls in a row fail."""

    MAX_CONSECUTIVE = 3

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.consecutive = 0

    def failed(self, exc: Exception) -> bool:
        """Count a failed call; True when the batch must stop."""
        with self.lock:
            self.consecutive += 1
            if "is not running" in str(exc) or self.consecutive >= self.MAX_CONSECUTIVE:
                self.stop.set()
                return True
            return False

    def succeeded(self) -> None:
        with self.lock:
            self.consecutive = 0


@dataclass
class DescribeRun:
    """Everything one `describe_batch` call shares between its worker threads."""

    cfg: Config
    store: Store
    llm: VisionLLM
    summary: DescribeSummary
    notes: str
    restrictions: Any
    style: prompts.Style
    spec: Any
    feedback: Any
    seen: metadata_rules.BatchSeen
    side: int
    cancel: Cancel | None = None
    live: speedcheck.LiveRun | None = None
    abort: AbortPolicy = field(default_factory=AbortPolicy)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def _flag_unreadable(self, photo: Photo, why: str) -> tuple[Photo, Status | None, str | None]:
        """A photo the app cannot open is flagged with the reason; the batch carries on."""
        store = self.store
        pid = photo.id
        assert pid is not None
        meta = {
            "filename": photo.orig_name,
            "description": "",
            "keywords": [],
            "categories": [],
            "editorial": False,
            "mature": False,
            "illustration": False,
            "warnings": [why],
            **models._file_facts(photo.metadata),
        }
        meta.pop("ai_run", None)
        _drop_prepared(store, photo)
        if photo.status != Status.READY:
            store.set_status(pid, Status.READY)
        store.update_photo(pid, metadata=meta)
        store.set_status(pid, Status.NEEDS_REVIEW)
        with self.lock:
            self.summary.errors.append((pid, why))
        return photo, Status.NEEDS_REVIEW, None

    def _log_call(self, use: VisionLLM, pid: int, **fields: Any) -> None:
        self.store.log_llm_call(LLMCall(use.name, use.model, photo_id=pid, **fields))

    def _describe(self, photo: Photo, use: VisionLLM, image: Any, tries: int) -> Any:
        cfg, style = self.cfg, self.style
        facts = photo_context(photo, style.english_only, cfg.prepare.include_gps)
        has_model, has_property = checks._release_flags(photo.release_ids)
        return describe.describe_photo(
            use,
            image,
            photo.orig_name,
            style=style,
            restrictions=self.restrictions,
            notes=self.notes,
            captured=core_text.parse_dt(photo.captured_at)
            or core_text.parse_dt(facts.get("captured")),
            feedback=self.feedback,
            max_tokens=cfg.llm.max_tokens,
            temperature=cfg.llm.temperature,
            retries=cfg.llm.json_retries,
            timeout_s=cfg.llm.timeout_s,
            has_model_release=has_model,
            has_property_release=has_property,
            filters=self.spec,
            at=core_text.now_iso(),
            seen=self.seen,
            facts=facts,
            condensed=selection.use_condensed_prompt(use),
            max_tries=tries,
        )

    def _save_result(self, photo: Photo, res: Any, use: VisionLLM, tries: int) -> Status:
        """Write the model's answer for one photo and move it to its next status."""
        store = self.store
        pid = photo.id
        assert pid is not None
        meta = res.metadata.to_dict()
        meta["location"] = res.location
        meta["place"] = res.place
        meta["language"] = self.style.text_language  # what the text was written in, per photo
        meta["caption_sentence"] = res.caption_sentence
        meta["commercial_description"] = res.commercial_description
        meta.update(models._file_facts(photo.metadata))
        meta["ai_run"] = {"model": f"{use.name}/{use.model}", "max_tries": tries}
        if res.issues:
            meta["metadata_issues"] = [i.to_dict() for i in res.issues]
        found = res.filters.to_dict() if res.filters else None
        _drop_prepared(store, photo)
        if photo.status != Status.READY:
            store.set_status(pid, Status.READY)
        store.update_photo(
            pid,
            metadata=meta,
            metadata_source=f"llm:{use.name}/{use.model}",
            detected=res.detected,
            guardrail=res.guardrails.to_dict(),
            filters=found,
        )
        status = checks._status_for(res.metadata, res.guardrails, found)
        store.set_status(pid, status)
        if status == Status.DESCRIBED and self.cfg.review.auto_approve_clean:
            store.set_status(pid, Status.APPROVED, detail={"auto": True})
            with self.lock:
                self.summary.approved += 1
        return status

    def work(self, photo: Photo) -> tuple[Photo, Status | None, str | None]:
        if self.abort.stop.is_set() or (self.cancel and self.cancel()):
            return photo, None, None
        pid = photo.id
        assert pid is not None
        live = self.live
        if live is not None:
            model_now, tries_now = live.snapshot()
            use = live.llm_for(model_now, self.llm)
        else:
            use, tries_now = self.llm, self.cfg.llm.max_tries
        try:
            try:
                image = imaging.preview_jpeg(photo.src_path, self.side)
            except Exception as exc:
                why = imaging.unreadable_message(photo.src_path, exc)
                if why is None:
                    raise
                return self._flag_unreadable(photo, why)
            res = self._describe(photo, use, image, tries_now)
        except LLMError as exc:
            self._log_call(use, pid, ok=False, error=str(exc)[:500])
            if self.abort.failed(exc):
                self.summary.aborted = str(exc)
            return photo, None, str(exc)
        except Exception as exc:
            return photo, None, f"{type(exc).__name__}: {exc}"
        self.abort.succeeded()
        r = res.llm
        self._log_call(
            use,
            pid,
            input_tokens=r.input_tokens,
            output_tokens=r.output_tokens,
            seconds=r.seconds,
            repaired=r.repaired,
            retries=r.retries,
        )
        status = self._save_result(photo, res, use, tries_now)
        with self.lock:
            self.summary.input_tokens += r.input_tokens
            self.summary.output_tokens += r.output_tokens
        if live is not None:
            live.record(r.seconds)
        return photo, status, None


def _seen_in_batch(store: Store, batch_id: str, photos: list[Photo]) -> metadata_rules.BatchSeen:
    """What the batch already says (photos not being redone), so no two photos match."""
    seen = metadata_rules.BatchSeen()
    redo = {p.id for p in photos}
    for other in store.list_photos(batch_id):
        om = other.metadata or {}
        if other.id not in redo and om.get("description"):
            seen.add(str(om["description"]), om.get("keywords") or [])
    return seen


def _tally(summary: DescribeSummary, photo: Photo, status: Status | None, err: str | None) -> None:
    if status == Status.BLOCKED:
        summary.blocked += 1
    elif status == Status.SET_ASIDE:
        summary.set_aside += 1
    elif status == Status.NEEDS_REVIEW:
        summary.needs_review += 1
    elif status == Status.DESCRIBED:
        summary.described += 1
    elif err:
        summary.failed += 1
        assert photo.id is not None
        summary.errors.append((photo.id, err))


def describe_batch(
    cfg: Config,
    store: Store,
    batch_id: str,
    only_ids: Iterable[int] | None = None,
    llm: VisionLLM | None = None,
    *,
    progress: Progress | None = None,
    cancel: Cancel | None = None,
    live: speedcheck.LiveRun | None = None,
) -> DescribeSummary:
    """Describe `ready` photos (or the given ids, which are sent back to `ready` first).

    Culled photos never reach the model. Result per photo: described / needs_review / blocked.
    Stops early when the provider is unreachable or several calls in a row fail.
    `live` (local models only) holds the max tries and model for the photos not started yet; it
    is read again before each photo, so changes made while the batch runs apply at once.
    """
    progress = progress or core_progress.noop
    llm = llm or selection.resolve_llm(cfg, store)
    photos = _describe_targets(store, batch_id, only_ids)
    summary = DescribeSummary()
    if not photos:
        return summary
    batch = store.get_batch(batch_id)
    run = DescribeRun(
        cfg,
        store,
        llm,
        summary,
        notes=batch.notes if batch else "",
        restrictions=checks.load_restrictions(cfg),
        style=prompts.Style.from_config(cfg.style),
        spec=filters_mod.filter_spec(cfg),
        feedback=prompts.feedback_lines(store.rejection_stats(), store.recent_rejection_examples()),
        seen=_seen_in_batch(store, batch_id, photos),
        side=cfg.llm.image_max_side or llm.default_image_side,
        cancel=cancel,
        live=live,
    )
    conc = max(1, cfg.llm.concurrency or llm.default_concurrency)
    done = 0
    with ThreadPoolExecutor(max_workers=conc) as pool:
        futures = [pool.submit(run.work, p) for p in photos]
        for fut in as_completed(futures):
            photo, status, err = fut.result()
            done += 1
            progress(done, len(photos), photo.orig_name)
            _tally(summary, photo, status, err)
    reports.refresh_report(cfg, store, batch_id)
    return summary


def _merge(total: DescribeSummary, part: DescribeSummary) -> None:
    for name in (
        "described",
        "needs_review",
        "blocked",
        "set_aside",
        "failed",
        "approved",
        "input_tokens",
        "output_tokens",
    ):
        setattr(total, name, getattr(total, name) + getattr(part, name))
    total.errors.extend(part.errors)
    total.aborted = total.aborted or part.aborted


def describe_waiting(
    cfg: Config,
    store: Store,
    batch_id: str,
    *,
    llm: VisionLLM | None = None,
    progress: Progress | None = None,
    cancel: Cancel | None = None,
    live: speedcheck.LiveRun | None = None,
) -> DescribeSummary:
    """Describe every `ready` photo, and the ones sent back while this runs.

    Each round takes the `ready` photos not tried yet in this call; it ends when none are left,
    when `cancel` says so or when the provider is down. A photo that failed stays `ready` and is
    not tried twice. The progress total grows when photos are added while it runs.
    """
    progress = progress or core_progress.noop
    llm = llm or selection.resolve_llm(cfg, store)
    summary = DescribeSummary()
    failed: set[int] = set()  # still `ready` after an attempt: not tried again in this call
    handled = 0
    while not (cancel and cancel()) and not summary.aborted:
        ids = [p.id for p in _describe_targets(store, batch_id, None) if p.id not in failed]
        ids = [i for i in ids if i is not None]
        if not ids:
            break
        base, total = handled, handled + len(ids)

        def part_progress(
            done: int, count: int, name: str = "", _base: int = base, _total: int = total
        ) -> None:
            progress(_base + done, _total, name)

        _merge(
            summary,
            describe_batch(
                cfg, store, batch_id, ids, llm, progress=part_progress, cancel=cancel, live=live
            ),
        )
        handled = total
        failed |= {p.id for p in store.list_photos(batch_id, [Status.READY]) if p.id in ids}
    return summary
