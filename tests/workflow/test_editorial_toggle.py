"""Switching editorial on and off rebuilds the description with no AI call."""

from __future__ import annotations

from datetime import date

from shuto.ai.providers.fake import DEFAULT_RESPONSE, FakeProvider
from shuto.core.models import Status
from shuto.metadata import editorial as ed
from shuto.workflow import describing, review

from .test_pipeline import _setup

COMMERCIAL = "A quiet residential street in Tokyo with parked bicycles and utility poles"


def _described(cfg, store, photos_dir, **response):
    batch = _setup(cfg, store, photos_dir)
    llm = FakeProvider(response={**DEFAULT_RESPONSE, "description": COMMERCIAL, **response})
    describing.describe_batch(cfg, store, batch, llm=llm)
    photo = store.list_photos(batch, [Status.DESCRIBED])[0]
    return batch, photo, llm


def test_describe_keeps_the_sentence_and_the_commercial_text(cfg, store, photos_dir):
    _, photo, _ = _described(cfg, store, photos_dir)
    assert photo.metadata["caption_sentence"] == "A quiet residential street with parked bicycles."
    assert photo.metadata["commercial_description"] == COMMERCIAL


def test_round_trip_commercial_editorial_commercial(cfg, store, photos_dir):
    _, photo, llm = _described(cfg, store, photos_dir)
    calls = len(llm.calls)
    on = review.update_metadata(cfg, store, photo.id, description=COMMERCIAL, editorial=True)
    assert on.metadata["editorial"] is True
    assert on.metadata["description"] == (
        "TOKYO, JAPAN - MARCH 4, 2025: A quiet residential street with parked bicycles."
    )
    assert ed.caption_problems(on.metadata["description"], date(2025, 3, 4)) == []
    assert on.status == Status.DESCRIBED
    off = review.update_metadata(
        cfg, store, photo.id, description=on.metadata["description"], editorial=False
    )
    assert off.metadata["editorial"] is False
    assert off.metadata["description"] == COMMERCIAL
    assert off.status == Status.DESCRIBED
    assert len(llm.calls) == calls  # the AI was never asked again


def test_bulk_edit_toggles_both_ways(cfg, store, photos_dir):
    batch, photo, _ = _described(cfg, store, photos_dir)
    done = review.bulk_edit(cfg, store, [photo.id], editorial=True)
    assert done.changed == [photo.id]
    got = store.get_photo(photo.id)
    assert got.metadata["description"].startswith("TOKYO, JAPAN - MARCH 4, 2025: ")
    review.bulk_edit(cfg, store, [photo.id], editorial=False)
    got = store.get_photo(photo.id)
    assert got.metadata["editorial"] is False and got.metadata["description"] == COMMERCIAL


def test_a_hand_typed_sentence_wins_over_the_stored_one(cfg, store, photos_dir):
    _, photo, _ = _described(cfg, store, photos_dir)
    mine = "Cyclists wait at the crossing under bright afternoon light"
    on = review.update_metadata(cfg, store, photo.id, description=mine, editorial=True)
    assert on.metadata["description"] == f"TOKYO, JAPAN - MARCH 4, 2025: {mine}"
    assert on.metadata["commercial_description"] == mine


def test_a_guardrail_forced_edit_builds_the_caption(cfg, store, photos_dir):
    _, photo, _ = _described(cfg, store, photos_dir)
    store.update_photo(photo.id, detected={"screen_content": True})
    edited = review.update_metadata(
        cfg, store, photo.id, keywords=["japan", *photo.metadata["keywords"]]
    )
    assert edited.metadata["editorial"] is True
    assert ed.is_editorial_caption(edited.metadata["description"])
    assert edited.metadata["commercial_description"] == COMMERCIAL
    assert edited.status == Status.DESCRIBED


def test_people_stay_editorial_with_a_message_unless_a_model_release_exists(cfg, store, photos_dir):
    _, photo, _ = _described(cfg, store, photos_dir)
    store.update_photo(photo.id, detected={"recognizable_people": 2})
    still = review.update_metadata(cfg, store, photo.id, description=COMMERCIAL, editorial=False)
    assert still.metadata["editorial"] is True
    assert any("model release" in m for m in still.guardrail["messages"])
    free = review.update_metadata(
        cfg, store, photo.id, description=still.metadata["commercial_description"],
        editorial=False, release_ids=["m-1"],
    )  # fmt: skip
    assert free.metadata["editorial"] is False and free.metadata["description"] == COMMERCIAL


def test_unticking_a_forced_editorial_photo_keeps_the_factual_sentence(cfg, store, photos_dir):
    _, photo, _ = _described(cfg, store, photos_dir)
    on = review.update_metadata(cfg, store, photo.id, description=COMMERCIAL, editorial=True)
    caption = on.metadata["description"]
    store.update_photo(photo.id, detected={"recognizable_people": 2})
    still = review.update_metadata(cfg, store, photo.id, description=caption, editorial=False)
    assert still.metadata["editorial"] is True
    assert still.metadata["description"] == caption
    assert still.metadata["caption_sentence"] == "A quiet residential street with parked bicycles."
    review.bulk_edit(cfg, store, [photo.id], editorial=False)
    got = store.get_photo(photo.id)
    assert got.metadata["description"] == caption


def test_the_default_place_fills_a_missing_place_and_a_missing_date_blocks_nothing_silently(
    cfg, store, photos_dir
):
    cfg.style.default_place.city = "Kyoto"
    cfg.style.default_place.country = "Japan"
    nowhere = {"city": None, "region": None, "country": None}
    _, photo, _ = _described(cfg, store, photos_dir, location=nowhere)
    on = review.update_metadata(cfg, store, photo.id, description=COMMERCIAL, editorial=True)
    assert on.metadata["description"].startswith("KYOTO, JAPAN - MARCH 4, 2025: ")
    assert on.metadata["place"]["default"] is True
    store.update_photo(photo.id, captured_at=None, metadata={**on.metadata, "file_meta": {}})
    again = review.update_metadata(cfg, store, photo.id, description=COMMERCIAL, editorial=False)
    redo = review.update_metadata(
        cfg, store, photo.id, description=again.metadata["description"], editorial=True
    )
    assert redo.metadata["description"].startswith("KYOTO, JAPAN - UNKNOWN: ")
    assert redo.status == Status.NEEDS_REVIEW  # an UNKNOWN date is never left unflagged
