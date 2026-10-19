"""An uploaded photo with a bad editorial caption is left out of the CSV; the rest is written."""

from __future__ import annotations

from shuto.core.models import Status
from shuto.workflow import exporting

GOOD = "SHIBUYA, TOKYO, JAPAN - OCTOBER 5, 2026: Commuters wait on the platform at the station."
BAD = "A quiet residential street in Tokyo with parked bicycles"


def _uploaded(store, n, description, editorial=True):
    pid = store.add_photo(
        sha256=f"s{n}", src_path=f"/x/{n}.jpg", orig_name=f"{n}.jpg", batch_id="b"
    )
    store.update_photo(
        pid,
        metadata={
            "description": description,
            "keywords": ["japan"],
            "categories": ["Travel"],
            "editorial": editorial,
        },
        submit_name=f"p{n}.jpg",
    )
    store.set_status(pid, Status.UPLOADED, force=True)
    return pid


def test_bad_caption_row_is_left_out_with_a_reason_and_the_rest_is_written(cfg, store):
    store.upsert_batch("b")
    good = _uploaded(store, 0, GOOD)
    bad = _uploaded(store, 1, BAD)
    s = exporting.export_batch(cfg, store, "b")
    assert [r["filename"] for r in s.rows] == ["p0.jpg"]
    assert set(s.skipped) == {bad}
    assert "1.jpg" in s.skipped[bad] and "caption" in s.skipped[bad]
    assert "1 photo(s) left out of the CSV" in s.skipped_text()
    csv_text = s.files[0].read_text(encoding="utf-8")
    assert "p0.jpg" in csv_text and "p1.jpg" not in csv_text
    assert store.get_photo(good).status == Status.EXPORTED
    assert store.get_photo(bad).status == Status.UPLOADED


def test_only_bad_rows_write_no_file_but_explain(cfg, store):
    store.upsert_batch("b")
    _uploaded(store, 0, BAD)
    s = exporting.export_batch(cfg, store, "b")
    assert not s.files and s.skipped and "caption" in s.skipped_text()
