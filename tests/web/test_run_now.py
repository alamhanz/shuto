"""Send back with "Run the AI right away" off, and the AI buttons while a job runs."""

from __future__ import annotations

from urllib.parse import unquote

from shuto.core.models import Status

from .test_send_back_flow import _bulk, _finish, gate  # noqa: F401
from .test_web_batch import _photos, batch, described, web  # noqa: F401


def _note(r):
    return unquote(r.headers["location"])


def _good_ids(web, batch_id, n):  # noqa: F811
    return [p.id for p in _photos(web, batch_id, Status.DESCRIBED)][:n]


def test_unticked_send_back_only_waits(web, described, gate):  # noqa: F811
    ids = _good_ids(web, described, 2)
    r = _bulk(web, described, "ready", ids, run_now_shown="1")  # the box is unticked
    assert r.status_code == 303
    note = _note(r)
    assert "2 photos sent back" in note and "wait for the AI" in note
    assert "Check all 2 photos on the Check page" in note
    assert web.state.runner.active_job(described) is None
    assert {p.id for p in _photos(web, described, Status.READY)} == set(ids)
    assert web.state.store.get_setting("ui.send_back_run_now") is False
    assert not gate.calls


def test_ticked_send_back_starts_the_ai(web, described, gate):  # noqa: F811
    ids = _good_ids(web, described, 2)
    r = _bulk(web, described, "ready", ids, run_now_shown="1", run_now="on")
    assert "wait for the AI" not in _note(r)
    assert web.state.runner.active_job(described) is not None
    assert web.state.store.get_setting("ui.send_back_run_now") is True
    gate.gate.set()
    assert _finish(web, described).state == "done"
    assert not _photos(web, described, Status.READY)


def test_first_time_the_box_is_ticked_and_the_choice_is_remembered(web, described, gate):  # noqa: F811
    page = web.get(f"/batch/{described}/review?show=good").text
    assert "Run the AI right away" in page and 'name="run_now" value="on" checked' in page
    ids = _good_ids(web, described, 2)
    _bulk(web, described, "ready", ids[:1], run_now_shown="1")
    page = web.get(f"/batch/{described}/review?show=good").text
    assert 'name="run_now" value="on" checked' not in page and "Run the AI right away" in page
    # A single photo's "send back" button follows the saved choice: it only waits.
    r = web.post(f"/photo/{ids[1]}/ready", {})
    assert r.status_code == 303 and "wait for the AI" in _note(r)
    assert web.state.runner.active_job(described) is None
    assert {p.id for p in _photos(web, described, Status.READY)} == set(ids)
    # Ticked again: the single photo button runs the AI.
    _bulk(web, described, "ready", ids[:1], run_now_shown="1", run_now="on")
    page = web.get(f"/batch/{described}/review?show=good").text
    assert 'name="run_now" value="on" checked' in page


def test_unticked_while_the_ai_runs_joins_that_job(web, described, gate):  # noqa: F811
    first, second = _good_ids(web, described, 2)
    web.state.cfg.llm.concurrency = 1
    _bulk(web, described, "ready", [first])
    job_id = web.state.runner.active_job(described)
    assert gate.entered.wait(10)
    r = _bulk(web, described, "ready", [second], run_now_shown="1")
    assert "already running for this batch and will include them" in _note(r)
    assert web.state.runner.active_job(described) == job_id
    gate.gate.set()
    assert web.state.runner.wait(job_id, timeout=60).state == "done"


def test_check_button_follows_the_running_job(web, described, gate):  # noqa: F811
    _bulk(web, described, "ready", _good_ids(web, described, 2))
    job_id = web.state.runner.active_job(described)
    assert gate.entered.wait(10)
    page = web.get(f"/batch/{described}/check").text
    assert "The AI is running" in page and "disabled" in page
    assert f"/batch/{described}/ai-status?place=check" in page and 'hx-trigger="every 2s"' in page
    poll = web.get(f"/batch/{described}/ai-status?place=check").text
    assert "The AI is running" in poll and 'hx-trigger="every 2s"' in poll

    gate.gate.set()
    assert web.state.runner.wait(job_id, timeout=60).state == "done"
    done = web.get(f"/batch/{described}/ai-status?place=check").text
    assert "The AI is running" not in done and 'hx-trigger="every 2s"' not in done
    assert "All photos are checked" in done and f"/batch/{described}/review" in done


def test_check_button_is_enabled_again_when_photos_still_wait(web, described, gate):  # noqa: F811
    _bulk(web, described, "ready", _good_ids(web, described, 2), run_now_shown="1")
    poll = web.get(f"/batch/{described}/ai-status?place=check").text
    assert "Check all 2 photos" in poll and "disabled" not in poll
    assert 'hx-trigger="every 2s"' not in poll


def test_pressing_check_while_busy_shows_the_running_job(web, described, gate):  # noqa: F811
    _bulk(web, described, "ready", _good_ids(web, described, 2))
    job_id = web.state.runner.active_job(described)
    assert gate.entered.wait(10)
    r = web.post(f"/batch/{described}/check", {})
    assert r.status_code == 200 and f'id="job-{job_id}"' in r.text
    assert web.state.runner.active_job(described) == job_id  # no second job
    gate.gate.set()
    assert web.state.runner.wait(job_id, timeout=60).state == "done"


def test_pressing_check_disables_the_button_at_once(web, batch, gate):  # noqa: F811
    r = web.post(f"/batch/{batch}/check", {})
    assert r.status_code == 200 and 'id="check-action"' in r.text
    assert 'hx-swap-oob="true"' in r.text and "The AI is running" in r.text
    gate.gate.set()
    _finish(web, batch)


def test_review_has_no_run_button_while_a_job_runs(web, described, gate):  # noqa: F811
    _bulk(web, described, "ready", _good_ids(web, described, 2))
    assert gate.entered.wait(10)
    page = web.get(f"/batch/{described}/review?show=good").text
    assert "The AI is working on 2 photos" in page and "Run the AI now" not in page
    gate.gate.set()
    _finish(web, described)


def test_review_offers_both_ways_to_start_the_ai(web, described):  # noqa: F811
    _bulk(web, described, "ready", _good_ids(web, described, 2), run_now_shown="1")
    page = web.get(f"/batch/{described}/review?show=good").text
    assert "2 photos are waiting for the AI" in page and "Run the AI now" in page
    assert "Check all 2 photos</a> on the Check page" in page
