"""Photos sent back to the AI leave every Review list until the AI has run."""

from __future__ import annotations

from shuto.core.models import Status

from .test_web_batch import _photos, batch, described, web  # noqa: F401


def test_sent_back_photo_is_hidden_everywhere(web, described):  # noqa: F811
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
    assert "1 photo is waiting for the AI" in page
    assert f"/batch/{described}/check" in page
    assert f'All <span class="chip-count">{total - 1}</span>' in page


def test_photo_comes_back_when_the_ai_is_done(web, described):  # noqa: F811
    store = web.state.store
    victim = _photos(web, described, Status.DESCRIBED)[0]
    web.post(f"/batch/{described}/bulk", {"action": "ready", "ids": [str(victim.id)]})
    store.set_status(victim.id, Status.DESCRIBED, force=True)
    page = web.get(f"/batch/{described}/review?show=good").text
    assert victim.orig_name in page and "waiting for the AI" not in page


def test_photo_pager_skips_waiting_photos(web, described):  # noqa: F811
    store = web.state.store
    ids = [p.id for p in _photos(web, described, Status.DESCRIBED)]
    assert len(ids) >= 3
    store.set_status(ids[1], Status.READY, force=True)
    page = web.get(f"/photo/{ids[0]}?back=all").text
    assert f"/photo/{ids[1]}?back=all" not in page
