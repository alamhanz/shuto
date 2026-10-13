"""Add photos: the batch name starts as the folder name."""

from __future__ import annotations

import re

from shuto import pipeline
from shuto.web import create_app

from .test_web import Web


def test_batch_name_defaults_to_last_folder_name(cfg, tmp_path):
    w = Web(create_app(cfg))
    try:
        empty = w.get("/add").text
        assert 'id="batch_name"' in empty and 'value=""' in empty
        assert "Defaults to the folder name. Change it if you like." in empty
        folder = tmp_path / "Kyoto walk"
        folder.mkdir()
        pipeline.set_ui_setting(w.state.store, "last_folder", str(folder) + "/")
        page = w.get("/add").text
        tag = re.search(r'<input id="batch_name"[^>]*>', page).group(0)
        assert 'value="Kyoto walk"' in tag and 'data-auto="1"' in tag
        assert 'id="path"' in page
    finally:
        w.state.runner.shutdown()


def test_empty_name_still_falls_back_to_the_folder_name(cfg, photos_dir):
    w = Web(create_app(cfg))
    try:
        job, _ = w.run_job("/import", {"path": str(photos_dir), "batch_name": ""})
        assert job.state == "done", job.error
        ids = [b["id"] for b in pipeline.batch_report(w.state.store)]
        assert len(ids) == 1 and "photos" in ids[0]
    finally:
        w.state.runner.shutdown()
