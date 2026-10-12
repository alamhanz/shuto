"""Shared web plumbing: state, CSRF-checked forms and the helpers every route module uses."""

from __future__ import annotations

import hmac
import re
import secrets as _stdlib_secrets
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import quote

from fastapi import Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.datastructures import FormData

from .. import pipeline
from ..categories import SHUTTERSTOCK_CATEGORIES
from ..config import Config
from ..jobs import JobBusy, JobContext, JobRunner
from ..models import REASON_CODES, Photo
from . import ux

HERE = Path(__file__).parent
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "[::1]", "::1"}
MODELS_JOB_KEY = "models"  # pseudo batch id: one model download/benchmark at a time
IMPORT_JOB_KEY = "import"


def hostname(host_header: str) -> str:
    """`example:8765` -> `example`; `[::1]:8765` -> `[::1]`."""
    host = host_header.strip().lower()
    if host.startswith("["):
        return host.split("]")[0] + "]"
    return host.split(":")[0]


def split_lines(text: str | None) -> list[str]:
    return [p.strip() for p in re.split(r"[,\n\r]+", text or "") if p.strip()]


def short(text: str, limit: int = 300) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "..."


class WebState:
    """Everything the routes share; one per app. Routes read `state.cfg` on every request."""

    def __init__(self, cfg: Config, config_path: str | None, extra_hosts: set[str]):
        self.cfg = cfg
        self.config_path = config_path
        self.store = pipeline.open_store(cfg)
        self.runner = JobRunner(self.store)
        self.csrf = _stdlib_secrets.token_urlsafe(32)
        self.allowed_hosts = LOOPBACK_HOSTS | {h.lower() for h in extra_hosts}
        if cfg.web.host not in ("0.0.0.0", "::"):
            self.allowed_hosts.add(hostname(cfg.web.host))
        self.templates = Jinja2Templates(directory=str(HERE / "templates"))
        self.templates.env.globals.update(
            categories=SHUTTERSTOCK_CATEGORIES,
            reason_codes=REASON_CODES,
            label=ux.label,
            bucket=ux.bucket,
            stage_of=ux.stage,
            next_action=ux.next_action,
            LABELS=ux.LABELS,
            BUCKET_LABELS=ux.BUCKET_LABELS,
            HELP=ux.HELP,
            TERMS=ux.TERMS,
        )
        self.templates.env.filters["pct"] = lambda v: f"{float(v):.0f}"


async def post_form(request: Request) -> FormData:
    """Parse a POST form and enforce the CSRF token (header or hidden field)."""
    form = await request.form()
    sent = request.headers.get("x-csrf-token") or str(form.get("csrf_token") or "")
    if not sent or not hmac.compare_digest(sent, request.app.state.web.csrf):
        raise HTTPException(status_code=403, detail="Missing or invalid CSRF token.")
    return form


PostForm = Annotated[FormData, Depends(post_form)]


class WebCtx:
    """Request helpers bound to one app's state; passed to every `register_*_routes`."""

    def __init__(self, state: WebState):
        self.state = state

    @property
    def cfg(self) -> Config:
        return self.state.cfg

    @property
    def store(self):
        return self.state.store

    @property
    def runner(self) -> JobRunner:
        return self.state.runner

    def render(
        self, request: Request, name: str, status_code: int = 200, **ctx: Any
    ) -> HTMLResponse:
        base = {
            "csrf": self.state.csrf,
            "notice": request.query_params.get("notice"),
            "error": request.query_params.get("error"),
            "upload_enabled": self.cfg.deliver.upload_enabled,
            "ui_lang": "en",
        }
        return self.state.templates.TemplateResponse(
            request, name, {**base, **ctx}, status_code=status_code
        )

    def back(
        self, path: str, notice: str | None = None, error: str | None = None
    ) -> RedirectResponse:
        query = []
        if notice:
            query.append("notice=" + quote(short(notice)))
        if error:
            query.append("error=" + quote(short(error)))
        sep = "&" if "?" in path else "?"
        url = path + (sep + "&".join(query) if query else "")
        return RedirectResponse(url, status_code=303)

    def get_batch_or_404(self, batch_id: str):
        batch = self.store.get_batch(batch_id)
        if batch is None:
            raise HTTPException(status_code=404, detail="Unknown batch.")
        return batch

    def get_photo_or_404(self, photo_id: int) -> Photo:
        photo = self.store.get_photo(photo_id)
        if photo is None:
            raise HTTPException(status_code=404, detail="Unknown photo.")
        return photo

    def job_fragment(self, request: Request, job_id: int, **extra: Any) -> HTMLResponse:
        job = self.runner.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Unknown job.")
        result = self.runner.result(job_id)
        return self.render(
            request,
            "_job.html",
            job=job,
            result=result if isinstance(result, str) else None,
            active=job.state in ("queued", "running"),
            **extra,
        )

    def message_fragment(self, request: Request, text: str) -> HTMLResponse:
        return self.render(request, "_msg.html", message=text)

    def start_job(
        self,
        request: Request,
        kind: str,
        key: str,
        func: Callable[[JobContext], Any],
        total: int = 0,
        **extra: Any,
    ) -> HTMLResponse:
        try:
            job_id = self.runner.submit(kind, key, func, total=total)
        except JobBusy as exc:
            return self.message_fragment(request, str(exc))
        return self.job_fragment(request, job_id, **extra)

    @staticmethod
    def ids_from(form: FormData) -> list[int]:
        return [int(v) for v in form.getlist("ids") if str(v).isdigit()]
