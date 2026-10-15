"""Health check and the home page with one card per batch."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse

from shuto.settings import ui as settings_ui
from shuto.web import ux
from shuto.web.routes.model_support import current_model
from shuto.web.state import Ctx, WebCtx, app_version
from shuto.workflow import check_batch, reports

router = APIRouter()


def _n(counts: dict[str, int], *names: str) -> int:
    return sum(int(counts.get(x, 0)) for x in names)


def batch_cards(ctx: WebCtx) -> list[dict]:
    cards = []
    for b in reports.batch_report(ctx.store):
        counts = b["counts"]

        stage = ux.stage(counts)
        estimate_text = None
        if stage == "check":
            try:
                estimate_text = check_batch.estimate_check(ctx.cfg, ctx.store, b["id"]).text
            except Exception:
                estimate_text = "Choose an AI model to see an estimate"
        chips = [
            ("ok", "Ready", _n(counts, "described", "approved", "prepared")),
            ("review", "Needs a look", _n(counts, "needs_review")),
            ("blocked", "Can't send", _n(counts, "blocked")),
            ("neutral", "Sent before", _n(counts, "already_uploaded")),
            (
                "neutral",
                "Set aside",
                _n(counts, "culled", "duplicate", "near_duplicate", "set_aside"),
            ),
            ("ok", "Uploaded", _n(counts, "uploaded", "exported")),
            ("ok", "Submitted", _n(counts, "submitted", "accepted", "rejected")),
        ]
        cards.append(
            {
                "id": b["id"],
                "notes": b["notes"],
                "date": (b["created_at"] or "")[:10],
                "total": b["total"],
                "stage": stage,
                "steps": ux.steps(b["id"], counts),
                "action": ux.next_action(b["id"], counts),
                "chips": [c for c in chips if c[2]],
                "estimate": estimate_text,
            }
        )
    return cards


@router.get("/healthz", response_class=PlainTextResponse)
def healthz():
    return f"shuto {app_version()}"


@router.get("/", response_class=HTMLResponse)
def index(request: Request, ctx: Ctx):
    cards = batch_cards(ctx)
    if not cards and not settings_ui.get_ui_setting(ctx.store, "onboarded"):
        return RedirectResponse("/welcome", status_code=303)
    return ctx.render(request, "index.html", cards=cards, model=current_model(ctx))
