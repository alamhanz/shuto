"""Default place for editorial captions: caption text, place source, report, Settings, pages."""

from __future__ import annotations

import csv
import io
import re

import pytest

from shuto.ai.describe import (
    NO_PLACE_WARNING,
    Style,
    describe_photo,
    editorial_caption,
    resolve_place,
)
from shuto.ai.providers.fake import DEFAULT_RESPONSE, FakeProvider
from shuto.core import errors
from shuto.core.config import Config, DefaultPlace
from shuto.core.models import Status
from shuto.core.validate import clean_place_part
from shuto.metadata import guardrails
from shuto.settings import effective
from shuto.settings import ui as settings_ui
from shuto.workflow import check_batch, reports
from shuto.workflow import review as workflow_review

from ..web.test_web import JARGON, main_text
from ..web.test_web_batch import batch, web  # noqa: F401  (fixtures)
from ..workflow.test_pipeline_batch import _import

R = guardrails.load_restrictions()
NO_LOC = {"city": None, "region": None, "country": None}
TOKYO = Style(default_city="Tokyo", default_country="Japan")


def _describe(location, *, style=None, **over):
    resp = {**DEFAULT_RESPONSE, "editorial": True, "caption_sentence": "a street.", **over}
    resp["location"] = location
    return describe_photo(
        FakeProvider(response=resp), b"j", "a.jpg", style=style or Style(), restrictions=R
    )


def test_default_fills_a_missing_location():
    res = _describe(NO_LOC, style=TOKYO)
    assert res.metadata.description.startswith("TOKYO, JAPAN - UNKNOWN: ")
    assert res.place == {"city": "TOKYO", "country": "JAPAN", "source": "default", "default": True}


def test_default_fills_only_the_missing_part():
    res = _describe({"city": "Kyoto", "region": None, "country": None}, style=TOKYO)
    assert res.metadata.description.startswith("KYOTO, JAPAN - ")
    assert res.place["source"] == "partly default" and res.place["default"] is True
    res = _describe({"city": None, "region": None, "country": "Japan"}, style=TOKYO)
    assert res.metadata.description.startswith("TOKYO, JAPAN - ")
    assert res.place["source"] == "partly default"


def test_a_real_location_wins_over_the_default():
    res = _describe({"city": "Osaka", "region": None, "country": "Japan"}, style=TOKYO)
    assert res.metadata.description.startswith("OSAKA, JAPAN - ")
    assert res.place["source"] == "ai" and res.place["default"] is False
    assert NO_PLACE_WARNING not in res.metadata.warnings


def test_no_default_keeps_unknown_and_warns():
    res = _describe(NO_LOC)
    assert res.metadata.description.startswith("UNKNOWN, UNKNOWN - ")
    assert res.place["source"] == "unknown" and res.place["default"] is False
    assert NO_PLACE_WARNING in res.metadata.warnings


def test_half_a_default_still_warns_for_the_other_half():
    res = _describe(NO_LOC, style=Style(default_city="Tokyo"))
    assert res.metadata.description.startswith("TOKYO, UNKNOWN - ")
    assert NO_PLACE_WARNING in res.metadata.warnings


def test_commercial_photos_never_use_the_default():
    res = _describe(NO_LOC, style=TOKYO, editorial=False)
    assert not res.metadata.editorial
    assert res.place["source"] == "unknown" and res.place["default"] is False
    assert NO_PLACE_WARNING not in res.metadata.warnings


def test_forced_editorial_uses_the_default():
    detected = {**DEFAULT_RESPONSE["detected"], "recognizable_people": 2}
    res = _describe(NO_LOC, style=TOKYO, editorial=False, detected=detected)
    assert res.metadata.editorial
    assert res.metadata.description.startswith("TOKYO, JAPAN - ")
    assert res.place["default"] is True


def test_resolve_place_sources():
    assert resolve_place({"city": "Nara", "country": "Japan"})["source"] == "ai"
    assert resolve_place({}, "Tokyo", "Japan")["source"] == "default"
    assert resolve_place({"city": "Nara"}, "Tokyo", "Japan")["source"] == "partly default"
    assert resolve_place({}, "", "")["source"] == "unknown"
    assert editorial_caption({}, None, "x", "Tōkyō", "Japan") == "TOKYO, JAPAN - UNKNOWN: X"


def test_clean_place_part():
    assert clean_place_part("  São   Paulo ") == "Sao Paulo"
    assert clean_place_part(None) == ""
    for bad in ("東京", "Tokyo 2", "x" * 61):
        with pytest.raises(ValueError):
            clean_place_part(bad)


def test_config_key_and_validation():
    assert Config().style.default_place == DefaultPlace()
    cfg = Config.model_validate({"style": {"default_place": {"city": "Tokyo", "country": "Japan"}}})
    assert cfg.style.default_place.city == "Tokyo"
    with pytest.raises(ValueError):
        Config.model_validate({"style": {"default_place": {"city": "東京"}}})


def test_ui_setting_validation_and_override(cfg, store):
    saved = settings_ui.set_ui_setting(
        store, "style", {"default_city": " Tokyo ", "default_country": "Japan"}
    )
    assert saved == {"default_city": "Tokyo", "default_country": "Japan"}
    styled = effective.apply_style_overrides(cfg, store)
    assert styled.style.default_place.city == "Tokyo"
    assert cfg.style.default_place.city == ""
    settings_ui.set_ui_setting(store, "style", {"default_city": "", "default_country": ""})
    assert effective.apply_style_overrides(cfg, store).style.default_place.city == ""
    for bad in ({"default_city": "東京"}, {"default_country": "J" * 61}):
        with pytest.raises(errors.PipelineError):
            settings_ui.set_ui_setting(store, "style", bad)


def _check_with_default(cfg, store, photos_dir, **style):
    bid = _import(cfg, store, photos_dir)
    settings_ui.set_ui_setting(store, "style", style)
    fake = FakeProvider(
        response={
            **DEFAULT_RESPONSE,
            "editorial": True,
            "location": NO_LOC,
            "caption_sentence": "x.",
        }
    )
    check_batch.check_batch(effective.apply_style_overrides(cfg, store), store, bid, llm=fake)
    return bid


def _rows(cfg, store, bid):
    path = reports.write_report(cfg, store, bid)
    return list(csv.DictReader(io.StringIO(path.read_text(encoding="utf-8-sig"))))


def test_place_is_stored_and_reported(cfg, store, photos_dir):
    bid = _check_with_default(cfg, store, photos_dir, default_city="Tokyo", default_country="Japan")
    photo = store.list_photos(bid)[0]
    assert photo.metadata["place"]["source"] == "default"
    assert photo.metadata["description"].startswith("TOKYO, JAPAN - ")
    rows = [r for r in _rows(cfg, store, bid) if r["place_source"]]
    assert rows
    header = list(rows[0])
    t = header.index("title")
    assert header[t - 1] == "status_label"
    assert header[t + 1 : t + 4] == ["place", "place_source", "default_place"]
    assert {r["place"] for r in rows} == {"TOKYO, JAPAN"}
    assert {r["place_source"] for r in rows} == {"default"}
    assert {r["default_place"] for r in rows} == {"yes"}


def test_report_without_place_is_blank(cfg, store, photos_dir):
    bid = _import(cfg, store, photos_dir)
    rows = _rows(cfg, store, bid)
    assert {r["place"] + r["place_source"] + r["default_place"] for r in rows} == {""}


def test_editing_the_caption_clears_the_default_flag(cfg, store, photos_dir):
    bid = _check_with_default(cfg, store, photos_dir, default_city="Tokyo", default_country="Japan")
    photo = store.list_photos(bid)[0]
    workflow_review.update_metadata(cfg, store, photo.id, title="A new title for this photo")
    assert store.get_photo(photo.id).metadata["place"]["default"] is True
    workflow_review.update_metadata(
        cfg, store, photo.id, description="OSAKA, JAPAN - OCTOBER 5, 2026: A street."
    )
    assert "place" not in store.get_photo(photo.id).metadata


def test_settings_save_default_place_needs_csrf(web):  # noqa: F811
    r = web.client.post("/settings/style", data={"default_city": "Tokyo"})
    assert r.status_code == 403
    r = web.post(
        "/settings/style",
        {
            "theme": "",
            "tone": "",
            "always_include": "",
            "default_city": "Tokyo",
            "default_country": "Japan",
        },
    )
    assert r.status_code == 303 and "notice=" in r.headers["location"]
    assert web.state.cfg.style.default_place.country == "Japan"
    page = web.get("/settings").text
    assert 'name="default_city" value="Tokyo"' in page
    assert "Check that it is right for each photo" in page
    bad = web.post("/settings/style", {"default_city": "東京"})
    assert "error=" in bad.headers["location"]
    assert web.state.cfg.style.default_place.city == "Tokyo"


def test_wizard_style_step_has_default_place(web):  # noqa: F811
    text = web.get("/welcome?step=5").text
    assert 'name="default_city"' in text and 'name="default_country"' in text


def test_default_place_chip_on_review_and_photo_pages(web, batch):  # noqa: F811
    web.post("/settings/style", {"default_city": "Tokyo", "default_country": "Japan"})
    store = web.state.store
    photo = store.list_photos(batch)[0]
    meta = {
        "description": "TOKYO, JAPAN - OCTOBER 5, 2026: A street.",
        "editorial": True,
        "place": {"city": "TOKYO", "country": "JAPAN", "source": "default", "default": True},
    }
    store.update_photo(photo.id, metadata=meta)
    store.set_status(photo.id, Status.DESCRIBED, force=True)
    review = web.get(f"/batch/{batch}/review?show=all").text
    assert "Default place" in review
    page = web.get(f"/photo/{photo.id}").text
    assert re.search(r'class="chip[^"]*"[^>]*>Default place', page)
    for html in (review, page, web.get("/settings").text):
        assert not re.findall(JARGON, main_text(html), flags=re.I)
    store.update_photo(photo.id, metadata={**meta, "place": {**meta["place"], "default": False}})
    assert "Default place</span>" not in web.get(f"/photo/{photo.id}").text
