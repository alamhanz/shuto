"""What the batch pages share: URLs, review filters, the stepper page and AI time facts."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from fastapi import Request

from shuto.core import paths
from shuto.core.models import UPLOADED_STATES, Photo, Status
from shuto.web import ux
from shuto.web.state import WebCtx
from shuto.workflow import reports

FILTERS = ("attention", "good", "aside", "all")

# Photos that wait for the AI (just imported, or sent back). Review hides them until it has run.
WAITING = (Status.IMPORTED, Status.READY)
SENT = (Status.ALREADY_UPLOADED, *UPLOADED_STATES)


def is_waiting(photo: Photo) -> bool:
    return photo.status in WAITING


def is_sent(photo: Photo) -> bool:
    """True when Shutterstock already has this photo (sent before, uploaded, submitted...)."""
    return photo.status in SENT


def batch_url(batch_id: str, page: str = "") -> str:
    return f"/batch/{quote(batch_id, safe='')}" + (f"/{page}" if page else "")


def filter_of(value: Any) -> str | None:
    """A review filter from user input, or None when it is not one of the known four."""
    text = str(value or "")
    return text if text in FILTERS else None


def count_by_status(photos: list[Photo]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for p in photos:
        counts[p.status.value] = counts.get(p.status.value, 0) + 1
    return counts


def in_filter(photo: Photo, show: str | None) -> bool:
    if is_waiting(photo):
        return False
    return show in (None, "all") or ux.bucket(photo.status) == show


def photo_path(photo_id: int, show: str | None) -> str:
    return f"/photo/{photo_id}" + (f"?back={show}" if show else "")


def batch_page(ctx: WebCtx, request: Request, name: str, batch_id: str, current: str, **extra: Any):
    """Render a batch-flow page with its stepper."""
    batch = ctx.get_batch_or_404(batch_id)
    photos = ctx.store.list_photos(batch_id)
    counts = count_by_status(photos)
    return ctx.render(
        request,
        name,
        batch=batch,
        counts=counts,
        total=len(photos),
        photos=photos,
        steps=ux.steps(batch_id, counts),
        step_current=current,
        active_job=ctx.runner.active_job(batch_id),
        batch_folder=str(paths.batch_dir(ctx.cfg, batch_id)),
        **extra,
    )


def ai_facts(ctx: WebCtx, batch_id: str) -> dict[str, Any]:
    """Total and average AI time for the batch, as plain text (None until the AI has run)."""
    t = reports.ai_time_summary(ctx.store, batch_id)
    if not t["photos"]:
        return {"ai_time": None}
    return {
        "ai_time": {
            "total": ux.ai_time_text(float(t["total"])),
            "average": ux.ai_time_text(float(t["average"])),
            "photos": int(t["photos"]),
        }
    }
