"""Shared web plumbing: state, CSRF-checked forms and the helpers every route module uses."""

from __future__ import annotations

import hmac
import re
import secrets as _stdlib_secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import quote

from fastapi import Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.datastructures import FormData

from .. import hardware, local_models, pipeline
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


CHECK_TIMEOUT_S = 3.0  # asking Ollama whether the chosen model is there must stay quick
CACHE_TTL_S = 10.0  # how long a model check is reused between page loads


@dataclass(frozen=True)
class ModelStatus:
    """Can the AI write text right now?

    state: ready | missing | downloading (the model the user asked for is on its way) |
    unknown (cannot tell).
    """

    state: str
    message: str = ""

    @property
    def ready(self) -> bool:
        return self.state == "ready"


NO_MODEL = (
    "No AI model is set. shuto can't write titles and keywords until you choose one in Settings."
)
CANT_TELL = "shuto can't tell which AI model is set right now. Open Settings to check."
LOCAL_AI_UNREACHABLE = (
    "shuto can't reach the local AI right now. Open the Ollama app, wait a moment and try again."
)


class WebState:
    """Everything the routes share; one per app. Routes read `state.cfg` on every request."""

    def __init__(self, cfg: Config, config_path: str | None, extra_hosts: set[str]):
        self.cfg = cfg
        self.config_path = config_path
        self.store = pipeline.open_store(cfg)
        self.runner = JobRunner(self.store)
        self.csrf = _stdlib_secrets.token_urlsafe(32)
        # (provider, model, ollama url) -> (when, ModelStatus); cleared by any change
        self.model_cache: dict[tuple[str, str, str], tuple[float, Any]] = {}
        # model the user started downloading in the setup wizard; chosen when the download ends
        self.pending_model: str | None = None
        # batch id -> live speed check of the local describe job running for it
        self.live: dict[str, Any] = {}
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
    request.app.state.web.model_cache.clear()  # any change may alter which model is usable
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

    def invalidate_model_status(self) -> None:
        """Forget cached model checks (a model was chosen, downloaded or removed, a key changed)."""
        self.state.model_cache.clear()

    def model_status(self, request: Request | None = None) -> ModelStatus:
        """Is a usable AI model chosen? Cheap: short timeout, reused for a few seconds.

        Only a missing choice is `missing`. Anything we cannot tell (local AI not reachable,
        unreadable settings) is `unknown`: the app-wide banner stays quiet.
        """
        cached = getattr(request.state, "model_status", None) if request else None
        if cached is not None:
            return cached
        status = self._model_status(full=False, cache=True)
        if request is not None:
            request.state.model_status = status
        return status

    def fresh_model_status(self) -> ModelStatus:
        """Like `model_status` but always asked again (never the cached answer)."""
        return self._model_status(full=False, cache=False)

    def _model_status(self, *, full: bool, cache: bool) -> ModelStatus:
        status = self._checked_status(full=full, cache=cache)
        pending = self.state.pending_model
        if status.state == "missing" and pending and "pull" in self.runner.active_kinds():
            return ModelStatus(
                "downloading",
                f"Downloading {pending}... It is chosen for you when the download finishes.",
            )
        return status

    def _checked_status(self, *, full: bool, cache: bool) -> ModelStatus:
        cfg = self.cfg
        try:
            sel = pipeline.resolve_selection(cfg, self.store)
        except local_models.ModelChoiceRequired:
            return ModelStatus("missing", NO_MODEL)
        except Exception:
            return ModelStatus("unknown", CANT_TELL)
        key = (sel.provider, sel.model, cfg.llm.ollama.url)
        hit = self.state.model_cache.get(key) if cache else None
        if hit is not None and time.monotonic() - hit[0] < CACHE_TTL_S:
            return hit[1]
        status = self._check(sel, full)
        if cache:
            self.state.model_cache[key] = (time.monotonic(), status)
        return status

    def _check(self, sel: pipeline.Selection, full: bool) -> ModelStatus:
        cfg = self.cfg
        if sel.provider == "ollama":
            client = pipeline.ollama_client(cfg)
            if hasattr(client, "timeout"):
                client.timeout = min(client.timeout, CHECK_TIMEOUT_S)
            found = hardware.ollama_status(client)
            if not found.running:
                return ModelStatus("unknown", LOCAL_AI_UNREACHABLE)
            have = {hardware.normalize_tag(p) for p in found.pulled}
            if hardware.normalize_tag(sel.model) not in have:
                return ModelStatus(
                    "missing", f"The AI model {sel.model} is not downloaded yet. Open Settings."
                )
            return ModelStatus("ready")
        problems = (
            pipeline.describe_preflight(cfg, sel) if full else pipeline.api_quick_problems(sel)
        )
        if problems:
            return ModelStatus(
                "missing",
                f"The online AI {sel.provider} is not ready yet. "
                "Open Settings to finish setting it up.",
            )
        return ModelStatus("ready")

    def model_block(self, request: Request) -> str | None:
        """Plain-words reason not to start an AI job now, or None when it can go ahead.

        Always a fresh, full check (never the cached one): a job must not start on stale news.
        """
        cached = getattr(request.state, "model_block", None)
        if cached is None:
            status = self._model_status(full=True, cache=False)
            cached = request.state.model_block = (None if status.ready else status.message,)
        return cached[0]

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
        live = self.state.live.get(job.batch_id or "")
        if live is not None and job.state in ("queued", "running"):
            extra.setdefault("speed", live.suggestion())
            extra.setdefault("speed_notice", live.notice)
        return self.render(
            request,
            "_job.html",
            job=job,
            result=result if isinstance(result, str) else None,
            active=job.state in ("queued", "running"),
            text=self.runner.text(job_id),
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
