"""Setup-side routes: home, welcome wizard, add photos, jobs, folder browser, settings, help."""

from __future__ import annotations

import re
import threading
from importlib import metadata
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from starlette.datastructures import FormData

from .. import estimate, hardware, local_models, ollama_store, pipeline, updates
from .. import filters as filters_mod
from .. import secrets as shuto_secrets
from ..jobs import JobBusy, JobCancelled, JobContext
from . import browse, ux
from .common import IMPORT_JOB_KEY, MODELS_JOB_KEY, PostForm, WebCtx

API_PROVIDERS = ("anthropic", "gemini", "bedrock")
API_KEY_ENV = {
    "anthropic": "ANTHROPIC_API_KEY",
    "gemini": "GEMINI_API_KEY",
}
TAG_RE = re.compile(r"^[A-Za-z0-9._:/-]{1,100}$")
ERROR_PREFIX_RE = re.compile(r"^[A-Za-z_.]*(?:Error|Exception|Busy|Refused): ")
OLLAMA_URL = "https://ollama.com/download"
FTPS_JOB_KEY = "ftps"
LOCAL_PATH_RE = re.compile(r"^/(?!/)[A-Za-z0-9._~%/-]*$")
RETURN_PAGES = {"/settings", "/welcome?step=3"}
MAX_KEY_LEN = 500
MIN_KEY_LEN = 8
WIZARD_STEPS = (
    ("welcome", "Welcome"),
    ("helper", "AI helper"),
    ("model", "AI model"),
    ("login", "Upload login"),
    ("style", "Your style"),
    ("done", "All set"),
)
MAX_BATCH_NAME = 80
MAX_RULES = filters_mod.MAX_RULES
MAX_RULE_ROWS = 40  # form rows read at most; the real limit is MAX_RULES


def _version() -> str:
    try:
        return metadata.version("shuto")
    except metadata.PackageNotFoundError:
        return "dev"


def _helper_steps(os_name: str) -> list[str]:
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


UPLOAD_JOB_KINDS = ("deliver", "ftps_test", "upload")
AI_JOB_KINDS = {"check", "describe"}  # jobs that use the AI model
# Pages that reload by themselves when a model download ends well, so the chosen model shows at
# once. Pages with text fields (other wizard steps, Settings, Add photos) are left alone: a
# reload would throw away what the user is typing. They get the final bar message instead.
REFRESH_ON_DOWNLOAD_DONE = {"/": None, "/welcome": "step=3"}  # path -> required query, if any
BAR_KINDS = ("pull", "models_dir")  # jobs shown as the download bar at the top of every page
CANCEL_TOO_LATE = "The download finished before it could be stopped. {tag} is ready to use."
CANCEL_STOPPED = (
    "Download stopped. {tag} is not ready to use. Partly downloaded files stay, "
    "so a later download can continue where this one stopped."
)
PINNED_TEXT = " Your settings file still names {tag}. Choose another model in Settings."
WRITING_BUSY = "Wait for shuto to finish writing titles and keywords, then try again."


def _friendly(error: str | None, kind: str = "") -> dict[str, str]:
    """Plain-words error; upload-type jobs get upload advice instead of AI advice."""
    context = "upload" if kind in UPLOAD_JOB_KINDS else "ai"
    return ux.friendly_error(ERROR_PREFIX_RE.sub("", error or ""), context=context)


def _n(counts: dict[str, int], *names: str) -> int:
    return sum(int(counts.get(x, 0)) for x in names)


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


def register_setup_routes(app: FastAPI, ctx: WebCtx) -> None:
    state = ctx.state
    state.base_cfg = state.cfg
    state.cfg = pipeline.apply_style_overrides(state.base_cfg, ctx.store)
    state.templates.env.globals["friendly"] = _friendly
    state.templates.env.globals["app_version"] = _version()
    import_results: dict[int, list[tuple[str, int]]] = {}
    pull_return: list[str] = ["/settings"]
    # (job id, {"tag", "status"}) of the latest models job: one tuple, swapped in one step
    pull_state: list[tuple[int, dict] | None] = [None]
    announced_done: set[int] = set()  # finished downloads whose end was already shown once

    def pull_info(job_id: int | None) -> dict | None:
        cur = pull_state[0]
        return cur[1] if cur is not None and cur[0] == job_id else None

    def pulling_tag() -> str | None:
        """Tag of the model being downloaded right now, if any."""
        active = ctx.runner.active_job(MODELS_JOB_KEY)
        job = ctx.runner.get(active) if active else None
        info = pull_info(job.id) if job is not None and job.kind == "pull" else None
        return str(info["tag"]) if info is not None else None

    def job_extras(job_id: int) -> dict:
        job = ctx.runner.get(job_id)
        extra: dict = {}
        if job is not None and job.kind == "import":
            extra["batches"] = import_results.get(job_id, [])
        if job is not None and job.kind in ("pull", "models_dir"):
            extra["next_href"] = pull_return[0]
        return extra

    # ------------------------------------------------------------------ health + home

    @app.get("/healthz", response_class=PlainTextResponse)
    def healthz():
        return f"shuto {_version()}"

    def current_model() -> str | None:
        try:
            sel = pipeline.resolve_selection(ctx.cfg, ctx.store)
        except Exception:
            return None
        return f"{sel.model}" if sel.provider == "ollama" else f"{sel.provider} / {sel.model}"

    def selected_tag() -> str | None:
        """The downloaded model that is in use now (None when an online AI or nothing).

        Raises RuntimeError when the choice cannot be read: callers must then not remove.
        """
        try:
            sel = pipeline.resolve_selection(ctx.cfg, ctx.store)
        except local_models.ModelChoiceRequired:
            return None  # nothing chosen yet, so nothing is in use
        except Exception:
            raise RuntimeError("The model in use could not be read.") from None
        return hardware.normalize_tag(sel.model) if sel.provider == "ollama" else None

    def writing_job_active() -> bool:
        """True while a check or a titles-and-keywords job is queued or running (it uses the AI
        model, so the model must not be removed under it)."""
        return bool(ctx.runner.active_kinds() & AI_JOB_KINDS)

    def removable_models(include_in_use: bool = False) -> list[dict]:
        """Downloaded models that may be removed: only models from the model list (they can be
        downloaded again; a model made by hand cannot). The one in use is left out unless
        `include_in_use`. size_gb is
        what Ollama would really free (layers shared with other models stay)."""
        try:
            rows = pipeline.ollama_client(ctx.cfg).tags()
            keep = selected_tag()
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
            freed = ollama_store.freeable_bytes(root, name)
            out.append({"tag": name, "size_gb": (freed or 0) / 1024**3, "known": freed is not None})
        return out

    def batch_cards() -> list[dict]:
        cards = []
        for b in pipeline.batch_report(ctx.store):
            counts = b["counts"]

            stage = ux.stage(counts)
            estimate_text = None
            if stage == "check":
                try:
                    estimate_text = pipeline.estimate_check(ctx.cfg, ctx.store, b["id"]).text
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

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        cards = batch_cards()
        if not cards and not pipeline.get_ui_setting(ctx.store, "onboarded"):
            return RedirectResponse("/welcome", status_code=303)
        return ctx.render(request, "index.html", cards=cards, model=current_model())

    # ------------------------------------------------------------------ shared actions

    def apply_model(spec: str) -> tuple[str | None, str | None]:
        """Returns (notice, error) after trying to use a model or an online provider."""
        cfg = ctx.cfg
        spec = spec.strip()
        if not TAG_RE.match(spec):
            return None, "That model name is not valid."
        prov = spec.partition(":")[0]
        if ":" in spec and prov in API_PROVIDERS:
            pass  # provider choice, validated by pipeline.use_model against the price table
        elif pipeline.model_disk_advice(cfg, spec) is None:
            return None, f"{spec} is not in the model catalog."
        try:
            sel = pipeline.use_model(cfg, ctx.store, spec)
        except pipeline.PipelineError as exc:
            return None, str(exc)
        return f"Now using {sel.provider}: {sel.model}.", None

    def save_login(form: FormData) -> tuple[str | None, str | None]:
        user = str(form.get("username") or "").strip()
        password = str(form.get("password") or "")
        saved = pipeline.ftps_login_status(ctx.cfg)
        if not user and not password:
            return None, None
        if not user:
            return None, "Enter the email or username you use on Shutterstock."
        if not password and not saved["password_set"]:
            return None, "Enter your Shutterstock password."
        try:
            shuto_secrets.set_secret("SHUTO_FTPS_USER", user)
            if password:
                shuto_secrets.set_secret("SHUTO_FTPS_PASSWORD", password)
        except RuntimeError as exc:
            return None, str(exc)
        return "Login saved on this computer.", None

    def save_style(form: FormData) -> tuple[str | None, str | None]:
        current = dict(pipeline.get_ui_setting(ctx.store, "style") or {})
        current["theme"] = str(form.get("theme") or "")
        current["tone"] = str(form.get("tone") or "")
        current["always_include"] = str(form.get("always_include") or "")
        if "region_focus" in form:
            current["region_focus"] = str(form.get("region_focus") or "")
        try:
            pipeline.set_ui_setting(ctx.store, "style", current)
        except pipeline.PipelineError as exc:
            return None, str(exc)
        state.cfg = pipeline.apply_style_overrides(state.base_cfg, ctx.store)
        return "Your style is saved.", None

    def save_rules(form: FormData) -> tuple[str | None, str | None]:
        """Save the theme check and the rule rows of the "Your photo rules" form."""
        rows = []
        for n in range(MAX_RULE_ROWS):
            text = str(form.get(f"rule_text_{n}") or "").strip()
            if not text or form.get(f"rule_remove_{n}"):
                continue
            rows.append(
                {
                    "id": str(form.get(f"rule_id_{n}") or ""),
                    "kind": str(form.get(f"rule_kind_{n}") or ""),
                    "text": text,
                    "mode": str(form.get(f"rule_mode_{n}") or ""),
                }
            )
        try:
            pipeline.set_ui_setting(
                ctx.store,
                "filters",
                {
                    "theme_check": form.get("theme_check") == "yes",
                    "theme_partly": str(form.get("theme_partly") or "pass"),
                    "rules": rows,
                },
            )
        except pipeline.PipelineError as exc:
            return None, str(exc)
        state.cfg = pipeline.apply_style_overrides(state.base_cfg, ctx.store)
        return "Your photo rules are saved. They apply to photos you check from now on.", None

    def _key_return(form: FormData) -> str:
        back = str(form.get("return_to") or "")
        if back == "/welcome?step=3":
            return "/welcome?step=3&online=1"
        return "/settings"

    def go(path: str, notice: str | None, error: str | None, request: Request | None = None):
        """Redirect with a message. For htmx posts (the confirm dialog ones) use HX-Redirect."""
        resp = ctx.back(path, notice=notice, error=error)
        if request is not None and request.headers.get("hx-request"):
            return Response(status_code=204, headers={"HX-Redirect": resp.headers["location"]})
        return resp

    # ------------------------------------------------------------------ welcome wizard

    def model_context(report) -> dict:
        rows = [r.to_dict() for r in report.rows]
        _tags(rows)
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
                    "region": ctx.cfg.llm.bedrock.region,
                    "profile": ctx.cfg.llm.bedrock.profile,
                }
            )
        list_updated, list_old = local_models.catalog_staleness()
        return {
            "list_updated": list_updated,
            "list_old": list_old,
            "cards": rows,
            "providers": providers,
            "selected": report.selected,
            "chosen": report.selected is not None,
            "pulling": pulling_tag(),
        }

    def storage_context() -> dict:
        """Where Ollama keeps models now, free space, and drives that could hold them."""
        cur = ollama_store.current_models_dir()
        size = ollama_store.models_size_bytes(cur)
        gb = size / 1024**3
        try:
            drives = hardware.other_volumes(max(10.0, gb + 2), cur)
        except Exception:
            drives = []
        machine = ollama_store.machine_wide_value()
        warning = None
        if ollama_store.supported() and not machine:
            try:
                warning = ollama_store.login_order_warning(pipeline.ollama_client(ctx.cfg))
            except Exception:
                warning = None
        return {
            "supported": ollama_store.supported(),
            "machine": machine,
            "machine_any": bool(ollama_store.machine_any_value()),
            "machine_text": ollama_store.MACHINE_TEXT,
            "machine_reset_text": ollama_store.MACHINE_RESET_TEXT,
            "mac_warning": warning,
            "current": str(cur),
            "free_gb": hardware.free_gb(cur),
            "custom": ollama_store.is_custom(),
            "has_models": size > 0,
            "models_gb": round(gb, 1),
            "drives": [v.to_dict() for v in drives],
            "linux_text": ollama_store.LINUX_TEXT,
        }

    def style_values() -> dict:
        cfg = ctx.cfg
        return {
            "theme": cfg.style.theme,
            "tone": cfg.style.tone,
            "always_include": ", ".join(cfg.style.keywords.always_include),
            "region_focus": cfg.style.region_focus,
        }

    def rules_values() -> dict:
        """Rule rows for the form: the saved ones plus two empty rows (up to the limit)."""
        cfg = ctx.cfg
        try:
            saved = [
                {"id": r.id, "kind": r.kind, "text": r.text, "mode": r.mode}
                for r in pipeline.get_filter_rules(cfg)
            ]
        except pipeline.PipelineError:
            saved = []
        blank = {"id": "", "kind": "avoid", "text": "", "mode": "set_aside"}
        room = max(0, MAX_RULES - len(saved))
        return {
            "theme_check": cfg.filters.theme_check,
            "theme_partly": cfg.filters.theme_partly,
            "theme": cfg.style.theme,
            "rule_rows": saved + [dict(blank) for _ in range(min(2, room))],
            "rule_count": len(saved),
            "max_rules": MAX_RULES,
        }

    @app.get("/welcome", response_class=HTMLResponse)
    def welcome(request: Request, step: int = 1):
        step = min(max(step, 1), len(WIZARD_STEPS))
        extra: dict = {}
        if step in (2, 3, 6):
            report = pipeline.doctor(ctx.cfg, ctx.store, config_path=state.config_path)
            extra.update(report=report, ollama=report.ollama, os_name=report.hardware.get("os"))
            extra["helper_steps"] = _helper_steps(report.hardware.get("os", ""))
            if step in (3, 6):
                extra.update(model_context(report))
            if step == 3:
                extra["model_status"] = ctx.model_status(request)
                extra["later"] = bool(request.query_params.get("later"))
                extra["ms"] = storage_context()
                active = ctx.runner.active_job(MODELS_JOB_KEY)
                extra.update(
                    job=ctx.runner.get(active) if active else None, job_active=bool(active)
                )
        if step in (4, 6):
            extra["login"] = pipeline.ftps_login_status(ctx.cfg)
        if step == 5:
            extra["style"] = style_values()
        if step == 6:
            extra["model_name"] = current_model()
        return ctx.render(
            request,
            "welcome.html",
            step=step,
            wizard=WIZARD_STEPS,
            ollama_url=OLLAMA_URL,
            **extra,
        )

    @app.get("/welcome/nav", response_class=HTMLResponse)
    def welcome_nav(request: Request):
        """The buttons under the model step, from the current model state (read-only)."""
        return ctx.render(request, "_wizard_model_nav.html", model_status=ctx.fresh_model_status())

    @app.post("/welcome/{step}")
    def welcome_post(step: int, form: PostForm):
        if not 1 <= step <= len(WIZARD_STEPS):
            raise HTTPException(status_code=404, detail="Unknown step.")
        here, nxt = f"/welcome?step={step}", f"/welcome?step={step + 1}"
        notice = error = None
        if step == 3:
            spec = str(form.get("spec") or "")
            if spec:
                notice, error = apply_model(spec)
            elif form.get("later") == "yes":
                return go(nxt, "You can choose the AI model later in Settings.", None)
            else:
                return go(here, None, "Choose an AI model first, or pick Set up later.")
        elif step == 4:
            notice, error = save_login(form)
        elif step == 5:
            notice, error = save_style(form)
        elif step == 6:
            pipeline.set_ui_setting(ctx.store, "onboarded", True)
            target = "/add" if form.get("then") == "add" else "/"
            return go(target, None, None)
        return go(here if error else nxt, notice, error)

    # ------------------------------------------------------------------ add photos + import

    @app.get("/add", response_class=HTMLResponse)
    def add_page(request: Request):
        last = str(pipeline.get_ui_setting(ctx.store, "last_folder") or "")
        return ctx.render(
            request,
            "add.html",
            last_folder=last,
            default_name=Path(last).expanduser().name[:MAX_BATCH_NAME] if last else "",
        )

    @app.post("/import", response_class=HTMLResponse)
    def start_import(request: Request, form: PostForm):
        cfg = ctx.cfg
        raw = str(form.get("path") or "").strip()
        if not raw:
            return ctx.message_fragment(request, "Enter the folder that holds your photos.")
        folder = Path(raw).expanduser()
        if not folder.is_dir():
            return ctx.message_fragment(request, f'"{raw}" is not a folder on this computer.')
        notes = str(form.get("notes") or "").strip() or None
        batch_name = (str(form.get("batch_name") or "").strip() or folder.name)[:MAX_BATCH_NAME]
        recursive = form.get("recursive") == "yes"
        force = form.get("force") == "yes"
        mark_uploaded = form.get("mark_uploaded") == "yes"
        if force and mark_uploaded:
            return ctx.message_fragment(
                request, "Choose either Add anyway or Just remember them, not both."
            )
        pipeline.set_ui_setting(ctx.store, "last_folder", str(folder))

        def run(job: JobContext) -> str:
            s = pipeline.import_folder(
                cfg,
                ctx.store,
                folder,
                recursive=recursive,
                notes=notes,
                progress=job.progress,
                force=force,
                mark_uploaded=mark_uploaded,
                batch_name=batch_name or None,
            )
            import_results[job.job_id] = list(s.batches.items())
            parts = [f"{s.imported} photo(s) added."]
            copies = s.duplicates + s.already_known
            if copies:
                parts.append(f"{copies} were already in shuto as exact copies.")
            if s.near_duplicates:
                parts.append(f"{s.near_duplicates} look like another photo in this folder.")
            if s.errors:
                parts.append(f"{len(s.errors)} could not be read.")
            if s.marked_uploaded:
                parts.append(f"{s.marked_uploaded} remembered as already sent.")
            if s.already_uploaded:
                shown = "; ".join(a.text() for a in s.already_uploaded[:10])
                more = len(s.already_uploaded) - 10
                parts.append(
                    f"{len(s.already_uploaded)} look like photos you sent before and were "
                    f"left out: {shown}" + (f"; and {more} more" if more > 0 else "") + ". "
                    'To add them anyway, tick "Add even photos I sent before" under Advanced.'
                )
            return " ".join(parts)

        return ctx.start_job(request, "import", IMPORT_JOB_KEY, run)

    # ------------------------------------------------------------------ jobs

    @app.get("/jobs/{job_id}", response_class=HTMLResponse)
    def job_status(request: Request, job_id: int):
        return ctx.job_fragment(request, job_id, **job_extras(job_id))

    @app.get("/model/banner", response_class=HTMLResponse)
    def model_banner(request: Request):
        """App-wide 'No AI model set' note. Quiet while the download bar shows, in the setup
        wizard, and when the local AI simply can't be reached (read-only)."""
        if urlparse(request.headers.get("hx-current-url", "")).path == "/welcome":
            return HTMLResponse("")
        if ctx.runner.active_job(MODELS_JOB_KEY):
            return HTMLResponse("")
        if ctx.model_status(request).state != "missing":
            return HTMLResponse("")
        return ctx.render(request, "_model_banner.html")

    def bar_response(request: Request, job_id: int, started: bool = False) -> HTMLResponse:
        found = ctx.runner.get(job_id)
        if found is None:
            return HTMLResponse("")
        active = found.state in ("queued", "running")
        result = ctx.runner.result(found.id)
        info = pull_info(found.id) or {"tag": "", "status": ""}
        try:
            in_use = bool(info["tag"]) and hardware.normalize_tag(info["tag"]) == selected_tag()
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
            href=pull_return[0],
            in_use=in_use,
        )
        if started or not active:
            # a download started or ended (not every 2 s poll): pages showing its state refresh
            resp.headers["HX-Trigger"] = "shuto:models-changed"
        if found.state == "done" and found.kind == "pull" and found.id not in announced_done:
            here = urlparse(request.headers.get("hx-current-url", ""))
            need = REFRESH_ON_DOWNLOAD_DONE.get(here.path, "")
            if here.path in REFRESH_ON_DOWNLOAD_DONE and (
                need is None or need in here.query.split("&")
            ):
                # once per download, and only used up by a page that really reloads
                announced_done.add(found.id)
                resp.headers["HX-Refresh"] = "true"
        return resp

    @app.get("/downloads/bar", response_class=HTMLResponse)
    def download_bar(request: Request, job: int = 0):
        """Small progress bar for the running model download, shown on every page (read-only)."""
        job_id = ctx.runner.active_job(MODELS_JOB_KEY) or job
        found = ctx.runner.get(job_id) if job_id else None
        if found is None or found.kind not in BAR_KINDS:
            return HTMLResponse("")
        if found.state not in ("queued", "running") and job != found.id:
            return HTMLResponse("")  # finished earlier: only the page that saw it run says so
        return bar_response(request, found.id)

    @app.post("/jobs/{job_id}/cancel", response_class=HTMLResponse)
    def job_cancel(request: Request, job_id: int, form: PostForm):
        if ctx.runner.get(job_id) is None:
            raise HTTPException(status_code=404, detail="Unknown job.")
        ctx.runner.cancel(job_id)
        if ctx.runner.get(job_id).kind in BAR_KINDS:
            return bar_response(request, job_id)
        return ctx.job_fragment(request, job_id, **job_extras(job_id))

    # ------------------------------------------------------------------ folder browser

    @app.get("/browse", response_class=HTMLResponse)
    def browse_folder(request: Request, path: str = ""):
        try:
            listing = browse.list_dir(path, ctx.cfg.import_.extensions)
            problem = None
        except browse.BrowseError as exc:
            listing, problem = None, str(exc)
        return ctx.render(
            request,
            "_browse.html",
            listing=listing,
            problem=problem,
            roots=browse.roots(),
        )

    # ------------------------------------------------------------------ updates

    def update_panel(request: Request, *, force: bool = False, problem: str | None = None):
        check_on = bool(pipeline.get_ui_setting(ctx.store, "update_check", True))
        if check_on or force:
            latest = updates.cached_latest(ctx.store, force=force)
        else:
            latest = updates.peek_cached(ctx.store)
        current = updates.current_version()
        newer = latest is not None and updates.is_newer(latest.version, current)
        elig = updates.eligibility(
            ctx.runner, client_host=request.client.host if request.client else ""
        )
        status = updates.read_status(ctx.cfg)
        if status and status.get("state") in ("started", "waiting", "installing"):
            status = None
        if status and status.get("state") == "done" and status.get("to") != current:
            status = None
        return ctx.render(
            request,
            "_updates.html",
            current=current,
            latest=latest,
            newer=newer,
            elig=elig,
            check_on=check_on,
            checked=force,
            check_failed=updates.last_check_failed(ctx.store),
            status=status,
            log_file=str(updates.log_path(ctx.cfg)),
            problem=problem,
        )

    @app.get("/updates/panel", response_class=HTMLResponse)
    def updates_panel(request: Request):
        return update_panel(request)

    @app.get("/updates/banner", response_class=HTMLResponse)
    def updates_banner(request: Request):
        status = updates.read_status(ctx.cfg)
        if (
            status
            and not status.get("seen")
            and status.get("state")
            in (
                "done",
                "failed",
                "rolled_back",
            )
        ):
            updates.mark_status_seen(ctx.cfg)
            return ctx.render(request, "_update_banner.html", status=status, latest=None)
        if not pipeline.get_ui_setting(ctx.store, "update_check", True):
            return Response("")
        latest = updates.cached_latest(ctx.store)
        if latest is None or not updates.is_newer(latest.version, updates.current_version()):
            return Response("")
        return ctx.render(request, "_update_banner.html", status=None, latest=latest)

    @app.post("/updates/check", response_class=HTMLResponse)
    def updates_check(request: Request, form: PostForm):
        return update_panel(request, force=True)

    @app.post("/updates/settings")
    def updates_settings(form: PostForm):
        on = str(form.get("update_check") or "") == "on"
        pipeline.set_ui_setting(ctx.store, "update_check", on)
        notice = (
            "shuto will check for new versions once a day." if on else "Automatic checks are off."
        )
        return go("/settings#updates", notice, None)

    @app.post("/updates/start", response_class=HTMLResponse)
    def updates_start(request: Request, form: PostForm):
        uv = updates.find_uv()
        host = request.client.host if request.client else ""
        elig = updates.eligibility(ctx.runner, client_host=host, uv=uv)
        if not elig.ok:
            return update_panel(request, problem=elig.reason)
        latest = updates.cached_latest(ctx.store)
        if (
            latest is None
            or not updates.is_newer(latest.version, updates.current_version())
            or str(form.get("version") or "") != latest.version
        ):
            return update_panel(
                request, problem="That version is no longer the newest. Check again, then update."
            )
        if not ctx.runner.close():
            return update_panel(
                request, problem="Wait until the current task finishes, then update."
            )
        started = False
        problem = None
        try:
            updates.start_update(
                ctx.cfg,
                latest,
                uv=uv or "",
                ui_args=getattr(app.state, "launch_args", None),
            )
            started = updates.wait_for_helper(ctx.cfg)
            if not started:
                problem = (
                    "The updater did not start, so nothing was changed. shuto is still "
                    "running. Try again, or run the install line again."
                )
        except updates.UpdateError as exc:
            problem = str(exc)
        except (OSError, ValueError):
            problem = "shuto could not start the update, so nothing was changed. Try again."
        finally:
            if not started:
                ctx.runner.reopen()
        if not started:
            return update_panel(request, problem=problem)
        from .app import request_shutdown

        timer = threading.Timer(1.0, request_shutdown, args=(app,))
        timer.daemon = True
        timer.start()
        return ctx.render(request, "_updating.html", version=latest.version)

    # ------------------------------------------------------------------ settings

    @app.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request):
        report = pipeline.doctor(ctx.cfg, ctx.store, config_path=state.config_path)
        usage = {"calls": 0, "input_tokens": 0, "output_tokens": 0, "seconds": 0.0}
        for b in pipeline.batch_report(ctx.store):
            for k in usage:
                usage[k] += b["usage"].get(k, 0) or 0
        active = ctx.runner.active_job(MODELS_JOB_KEY)
        return ctx.render(
            request,
            "settings.html",
            report=report,
            hw=report.hardware,
            ollama=report.ollama,
            helper_steps=_helper_steps(report.hardware.get("os", "")),
            ollama_url=OLLAMA_URL,
            login=pipeline.ftps_login_status(ctx.cfg),
            style=style_values(),
            rules=rules_values(),
            usage=usage,
            job=ctx.runner.get(active) if active else None,
            job_active=bool(active),
            model_status=ctx.model_status(request),
            ms=storage_context(),
            **model_context(report),
        )

    @app.get("/settings/advice", response_class=HTMLResponse)
    def settings_advice(request: Request, tag: str, return_to: str = "/settings"):
        if not TAG_RE.match(tag):
            raise HTTPException(status_code=404, detail="Unknown model.")
        advice = pipeline.model_disk_advice(ctx.cfg, tag)
        if advice is None:
            raise HTTPException(status_code=404, detail="Unknown model.")
        replaceable = []
        if not advice.fits and advice.free_gb is not None:
            replaceable = [
                m for m in removable_models() if advice.free_gb + m["size_gb"] >= advice.need_gb
            ]
        return ctx.render(
            request,
            "_advice.html",
            replaceable=replaceable,
            advice=advice.to_dict(),
            tag=tag,
            return_to=return_to if return_to in RETURN_PAGES else "/settings",
            can_set=ollama_store.supported(),
            storage_href=(
                "/welcome?step=3&storage=1#storage"
                if return_to == "/welcome?step=3"
                else "/settings#storage"
            ),
        )

    @app.post("/settings/use")
    def settings_use(form: PostForm):
        notice, error = apply_model(str(form.get("spec") or ""))
        return go("/settings", notice, error)

    @app.post("/settings/pull", response_class=HTMLResponse)
    def settings_pull(request: Request, form: PostForm):
        cfg = ctx.cfg
        tag = str(form.get("tag") or "")
        if form.get("confirm") != "yes" or not TAG_RE.match(tag):
            return ctx.message_fragment(request, "Download not confirmed.")
        advice = pipeline.model_disk_advice(cfg, tag)
        if advice is None:
            return ctx.message_fragment(request, f"{tag} is not in the model catalog.")
        replace = str(form.get("replace") or "")
        info = {"tag": tag, "status": ""}
        victim: dict | None = None
        if replace:
            if writing_job_active():
                return ctx.message_fragment(request, WRITING_BUSY)
            victim = next((m for m in removable_models() if m["tag"] == replace), None)
            if victim is None or hardware.normalize_tag(replace) == hardware.normalize_tag(tag):
                return ctx.message_fragment(
                    request, f"{replace} cannot be removed. It is in use, or not downloaded."
                )
            if advice.free_gb is None or advice.free_gb + victim["size_gb"] < advice.need_gb:
                return ctx.message_fragment(
                    request, f"Removing {replace} would not free enough room for {tag}."
                )
        elif not advice.fits:
            return ctx.message_fragment(request, advice.message or "Not enough free disk space.")
        back = str(form.get("return_to") or "")
        back = back if back in RETURN_PAGES else "/settings"
        wizard = back == "/welcome?step=3"

        def run(job: JobContext) -> str:
            last: list[object] = [None]
            if victim is not None:
                # this job may have waited in the queue: check again that it is still safe
                if writing_job_active():
                    raise RuntimeError(f"{WRITING_BUSY} {tag} was not downloaded.")
                try:
                    now_in_use = hardware.normalize_tag(replace) == selected_tag()
                except RuntimeError:
                    now_in_use = True
                if now_in_use:
                    raise RuntimeError(
                        f"{replace} is now the model in use, so it was not removed and "
                        f"{tag} was not downloaded."
                    )
                if not any(m["tag"] == replace for m in removable_models()):
                    raise RuntimeError(
                        f"{replace} is no longer there to remove, so {tag} was not downloaded."
                    )
                try:
                    pipeline.ollama_client(cfg).delete(replace)
                except Exception as exc:
                    raise RuntimeError(
                        f"{replace} could not be removed, so {tag} was not downloaded: {exc}"
                    ) from None

            info["status"] = "Getting ready"

            def on_progress(ev: dict) -> None:
                job.raise_if_cancelled()  # leaving the stream makes Ollama stop the download
                total, done = ev.get("total") or 0, ev.get("completed") or 0
                pct = int(done * 100 / total) if total else 0
                mark = (str(ev.get("status")), pct)
                if mark == last[0]:
                    return  # one database write per percent, not per Ollama event
                last[0] = mark
                info["status"] = plain_pull_status(mark[0], total)
                job.progress(int(done / 1_000_000), int(total / 1_000_000), mark[0])

            removed = f" {replace} was removed." if victim is not None else ""
            try:
                try:
                    job.raise_if_cancelled()
                    pipeline.pull_model(cfg, tag, on_progress)
                finally:
                    state.pending_model = None
            except JobCancelled:
                if model_is_downloaded(tag):
                    job.mark_completed()
                    return f"{CANCEL_TOO_LATE.format(tag=tag)}{removed}"
                return f"{CANCEL_STOPPED.format(tag=tag)}{removed}"
            except Exception as exc:
                if victim is None:
                    raise
                raise RuntimeError(
                    f"{replace} was removed, but {tag} could not be downloaded: {exc}"
                ) from None
            if job.cancelled():  # cancel came too late: the model is complete, but not chosen
                job.mark_completed()
                return f"{CANCEL_TOO_LATE.format(tag=tag)}{removed}"
            if wizard and ctx.fresh_model_status().state == "missing":
                _, error = apply_model(tag)  # nothing usable was chosen meanwhile
                if error is None:
                    return f"Downloaded {tag}.{removed} It is now the AI model shuto uses."
            return f"Downloaded {tag}.{removed} Go back and press Use this."

        return submit_models_job(request, "pull", run, back, info, pending=tag if wizard else None)

    def model_is_downloaded(tag: str) -> bool:
        try:
            rows = pipeline.ollama_client(ctx.cfg).tags()
        except Exception:
            return False
        have = {hardware.normalize_tag(str(m.get("name") or m.get("model") or "")) for m in rows}
        return hardware.normalize_tag(tag) in have

    def remove_problem(tag: str) -> str | None:
        """Plain reason this model cannot be removed now, or None when it can."""
        if ctx.runner.active_job(MODELS_JOB_KEY):
            return "Wait for the download or change in progress to finish."
        if writing_job_active():
            return WRITING_BUSY
        try:
            selected_tag()
        except RuntimeError:
            return "shuto could not tell which model is in use, so it removed nothing."
        if not any(m["tag"] == tag for m in removable_models(include_in_use=True)):
            return f"{tag} cannot be removed here: it is not downloaded, or not on the model list."
        return None

    def config_pins(tag: str) -> bool:
        """True when the settings file itself names this model, so clearing the saved choice
        cannot make shuto forget it."""
        return model_in_use(tag) and hardware.normalize_tag(ctx.cfg.llm.model) == (
            hardware.normalize_tag(tag)
        )

    def model_in_use(tag: str) -> bool:
        try:
            return hardware.normalize_tag(tag) == selected_tag()
        except RuntimeError:
            return False

    @app.get("/settings/model/remove", response_class=HTMLResponse)
    def settings_model_remove_confirm(request: Request, tag: str, return_to: str = "/settings"):
        """Read-only confirmation page, so Remove is safe without JavaScript."""
        if not TAG_RE.match(tag):
            raise HTTPException(status_code=404, detail="Unknown model.")
        back = return_to if return_to in RETURN_PAGES else "/settings"
        problem = remove_problem(tag)
        if problem:
            return go(back, None, problem)
        return ctx.render(
            request,
            "model_remove.html",
            tag=tag,
            return_to=back,
            in_use=model_in_use(tag),
            pinned=config_pins(tag),
        )

    @app.post("/settings/model/remove")
    def settings_model_remove(request: Request, form: PostForm):
        tag = str(form.get("tag") or "")
        back = str(form.get("return_to") or "")
        back = back if back in RETURN_PAGES else "/settings"
        if form.get("confirm") != "yes" or not TAG_RE.match(tag):
            return go(back, None, "Removal not confirmed.", request)
        problem = remove_problem(tag)
        if problem:
            return go(back, None, problem, request)
        client = pipeline.ollama_client(ctx.cfg)
        try:
            # a job could start between the check above and this delete; worst case that job
            # fails and can be run again, nothing is lost
            client.delete(tag)
        except Exception as exc:
            why = _friendly(str(exc))
            msg = f"{tag} could not be removed. {why['what']} {why['todo']}"
            return go(back, None, msg, request)
        if model_in_use(tag):
            # nothing is chosen any more: the "No AI model set" note shows until the user picks
            local_models.clear_choice(pipeline.model_choice_path())
            ctx.store.set_setting("llm.provider", "")
            ctx.store.set_setting("llm.model", "")
            ctx.invalidate_model_status()
        notice = f"{tag} was removed from this computer."
        if config_pins(tag):
            notice += PINNED_TEXT.format(tag=tag)
        return go(back, notice, None, request)

    def plain_pull_status(status: str, total: int) -> str:
        """Ollama's own words ('pulling manifest', 'verifying sha256 digest') in plain ones."""
        if status.startswith("pulling") and total:
            return ""  # the percentage says it all
        if status.startswith("verifying"):
            return "Checking the download"
        if status.startswith(("writing", "removing", "success")):
            return "Finishing"
        return "Getting ready"

    def submit_models_job(
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

        before = state.pending_model
        ctx.invalidate_model_status()
        if pending is not None:
            state.pending_model = pending  # before submit: a quick job may finish at once
        try:
            job_id = ctx.runner.submit(kind, MODELS_JOB_KEY, run_and_forget)
        except JobBusy as exc:
            state.pending_model = before  # the job already running keeps its own pending model
            return ctx.message_fragment(request, str(exc))
        pull_return[0] = back
        pull_state[0] = (job_id, info if info is not None else {"tag": "", "status": ""})
        if kind in BAR_KINDS:
            # always the one slot at the top of the page, whichever page started the job
            resp = bar_response(request, job_id, started=True)
            resp.headers["HX-Retarget"] = "#dl-slot"
            resp.headers["HX-Reswap"] = "innerHTML"
            return resp
        return ctx.job_fragment(request, job_id, next_href=back)

    def change_models_dir(request: Request, form: PostForm, reset: bool):
        if not ollama_store.supported():
            return ctx.message_fragment(request, ollama_store.LINUX_TEXT)
        if ollama_store.machine_wide_value():
            return ctx.message_fragment(request, ollama_store.MACHINE_TEXT)
        if reset and ollama_store.machine_any_value():
            return ctx.message_fragment(request, ollama_store.MACHINE_RESET_TEXT)
        back = str(form.get("return_to") or "")
        back = back if back in RETURN_PAGES else "/settings"
        target, warnings = None, []
        if not reset:
            try:
                check = ollama_store.validate_target(str(form.get("path") or ""))
            except ollama_store.StoreError as exc:
                return ctx.message_fragment(request, str(exc))
            target, warnings = check.path, check.warnings
        move = form.get("move") == "yes"
        client = pipeline.ollama_client(ctx.cfg)

        def run(job: JobContext) -> str:
            text = ollama_store.change_models_dir(
                target,
                move=move,
                client=client,
                progress=job.progress,
                cancelled=job.cancelled,
            )
            return " ".join([text, *warnings])

        return submit_models_job(request, "models_dir", run, back)

    @app.post("/settings/models-dir", response_class=HTMLResponse)
    def settings_models_dir(request: Request, form: PostForm):
        return change_models_dir(request, form, reset=False)

    @app.post("/settings/models-dir/reset", response_class=HTMLResponse)
    def settings_models_dir_reset(request: Request, form: PostForm):
        return change_models_dir(request, form, reset=True)

    @app.post("/settings/benchmark", response_class=HTMLResponse)
    def settings_benchmark(request: Request, form: PostForm):
        cfg = ctx.cfg
        tag = str(form.get("tag") or "")
        if not TAG_RE.match(tag) or pipeline.model_disk_advice(cfg, tag) is None:
            return ctx.message_fragment(request, "Unknown model.")

        def run(job: JobContext) -> str:
            r = pipeline.bench_model(cfg, ctx.store, tag)
            ok = "valid answers" if r.get("ok_json") else "answers that could not be read"
            return (
                f"Speed test for {tag}: the first photo took {r.get('cold_s')} s (the model "
                f"is loading), then {r.get('warm_s')} s per photo; {ok}. "
                "Reload the page to see the updated estimates."
            )

        return ctx.start_job(request, "benchmark", MODELS_JOB_KEY, run, total=2)

    @app.post("/settings/ftps")
    def settings_ftps(form: PostForm):
        notice, error = save_login(form)
        if not notice and not error:
            error = "Enter your Shutterstock email (or username) and password."
        back = str(form.get("back") or "")
        if not LOCAL_PATH_RE.match(back):
            back = "/settings"
        return go(back, notice, error)

    @app.post("/settings/ftps/test", response_class=HTMLResponse)
    def settings_ftps_test(request: Request, form: PostForm):
        cfg = ctx.cfg

        def run(job: JobContext) -> str:
            pipeline.test_ftps_login(cfg)
            return "The login works. Shutterstock accepted it."

        return ctx.start_job(request, "ftps_test", FTPS_JOB_KEY, run)

    @app.post("/settings/ftps/forget")
    def settings_ftps_forget(request: Request, form: PostForm):
        try:
            shuto_secrets.delete_secret("SHUTO_FTPS_USER")
            shuto_secrets.delete_secret("SHUTO_FTPS_PASSWORD")
        except RuntimeError as exc:
            return go("/settings", None, str(exc), request)
        left = pipeline.ftps_login_status(ctx.cfg)
        if left["user_set"] or left["password_set"]:
            return go(
                "/settings",
                "Removed from the keyring.",
                "A login is still found in another place on this computer "
                "(a settings file). Remove it there too.",
                request,
            )
        return go("/settings", "The saved login was removed.", None, request)

    @app.post("/settings/key")
    def settings_key(form: PostForm):
        """Save an online AI access key (keyring) or the AWS profile name. Never echoed back."""
        prov = str(form.get("provider") or "")
        here = _key_return(form)
        if prov == "bedrock":
            name = str(form.get("profile") or "").strip()
            try:
                pipeline.set_ui_setting(ctx.store, "bedrock_profile", name)
            except pipeline.PipelineError as exc:
                return go(here, None, str(exc))
            state.cfg = pipeline.apply_style_overrides(state.base_cfg, ctx.store)
            return go(here, "AWS profile saved." if name else "AWS profile cleared.", None)
        env = API_KEY_ENV.get(prov)
        if env is None:
            raise HTTPException(status_code=404, detail="Unknown provider.")
        key = str(form.get("key") or "").strip()
        if not key:
            return go(here, None, "Paste the access key first.")
        if not MIN_KEY_LEN <= len(key) <= MAX_KEY_LEN or re.search(r"\s", key):
            return go(here, None, "That does not look like an access key. Paste it again.")
        try:
            shuto_secrets.set_secret(env, key)
        except RuntimeError as exc:
            return go(here, None, str(exc).replace(key, ""))
        return go(here, f"Access key saved for {prov.capitalize()}.", None)

    @app.post("/settings/key/forget")
    def settings_key_forget(request: Request, form: PostForm):
        prov = str(form.get("provider") or "")
        here = _key_return(form)
        if prov == "bedrock":
            pipeline.set_ui_setting(ctx.store, "bedrock_profile", "")
            state.cfg = pipeline.apply_style_overrides(state.base_cfg, ctx.store)
            return go(here, "AWS profile cleared.", None, request)
        env = API_KEY_ENV.get(prov)
        if env is None:
            raise HTTPException(status_code=404, detail="Unknown provider.")
        try:
            shuto_secrets.delete_secret(env)
        except RuntimeError as exc:
            return go(here, None, str(exc), request)
        if shuto_secrets.get_secret(env):
            return go(
                here,
                "Removed from the secure password store.",
                "An access key is still found in another place on this computer "
                "(a settings file). Remove it there too.",
                request,
            )
        return go(here, f"The saved access key for {prov.capitalize()} was removed.", None, request)

    @app.post("/settings/style")
    def settings_style(form: PostForm):
        notice, error = save_style(form)
        return go("/settings", notice, error)

    @app.post("/settings/rules")
    def settings_rules(form: PostForm):
        notice, error = save_rules(form)
        return go("/settings", notice, error)

    # ------------------------------------------------------------------ help

    @app.get("/help", response_class=HTMLResponse)
    def help_page(request: Request):
        return ctx.render(request, "help.html")
