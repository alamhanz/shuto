"""The Check job panel shows the step text, not a bare 'N of 1000'."""

from __future__ import annotations

import threading
import time

from shuto import pipeline
from shuto.web import create_app

from .test_web import Web


def test_job_panel_shows_step_text_while_running(cfg):
    w = Web(create_app(cfg))
    release = threading.Event()

    def run(job):
        job.progress_text(300, pipeline.CHECK_SCALE, "Step 2 of 2 · Writing titles & keywords 1/3")
        release.wait(30)
        return "ok"

    try:
        job_id = w.state.runner.submit("check", "b", run, total=pipeline.CHECK_SCALE)
        for _ in range(500):
            if w.state.runner.text(job_id):
                break
            time.sleep(0.01)
        text = w.get(f"/jobs/{job_id}").text
        assert "Step 2 of 2 · Writing titles &amp; keywords 1/3" in text
        assert "of 1000" not in text
        release.set()
        w.state.runner.wait(job_id)
        assert w.state.runner.text(job_id) == ""
    finally:
        release.set()
        w.state.runner.shutdown()
