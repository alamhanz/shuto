"""Settings routes for AI models: choose, download, remove, move the folder, speed test."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from shuto.ai import catalog as local_models
from shuto.ai import hardware, selection
from shuto.core import paths
from shuto.core.jobs import JobCancelled, JobContext
from shuto.ollama import models_dir as ollama_store
from shuto.web import ux
from shuto.web.routes.actions import RETURN_PAGES, TAG_RE, apply_model
from shuto.web.routes.model_support import (
    model_is_downloaded,
    removable_models,
    selected_tag,
    submit_models_job,
    writing_job_active,
)
from shuto.web.state import MODELS_JOB_KEY, Ctx, PostForm, WebCtx
from shuto.workflow import models as workflow_models

router = APIRouter()

CANCEL_TOO_LATE = "The download finished before it could be stopped. {tag} is ready to use."
CANCEL_STOPPED = (
    "Download stopped. {tag} is not ready to use. Partly downloaded files stay, "
    "so a later download can continue where this one stopped."
)
PINNED_TEXT = " Your settings file still names {tag}. Choose another model in Settings."
WRITING_BUSY = "Wait for shuto to finish writing titles and keywords, then try again."


def plain_pull_status(status: str, total: int) -> str:
    """Ollama's own words ('pulling manifest', 'verifying sha256 digest') in plain ones."""
    if status.startswith("pulling") and total:
        return ""  # the percentage says it all
    if status.startswith("verifying"):
        return "Checking the download"
    if status.startswith(("writing", "removing", "success")):
        return "Finishing"
    return "Getting ready"


def remove_problem(ctx: WebCtx, tag: str) -> str | None:
    """Plain reason this model cannot be removed now, or None when it can."""
    if ctx.runner.active_job(MODELS_JOB_KEY):
        return "Wait for the download or change in progress to finish."
    if writing_job_active(ctx):
        return WRITING_BUSY
    try:
        selected_tag(ctx)
    except RuntimeError:
        return "shuto could not tell which model is in use, so it removed nothing."
    if not any(m["tag"] == tag for m in removable_models(ctx, include_in_use=True)):
        return f"{tag} cannot be removed here: it is not downloaded, or not on the model list."
    return None


def model_in_use(ctx: WebCtx, tag: str) -> bool:
    try:
        return hardware.normalize_tag(tag) == selected_tag(ctx)
    except RuntimeError:
        return False


def config_pins(ctx: WebCtx, tag: str) -> bool:
    """True when the settings file itself names this model, so clearing the saved choice
    cannot make shuto forget it."""
    return model_in_use(ctx, tag) and hardware.normalize_tag(ctx.cfg.llm.model) == (
        hardware.normalize_tag(tag)
    )


@router.get("/settings/advice", response_class=HTMLResponse)
def settings_advice(request: Request, tag: str, ctx: Ctx, return_to: str = "/settings"):
    if not TAG_RE.match(tag):
        raise HTTPException(status_code=404, detail="Unknown model.")
    advice = workflow_models.model_disk_advice(ctx.cfg, tag)
    if advice is None:
        raise HTTPException(status_code=404, detail="Unknown model.")
    replaceable = []
    if not advice.fits and advice.free_gb is not None:
        replaceable = [
            m for m in removable_models(ctx) if advice.free_gb + m["size_gb"] >= advice.need_gb
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


@router.post("/settings/use")
def settings_use(form: PostForm, ctx: Ctx):
    notice, error = apply_model(ctx, str(form.get("spec") or ""))
    return ctx.go("/settings", notice, error)


@router.post("/settings/pull", response_class=HTMLResponse)
def settings_pull(request: Request, form: PostForm, ctx: Ctx):
    cfg = ctx.cfg
    tag = str(form.get("tag") or "")
    if form.get("confirm") != "yes" or not TAG_RE.match(tag):
        return ctx.message_fragment(request, "Download not confirmed.")
    advice = workflow_models.model_disk_advice(cfg, tag)
    if advice is None:
        return ctx.message_fragment(request, f"{tag} is not in the model catalog.")
    replace = str(form.get("replace") or "")
    info = {"tag": tag, "status": ""}
    victim: dict | None = None
    if replace:
        if writing_job_active(ctx):
            return ctx.message_fragment(request, WRITING_BUSY)
        victim = next((m for m in removable_models(ctx) if m["tag"] == replace), None)
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
            if writing_job_active(ctx):
                raise RuntimeError(f"{WRITING_BUSY} {tag} was not downloaded.")
            try:
                now_in_use = hardware.normalize_tag(replace) == selected_tag(ctx)
            except RuntimeError:
                now_in_use = True
            if now_in_use:
                raise RuntimeError(
                    f"{replace} is now the model in use, so it was not removed and "
                    f"{tag} was not downloaded."
                )
            if not any(m["tag"] == replace for m in removable_models(ctx)):
                raise RuntimeError(
                    f"{replace} is no longer there to remove, so {tag} was not downloaded."
                )
            try:
                selection.ollama_client(cfg).delete(replace)
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
                workflow_models.pull_model(cfg, tag, on_progress)
            finally:
                ctx.state.pending_model = None
        except JobCancelled:
            if model_is_downloaded(ctx, tag):
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
            _, error = apply_model(ctx, tag)  # nothing usable was chosen meanwhile
            if error is None:
                return f"Downloaded {tag}.{removed} It is now the AI model shuto uses."
        return f"Downloaded {tag}.{removed} Go back and press Use this."

    return submit_models_job(ctx, request, "pull", run, back, info, pending=tag if wizard else None)


@router.get("/settings/model/remove", response_class=HTMLResponse)
def settings_model_remove_confirm(
    request: Request, tag: str, ctx: Ctx, return_to: str = "/settings"
):
    """Read-only confirmation page, so Remove is safe without JavaScript."""
    if not TAG_RE.match(tag):
        raise HTTPException(status_code=404, detail="Unknown model.")
    back = return_to if return_to in RETURN_PAGES else "/settings"
    problem = remove_problem(ctx, tag)
    if problem:
        return ctx.go(back, None, problem)
    return ctx.render(
        request,
        "model_remove.html",
        tag=tag,
        return_to=back,
        in_use=model_in_use(ctx, tag),
        pinned=config_pins(ctx, tag),
    )


@router.post("/settings/model/remove")
def settings_model_remove(request: Request, form: PostForm, ctx: Ctx):
    tag = str(form.get("tag") or "")
    back = str(form.get("return_to") or "")
    back = back if back in RETURN_PAGES else "/settings"
    if form.get("confirm") != "yes" or not TAG_RE.match(tag):
        return ctx.go(back, None, "Removal not confirmed.", request)
    problem = remove_problem(ctx, tag)
    if problem:
        return ctx.go(back, None, problem, request)
    client = selection.ollama_client(ctx.cfg)
    try:
        # a job could start between the check above and this delete; worst case that job
        # fails and can be run again, nothing is lost
        client.delete(tag)
    except Exception as exc:
        why = ux.friendly(str(exc))
        msg = f"{tag} could not be removed. {why['what']} {why['todo']}"
        return ctx.go(back, None, msg, request)
    if model_in_use(ctx, tag):
        # nothing is chosen any more: the "No AI model set" note shows until the user picks
        local_models.clear_choice(paths.model_choice_path())
        ctx.store.set_setting("llm.provider", "")
        ctx.store.set_setting("llm.model", "")
        ctx.invalidate_model_status()
    notice = f"{tag} was removed from this computer."
    if config_pins(ctx, tag):
        notice += PINNED_TEXT.format(tag=tag)
    return ctx.go(back, notice, None, request)


def change_models_dir(ctx: WebCtx, request: Request, form, reset: bool):
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
    client = selection.ollama_client(ctx.cfg)

    def run(job: JobContext) -> str:
        text = ollama_store.change_models_dir(
            target,
            move=move,
            client=client,
            progress=job.progress,
            cancelled=job.cancelled,
        )
        return " ".join([text, *warnings])

    return submit_models_job(ctx, request, "models_dir", run, back)


@router.post("/settings/models-dir", response_class=HTMLResponse)
def settings_models_dir(request: Request, form: PostForm, ctx: Ctx):
    return change_models_dir(ctx, request, form, reset=False)


@router.post("/settings/models-dir/reset", response_class=HTMLResponse)
def settings_models_dir_reset(request: Request, form: PostForm, ctx: Ctx):
    return change_models_dir(ctx, request, form, reset=True)


@router.post("/settings/benchmark", response_class=HTMLResponse)
def settings_benchmark(request: Request, form: PostForm, ctx: Ctx):
    cfg = ctx.cfg
    tag = str(form.get("tag") or "")
    if not TAG_RE.match(tag) or workflow_models.model_disk_advice(cfg, tag) is None:
        return ctx.message_fragment(request, "Unknown model.")

    def run(job: JobContext) -> str:
        r = workflow_models.bench_model(cfg, ctx.store, tag)
        ok = "valid answers" if r.get("ok_json") else "answers that could not be read"
        return (
            f"Speed test for {tag}: the first photo took {r.get('cold_s')} s (the model "
            f"is loading), then {r.get('warm_s')} s per photo; {ok}. "
            "Reload the page to see the updated estimates."
        )

    return ctx.start_job(request, "benchmark", MODELS_JOB_KEY, run, total=2)
