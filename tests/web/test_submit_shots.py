"""The Submit page shows the owner's real Shutterstock screenshots."""

from __future__ import annotations

import re
import struct

import pytest

from shuto.core.models import Status

from .test_web_batch import _photos, batch, described, web  # noqa: F401

SIZES = {1: (834, 406), 2: (878, 597), 3: (1032, 314)}


@pytest.mark.parametrize("n", [1, 2, 3])
def test_screenshot_is_served(web, n):  # noqa: F811
    r = web.get(f"/static/img/submit-{n}.png")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    w, h = struct.unpack(">II", r.content[16:24])
    assert (w, h) == SIZES[n]


def test_submit_page_images_match_their_real_size(web, described, tmp_path):  # noqa: F811
    photo = _photos(web, described, Status.DESCRIBED)[0]
    web.state.store.set_status(photo.id, Status.EXPORTED, force=True)
    page = web.get(f"/batch/{described}/submit").text
    for n, (w, h) in SIZES.items():
        tag = re.search(rf"<img[^>]*submit-{n}\.png[^>]*>", page)
        assert tag, n
        assert f'width="{w}"' in tag.group(0) and f'height="{h}"' in tag.group(0)
        assert "placeholder" not in tag.group(0)
    assert "do not attach releases" in page
