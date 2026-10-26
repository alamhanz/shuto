"""Quality check on the web: backfill job, calibration table, 100% crop and the Review badge."""

from __future__ import annotations

import io

from PIL import Image

from shuto.core.models import Status
from shuto.workflow import quality, resend

from .test_web_batch import _photos, batch, described, web  # noqa: F401


def _without_quality(web_, batch_id):
    store = web_.state.store
    for p in store.list_photos(batch_id):
        cull = {k: v for k, v in (p.cull or {}).items() if k not in ("quality", "warnings")}
        store.update_photo(p.id, cull=cull)


def test_check_stores_quality_numbers(web, batch):  # noqa: F811
    assert web.run_job(f"/batch/{batch}/cull").state == "done"
    for p in web.state.store.list_photos(batch):
        assert p.cull["quality"]["version"] == 1


def test_measure_job_fills_missing_and_keeps_status(web, batch):  # noqa: F811
    web.run_job(f"/batch/{batch}/cull")
    store = web.state.store
    before = {p.id: p.status for p in store.list_photos(batch)}
    _without_quality(web, batch)
    assert quality.missing(store, batch)
    job = web.run_job("/quality/measure", {"batch_id": batch})
    assert job.state == "done"
    assert not quality.missing(store, batch)
    assert {p.id: p.status for p in store.list_photos(batch)} == before
    again = web.post("/quality/measure", {"batch_id": batch})
    assert "already been measured" in again.text


def test_measure_job_for_all_batches(web, batch):  # noqa: F811
    web.run_job(f"/batch/{batch}/cull")
    _without_quality(web, batch)
    assert web.run_job("/quality/measure").state == "done"
    assert not quality.missing(web.state.store)


def test_measure_skips_current_rows_and_missing_files(web, batch, cfg):  # noqa: F811
    web.run_job(f"/batch/{batch}/cull")
    store = web.state.store
    first, *rest = store.list_photos(batch)
    stamp = {p.id: p.cull["quality"] for p in rest}
    _without_quality(web, batch)
    for p in rest:
        store.update_photo(p.id, cull={**store.get_photo(p.id).cull, "quality": stamp[p.id]})
    import os

    os.remove(first.src_path)
    seen = []
    done = quality.measure_missing(cfg, store, batch, progress=lambda n, t, m="": seen.append(t))
    assert done == 0 and seen == [1]
    assert "quality" not in store.get_photo(first.id).cull


def test_measure_unknown_batch_is_404(web):  # noqa: F811
    assert web.post("/quality/measure", {"batch_id": "nope"}).status_code == 404


def test_crop_route(web, batch):  # noqa: F811
    store = web.state.store
    first = store.list_photos(batch)[0]
    assert web.get(f"/photo/{first.id}/crop").status_code == 404  # not measured yet
    assert web.get("/photo/99999/crop").status_code == 404
    web.run_job(f"/batch/{batch}/cull")
    r = web.get(f"/photo/{first.id}/crop")
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    im = Image.open(io.BytesIO(r.content))
    assert max(im.size) <= quality.CROP_SIDE


def test_photo_page_quality_block(web, batch):  # noqa: F811
    first = web.state.store.list_photos(batch)[0]
    web.run_job(f"/batch/{batch}/cull")
    page = web.get(f"/photo/{first.id}").text
    assert "Close-up check" in page and f"/photo/{first.id}/crop" in page
    _without_quality(web, batch)
    page = web.get(f"/photo/{first.id}").text
    assert "Not checked at full size yet" in page and "Measure the photos of this batch" in page


def test_review_card_badge(web, described):  # noqa: F811
    photo = _photos(web, described, Status.DESCRIBED)[0]
    assert "May be rejected for quality" not in web.get(f"/batch/{described}/review?show=all").text
    web.state.store.update_photo(
        photo.id, cull={**photo.cull, "warnings": ["Looks soft when zoomed to 100%."]}
    )
    page = web.get(f"/batch/{described}/review?show=all").text
    assert page.count("May be rejected for quality") == 1
    assert web.state.store.get_photo(photo.id).status == Status.DESCRIBED


def test_settings_section_without_results(web):  # noqa: F811
    page = web.get("/settings").text
    assert 'id="quality"' in page and "Measure earlier photos" in page
    assert "No results recorded yet" in page


def test_settings_section_with_two_results(web, described):  # noqa: F811
    store = web.state.store
    one, two = _photos(web, described, Status.DESCRIBED)[:2]
    for p in (one, two):
        store.set_status(p.id, Status.UPLOADED, force=True)
        store.set_status(p.id, Status.SUBMITTED, force=True)
    store.update_photo(
        one.id,
        cull={
            **one.cull,
            "quality": {
                **one.cull["quality"],
                "problems": [
                    {"code": "soft_full_size", "text": "soft", "shown": True, "fixable": True}
                ],
            },
        },
    )
    resend.record_result(store, one.id, "rejected", ["quality", "title"], None)
    resend.record_result(store, two.id, "data_licensing", [], None)
    rows = {r["id"]: r for r in quality.calibration_rows(store)}
    assert rows[one.id]["outcome"] == "rejected" and rows[one.id]["quality_reject"]
    assert rows[one.id]["problems"] == ["soft"]
    assert rows[two.id]["outcome"] == "data_licensing" and rows[two.id]["quality_reject"]
    summary = quality.calibration_summary(list(rows.values()))
    assert summary["quality_rejects"] == 2 and summary["warned_and_rejected"] == 1
    assert summary["missed"] == 1
    page = web.get("/settings").text
    assert page.count('<th scope="row">') == 2
    assert "Accepted for data licensing only" in page and "May be rejected" in page
    assert "No results recorded yet" not in page
