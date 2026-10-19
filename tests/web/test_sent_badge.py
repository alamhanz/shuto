"""Photos Shutterstock already has stand out on the Review grid and the photo page."""

from __future__ import annotations

import pytest

from shuto.core.models import Status

from .test_web_batch import _photos, batch, described, web  # noqa: F401


@pytest.mark.parametrize(
    "status", [Status.ALREADY_UPLOADED, Status.UPLOADED, Status.SUBMITTED, Status.ACCEPTED]
)
def test_sent_photo_has_chip_and_ribbon(web, described, status):  # noqa: F811
    photo = _photos(web, described, Status.DESCRIBED)[0]
    web.state.store.set_status(photo.id, status, force=True)
    page = web.get(f"/batch/{described}/review?show=all").text
    assert 'class="chip sent"' in page and 'class="ribbon"' in page
    assert 'class="card ' + status.value + " sent" in page
    one = web.get(f"/photo/{photo.id}?back=all").text
    assert 'class="chip sent"' in one and "Shutterstock already has this photo" in one


def test_ordinary_photo_has_no_sent_mark(web, described):  # noqa: F811
    page = web.get(f"/batch/{described}/review?show=good").text
    assert "chip sent" not in page and "ribbon" not in page
    photo = _photos(web, described, Status.DESCRIBED)[0]
    assert "chip sent" not in web.get(f"/photo/{photo.id}").text
