"""Check a batch with the AI (the "check" step)."""

from __future__ import annotations

import time
from dataclasses import dataclass, replace

from shuto.ai import estimate, selection, speedcheck
from shuto.ai.providers.base import VisionLLM
from shuto.core import progress as core_progress
from shuto.core.config import Config
from shuto.core.errors import PipelineError
from shuto.core.models import Status
from shuto.core.progress import Cancel, Progress
from shuto.store import Store
from shuto.workflow import checks, culling, describing, importing, speed
from shuto.workflow.culling import CullSummary
from shuto.workflow.describing import DescribeSummary


@dataclass
class CheckSummary:
    cull: CullSummary
    describe: DescribeSummary | None = None


CHECK_SCALE = 1000  # units of the combined Check bar


def _mono() -> float:
    return time.monotonic()


def _time_left(seconds: float) -> str:
    if seconds < 60:
        return "less than a minute left"
    return f"about {round(seconds / 60)} min left"


def check_batch(
    cfg: Config,
    store: Store,
    batch_id: str,
    *,
    progress: Progress | None = None,
    cancel: Cancel | None = None,
    llm: VisionLLM | None = None,
    live: speedcheck.LiveRun | None = None,
) -> CheckSummary:
    """Quality check, then titles and keywords, with one combined progress.

    Raises ModelChoiceRequired / PipelineError (friendly text) before doing any work when the
    AI cannot run. `describe` is None when cancelled before the second step.
    """
    progress = progress or core_progress.noop
    if llm is None:
        problems = selection.describe_preflight(cfg, selection.resolve_selection(cfg, store))
        if problems:
            raise PipelineError(" ".join(problems))
        llm = selection.resolve_llm(cfg, store)
    # one bar over CHECK_SCALE units: the quality check is the first tenth, the AI the rest. The AI
    # photo count is only known after culling, so each step reports its own counter.
    quality_units = CHECK_SCALE // 10

    def cull_progress(done: int, count: int, name: str = "") -> None:
        text = f"Step 1 of 2 · Quality check {done}/{count}"
        if name and done < count:
            text += f" (just finished: {name})"
        progress(quality_units * done // max(count, 1), CHECK_SCALE, text)

    started = [0.0]

    def describe_progress(done: int, count: int, name: str = "") -> None:
        text = f"Step 2 of 2 · Writing titles & keywords {done}/{count}"
        if done and done < count:
            per_photo = (time.monotonic() - started[0]) / done
            text += f" · {_time_left((count - done) * per_photo)}"
        if name and done < count:
            text += f" (just finished: {name})"
        units = quality_units + (CHECK_SCALE - quality_units) * done // max(count, 1)
        progress(min(units, CHECK_SCALE - 1), CHECK_SCALE, text)

    to_cull = min(len(store.list_photos(batch_id, [Status.IMPORTED])), importing.MAX_BATCH_PHOTOS)
    if to_cull:
        cull_progress(0, to_cull)
    culled = culling.cull_batch(cfg, store, batch_id, progress=cull_progress, cancel=cancel)
    if cancel and cancel():
        return CheckSummary(culled, None)
    to_describe = len(describing._describe_targets(store, batch_id, None))
    started[0] = time.monotonic()
    if to_describe:
        describe_progress(0, to_describe)
    else:
        progress(
            CHECK_SCALE - 1,
            CHECK_SCALE,
            "No photos left for the AI after the quality check",
        )
    described = describing.describe_batch(
        cfg, store, batch_id, llm=llm, progress=describe_progress, cancel=cancel, live=live
    )
    return CheckSummary(culled, described)


def estimate_check(cfg: Config, store: Store, batch_id: str) -> estimate.Estimate:
    """Like estimate_describe for imported + ready photos (an upper bound: some get culled)."""
    sel = selection.resolve_selection(cfg, store)
    n = len(store.list_photos(batch_id, [Status.IMPORTED, Status.READY]))
    llm_cls = selection.get_provider_class(sel.provider)
    side = cfg.llm.image_max_side or llm_cls.default_image_side
    conc = cfg.llm.concurrency or llm_cls.default_concurrency
    if sel.provider == "ollama":
        recs = speed._recs(cfg, [])
        rec = next((r for r in recs if r.model.tag == sel.model), None)
        est = estimate.for_batch(
            n,
            "ollama",
            sel.model,
            recommendation=rec,
            concurrency=conc,
            **checks._estimate_tokens(cfg),
        )
    else:
        est = estimate.for_batch(
            n,
            sel.provider,
            sel.model,
            api_model=selection.api_model(sel.provider, sel.model),
            concurrency=conc,
            image_side=side,
            **checks._estimate_tokens(cfg),
        )
    return replace(est, text=f"up to {est.text}")
