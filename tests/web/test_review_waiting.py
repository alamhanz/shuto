"""Photos that wait for the AI leave every Review list until the AI has run."""

from __future__ import annotations

from shuto.core.models import Status

from .test_send_back_flow import _finish, gate  # noqa: F401
from .test_web_batch import _photos, batch, described, web  # noqa: F401


def test_sent_back_photo_is_hidden_everywhere(web, described, gate):  # noqa: F811
    store = web.state.store
    photos = _photos(web, described, Status.DESCRIBED)
    victim = photos[0]
    base = f"/batch/{described}/review"
    total = len(store.list_photos(described))
    r = web.post(f"/batch/{described}/bulk", {"action": "ready", "ids": [str(victim.id)]})
    assert r.status_code == 303
    assert store.get_photo(victim.id).status == Status.READY
    for show in ("attention", "good", "aside", "all"):
        page = web.get(f"{base}?show={show}").text
        assert victim.orig_name not in page, show
    page = web.get(f"{base}?show=all").text
    assert "The AI is working on 1 photo" in page
    assert f'All <span class="chip-count">{total - 1}</span>' in page
    gate.gate.set()
    _finish(web, described)


def test_photo_comes_back_when_the_ai_is_done(web, described, gate):  # noqa: F811
    victim = _photos(web, described, Status.DESCRIBED)[0]
    web.post(f"/batch/{described}/bulk", {"action": "ready", "ids": [str(victim.id)]})
    gate.gate.set()
    _finish(web, described)
    page = web.get(f"/batch/{described}/review?show=good").text
    assert victim.orig_name in page and "waiting for the AI" not in page


def test_waiting_photo_without_a_job_offers_to_run_the_ai(web, described):  # noqa: F811
    victim = _photos(web, described, Status.DESCRIBED)[0]
    web.state.store.set_status(victim.id, Status.READY)
    page = web.get(f"/batch/{described}/review?show=all").text
    assert victim.orig_name not in page
    assert "1 photo is waiting for the AI" in page and "Run the AI now" in page


def test_new_photos_point_to_check(web, batch):  # noqa: F811
    page = web.get(f"/batch/{batch}/review").text
    assert "waiting for the AI" in page and f"/batch/{batch}/check" in page
    assert "Run the AI now" not in page


def test_photo_pager_skips_waiting_photos(web, described):  # noqa: F811
    store = web.state.store
    ids = [p.id for p in _photos(web, described, Status.DESCRIBED)]
    assert len(ids) >= 3
    store.set_status(ids[1], Status.READY, force=True)
    page = web.get(f"/photo/{ids[0]}?back=all").text
    assert f"/photo/{ids[1]}?back=all" not in page


def test_review_stepper_has_one_current_step_and_links(web, described):  # noqa: F811
    victim = _photos(web, described, Status.DESCRIBED)[0]
    web.state.store.set_status(victim.id, Status.READY)
    page = web.get(f"/batch/{described}/review").text
    assert page.count('aria-current="step"') == 1
    assert (
        f'href="/batch/{described}/review"' in page and f'href="/batch/{described}/check"' in page
    )
