"""The setup buttons and the download bar follow a model download without a page reload."""

# ruff: noqa: F811  (pytest fixtures are imported, then used as test arguments)

from __future__ import annotations

import pytest

from shuto import pipeline
from shuto.web import create_app

from .test_web import Web
from .test_web_pull_progress import slow, start_pull  # noqa: F401


@pytest.fixture
def web(cfg, slow):
    """No model chosen yet (the local AI is the provider), as on a first run."""
    local = cfg.model_copy(update={"llm": cfg.llm.model_copy(update={"provider": "ollama"})})
    w = Web(create_app(local))
    pipeline.set_ui_setting(w.state.store, "onboarded", True)
    yield w
    slow.release.set()
    w.state.runner.shutdown()


def disabled_continue(text: str) -> bool:
    return "disabled" in text and 'href="/welcome?step=4"' not in text


def test_wizard_continue_unlocks_without_reload(web, cfg, slow):
    page = web.get("/welcome?step=3")
    assert 'id="wizard-nav"' in page.text and 'hx-get="/welcome/nav"' in page.text
    assert "shuto:models-changed from:body" in page.text
    assert disabled_continue(web.get("/welcome/nav").text)
    tag = next(r.tag for r in pipeline.list_models(cfg) if not r.pulled and r.fits_hardware)
    # the button asks for the confirm fragment, whose form posts the pull (no page reload)
    advice = web.get(f"/settings/advice?tag={tag}&return_to=/welcome?step=3")
    assert 'hx-post="/settings/pull"' in advice.text
    resp = web.post(
        "/settings/pull", {"tag": tag, "confirm": "yes", "return_to": "/welcome?step=3"}
    )
    assert resp.headers["HX-Trigger"] == "shuto:models-changed"
    assert resp.headers["HX-Retarget"] == "#dl-slot"
    assert slow.started.wait(30)
    nav = web.get("/welcome/nav").text
    assert 'href="/welcome?step=4"' in nav and "Downloading" in nav and "disabled" not in nav
    job_id = web.state.runner.active_job("models")
    slow.pulled.append(tag)  # the pull put the model on disk
    slow.release.set()
    web.state.runner.wait(job_id)
    done = web.get(f"/downloads/bar?job={job_id}")
    assert done.headers["HX-Trigger"] == "shuto:models-changed"
    nav = web.get("/welcome/nav").text
    assert 'href="/welcome?step=4"' in nav and "Downloading" not in nav


def test_wizard_nav_refreshes_when_download_is_stopped(web, cfg, slow):
    start_pull(web, cfg, slow, "/welcome?step=3")
    job_id = web.state.runner.active_job("models")
    web.post(f"/jobs/{job_id}/cancel", {})
    slow.release.set()
    web.state.runner.wait(job_id)
    ended = web.get(f"/downloads/bar?job={job_id}")
    assert ended.headers["HX-Trigger"] == "shuto:models-changed"
    assert disabled_continue(web.get("/welcome/nav").text)


def test_bar_slot_stays_on_every_page_and_sticks(web, cfg, slow):
    start_pull(web, cfg, slow, "/welcome?step=3")
    for path in [f"/welcome?step={n}" for n in range(3, 7)] + ["/settings", "/help"]:
        assert 'id="dl-slot" hx-get="/downloads/bar"' in web.get(path).text
    assert "every 2s" in web.get("/downloads/bar").text
    css = web.get("/static/shuto.css").text
    assert "#dl-slot { position: sticky; top: 0; z-index: 20;" in css
    assert "#dl-slot:empty { display: none; }" in css


def finish(web, slow):
    job_id = web.state.runner.active_job("models")
    slow.release.set()
    web.state.runner.wait(job_id)
    return job_id


def poll(web, job_id, page):
    return web.get(f"/downloads/bar?job={job_id}", headers={"hx-current-url": page})


def test_done_download_reloads_the_model_step_once(web, cfg, slow):
    start_pull(web, cfg, slow, "/welcome?step=3")
    job_id = finish(web, slow)
    first = poll(web, job_id, "http://x/welcome?step=3")
    assert first.headers["HX-Refresh"] == "true"
    assert "is downloaded and chosen" in first.text
    second = poll(web, job_id, "http://x/welcome?step=3")
    assert "HX-Refresh" not in second.headers


def test_done_download_reloads_home_but_not_pages_with_typing(web, cfg, slow):
    start_pull(web, cfg, slow, "/welcome?step=3")
    job_id = finish(web, slow)
    typing = poll(web, job_id, "http://x/welcome?step=4")
    assert "HX-Refresh" not in typing.headers
    assert "is downloaded and chosen" in typing.text
    assert typing.headers["HX-Trigger"] == "shuto:models-changed"


def test_done_download_reloads_home(web, cfg, slow):
    start_pull(web, cfg, slow, "/welcome?step=3")
    job_id = finish(web, slow)
    assert poll(web, job_id, "http://x/").headers["HX-Refresh"] == "true"


def test_settings_is_never_reloaded(web, cfg, slow):
    start_pull(web, cfg, slow, "/welcome?step=3")
    job_id = finish(web, slow)
    assert "HX-Refresh" not in poll(web, job_id, "http://x/settings").headers


def test_failed_and_stopped_downloads_do_not_reload(web, cfg, slow):
    start_pull(web, cfg, slow, "/welcome?step=3")
    job_id = web.state.runner.active_job("models")
    slow.fail = True
    stopped = web.post(f"/jobs/{job_id}/cancel", {})
    assert "HX-Refresh" not in stopped.headers
    finish(web, slow)
    assert "HX-Refresh" not in poll(web, job_id, "http://x/welcome?step=3").headers


def test_trigger_only_on_start_and_end_not_on_every_poll(web, cfg, slow):
    start_pull(web, cfg, slow, "/welcome?step=3")
    job_id = web.state.runner.active_job("models")
    mid = web.get(f"/downloads/bar?job={job_id}")
    assert "every 2s" in mid.text and "HX-Trigger" not in mid.headers
    finish(web, slow)
    assert poll(web, job_id, "http://x/help").headers["HX-Trigger"] == "shuto:models-changed"


def test_a_tab_on_a_typing_page_does_not_use_up_the_reload(web, cfg, slow):
    start_pull(web, cfg, slow, "/welcome?step=3")
    job_id = finish(web, slow)
    assert "HX-Refresh" not in poll(web, job_id, "http://x/welcome?step=4").headers
    assert "HX-Refresh" not in poll(web, job_id, "http://x/settings").headers
    assert poll(web, job_id, "http://x/welcome?step=3").headers["HX-Refresh"] == "true"
    assert "HX-Refresh" not in poll(web, job_id, "http://x/").headers
