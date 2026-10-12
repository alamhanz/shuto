"""Setup-side routes: home, welcome wizard, add photos, jobs, folder browser, settings, help."""

from __future__ import annotations

import re
from importlib import metadata
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from starlette.datastructures import FormData

from .. import estimate, hardware, ollama_store, pipeline
from .. import secrets as shuto_secrets
from ..jobs import JobContext
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
    import_results: dict[int, list[tuple[str, int]]] = {}
    pull_return: list[str] = ["/settings"]

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
                ("neutral", "Set aside", _n(counts, "culled", "duplicate", "near_duplicate")),
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
        return {
            "cards": rows,
            "providers": providers,
            "selected": report.selected,
            "chosen": report.selected is not None,
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
        return {
            "supported": ollama_store.supported(),
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
                extra["ms"] = storage_context()
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
        return ctx.render(
            request,
            "add.html",
            last_folder=pipeline.get_ui_setting(ctx.store, "last_folder") or "",
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

    @app.post("/jobs/{job_id}/cancel", response_class=HTMLResponse)
    def job_cancel(request: Request, job_id: int, form: PostForm):
        if ctx.runner.get(job_id) is None:
            raise HTTPException(status_code=404, detail="Unknown job.")
        ctx.runner.cancel(job_id)
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
            usage=usage,
            job=ctx.runner.get(active) if active else None,
            job_active=bool(active),
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
        return ctx.render(
            request,
            "_advice.html",
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
        if not advice.fits:
            return ctx.message_fragment(request, advice.message or "Not enough free disk space.")
        back = str(form.get("return_to") or "")
        pull_return[0] = back if back in RETURN_PAGES else "/settings"

        def run(job: JobContext) -> str:
            last: list[object] = [None]

            def on_progress(ev: dict) -> None:
                total, done = ev.get("total") or 0, ev.get("completed") or 0
                pct = int(done * 100 / total) if total else 0
                mark = (str(ev.get("status")), pct)
                if mark == last[0]:
                    return  # one database write per percent, not per Ollama event
                last[0] = mark
                job.progress(int(done / 1_000_000), int(total / 1_000_000), mark[0])

            pipeline.pull_model(cfg, tag, on_progress)
            return f"Downloaded {tag}. Go back and press Use this."

        return ctx.start_job(request, "pull", MODELS_JOB_KEY, run, next_href=pull_return[0])

    def change_models_dir(request: Request, form: PostForm, reset: bool):
        if not ollama_store.supported():
            return ctx.message_fragment(request, ollama_store.LINUX_TEXT)
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
        pull_return[0] = back

        def run(job: JobContext) -> str:
            text = ollama_store.change_models_dir(
                target,
                move=move,
                client=client,
                progress=job.progress,
                cancelled=job.cancelled,
            )
            return " ".join([text, *warnings])

        return ctx.start_job(request, "models_dir", MODELS_JOB_KEY, run, next_href=back)

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

    # ------------------------------------------------------------------ help

    @app.get("/help", response_class=HTMLResponse)
    def help_page(request: Request):
        return ctx.render(request, "help.html")
