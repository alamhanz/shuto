"""Quality check on the web: backfill job, calibration table, 100% crop and the Review badge."""

from __future__ import annotations

import io
from pathlib import Path

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
    assert first.id not in [p.id for p in quality.missing(store, batch)]  # cannot be measured
    done = quality.measure_missing(cfg, store, batch, progress=lambda n, t, m="": seen.append(t))
    assert done == 0 and seen == []
    assert "quality" not in store.get_photo(first.id).cull


def test_measure_cancel_stops_before_the_next_photo(web, batch, cfg):  # noqa: F811
    web.run_job(f"/batch/{batch}/cull")
    store = web.state.store
    _without_quality(web, batch)
    total = len(quality.missing(store, batch))
    assert total > 1
    calls = []

    def cancel():
        calls.append(1)
        return len(calls) > 1  # the first photo goes through, then Cancel

    assert quality.measure_missing(cfg, store, batch, cancel=cancel) == 1
    assert len(quality.missing(store, batch)) == total - 1


def test_measure_unreadable_photo_is_logged_and_not_retried(web, batch, cfg, caplog):  # noqa: F811
    web.run_job(f"/batch/{batch}/cull")
    store = web.state.store
    _without_quality(web, batch)
    bad = store.list_photos(batch)[0]
    Path(bad.src_path).write_bytes(b"not a picture")
    with caplog.at_level("WARNING"):
        done = quality.measure_missing(cfg, store, batch)
    assert done == len(store.list_photos(batch)) - 1
    assert bad.src_path in caplog.text
    assert not quality.missing(store, batch)
    assert store.get_photo(bad.id).cull["quality"]["failed"] is True
    page = web.get(f"/photo/{bad.id}").text
    assert "could not read this photo at full size" in page


def test_measure_keeps_cull_data_written_meanwhile(web, batch, cfg, monkeypatch):  # noqa: F811
    web.run_job(f"/batch/{batch}/cull")
    store = web.state.store
    _without_quality(web, batch)
    first = store.list_photos(batch)[0]
    real = quality.cull_mod.evaluate

    def evaluate(path, *a, **kw):
        store.update_photo(first.id, cull={**store.get_photo(first.id).cull, "marker": 1})
        return real(path, *a, **kw)

    monkeypatch.setattr(quality.cull_mod, "evaluate", evaluate)
    quality.measure_missing(cfg, store, batch)
    cull = store.get_photo(first.id).cull
    assert cull["marker"] == 1 and cull["quality"]["version"] == 1


def test_crop_temp_files_do_not_stay(web, batch, cfg):  # noqa: F811
    web.run_job(f"/batch/{batch}/cull")
    first = web.state.store.list_photos(batch)[0]
    assert web.get(f"/photo/{first.id}/crop").status_code == 200
    assert not list(cfg.workspace_path("crops").glob("*.tmp"))


def test_result_without_reason_is_allowed_on_the_web(web, batch):  # noqa: F811
    store = web.state.store
    first = store.list_photos(batch)[0]
    store.set_status(first.id, Status.SUBMITTED, force=True)
    assert "leave all boxes empty" in web.get(f"/photo/{first.id}").text
    r = web.post(f"/photo/{first.id}/result", {"outcome": "reject"}, follow_redirects=False)
    assert r.status_code in (302, 303)
    assert store.get_photo(first.id).status == Status.REJECTED
    assert store.list_rejections(first.id) == []


def test_batch_results_ignores_odd_ids(web, batch):  # noqa: F811
    r = web.post(
        f"/batch/{batch}/results",
        {"result_ids": ["²", "x"], "outcome_2": "accept"},
        follow_redirects=True,
    )
    assert r.status_code == 200 and "Choose a result" in r.text


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
