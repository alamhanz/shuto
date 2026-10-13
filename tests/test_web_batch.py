"""Batch-flow pages: stage redirects, review filters, bulk edit, deliver and safe redirects."""

from __future__ import annotations

import html
import re
import time

import pytest
from fastapi.testclient import TestClient

from shuto import pipeline
from shuto.models import Status
from shuto.web import create_app

from .conftest import FakeOllama

BASE = "http://127.0.0.1:8765"
JARGON = ("dhash", "iptc", "csrf", "ftps", "cull", "guardrail")


class Web:
    """TestClient wrapper that adds the CSRF token to posts and waits for jobs."""

    def __init__(self, app):
        self.app = app
        self.state = app.state.web
        self.client = TestClient(app, base_url=BASE, follow_redirects=False)

    def get(self, path, **kw):
        return self.client.get(path, **kw)

    def post(self, path, data=None, **kw):
        return self.client.post(path, data={**(data or {}), "csrf_token": self.state.csrf}, **kw)

    def run_job(self, path, data=None):
        r = self.post(path, data)
        assert r.status_code == 200, r.text
        match = re.search(r'id="job-(\d+)"', r.text)
        assert match, r.text
        return self.state.runner.wait(int(match.group(1)), timeout=60)


def visible(html: str) -> str:
    return re.sub(r"<[^>]+>", " ", html).lower()


@pytest.fixture
def web(cfg, monkeypatch):
    monkeypatch.setattr(pipeline, "ollama_client", lambda c: FakeOllama(pulled=["gemma3:4b"]))
    w = Web(create_app(cfg))
    yield w
    w.state.runner.shutdown()


@pytest.fixture
def batch(web, photos_dir):
    assert web.run_job("/import", {"path": str(photos_dir)}).state == "done"
    (b,) = [x["id"] for x in pipeline.batch_report(web.state.store)]
    return b


@pytest.fixture
def described(web, batch):
    assert web.run_job(f"/batch/{batch}/cull").state == "done"
    job = web.run_job(f"/batch/{batch}/describe")
    assert job.state == "done", job.error
    return batch


def _photos(web, batch, *statuses):
    return web.state.store.list_photos(batch, list(statuses) or None)


# ------------------------------------------------------------------ stage redirects


@pytest.mark.parametrize(
    ("statuses", "target"),
    [
        ([], "review"),
        ([Status.IMPORTED], "check"),
        ([Status.READY, Status.DESCRIBED], "check"),
        ([Status.DESCRIBED], "review"),
        ([Status.NEEDS_REVIEW, Status.APPROVED], "review"),
        ([Status.APPROVED], "upload"),
        ([Status.PREPARED], "upload"),
        ([Status.EXPORTED], "submit"),
        ([Status.SUBMITTED], "submit"),
    ],
)
def test_batch_redirects_to_stage(web, statuses, target):
    store = web.state.store
    store.upsert_batch("b1")
    for i, status in enumerate(statuses):
        pid = store.add_photo(
            sha256=f"{i:064d}", src_path=f"/x/{i}.jpg", orig_name=f"{i}.jpg", batch_id="b1"
        )
        store.set_status(pid, status, force=True)
    r = web.get("/batch/b1")
    assert r.status_code == 303 and r.headers["location"] == f"/batch/b1/{target}"
    assert web.get(r.headers["location"]).status_code == 200


def test_unknown_batch_is_404(web):
    for page in ("", "/check", "/review", "/upload", "/submit", "/bulk-edit"):
        assert web.get(f"/batch/nope{page}").status_code == 404


# ------------------------------------------------------------------ check


def test_check_page_and_job(web, batch):
    text = web.get(f"/batch/{batch}/check").text
    assert "Check all 5 photos" in text and "AI in use" in text
    assert not any(word in visible(text) for word in JARGON)
    job = web.run_job(f"/batch/{batch}/check")
    assert job.state == "done", job.error
    counts = pipeline.batch_summary(web.state.store, batch)["counts"]
    assert counts.get("imported", 0) == 0 and counts.get("described", 0) > 0
    assert "Nothing new to check" in web.get(f"/batch/{batch}/check").text


def test_check_without_photos_or_busy(web, batch):
    started = web.state.runner.submit("x", batch, lambda ctx: time.sleep(0.3))
    r = web.post(f"/batch/{batch}/check")
    assert "already has job" in r.text
    web.state.runner.wait(started)


# ------------------------------------------------------------------ review


def test_review_filters_use_buckets(web, described):
    store = web.state.store
    good = _photos(web, described, Status.DESCRIBED)
    aside = _photos(web, described, Status.CULLED)
    assert good and aside
    store.set_status(good[0].id, Status.NEEDS_REVIEW, force=True)
    base = f"/batch/{described}/review"

    default = web.get(base).text  # something needs a look, so that is the default
    assert good[0].orig_name in default and aside[0].orig_name not in default
    assert 'aria-current="true"' in default
    assert aside[0].orig_name in web.get(f"{base}?show=aside").text
    assert good[0].orig_name not in web.get(f"{base}?show=good").text
    everything = web.get(f"{base}?show=all").text
    assert aside[0].orig_name in everything and good[0].orig_name in everything
    assert not any(word in visible(everything) for word in JARGON)
    assert f"/photo/{good[0].id}?back=all" in everything
    assert web.get(f"{base}?show=bogus").status_code == 200


def test_review_default_is_good_and_empty_states(web, described):
    base = f"/batch/{described}/review"
    assert "Approve all that look good" in web.get(base).text
    assert "Nothing needs a look" in web.get(f"{base}?show=attention").text
    pipeline.approve(web.state.cfg, web.state.store, batch_id=described)
    page = web.get(base).text
    assert "Go to Upload" in page and "approved)" in page


def test_bulk_approve_and_send_back(web, described):
    store = web.state.store
    ids = [str(p.id) for p in _photos(web, described, Status.DESCRIBED)]
    r = web.post(f"/batch/{described}/bulk", {"action": "approve", "ids": ids, "show": "good"})
    assert r.status_code == 303 and "/review?show=good" in r.headers["location"]
    assert not _photos(web, described, Status.DESCRIBED)
    web.post(f"/batch/{described}/bulk", {"action": "ready", "ids": ids[:1]})
    assert store.get_photo(int(ids[0])).status == Status.READY
    r = web.post(f"/batch/{described}/bulk", {"action": "approve"})
    assert "Select" in r.headers["location"] or "Select%20at" in r.headers["location"]


def test_bulk_edit_fragment(web, described):
    ids = [p.id for p in _photos(web, described, Status.DESCRIBED)][:2]
    frag = web.get(f"/batch/{described}/bulk-edit", params={"ids": ids, "show": "good"})
    assert frag.status_code == 200 and "Edit 2 selected photos" in frag.text
    assert 'name="ids"' in frag.text and "Add these keywords" in frag.text
    assert "Tick some photos first" in web.get(f"/batch/{described}/bulk-edit").text


def test_bulk_edit_changes_keywords_and_categories(web, described):
    store = web.state.store
    p = _photos(web, described, Status.DESCRIBED)[0]
    old = store.get_photo(p.id).metadata["keywords"]
    data = {
        "action": "edit",
        "ids": [str(p.id)],
        "add_keywords": "zenmoment, tokyo night",
        "remove_keywords": old[0],
        "categories_mode": "set",
        "categories": ["Nature", "Objects"],
        "kind": "keep",
    }
    r = web.post(f"/batch/{described}/bulk", data)
    assert r.status_code == 303 and "Changed%201" in r.headers["location"]
    after = store.get_photo(p.id)
    kw = [k.lower() for k in after.metadata["keywords"]]
    assert "zenmoment" in kw and "tokyo night" in kw and old[0].lower() not in kw
    assert after.metadata["categories"] == ["Nature", "Objects"]
    assert after.guardrail is not None  # rules ran again


def test_bulk_edit_editorial_switch_flags_caption(web, described):
    store = web.state.store
    p = _photos(web, described, Status.DESCRIBED)[0]
    r = web.post(
        f"/batch/{described}/bulk",
        {"action": "edit", "ids": [str(p.id)], "kind": "editorial"},
    )
    assert r.status_code == 303
    after = store.get_photo(p.id)
    assert after.metadata["editorial"] is True
    assert after.status in (Status.NEEDS_REVIEW, Status.BLOCKED)
    assert after.guardrail["messages"]


def test_bulk_edit_validates(web, described):
    pid = str(_photos(web, described, Status.DESCRIBED)[0].id)
    nothing = web.post(f"/batch/{described}/bulk", {"action": "edit", "ids": [pid]})
    assert "at%20least%20one%20change" in nothing.headers["location"]
    three = web.post(
        f"/batch/{described}/bulk",
        {
            "action": "edit",
            "ids": [pid],
            "categories_mode": "set",
            "categories": ["Nature", "Objects", "Arts"],
        },
    )
    assert (
        "at%20most%202" in three.headers["location"].lower()
        or "2%20categories" in (three.headers["location"])
    )
    assert web.post(f"/batch/{described}/bulk", {"action": "zzz"}).status_code == 400


# ------------------------------------------------------------------ photo page


def test_photo_page_filter_navigation_and_labels(web, described):
    photos = _photos(web, described, Status.DESCRIBED)
    first = photos[0]
    page = web.get(f"/photo/{first.id}?back=good").text
    assert "What shuto noticed" in page and "Commercial or Editorial" in page
    assert not any(word in visible(page) for word in JARGON)
    assert f"/photo/{photos[1].id}?back=good" in page  # next stays in the same filter
    assert f"/batch/{described}/review?show=good" in page
    assert 'name="next"' in page
    stray = web.get(f"/photo/{first.id}?back=<script>").text
    assert "<script>" not in stray


def test_approve_and_next_never_leaves_the_site(web, described):
    photos = _photos(web, described, Status.DESCRIBED)
    a, b, c = photos[0], photos[1], photos[2]
    r = web.post(f"/photo/{a.id}/approve", {"next": str(b.id), "back": "good"})
    assert r.headers["location"].startswith(f"/photo/{b.id}?back=good")
    for evil in ("http://evil.example/", "//evil.example", "/etc/passwd", "-1", "99999999", "1e3"):
        web.state.store.set_status(b.id, Status.DESCRIBED, force=True)
        r = web.post(f"/photo/{b.id}/approve", {"next": evil})
        loc = r.headers["location"]
        assert loc.startswith(f"/batch/{described}/review"), (evil, loc)
    other = web.post(f"/photo/{c.id}/approve", {"next": "x", "back": "https://evil"})
    assert other.headers["location"].startswith(f"/batch/{described}/review")
    assert "evil" not in other.headers["location"]


def test_approve_saves_edits_first(web, described):
    a, b = _photos(web, described, Status.DESCRIBED)[:2]
    page = web.get(f"/photo/{a.id}?back=good").text
    assert 'id="approve-form"' in page and f'action="/photo/{a.id}/approve"' in page
    assert f'formaction="/photo/{a.id}"' in page and "Save and approve" in page
    r = web.post(
        f"/photo/{a.id}/approve",
        {
            "description": "A quiet street at dusk.",
            "keywords": "street, dusk, quiet, city, evening, lanterns, walk",
            "next": str(b.id),
            "back": "good",
        },
    )
    assert r.headers["location"].startswith(f"/photo/{b.id}?back=good")
    saved = web.state.store.get_photo(a.id)
    assert saved.status == Status.APPROVED
    assert saved.metadata["description"] == "A quiet street at dusk."
    assert "lanterns" in saved.metadata["keywords"]


def test_approve_with_bad_edit_is_refused(web, described):
    a = _photos(web, described, Status.DESCRIBED)[0]
    r = web.post(
        f"/photo/{a.id}/approve",
        {"description": "A street.", "keywords": "street", "categories": ["a", "b", "c"]},
    )
    assert "error=" in r.headers["location"]
    assert web.state.store.get_photo(a.id).status == Status.DESCRIBED


# ------------------------------------------------------------------ upload and submit


def test_upload_page_login_and_summary(web, described, monkeypatch):
    pipeline.approve(web.state.cfg, web.state.store, batch_id=described)
    page = web.get(f"/batch/{described}/upload").text
    assert "Shutterstock upload login" in page and 'action="/settings/ftps"' in page
    assert f'value="/batch/{described}/upload"' in page
    assert "Upload whole batch" in page and "MB" in page
    assert "only to send these photos" in page
    assert f'href="/batch/{described}">Skip for now' in page
    assert not any(word in visible(page) for word in JARGON)
    monkeypatch.setattr(
        pipeline,
        "ftps_login_status",
        lambda cfg: {"user_set": True, "password_set": True, "keyring_ok": True},
    )
    saved = web.get(f"/batch/{described}/upload").text
    assert "Your login is saved" in saved and "Use a different login" in saved
    assert "Skip for now" not in saved


def test_deliver_requires_confirm_and_runs_one_job(web, described, monkeypatch):
    pipeline.approve(web.state.cfg, web.state.store, batch_id=described)
    calls = []

    def fake_deliver(cfg, store, batch_id, **kw):
        calls.append(kw)
        return pipeline.DeliverSummary(
            pipeline.PrepareSummary(prepared=["a"]),
            pipeline.UploadSummary(uploaded=["a"]),
            pipeline.ExportSummary(),
        )

    monkeypatch.setattr(pipeline, "deliver_batch", fake_deliver)
    refused = web.post(f"/batch/{described}/deliver")
    assert "Tick the box" in refused.text and not calls
    wrong = web.post(f"/batch/{described}/deliver", {"confirm": "no"})
    assert "Tick the box" in wrong.text and not calls
    job = web.run_job(f"/batch/{described}/deliver", {"confirm": "yes"})
    assert job.state == "done" and calls and calls[0]["confirm"] is True
    started = web.state.runner.submit("x", described, lambda ctx: time.sleep(0.3))
    busy = web.post(f"/batch/{described}/deliver", {"confirm": "yes"})
    assert "already has job" in busy.text
    web.state.runner.wait(started)


def test_deliver_with_nothing_approved(web, described):
    r = web.post(f"/batch/{described}/deliver", {"confirm": "yes"})
    assert "No approved photos" in r.text


def test_deliver_upload_failure_is_plain(web, described, monkeypatch):
    pipeline.approve(web.state.cfg, web.state.store, batch_id=described)
    monkeypatch.setattr(
        pipeline,
        "deliver_batch",
        lambda *a, **k: pipeline.DeliverSummary(
            pipeline.PrepareSummary(),
            pipeline.UploadSummary(failed={"a.jpg": "530 Login incorrect"}),
        ),
    )
    job = web.run_job(f"/batch/{described}/deliver", {"confirm": "yes"})
    assert job.state == "failed"
    assert "did not accept the upload login" in job.error and "Try again" in job.error


def test_deliver_partial_success_finishes_and_writes_csv(web, described, monkeypatch):
    pipeline.approve(web.state.cfg, web.state.store, batch_id=described)
    monkeypatch.setattr(
        pipeline,
        "deliver_batch",
        lambda *a, **k: pipeline.DeliverSummary(
            pipeline.PrepareSummary(prepared=["a"], problems={7: ["Needs a title."]}),
            pipeline.UploadSummary(uploaded=["a"]),
        ),
    )
    monkeypatch.setattr(pipeline, "export_batch", lambda *a, **k: pipeline.ExportSummary())
    job = web.run_job(f"/batch/{described}/deliver", {"confirm": "yes"})
    assert job.state == "done"
    assert "Uploaded 1; 1 need a fix in Review" in web.state.runner.result(job.id)


def test_submit_page_and_marking(web, described, tmp_path):
    store = web.state.store
    photo = _photos(web, described, Status.DESCRIBED)[0]
    csv = tmp_path / "ws" / "exports" / "commercial.csv"
    csv.parent.mkdir(parents=True, exist_ok=True)
    csv.write_text("Filename\n", encoding="utf-8")
    store.update_photo(photo.id, export_file=str(csv))
    store.set_status(photo.id, Status.EXPORTED, force=True)
    page = web.get(f"/batch/{described}/submit").text
    assert "submit.shutterstock.com" in page and "I submitted this batch" in page
    assert "Download commercial.csv" in page and 'alt="Screenshot placeholder' in page
    text = html.unescape(re.sub(r"<[^>]+>", " ", page))
    assert "Portfolio → Not submitted" in text and "Upload CSV" in text
    assert "Portfolio → Pending" in text and "Submit content" in text
    assert "commercial.csv" in text and str(web.state.cfg.workspace_path("batches")) in text
    assert 'rel="noopener noreferrer"' in page and "waiting there" not in text
    assert not any(word in visible(page) for word in JARGON)
    r = web.post(f"/batch/{described}/submitted")
    assert r.status_code == 303
    assert store.get_photo(photo.id).status == Status.SUBMITTED
    again = web.post(f"/batch/{described}/submitted")
    assert "Nothing" in again.headers["location"] or "Nothing%20is" in again.headers["location"]


# ------------------------------------------------------------------ legacy


def test_legacy_routes_still_work(web, described):
    assert web.post(f"/batch/{described}/bulk", {"action": "approve_all"}).status_code == 303
    job = web.run_job(f"/batch/{described}/prepare")
    assert job.state == "done"
    assert web.post(f"/batch/{described}/format-disk").status_code == 404
    assert "Tick the box" in web.post(f"/batch/{described}/upload").text


def test_deliver_cancel_is_truthful(web, described, monkeypatch):
    pipeline.approve(web.state.cfg, web.state.store, batch_id=described)
    seen = {}

    def fake_deliver(cfg, store, batch_id, **kw):
        seen.update(kw)
        kw["progress"](1, 3, "Step 2 of 2 · Uploading photo 1 of 2")
        return pipeline.DeliverSummary(
            pipeline.PrepareSummary(prepared=["a", "b"]),
            pipeline.UploadSummary(uploaded=["a"], stopped=1),
        )

    monkeypatch.setattr(pipeline, "deliver_batch", fake_deliver)
    job = web.run_job(f"/batch/{described}/deliver", {"confirm": "yes"})
    assert callable(seen["cancel"])
    # cancel was never asked for, so this ends as done; the text is still the truthful one
    assert (
        web.state.runner.result(job.id)
        == "Upload stopped: 1 of 2 sent. The other 1 are ready to send later."
    )


def test_deliver_lost_internet_is_plain(web, described, monkeypatch):
    from shuto import ftps

    pipeline.approve(web.state.cfg, web.state.store, batch_id=described)
    lost = ftps.lost_message(2)
    monkeypatch.setattr(
        pipeline,
        "deliver_batch",
        lambda *a, **k: pipeline.DeliverSummary(
            pipeline.PrepareSummary(), pipeline.UploadSummary(failed={"a": lost, "b": lost})
        ),
    )
    job = web.run_job(f"/batch/{described}/deliver", {"confirm": "yes"})
    assert job.state == "failed" and "press Upload to send them" in job.error


def test_pages_show_the_files_note_and_path(web, described):
    folder = str(pipeline.batch_dir(web.state.cfg, described))
    for page in ("review", "submit"):
        text = web.get(f"/batch/{described}/{page}").text
        assert "These files are also saved on this computer in:" in text
        assert "Open folder" in text and "Download report" in text
        assert f"/batch/{described}/open-folder" in text
        assert folder in text.replace("&amp;", "&")


def test_open_folder_needs_csrf_and_opens_only_the_batch_folder(web, described, monkeypatch):
    from shuto import openfolder

    opened = []
    monkeypatch.setattr(openfolder, "open_folder", lambda p: opened.append(p))
    assert web.get(f"/batch/{described}/open-folder").status_code in (404, 405)
    assert web.client.post(f"/batch/{described}/open-folder", data={}).status_code == 403
    assert not opened
    r = web.post(
        f"/batch/{described}/open-folder",
        {"page": "review", "path": "C:/Windows", "folder": "/"},
    )
    assert r.status_code == 303 and "/review" in r.headers["location"]
    assert opened == [pipeline.batch_dir(web.state.cfg, described)] and opened[0].is_dir()
    other = web.post(f"/batch/{described}/open-folder", {"page": "https://evil.example"})
    assert other.headers["location"].startswith(f"/batch/{described}")
    assert "evil" not in other.headers["location"]
    assert web.post("/batch/nope/open-folder").status_code == 404


def test_open_folder_fails_softly(web, described, monkeypatch):
    from shuto import openfolder

    monkeypatch.setattr(openfolder, "open_folder", lambda p: openfolder.NO_DESKTOP)
    r = web.post(f"/batch/{described}/open-folder", {"page": "submit"})
    assert r.status_code == 303 and "error=" in r.headers["location"]


def test_old_export_files_stay_downloadable(web, described):
    photo = _photos(web, described, Status.DESCRIBED)[0]
    old_csv = web.state.cfg.workspace_path("exports", "old.csv")
    old_csv.parent.mkdir(parents=True)
    old_csv.write_text("Filename\n", encoding="utf-8")
    web.state.store.update_photo(photo.id, export_file=str(old_csv))
    r = web.get(f"/batch/{described}/exports/old.csv")
    assert r.status_code == 200 and r.text.startswith("Filename")
