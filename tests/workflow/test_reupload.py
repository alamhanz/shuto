"""Photos deleted on Shutterstock: send them again (pipeline, web, CLI) and Add anyway on import."""

from __future__ import annotations

import os
import re

import pytest

from shuto import cli
from shuto.ai import selection
from shuto.core import models, paths
from shuto.core.models import Status
from shuto.deliver import ledger
from shuto.settings import ui as settings_ui
from shuto.web import create_app
from shuto.workflow import delivering, importing, reports, resend

from ..helpers import FakeOllama
from ..web.test_web import Web
from .test_pipeline import FakeFTP
from .test_uploaded_memory import _edited_copy, uploaded  # noqa: F401 (fixture)

JARGON = r"dHash|IPTC|CSRF|FTPS|cull|guardrail|ledger|sha256"


def _shas(cfg):
    return {r.sha256 for r in ledger.read_ledger(paths.ledger_path(cfg))}


def _upload_again(cfg, store, batch):
    ftp = FakeFTP()
    s = delivering.upload_batch(cfg, store, batch, confirm=True, connect=lambda *a, **k: ftp)
    return s, ftp


@pytest.fixture
def web(cfg, monkeypatch):
    monkeypatch.setattr(selection, "ollama_client", lambda c: FakeOllama())
    w = Web(create_app(cfg))
    settings_ui.set_ui_setting(w.state.store, "onboarded", True)
    yield w
    w.state.runner.shutdown()


def test_transitions_allow_sending_again():
    for src in (Status.UPLOADED, Status.EXPORTED, Status.SUBMITTED):
        assert Status.PREPARED in models.ALLOWED[src] and Status.APPROVED in models.ALLOWED[src]
    assert not models.ALLOWED[Status.ACCEPTED] and not models.ALLOWED[Status.REJECTED]


def test_reupload_moves_back_and_next_upload_sends_only_those(cfg, store, uploaded):  # noqa: F811
    photos = store.list_photos(uploaded, [Status.UPLOADED])
    chosen, kept = photos[:2], photos[2]
    assert len(_shas(cfg)) == 3
    s = resend.reupload_photos(cfg, store, [p.id for p in chosen], batch_id=uploaded)
    assert set(s.moved) == {p.id for p in chosen} and not s.refused
    for p in chosen:
        now = store.get_photo(p.id)
        assert now.status == Status.PREPARED and now.uploaded_at is None
        assert now.export_file is None and now.exported_at is None
        (ev,) = [e for e in store.events_for(p.id) if e["kind"] == "reupload_requested"]
        assert ev["detail"]["at"] and ev["detail"]["from"] == "uploaded"
    assert store.get_photo(kept.id).status == Status.UPLOADED
    assert _shas(cfg) == {kept.sha256}  # the list of sent photos forgets them
    report = reports.report_bytes(cfg, store, uploaded).decode("utf-8-sig")
    assert report.count("prepared") >= 2
    up, ftp = _upload_again(cfg, store, uploaded)
    assert sorted(up.uploaded) == sorted(p.submit_name for p in chosen)
    assert set(ftp.stored) == {p.submit_name for p in chosen}
    assert len(_shas(cfg)) == 3


def test_reupload_without_a_copy_and_refusals(cfg, store, uploaded):  # noqa: F811
    a, b, c = store.list_photos(uploaded, [Status.UPLOADED])
    store.set_status(c.id, Status.SUBMITTED, force=True)
    resend.record_result(store, c.id, "accepted", cfg=cfg)  # accepted by Shutterstock
    os.remove(a.prepared_path)
    s = resend.reupload_photos(cfg, store, [a.id, b.id, c.id, 9999], batch_id=uploaded)
    assert s.moved == {a.id: Status.APPROVED, b.id: Status.PREPARED}
    assert set(s.refused) == {c.id, 9999} and "already gave an answer" in s.refused[c.id]
    assert store.get_photo(c.id).status == Status.ACCEPTED
    other = resend.reupload_photos(cfg, store, [b.id], batch_id="elsewhere")
    assert not other.moved and "not in this batch" in other.refused[b.id]


def test_web_reupload_needs_csrf_and_confirm(web, cfg, store, uploaded):  # noqa: F811
    first, *_ = store.list_photos(uploaded, [Status.UPLOADED])
    url = f"/batch/{uploaded}/resend"
    assert web.post(url, {"ids": [str(first.id)], "confirm": "yes"}, token=False).status_code == 403
    r = web.post(url, {"ids": [str(first.id)], "page": "submit"})
    assert r.status_code == 303 and "Tick%20the%20box" in r.headers["location"]
    assert r.headers["location"].startswith(f"/batch/{uploaded}/submit")
    r = web.post(url, {"confirm": "yes"})
    assert "Tick%20the%20photos" in r.headers["location"]
    assert store.get_photo(first.id).status == Status.UPLOADED
    r = web.post(url, {"ids": [str(first.id)], "confirm": "yes"})
    assert r.status_code == 303 and r.headers["location"].startswith(f"/batch/{uploaded}/upload")
    assert store.get_photo(first.id).status == Status.PREPARED
    assert first.sha256 not in _shas(cfg)
    assert web.post("/batch/nope/resend", {"confirm": "yes"}).status_code == 404


def test_pages_list_uploaded_photos_with_checkboxes(web, store, uploaded):  # noqa: F811
    photos = store.list_photos(uploaded, [Status.UPLOADED])
    store.set_status(photos[2].id, Status.SUBMITTED, force=True)
    for page in ("upload", "submit"):
        text = web.get(f"/batch/{uploaded}/{page}").text
        assert text.count('name="ids"') == 3 and "upload them again" in text
        assert f'action="/batch/{uploaded}/resend"' in text and 'name="csrf_token"' in text
        assert f'name="page" value="{page}"' in text and 'name="confirm"' in text
        assert "Already marked as submitted" in text
        visible = re.sub(r"<[^>]+>", " ", text)
        assert not re.findall(JARGON, visible, flags=re.I)


def test_import_offers_add_anyway_for_look_alikes(web, store, tmp_path, uploaded):  # noqa: F811
    new = tmp_path / "new"
    _edited_copy(new)
    job, text = web.run_job("/import", {"path": str(new)})
    assert "Add anyway" in text and 'action="/import/add-anyway"' in text and "csrf_token" in text
    (look,) = store.list_photos(statuses=[Status.ALREADY_UPLOADED])
    assert f'name="ids" value="{look.id}"' in text
    assert web.post("/import/add-anyway", {"ids": [str(look.id)]}, token=False).status_code == 403
    r = web.post("/import/add-anyway", {"ids": [str(look.id)]})
    assert r.status_code == 303 and r.headers["location"].startswith("/batch/")
    assert store.get_photo(look.id).status == Status.IMPORTED
    assert "nothing" in web.post("/import/add-anyway", {}).headers["location"].lower()


def test_import_add_anyway_for_exact_copies_sends_them_again(
    web,
    cfg,
    store,
    photos_dir,
    uploaded,  # noqa: F811
):
    job, text = web.run_job("/import", {"path": str(photos_dir)})
    assert "Add anyway" in text and "already uploaded on" in text
    before = store.list_photos(uploaded, [Status.UPLOADED])
    ids = re.findall(r'name="ids" value="(\d+)"', text)
    assert sorted(map(int, ids)) == sorted(p.id for p in before)
    r = web.post("/import/add-anyway", {"ids": ids})
    assert r.status_code == 303 and f"/batch/{uploaded}" in r.headers["location"]
    assert {p.status for p in store.list_photos(uploaded, [Status.PREPARED, Status.UPLOADED])} == {
        Status.PREPARED
    }
    assert not _shas(cfg)
    up, _ = _upload_again(cfg, store, uploaded)
    assert len(up.uploaded) == 3


def test_cli_reupload(cfg, store, uploaded, monkeypatch, capsys):  # noqa: F811
    monkeypatch.setattr("shuto.cli.load_config", lambda *a, **k: cfg)
    monkeypatch.setattr("shuto.cli.find_config_path", lambda *a, **k: None)
    photos = store.list_photos(uploaded, [Status.UPLOADED])
    assert cli.main(["reupload", uploaded]) == 2  # needs --ids or --all
    assert cli.main(["reupload", uploaded, "--all"]) == 1  # needs --yes
    assert "--yes" in capsys.readouterr().out
    assert len(store.list_photos(uploaded, [Status.UPLOADED])) == 3
    assert cli.main(["reupload", uploaded, "--ids", str(photos[0].id), "--yes"]) == 0
    assert "1 photo(s) will be uploaded again" in capsys.readouterr().out
    assert store.get_photo(photos[0].id).status == Status.PREPARED
    assert cli.main(["reupload", uploaded, "--all", "--yes"]) == 0
    assert {p.status for p in store.list_photos(uploaded, [Status.PREPARED, Status.UPLOADED])} == {
        Status.PREPARED
    }
    assert cli.main(["reupload", uploaded, "--all", "--yes"]) == 0
    assert "Nothing to send again" in capsys.readouterr().out


def test_remembered_photo_goes_back_to_ready_when_the_original_exists(cfg, store, photos_dir):
    importing.import_folder(cfg, store, photos_dir, mark_uploaded=True)
    photo = store.list_photos()[0]
    s = resend.reupload_photos(cfg, store, [photo.id])
    assert s.moved == {photo.id: Status.READY}
    assert photo.sha256 not in _shas(cfg)
