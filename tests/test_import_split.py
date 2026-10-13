"""Folders with more than MAX_BATCH_PHOTOS new photos are split into batches."""

from __future__ import annotations

import random

import pytest
import yaml
from PIL import Image

from shuto import pipeline
from shuto.cli import main
from shuto.web import create_app

from .helpers import write_jpeg
from .test_web import Web


def _tiny(path, seed):
    rng = random.Random(seed)
    img = Image.new("RGB", (16, 16))
    img.putdata([tuple(rng.randrange(256) for _ in range(3)) for _ in range(256)])
    img.save(path, "JPEG", quality=90)


def _folder(tmp_path, n, name="trip"):
    d = tmp_path / name
    d.mkdir()
    for i in range(n):
        _tiny(d / f"img{i:04d}.jpg", i)
    return d


def test_600_photos_make_three_batches(cfg, store, tmp_path):
    folder = _folder(tmp_path, 600)
    s = pipeline.import_folder(cfg, store, folder, batch_name="trip")
    assert s.batches == {"trip": 250, "trip (2)": 250, "trip (3)": 100}
    assert s.imported == 600
    first = [p.orig_name for p in store.list_photos("trip")]
    assert first[0] == "img0000.jpg" and first[-1] == "img0249.jpg"
    assert store.list_photos("trip (3)")[0].orig_name == "img0500.jpg"
    assert pipeline.batches_text(s.batches) == (
        "600 photos added in 3 batches: trip: 250, trip (2): 250, trip (3): 100"
    )


def test_exact_and_near_duplicates_found_across_batches(cfg, store, tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline, "MAX_BATCH_PHOTOS", 3)
    d = tmp_path / "trip"
    d.mkdir()
    for i in range(6):
        write_jpeg(d / f"a{i}.jpg", "sharp", size=(200, 150), seed=i + 1)
    (d / "z-copy.jpg").write_bytes((d / "a0.jpg").read_bytes())  # exact copy of the first
    write_jpeg(d / "z-near.jpg", "sharp", size=(200, 150), seed=1, quality=60)  # near copy
    s = pipeline.import_folder(cfg, store, d, batch_name="trip")
    assert s.duplicates == 1
    assert list(s.batches) == ["trip", "trip (2)", "trip (3)"]
    assert s.near_duplicates >= 1
    near = [p for p in store.list_photos(statuses=[pipeline.Status.NEAR_DUPLICATE])]
    assert near and {p.batch_id for p in near} <= set(s.batches)


def test_adding_to_an_existing_batch_never_passes_the_limit(cfg, store, tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline, "MAX_BATCH_PHOTOS", 4)
    pipeline.import_folder(cfg, store, _folder(tmp_path, 3, "one"), batch_name="trip")
    more = tmp_path / "two"
    more.mkdir()
    for i in range(10, 13):
        _tiny(more / f"m{i}.jpg", i)
    s = pipeline.import_folder(cfg, store, more, batch_name="trip")
    assert s.batches == {"trip": 1, "trip (2)": 2}
    assert len(store.list_photos("trip")) == 4


def test_cli_prints_the_split(tmp_path, isolated, capsys, monkeypatch):
    monkeypatch.setattr(pipeline, "MAX_BATCH_PHOTOS", 2)
    conf = tmp_path / "shuto.yaml"
    conf.write_text(
        yaml.safe_dump(
            {
                "workspace": str(tmp_path / "ws"),
                "llm": {"provider": "fake"},
                "import": {"batch_by": "folder"},
            }
        ),
        encoding="utf-8",
    )
    folder = _folder(tmp_path, 5)
    assert main(["--config", str(conf), "import", str(folder)]) == 0
    out = capsys.readouterr().out
    assert "5 photos added in 3 batches: trip: 2, trip (2): 2, trip (3): 1" in out


def test_web_result_message_and_links(cfg, tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline, "MAX_BATCH_PHOTOS", 2)
    folder = _folder(tmp_path, 5)
    w = Web(create_app(cfg))
    try:
        job, text = w.run_job("/import", {"path": str(folder), "batch_name": "trip"})
        assert job.state == "done", job.error
        assert "5 photos added in 3 batches: trip: 2, trip (2): 2, trip (3): 1" in text
        assert text.count("Open batch") == 3
        assert 'href="/batch/trip%20%282%29"' in text
        page = w.get("/batch/trip%20%282%29")  # sends you to the right step of that batch
        assert page.status_code == 303 and "/batch/trip%20%282%29/" in page.headers["location"]
    finally:
        w.state.runner.shutdown()


def test_check_never_sends_more_than_the_limit(cfg, store, monkeypatch):
    monkeypatch.setattr(pipeline, "MAX_BATCH_PHOTOS", 2)
    store.upsert_batch("b")
    for i in range(5):
        pid = store.add_photo(sha256=f"h{i}", src_path="/x", orig_name=f"{i}.jpg", batch_id="b")
        store.set_status(pid, pipeline.Status.READY, force=True)
    assert len(pipeline._describe_targets(store, "b", None)) == 2


@pytest.mark.parametrize("n", [250])
def test_limit_constant(n):
    assert pipeline.MAX_BATCH_PHOTOS == n
