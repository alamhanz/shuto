"""Prepare fixes a hand-edited caption for good; export refuses one that cannot be fixed."""

from __future__ import annotations

from shuto.ai.providers.fake import DEFAULT_RESPONSE, FakeProvider
from shuto.core.models import Status
from shuto.workflow import delivering, describing, exporting

from .test_pipeline import _setup

MESSY = "Shibuya, Tokyo, Japan – March 4th 2025: commuters wait on the platform at the station."
FIXED = "SHIBUYA, TOKYO, JAPAN - MARCH 4, 2025: Commuters wait on the platform at the station."


def _approved_editorial(cfg, store, photos_dir):
    batch = _setup(cfg, store, photos_dir)
    describing.describe_batch(cfg, store, batch, llm=FakeProvider(response=DEFAULT_RESPONSE))
    photo = store.list_photos(batch, [Status.DESCRIBED])[0]
    meta = {**photo.metadata, "editorial": True, "description": MESSY}
    store.update_photo(photo.id, metadata=meta)
    store.set_status(photo.id, Status.APPROVED)
    return batch, photo.id


def test_prepare_writes_the_fixed_caption_back(cfg, store, photos_dir):
    batch, pid = _approved_editorial(cfg, store, photos_dir)
    summary = delivering.prepare_batch(cfg, store, batch, only_ids=[pid])
    assert summary.prepared and not summary.problems
    assert store.get_photo(pid).metadata["description"] == FIXED


def test_prepare_warns_about_releases_on_an_editorial_photo(cfg, store, photos_dir):
    batch, pid = _approved_editorial(cfg, store, photos_dir)
    store.update_photo(pid, release_ids=["m-1"])
    summary = delivering.prepare_batch(cfg, store, batch, only_ids=[pid])
    assert summary.prepared and not summary.problems
    assert "does not accept releases on editorial" in summary.notes[pid][0]
    assert not summary.warnings and summary.warning_text() == ""
    assert "will reject" not in summary.note_text() and "Fix them" not in summary.note_text()


def test_prepare_refuses_a_caption_whose_date_is_not_the_capture_date(cfg, store, photos_dir):
    batch, pid = _approved_editorial(cfg, store, photos_dir)
    photo = store.get_photo(pid)
    store.update_photo(pid, metadata={**photo.metadata, "description": FIXED.replace("4,", "5,")})
    summary = delivering.prepare_batch(cfg, store, batch, only_ids=[pid])
    assert not summary.prepared and "capture date" in summary.problems[pid][0]


def test_export_leaves_a_bad_caption_out_with_a_reason_and_writes_no_file(cfg, store, photos_dir):
    batch, pid = _approved_editorial(cfg, store, photos_dir)
    delivering.prepare_batch(cfg, store, batch, only_ids=[pid])
    store.set_status(pid, Status.UPLOADED)
    photo = store.get_photo(pid)
    store.update_photo(pid, metadata={**photo.metadata, "description": "Crowds at a crossing"})
    got = exporting.export_batch(cfg, store, batch)
    assert not got.files and set(got.skipped) == {pid}
    assert "editorial caption is wrong" in got.skipped_text()
    assert store.get_photo(pid).status == Status.UPLOADED
    store.update_photo(pid, metadata={**photo.metadata, "description": MESSY})
    done = exporting.export_batch(cfg, store, batch)
    assert done.files and FIXED in done.files[0].read_text(encoding="utf-8")
