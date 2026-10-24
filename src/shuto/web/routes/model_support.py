"""What the model pages share: the model in use, what can be removed, the download bar."""

from __future__ import annotations

from urllib.parse import urlparse

from fastapi import Request
from fastapi.responses import HTMLResponse

from shuto.ai import catalog as local_models
from shuto.ai import estimate, hardware, selection
from shuto.ai import model_choice as ai_choice
from shuto.ai.providers import API_KEY_SECRETS, API_PROVIDERS
from shuto.core import secrets as shuto_secrets
from shuto.core.jobs import JobBusy, JobContext
from shuto.ollama import env as ollama_env
from shuto.ollama import models_dir as ollama_store
from shuto.ollama import move as ollama_move
from shuto.web.routes.actions import TAG_RE
from shuto.web.state import MODELS_JOB_KEY, WebCtx

AI_JOB_KINDS = {"check", "describe", "redo"}  # jobs that use the AI model
# Pages that reload by themselves when a model download ends well, so the chosen model shows at
# once. Pages with text fields (other wizard steps, Settings, Add photos) are left alone: a
# reload would throw away what the user is typing. They get the final bar message instead.
REFRESH_ON_DOWNLOAD_DONE = {"/": None, "/welcome": "step=3"}  # path -> required query, if any
BAR_KINDS = ("pull", "models_dir")  # jobs shown as the download bar at the top of every page


def _plain_seconds(seconds: float) -> str:
    return estimate.format_duration(seconds)


def _tags(rows: list[dict]) -> None:
    """Add the plain-words tags ("Best match", "Faster", "Higher quality") to model cards."""
    if not rows:
        return
    fastest = min(rows, key=lambda r: r["est_seconds_per_image"])
    best = max(rows, key=lambda r: r["quality"])
    for r in rows:
        tags = []
        if r["recommended"]:
            tags.append("Best match")
        if r is fastest and len(rows) > 1:
            tags.append("Faster")
        if r is best and len(rows) > 1:
            tags.append("Higher quality")
        r["tags"] = tags
        r["time_text"] = f"about {_plain_seconds(r['est_seconds_per_image'])} per photo"


def _mark_thinkers(ctx: WebCtx, rows: list[dict]) -> None:
    """Set `thinks` on the downloaded models Ollama says think before they answer (slower)."""
    client = selection.ollama_client(ctx.cfg)
    for r in rows:
        try:
            r["thinks"] = bool(r.get("pulled") and client.supports_thinking(r["tag"]))
        except Exception:
            r["thinks"] = False  # a hint is never worth breaking the page for


def helper_steps(os_name: str) -> list[str]:
    if os_name == "windows":
        return [
            "Download the Windows installer from the Ollama website.",
            "Open the downloaded file and follow the steps.",
            "Open Ollama from the Start menu and leave it running.",
        ]
    if os_name == "macos":
        return [
            "Download the Mac app from the Ollama website.",
            "Open the downloaded file and drag Ollama into Applications.",
            "Open Ollama from Applications and leave it running.",
        ]
    return [
        "Download Ollama from the Ollama website and follow the Linux steps there.",
        "The Linux installer needs an administrator password. Ask someone to help, "
        "or use an online AI instead.",
        "Start Ollama and leave it running.",
    ]


def pull_info(ctx: WebCtx, job_id: int | None) -> dict | None:
    cur = ctx.state.pull_state
    return cur[1] if cur is not None and cur[0] == job_id else None


def pulling_tag(ctx: WebCtx) -> str | None:
    """Tag of the model being downloaded right now, if any."""
    active = ctx.runner.active_job(MODELS_JOB_KEY)
    job = ctx.runner.get(active) if active else None
    info = pull_info(ctx, job.id) if job is not None and job.kind == "pull" else None
    return str(info["tag"]) if info is not None else None


def current_model(ctx: WebCtx) -> str | None:
    try:
        sel = selection.resolve_selection(ctx.cfg, ctx.store)
    except Exception:
        return None
    return f"{sel.model}" if sel.provider == "ollama" else f"{sel.provider} / {sel.model}"


def selected_tag(ctx: WebCtx) -> str | None:
    """The downloaded model that is in use now (None when an online AI or nothing).

    Raises RuntimeError when the choice cannot be read: callers must then not remove.
    """
    try:
        sel = selection.resolve_selection(ctx.cfg, ctx.store)
    except ai_choice.ModelChoiceRequired:
        return None  # nothing chosen yet, so nothing is in use
    except Exception:
        raise RuntimeError("The model in use could not be read.") from None
    return hardware.normalize_tag(sel.model) if sel.provider == "ollama" else None


def writing_job_active(ctx: WebCtx) -> bool:
    """True while a check or a titles-and-keywords job is queued or running (it uses the AI
    model, so the model must not be removed under it)."""
    return bool(ctx.runner.active_kinds() & AI_JOB_KINDS)


def removable_models(ctx: WebCtx, include_in_use: bool = False) -> list[dict]:
    """Downloaded models that may be removed: only models from the model list (they can be
    downloaded again; a model made by hand cannot). The one in use is left out unless
    `include_in_use`. size_gb is
    what Ollama would really free (layers shared with other models stay)."""
    try:
        rows = selection.ollama_client(ctx.cfg).tags()
        keep = selected_tag(ctx)
    except Exception:
        return []
    listed = {hardware.normalize_tag(m.tag) for m in local_models.load_catalog()[0]}
    root = ollama_store.current_models_dir()
    out = []
    for m in rows:
        name = str(m.get("name") or m.get("model") or "")
        norm = hardware.normalize_tag(name)
        if (
            not name
            or not TAG_RE.match(name)
            or (norm == keep and not include_in_use)
            or norm not in listed
        ):
            continue
        freed = ollama_move.freeable_bytes(root, name)
        out.append({"tag": name, "size_gb": (freed or 0) / 1024**3, "known": freed is not None})
    return out


def model_is_downloaded(ctx: WebCtx, tag: str) -> bool:
    try:
        rows = selection.ollama_client(ctx.cfg).tags()
    except Exception:
        return False
    have = {hardware.normalize_tag(str(m.get("name") or m.get("model") or "")) for m in rows}
    return hardware.normalize_tag(tag) in have


def model_context(ctx: WebCtx, report) -> dict:
    rows = [r.to_dict() for r in report.rows]
    _tags(rows)
    _mark_thinkers(ctx, rows)
    providers = []
    for prov in API_PROVIDERS:
        entry = selection.api_model(prov)
        env = API_KEY_SECRETS.get(prov)
        providers.append(
            {
                "name": prov,
                "model": entry.model if entry else None,
                "usd_per_image": estimate.cost_per_image(entry) if entry else None,
                "key_env": env,
                "key_set": bool(shuto_secrets.get_secret(env)) if env else None,
                "selected": report.selected is not None and report.selected.startswith(prov + ":"),
                "region": ctx.cfg.llm.bedrock.region,
                "profile": ctx.cfg.llm.bedrock.profile,
            }
        )
    list_updated, list_old = local_models.catalog_staleness()
    small_warning = None
    if report.selected and report.selected.startswith("ollama:"):
        small_warning = local_models.small_model_warning(report.selected[7:], report.rows)
    return {
        "small_warning": small_warning,
        "list_updated": list_updated,
        "list_old": list_old,
        "cards": rows,
        "providers": providers,
        "selected": report.selected,
        "chosen": report.selected is not None,
        "conflict": ctx.ollama_conflict(),
        "pulling": pulling_tag(ctx),
    }


def storage_context(ctx: WebCtx) -> dict:
    """Where Ollama keeps models now, free space, and drives that could hold them."""
    cur = ollama_store.current_models_dir()
    size = ollama_move.models_size_bytes(cur)
    gb = size / 1024**3
    try:
        drives = hardware.other_volumes(max(10.0, gb + 2), cur)
    except Exception:
        drives = []
    machine = ollama_store.machine_wide_value()
    warning = None
    if ollama_store.supported() and not machine:
        try:
            warning = ollama_store.login_order_warning(selection.ollama_client(ctx.cfg))
        except Exception:
            warning = None
    return {
        "supported": ollama_store.supported(),
        "machine": machine,
        "machine_any": bool(ollama_store.machine_any_value()),
        "machine_text": ollama_env.MACHINE_TEXT,
        "machine_reset_text": ollama_env.MACHINE_RESET_TEXT,
        "mac_warning": warning,
        "current": str(cur),
        "free_gb": hardware.free_gb(cur),
        "custom": ollama_store.is_custom(),
        "has_models": size > 0,
        "models_gb": round(gb, 1),
        "drives": [v.to_dict() for v in drives],
        "linux_text": ollama_env.LINUX_TEXT,
    }


def bar_response(ctx: WebCtx, request: Request, job_id: int, started: bool = False) -> HTMLResponse:
    found = ctx.runner.get(job_id)
    if found is None:
        return HTMLResponse("")
    active = found.state in ("queued", "running")
    result = ctx.runner.result(found.id)
    info = pull_info(ctx, found.id) or {"tag": "", "status": ""}
    try:
        in_use = bool(info["tag"]) and hardware.normalize_tag(info["tag"]) == selected_tag(ctx)
    except RuntimeError:
        in_use = False
    resp = ctx.render(
        request,
        "_download_bar.html",
        job=found,
        active=active,
        result=result if isinstance(result, str) else None,
        tag=info["tag"],
        status=info["status"],
        href=ctx.state.pull_return,
        in_use=in_use,
    )
    if started or not active:
        # a download started or ended (not every 2 s poll): pages showing its state refresh
        resp.headers["HX-Trigger"] = "shuto:models-changed"
    if found.state == "done" and found.kind == "pull" and found.id not in ctx.state.announced_done:
        here = urlparse(request.headers.get("hx-current-url", ""))
        need = REFRESH_ON_DOWNLOAD_DONE.get(here.path, "")
        if here.path in REFRESH_ON_DOWNLOAD_DONE and (
            need is None or need in here.query.split("&")
        ):
            # once per download, and only used up by a page that really reloads
            ctx.state.announced_done.add(found.id)
            resp.headers["HX-Refresh"] = "true"
    return resp


def submit_models_job(
    ctx: WebCtx,
    request: Request,
    kind: str,
    run,
    back: str,
    info: dict | None = None,
    pending: str | None = None,
):
    """Start a models job; the page to return to is only remembered once it really started.
    `pending` is the model to choose when a download ends (set before the job can finish)."""

    def run_and_forget(job: JobContext) -> str:
        try:
            return run(job)
        finally:
            ctx.invalidate_model_status()  # models or their folder just changed

    state = ctx.state
    before = state.pending_model
    ctx.invalidate_model_status()
    if pending is not None:
        state.pending_model = pending  # before submit: a quick job may finish at once
    try:
        job_id = ctx.runner.submit(kind, MODELS_JOB_KEY, run_and_forget)
    except JobBusy as exc:
        state.pending_model = before  # the job already running keeps its own pending model
        return ctx.message_fragment(request, str(exc))
    state.pull_return = back
    state.pull_state = (job_id, info if info is not None else {"tag": "", "status": ""})
    if kind in BAR_KINDS:
        # always the one slot at the top of the page, whichever page started the job
        resp = bar_response(ctx, request, job_id, started=True)
        resp.headers["HX-Retarget"] = "#dl-slot"
        resp.headers["HX-Reswap"] = "innerHTML"
        return resp
    return ctx.job_fragment(request, job_id, next_href=back)
