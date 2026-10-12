"""Local web app: FastAPI + Jinja2 + htmx. Calls only the service layer (pipeline, jobs).

Security model: loopback bind by default, Host header allow-list (DNS rebinding), Origin check on
every state-changing request, a per-process CSRF token, autoescaped templates, files served by
photo id only (paths come from the database), no secret values ever rendered.
"""

from __future__ import annotations

import hmac
import re
import secrets as _stdlib_secrets
import threading
from collections.abc import Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import quote, urlsplit

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.datastructures import FormData

from .. import estimate, imaging, pipeline
from .. import secrets as shuto_secrets
from ..categories import SHUTTERSTOCK_CATEGORIES
from ..config import Config
from ..jobs import JobBusy, JobContext, JobRunner
from ..local_models import ModelChoiceRequired
from ..models import ALLOWED, REASON_CODES, Photo, Status
from ..pipeline import PipelineError

HERE = Path(__file__).parent
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "[::1]", "::1"}
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
BATCH_JOBS = ("cull", "describe", "prepare", "upload", "export")
API_PROVIDERS = ("anthropic", "gemini", "bedrock")
API_KEY_ENV = {
    "anthropic": "ANTHROPIC_API_KEY",
    "gemini": "GEMINI_API_KEY",
}
PREVIEW_SIDE = 2048  # page preview size; originals can be tens of MB
TAG_RE = re.compile(r"^[A-Za-z0-9._:/-]{1,100}$")
MODELS_JOB_KEY = "models"  # pseudo batch id: one model download/benchmark at a time
IMPORT_JOB_KEY = "import"
CSP = (
    "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
    "object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
)
OLLAMA_URL = "https://ollama.com/download"


def _hostname(host_header: str) -> str:
    """`example:8765` -> `example`; `[::1]:8765` -> `[::1]`."""
    host = host_header.strip().lower()
    if host.startswith("["):
        return host.split("]")[0] + "]"
    return host.split(":")[0]


def _split_lines(text: str | None) -> list[str]:
    return [p.strip() for p in re.split(r"[,\n\r]+", text or "") if p.strip()]


def _short(text: str, limit: int = 300) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "..."


class WebState:
    """Everything the routes share; one per app."""

    def __init__(self, cfg: Config, config_path: str | None, extra_hosts: set[str]):
        self.cfg = cfg
        self.config_path = config_path
        self.store = pipeline.open_store(cfg)
        self.runner = JobRunner(self.store)
        self.csrf = _stdlib_secrets.token_urlsafe(32)
        self.allowed_hosts = LOOPBACK_HOSTS | {h.lower() for h in extra_hosts}
        if cfg.web.host not in ("0.0.0.0", "::"):
            self.allowed_hosts.add(_hostname(cfg.web.host))
        self.templates = Jinja2Templates(directory=str(HERE / "templates"))
        self.templates.env.globals.update(
            categories=SHUTTERSTOCK_CATEGORIES, reason_codes=REASON_CODES
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


def create_app(
    cfg: Config,
    *,
    config_path: str | None = None,
    allowed_hosts: set[str] | None = None,
) -> FastAPI:
    state = WebState(cfg, config_path, allowed_hosts or set())

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        state.runner.shutdown()

    app = FastAPI(title="shuto", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.web = state
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")

    # ------------------------------------------------------------------ security

    @app.middleware("http")
    async def guard(request: Request, call_next):
        host = request.headers.get("host", "")
        if _hostname(host) not in state.allowed_hosts:
            return PlainTextResponse("Bad Host header.", status_code=400)
        if request.method not in SAFE_METHODS:
            origin = request.headers.get("origin")
            if origin is not None and urlsplit(origin).netloc.lower() != host.strip().lower():
                return PlainTextResponse("Cross-origin request refused.", status_code=403)
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = CSP
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        return response

    # ------------------------------------------------------------------ helpers

    def render(request: Request, name: str, status_code: int = 200, **ctx: Any) -> HTMLResponse:
        base = {
            "csrf": state.csrf,
            "notice": request.query_params.get("notice"),
            "error": request.query_params.get("error"),
            "upload_enabled": cfg.deliver.upload_enabled,
        }
        return state.templates.TemplateResponse(
            request, name, {**base, **ctx}, status_code=status_code
        )

    def back(path: str, notice: str | None = None, error: str | None = None) -> RedirectResponse:
        query = []
        if notice:
            query.append("notice=" + quote(_short(notice)))
        if error:
            query.append("error=" + quote(_short(error)))
        sep = "&" if "?" in path else "?"
        url = path + (sep + "&".join(query) if query else "")
        return RedirectResponse(url, status_code=303)

    def get_batch_or_404(batch_id: str):
        batch = state.store.get_batch(batch_id)
        if batch is None:
            raise HTTPException(status_code=404, detail="Unknown batch.")
        return batch

    def get_photo_or_404(photo_id: int) -> Photo:
        photo = state.store.get_photo(photo_id)
        if photo is None:
            raise HTTPException(status_code=404, detail="Unknown photo.")
        return photo

    def job_fragment(request: Request, job_id: int) -> HTMLResponse:
        job = state.runner.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Unknown job.")
        result = state.runner.result(job_id)
        return render(
            request,
            "_job.html",
            job=job,
            result=result if isinstance(result, str) else None,
            active=job.state in ("queued", "running"),
        )

    def message_fragment(request: Request, text: str) -> HTMLResponse:
        return render(request, "_msg.html", message=text)

    def start_job(
        request: Request, kind: str, key: str, func: Callable[[JobContext], Any], total: int = 0
    ) -> HTMLResponse:
        try:
            job_id = state.runner.submit(kind, key, func, total=total)
        except JobBusy as exc:
            return message_fragment(request, str(exc))
        return job_fragment(request, job_id)

    def ids_from(form) -> list[int]:
        out = []
        for value in form.getlist("ids"):
            if str(value).isdigit():
                out.append(int(value))
        return out

    # ------------------------------------------------------------------ pages

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        active = state.runner.active_job(IMPORT_JOB_KEY)
        return render(
            request,
            "index.html",
            batches=pipeline.batch_report(state.store),
            import_job=state.runner.get(active) if active else None,
            job_active=bool(active),
            workspace=str(cfg.workspace_dir),
        )

    @app.post("/import", response_class=HTMLResponse)
    def start_import(request: Request, form: PostForm):
        raw = str(form.get("path") or "").strip()
        if not raw:
            return message_fragment(request, "Enter the folder that holds your photos.")
        folder = Path(raw).expanduser()
        if not folder.is_dir():
            return message_fragment(request, f"{raw!r} is not a folder on this computer.")
        notes = str(form.get("notes") or "").strip() or None
        recursive = form.get("recursive") == "yes"
        force = form.get("force") == "yes"
        mark_uploaded = form.get("mark_uploaded") == "yes"
        if force and mark_uploaded:
            return message_fragment(request, "Choose either Import anyway or Remember, not both.")

        def run(ctx: JobContext) -> str:
            s = pipeline.import_folder(
                cfg,
                state.store,
                folder,
                recursive=recursive,
                notes=notes,
                progress=ctx.progress,
                force=force,
                mark_uploaded=mark_uploaded,
            )
            batches = ", ".join(f"{b} ({n})" for b, n in s.batches.items()) or "none"
            text = (
                f"Imported {s.imported} photo(s) into batches: {batches}. "
                f"{s.duplicates + s.already_known} duplicate/known, {s.near_duplicates} near-"
                f"duplicate, {len(s.errors)} unreadable."
            )
            if s.marked_uploaded:
                text += f" Registered {s.marked_uploaded} photo(s) as already uploaded."
            if s.already_uploaded:
                shown = "; ".join(a.text() for a in s.already_uploaded[:10])
                more = len(s.already_uploaded) - 10
                text += (
                    f" Skipped {len(s.already_uploaded)} photo(s) uploaded before: {shown}"
                    + (f"; and {more} more" if more > 0 else "")
                    + '. Tick "Import anyway" to process them again.'
                )
            return text

        return start_job(request, "import", IMPORT_JOB_KEY, run)

    @app.get("/jobs/{job_id}", response_class=HTMLResponse)
    def job_status(request: Request, job_id: int):
        return job_fragment(request, job_id)

    @app.get("/batch/{batch_id}", response_class=HTMLResponse)
    def batch_page(request: Request, batch_id: str, status: str | None = None):
        batch = get_batch_or_404(batch_id)
        photos = state.store.list_photos(batch_id)
        counts: dict[str, int] = {}
        for p in photos:
            counts[p.status.value] = counts.get(p.status.value, 0) + 1
        shown = [p for p in photos if status is None or p.status.value == status]
        # Near-duplicate groups stay together, in order of first appearance.
        buckets: dict[tuple[str, int], list[Photo]] = {}
        for p in shown:
            key = ("g", p.near_dup_group) if p.near_dup_group is not None else ("p", p.id or 0)
            buckets.setdefault(key, []).append(p)
        cards = [_card(p) for group in buckets.values() for p in group]

        est_text, est_problem, selection, problems = None, None, None, []
        ready = counts.get(Status.READY.value, 0)
        if ready:
            try:
                est = pipeline.estimate_describe(cfg, state.store, batch_id)
                est_text = est.text
                sel = pipeline.resolve_selection(cfg, state.store)
                selection = f"{sel.provider} / {sel.model}"
                problems = pipeline.describe_preflight(cfg, sel)
            except ModelChoiceRequired:
                est_problem = "No local model chosen yet. Pick one on the Settings page first."
            except PipelineError as exc:
                est_problem = str(exc)
        active = state.runner.active_job(batch_id)
        exports = sorted({p.export_file for p in photos if p.export_file})
        return render(
            request,
            "batch.html",
            batch=batch,
            counts=counts,
            total=len(photos),
            cards=cards,
            filter=status,
            statuses=[s.value for s in Status],
            estimate_text=est_text,
            estimate_problem=est_problem,
            selection=selection,
            problems=problems,
            ready=ready,
            export_files=[Path(f).name for f in exports],
            job=state.runner.get(active) if active else None,
            job_active=bool(active),
            usage=state.store.usage_by_batch(batch_id),
        )

    def _card(p: Photo) -> dict[str, Any]:
        gr = p.guardrail or {}
        meta = p.metadata or {}
        reasons: list[str] = []
        if p.status in (Status.CULLED,):
            reasons = list((p.cull or {}).get("reasons") or [])
        elif p.status in (Status.NEEDS_REVIEW, Status.BLOCKED):
            reasons = list(gr.get("messages") or [])
        return {
            "id": p.id,
            "name": p.orig_name,
            "status": p.status.value,
            "group": p.near_dup_group,
            "reasons": reasons[:3],
            "snippet": _short(str(meta.get("description") or ""), 90),
            "editorial": bool(meta.get("editorial")),
            "gps": p.has_gps,
        }

    @app.post("/batch/{batch_id}/bulk")
    def batch_bulk(batch_id: str, form: PostForm):
        get_batch_or_404(batch_id)
        action = str(form.get("action") or "")
        ids = ids_from(form)
        path = f"/batch/{quote(batch_id)}"
        try:
            if action == "approve_all":
                s = pipeline.approve(cfg, state.store, batch_id=batch_id)
            elif action in ("approve", "ready"):
                if not ids:
                    return back(path, error="Select at least one photo first.")
                if action == "approve":
                    s = pipeline.approve(cfg, state.store, photo_ids=ids, include_needs_review=True)
                else:
                    changed = 0
                    refused = []
                    for pid in ids:
                        try:
                            pipeline.override_status(state.store, pid, Status.READY)
                            changed += 1
                        except PipelineError as exc:
                            refused.append(str(exc))
                    return back(
                        path,
                        notice=f"{changed} photo(s) marked ready to describe.",
                        error=refused[0] if refused else None,
                    )
            else:
                raise HTTPException(status_code=400, detail="Unknown bulk action.")
        except PipelineError as exc:
            return back(path, error=str(exc))
        refused_text = "; ".join(f"#{i}: {why}" for i, why in list(s.refused.items())[:3])
        return back(
            path, notice=f"Approved {len(s.approved)} photo(s).", error=refused_text or None
        )

    @app.post("/batch/{batch_id}/submitted")
    def batch_submitted(batch_id: str, form: PostForm):
        get_batch_or_404(batch_id)
        ids = pipeline.mark_submitted(state.store, batch_id)
        return back(f"/batch/{quote(batch_id)}", notice=f"Marked {len(ids)} photo(s) as submitted.")

    @app.post("/batch/{batch_id}/{action}", response_class=HTMLResponse)
    def batch_job(request: Request, batch_id: str, action: str, form: PostForm):
        if action not in BATCH_JOBS:
            raise HTTPException(status_code=404, detail="Unknown action.")
        get_batch_or_404(batch_id)
        only = ids_from(form) or None
        store = state.store
        if action == "cull":

            def run(ctx: JobContext) -> str:
                s = pipeline.cull_batch(
                    cfg, store, batch_id, progress=ctx.progress, cancel=ctx.cancelled
                )
                return f"Cull done: {s.ready} ready, {s.culled} culled (of {s.total} new)."

        elif action == "describe":
            try:
                sel = pipeline.resolve_selection(cfg, store)
            except ModelChoiceRequired:
                return message_fragment(request, "Choose a local model on the Settings page first.")
            except PipelineError as exc:
                return message_fragment(request, str(exc))
            problems = pipeline.describe_preflight(cfg, sel)
            if problems:
                return message_fragment(request, " ".join(problems))

            def run(ctx: JobContext) -> str:
                s = pipeline.describe_batch(
                    cfg, store, batch_id, only_ids=only, progress=ctx.progress, cancel=ctx.cancelled
                )
                text = (
                    f"Described {s.described + s.needs_review + s.blocked}: {s.described} clean, "
                    f"{s.needs_review} need review, {s.blocked} blocked, {s.failed} failed."
                )
                if s.aborted:
                    raise PipelineError(f"Stopped early: {s.aborted}. {text}")
                if s.errors:
                    text += f" First error: {s.errors[0][1]}"
                return text

        elif action == "prepare":

            def run(ctx: JobContext) -> str:
                s = pipeline.prepare_batch(
                    cfg, store, batch_id, only_ids=only, progress=ctx.progress, cancel=ctx.cancelled
                )
                text = f"Prepared {len(s.prepared)} file(s)."
                if s.problems:
                    first = next(iter(s.problems.values()))[0]
                    text += f" {len(s.problems)} photo(s) have problems, e.g. {first}"
                return text

        elif action == "upload":
            if form.get("confirm") != "yes":
                return message_fragment(
                    request, "Tick the box to confirm you want to upload to Shutterstock."
                )

            def run(ctx: JobContext) -> str:
                s = pipeline.upload_batch(
                    cfg, store, batch_id, confirm=True, only_ids=only, progress=ctx.progress
                )
                text = f"Uploaded {len(s.uploaded)} file(s)."
                if s.failed:
                    name, err = next(iter(s.failed.items()))
                    text += f" {len(s.failed)} failed, e.g. {name}: {err}"
                if s.skipped_blocked:
                    text += f" Skipped {len(s.skipped_blocked)} blocked."
                return text

        else:

            def run(ctx: JobContext) -> str:
                s = pipeline.export_batch(cfg, store, batch_id)
                if not s.files:
                    return "Nothing to export: upload prepared photos first."
                return f"Wrote {len(s.files)} CSV file(s) for {len(s.rows)} photo(s)."

        return start_job(request, action, batch_id, run)

    @app.get("/batch/{batch_id}/exports/{filename}")
    def export_download(batch_id: str, filename: str):
        get_batch_or_404(batch_id)
        for p in state.store.list_photos(batch_id):
            if p.export_file and Path(p.export_file).name == filename:
                path = Path(p.export_file)
                if path.is_file():
                    return FileResponse(path, media_type="text/csv", filename=filename)
        raise HTTPException(status_code=404, detail="Unknown export file.")

    # ------------------------------------------------------------------ photos

    @app.get("/thumb/{photo_id}")
    def thumb(photo_id: int):
        photo = get_photo_or_404(photo_id)
        path = cfg.workspace_path("thumbs", f"{photo.sha256}.jpg")
        if not path.is_file():
            raise HTTPException(status_code=404, detail="No thumbnail.")
        return FileResponse(path, media_type="image/jpeg")

    @app.get("/image/{photo_id}")
    def image(photo_id: int):
        photo = get_photo_or_404(photo_id)
        src = Path(photo.src_path)
        dest = cfg.workspace_path("previews", f"{photo.sha256}.jpg")
        if not dest.is_file() and src.is_file():
            try:  # cached ~2048 px copy; the original is only read, never served or touched
                imaging.make_thumbnail(src, dest, max_side=PREVIEW_SIDE)
            except Exception:  # unreadable or unsupported file: fall back to the thumbnail
                pass
        if dest.is_file():
            return FileResponse(dest, media_type="image/jpeg")
        return thumb(photo_id)

    @app.get("/photo/{photo_id}", response_class=HTMLResponse)
    def photo_page(request: Request, photo_id: int):
        photo = get_photo_or_404(photo_id)
        meta = photo.metadata or {}
        siblings = [p.id for p in state.store.list_photos(photo.batch_id)] if photo.batch_id else []
        idx = siblings.index(photo_id) if photo_id in siblings else -1
        return render(
            request,
            "photo.html",
            photo=photo,
            meta=meta,
            keywords_text=", ".join(meta.get("keywords") or []),
            keyword_count=len(meta.get("keywords") or []),
            gr=photo.guardrail or {},
            cull=photo.cull or {},
            detected=photo.detected or {},
            location=meta.get("location") or {},
            events=state.store.events_for(photo_id),
            rejections=state.store.list_rejections(photo_id),
            prev_id=siblings[idx - 1] if idx > 0 else None,
            next_id=siblings[idx + 1] if 0 <= idx < len(siblings) - 1 else None,
            editable=photo.status in pipeline.PRE_UPLOAD_EDITABLE,
            can_approve=photo.status in (Status.DESCRIBED, Status.NEEDS_REVIEW),
            can_ready=photo.status != Status.READY
            and Status.READY in ALLOWED.get(photo.status, ()),
            can_result=photo.status in (Status.EXPORTED, Status.SUBMITTED),
            releases_text="\n".join(photo.release_ids),
        )

    @app.post("/photo/{photo_id}")
    def photo_save(photo_id: int, form: PostForm):
        get_photo_or_404(photo_id)
        path = f"/photo/{photo_id}"
        categories = [str(c) for c in form.getlist("categories")]
        if len(categories) > 2:
            return back(path, error="Choose at most 2 categories.")
        try:
            pipeline.update_metadata(
                cfg,
                state.store,
                photo_id,
                description=str(form.get("description") or "").strip(),
                title=str(form.get("title") or "").strip(),
                keywords=_split_lines(str(form.get("keywords") or "")),
                categories=categories,
                editorial=form.get("editorial") == "yes",
                mature=form.get("mature") == "yes",
                release_ids=_split_lines(str(form.get("release_ids") or "")),
            )
        except PipelineError as exc:
            return back(path, error=str(exc))
        return back(path, notice="Saved. Guardrails were re-checked.")

    @app.post("/photo/{photo_id}/approve")
    def photo_approve(photo_id: int, form: PostForm):
        get_photo_or_404(photo_id)
        path = f"/photo/{photo_id}"
        try:
            s = pipeline.approve(cfg, state.store, photo_ids=[photo_id], include_needs_review=True)
        except PipelineError as exc:
            return back(path, error=str(exc))
        if s.refused:
            return back(path, error=next(iter(s.refused.values())))
        return back(path, notice="Approved.")

    @app.post("/photo/{photo_id}/ready")
    def photo_ready(photo_id: int, form: PostForm):
        get_photo_or_404(photo_id)
        path = f"/photo/{photo_id}"
        try:
            pipeline.override_status(state.store, photo_id, Status.READY)
        except PipelineError as exc:
            return back(path, error=str(exc))
        return back(path, notice="Marked ready: it will be picked up by the next Describe.")

    @app.post("/photo/{photo_id}/regenerate", response_class=HTMLResponse)
    def photo_regenerate(request: Request, photo_id: int, form: PostForm):
        photo = get_photo_or_404(photo_id)
        if not photo.batch_id:
            return message_fragment(request, "This photo has no batch.")
        batch_id = photo.batch_id
        try:
            sel = pipeline.resolve_selection(cfg, state.store)
        except ModelChoiceRequired:
            return message_fragment(request, "Choose a local model on the Settings page first.")
        problems = pipeline.describe_preflight(cfg, sel)
        if problems:
            return message_fragment(request, " ".join(problems))

        def run(ctx: JobContext) -> str:
            s = pipeline.describe_batch(
                cfg, state.store, batch_id, only_ids=[photo_id], cancel=ctx.cancelled
            )
            if s.aborted or s.errors:
                raise PipelineError(s.aborted or s.errors[0][1])
            return "Described again. Reload the page to see the new text."

        return start_job(request, "describe", batch_id, run, total=1)

    @app.post("/photo/{photo_id}/result")
    def photo_result(photo_id: int, form: PostForm):
        get_photo_or_404(photo_id)
        path = f"/photo/{photo_id}"
        accepted = form.get("outcome") == "accept"
        try:
            pipeline.record_result(
                state.store,
                photo_id,
                accepted,
                reason_code=str(form.get("reason") or "") or None,
                note=str(form.get("note") or "").strip() or None,
            )
        except PipelineError as exc:
            return back(path, error=str(exc))
        return back(path, notice="Accepted." if accepted else "Rejection recorded.")

    # ------------------------------------------------------------------ settings

    @app.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request):
        report = pipeline.doctor(cfg, state.store, config_path=state.config_path)
        providers = []
        for prov in API_PROVIDERS:
            entry = pipeline.api_model(prov)
            env = API_KEY_ENV.get(prov)
            providers.append(
                {
                    "name": prov,
                    "model": entry.model if entry else None,
                    "usd_per_image": estimate.cost_per_image(entry) if entry else None,
                    "key_env": env,
                    "key_set": bool(shuto_secrets.get_secret(env)) if env else None,
                    "selected": report.selected is not None
                    and report.selected.startswith(prov + ":"),
                    "region": cfg.llm.bedrock.region,
                    "profile": cfg.llm.bedrock.profile,
                }
            )
        active = state.runner.active_job(MODELS_JOB_KEY)
        return render(
            request,
            "settings.html",
            report=report,
            hw=report.hardware,
            ollama=report.ollama,
            rows=[r.to_dict() for r in report.rows],
            providers=providers,
            guidance=_ollama_guidance(report.hardware.get("os", "")),
            job=state.runner.get(active) if active else None,
            job_active=bool(active),
        )

    def _ollama_guidance(os_name: str) -> str:
        step = {
            "windows": "Run the installer, then start Ollama from the Start menu.",
            "macos": "Download the app, open it, and keep it running.",
        }.get(os_name, "Use the install command on the page, then run `ollama serve`.")
        return f"Local models need Ollama (free). Download: {OLLAMA_URL}. {step}"

    @app.get("/settings/advice", response_class=HTMLResponse)
    def settings_advice(request: Request, tag: str):
        if not TAG_RE.match(tag):
            raise HTTPException(status_code=404, detail="Unknown model.")
        advice = pipeline.model_disk_advice(cfg, tag)
        if advice is None:
            raise HTTPException(status_code=404, detail="Unknown model.")
        return render(request, "_advice.html", advice=advice.to_dict(), tag=tag)

    @app.post("/settings/use")
    def settings_use(form: PostForm):
        spec = str(form.get("spec") or "").strip()
        if not TAG_RE.match(spec):
            return back("/settings", error="That model name is not valid.")
        prov = spec.partition(":")[0]
        if ":" in spec and prov in API_PROVIDERS:
            pass  # provider choice, validated by pipeline.use_model against the price table
        elif pipeline.model_disk_advice(cfg, spec) is None:
            return back("/settings", error=f"{spec} is not in the model catalog.")
        try:
            sel = pipeline.use_model(cfg, state.store, spec)
        except PipelineError as exc:
            return back("/settings", error=str(exc))
        return back("/settings", notice=f"Now using {sel.provider}: {sel.model}.")

    @app.post("/settings/pull", response_class=HTMLResponse)
    def settings_pull(request: Request, form: PostForm):
        tag = str(form.get("tag") or "")
        if form.get("confirm") != "yes" or not TAG_RE.match(tag):
            return message_fragment(request, "Download not confirmed.")
        advice = pipeline.model_disk_advice(cfg, tag)
        if advice is None:
            return message_fragment(request, f"{tag} is not in the model catalog.")
        if not advice.fits:
            return message_fragment(request, advice.message or "Not enough free disk space.")

        def run(ctx: JobContext) -> str:
            last: list[object] = [None]

            def on_progress(ev: dict) -> None:
                total, done = ev.get("total") or 0, ev.get("completed") or 0
                pct = int(done * 100 / total) if total else 0
                mark = (str(ev.get("status")), pct)
                if mark == last[0]:
                    return  # one database write per percent, not per Ollama event
                last[0] = mark
                ctx.progress(int(done / 1_000_000), int(total / 1_000_000), mark[0])

            pipeline.pull_model(cfg, tag, on_progress)
            return f"Downloaded {tag}. Reload the page, then press 'Use this model'."

        return start_job(request, "pull", MODELS_JOB_KEY, run)

    @app.post("/settings/benchmark", response_class=HTMLResponse)
    def settings_benchmark(request: Request, form: PostForm):
        tag = str(form.get("tag") or "")
        if not TAG_RE.match(tag) or pipeline.model_disk_advice(cfg, tag) is None:
            return message_fragment(request, "Unknown model.")

        def run(ctx: JobContext) -> str:
            r = pipeline.bench_model(cfg, state.store, tag)
            ok = "valid JSON" if r.get("ok_json") else "INVALID JSON output"
            return (
                f"Benchmark for {tag}: first image {r.get('cold_s')} s (includes loading), "
                f"then {r.get('warm_s')} s per image; {ok}. Reload to see the updated estimate."
            )

        return start_job(request, "benchmark", MODELS_JOB_KEY, run, total=2)

    return app


def serve(
    cfg: Config,
    *,
    host: str,
    port: int,
    open_browser: bool = True,
    config_path: str | None = None,
) -> None:
    """Run the web UI until interrupted. Bind to loopback unless the user asks otherwise."""
    import uvicorn

    app = create_app(
        cfg,
        config_path=config_path,
        allowed_hosts={host} if host not in ("0.0.0.0", "::") else set(),
    )
    url = f"http://{host if ':' not in host else '[' + host + ']'}:{port}"
    if host not in LOOPBACK_HOSTS:
        print(f"WARNING: listening on {host}. Anyone on your network can reach shuto.")
    print(f"shuto web UI: {url}  (press Ctrl+C to stop)")
    if open_browser:
        import webbrowser

        threading.Timer(1.0, webbrowser.open, args=(url,)).start()
    uvicorn.run(app, host=host, port=port, log_level="warning")
