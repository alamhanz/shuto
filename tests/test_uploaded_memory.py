"""Already-uploaded memory: history check on import, --mark-uploaded, uploaded.csv ledger."""

from __future__ import annotations

import csv
from datetime import datetime

import pytest

from shuto import cli, ledger, pipeline
from shuto.config import Config
from shuto.llm.fake import FakeProvider
from shuto.models import Status
from shuto.web import create_app

from .conftest import FakeOllama
from .helpers import write_jpeg
from .test_pipeline import FakeFTP
from .test_web import Web


@pytest.fixture
def uploaded(cfg, store, photos_dir, monkeypatch):
    """photos_dir imported, described, prepared and uploaded through a fake FTPS."""
    imp = pipeline.import_folder(cfg, store, photos_dir)
    (batch,) = imp.batches
    pipeline.cull_batch(cfg, store, batch)
    pipeline.describe_batch(cfg, store, batch, llm=FakeProvider("fake"))
    pipeline.approve(cfg, store, batch_id=batch)
    pipeline.prepare_batch(cfg, store, batch)
    monkeypatch.setenv("SHUTO_FTPS_USER", "u")
    monkeypatch.setenv("SHUTO_FTPS_PASSWORD", "p")
    up = pipeline.upload_batch(cfg, store, batch, confirm=True, connect=lambda *a, **k: FakeFTP())
    assert len(up.uploaded) == 3
    return batch


def _edited_copy(folder, name="edited.jpg"):
    """Same picture as a.jpg, re-saved smaller and more compressed (different bytes)."""
    return write_jpeg(
        folder / name,
        "sharp",
        size=(1200, 900),
        seed=1,
        quality=60,
        captured=datetime(2025, 3, 4, 10, 0, 0),
    )


def test_ledger_written_after_upload(cfg, store, uploaded):
    path = pipeline.ledger_path(cfg)
    assert path.name == "uploaded.csv"
    with path.open(encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert list(rows[0]) == list(ledger.COLUMNS)
    assert len(rows) == 3
    photo = store.list_photos(uploaded, [Status.UPLOADED])[0]
    row = next(r for r in rows if r["sha256"] == photo.sha256)
    assert row["submit_name"] == photo.submit_name and row["batch_id"] == uploaded
    assert int(row["dhash"]) == photo.dhash and row["uploaded_at"] == photo.uploaded_at


def test_exact_reimport_gives_friendly_message(cfg, store, photos_dir, uploaded):
    again = pipeline.import_folder(cfg, store, photos_dir)
    assert again.imported == 0 and again.duplicates == 0
    assert len(again.already_uploaded) == 3 and all(a.exact for a in again.already_uploaded)
    first = again.already_uploaded[0]
    photo = store.find_by_submit_name(first.submit_name)
    assert f"already uploaded on {photo.uploaded_at[:10]} as {photo.submit_name}" in first.text()


def test_edited_copy_is_skipped_and_recorded(cfg, store, tmp_path, uploaded):
    new = tmp_path / "new"
    _edited_copy(new)
    write_jpeg(new / "fresh.jpg", "sharp", seed=9)
    imp = pipeline.import_folder(cfg, store, new)
    assert imp.imported == 1
    (hit,) = imp.already_uploaded
    assert hit.name == "edited.jpg" and not hit.exact
    assert "looks like a photo already uploaded" in hit.text()
    (skipped,) = [p for p in store.list_photos() if p.orig_name == "edited.jpg"]
    assert skipped.status == Status.ALREADY_UPLOADED
    (batch,) = imp.batches
    assert pipeline.cull_batch(cfg, store, batch).total == 1  # the skipped copy is not processed
    assert store.get_photo(skipped.id).status == Status.ALREADY_UPLOADED
    # it can still be sent back to ready by hand
    assert pipeline.override_status(store, skipped.id, Status.READY).status == Status.READY


def test_force_imports_look_alikes(cfg, store, tmp_path, uploaded):
    new = tmp_path / "new"
    _edited_copy(new)
    imp = pipeline.import_folder(cfg, store, new, force=True)
    assert imp.imported == 1 and not imp.already_uploaded


def test_mark_uploaded_then_remembered(cfg, store, tmp_path):
    old = tmp_path / "old"
    write_jpeg(old / "x.jpg", "sharp", seed=11)
    write_jpeg(old / "y.jpg", "sharp", seed=12)
    imp = pipeline.import_folder(cfg, store, old, mark_uploaded=True)
    assert imp.marked_uploaded == 2 and imp.imported == 0 and not imp.batches
    photos = store.list_photos()
    assert {p.status for p in photos} == {Status.UPLOADED}
    assert all(p.uploaded_at for p in photos)
    assert len(pipeline.ledger_path(cfg).read_text(encoding="utf-8").splitlines()) == 3
    # an edited copy of one of them is now recognised
    new = tmp_path / "new"
    write_jpeg(new / "x2.jpg", "sharp", size=(1200, 900), seed=11, quality=60)
    again = pipeline.import_folder(cfg, store, new)
    assert len(again.already_uploaded) == 1 and again.imported == 0
    # marking again changes nothing
    assert pipeline.import_folder(cfg, store, old, mark_uploaded=True).marked_uploaded == 0


def test_mark_uploaded_existing_photo(cfg, store, photos_dir):
    pipeline.import_folder(cfg, store, photos_dir)
    imp = pipeline.import_folder(cfg, store, photos_dir, mark_uploaded=True)
    assert imp.marked_uploaded == 5
    assert {p.status for p in store.list_photos()} == {Status.UPLOADED}


def test_ledger_export_import_round_trip_and_idempotent(cfg, store, tmp_path, uploaded):
    out = tmp_path / "backup" / "uploaded.csv"
    assert pipeline.export_ledger(store, out) == 3
    assert out.read_bytes().startswith(b"sha256,")  # UTF-8, no BOM

    cfg2 = Config.model_validate({**cfg.model_dump(mode="json"), "workspace": str(tmp_path / "w2")})
    store2 = pipeline.open_store(cfg2)
    first = pipeline.import_ledger(cfg2, store2, out)
    assert (first.added, first.already_known) == (3, 0)
    again = pipeline.import_ledger(cfg2, store2, out)
    assert (again.added, again.already_known) == (0, 3)
    assert len(store2.list_photos(statuses=[Status.UPLOADED])) == 3
    # the new workspace now recognises look-alikes and keeps its own ledger
    new = tmp_path / "new"
    _edited_copy(new)
    assert len(pipeline.import_folder(cfg2, store2, new).already_uploaded) == 1
    assert pipeline.ledger_path(cfg2).is_file()
    out2 = tmp_path / "again.csv"
    pipeline.export_ledger(store2, out2)
    assert {r.sha256 for r in ledger.read_ledger(out2)} == {
        p.sha256 for p in store.list_photos(statuses=[Status.UPLOADED])
    }


def test_ledger_import_marks_known_photo_and_validates_file(cfg, store, photos_dir, tmp_path):
    pipeline.import_folder(cfg, store, photos_dir)
    photo = store.list_photos()[0]
    path = tmp_path / "l.csv"
    path.write_text(
        "sha256,dhash,orig_name,submit_name,uploaded_at,batch_id\r\n"
        f"{photo.sha256},,{photo.orig_name},,2024-01-02T03:04:05,old\r\n",
        encoding="utf-8",
        newline="",
    )
    res = pipeline.import_ledger(cfg, store, path)
    assert res.marked == 1 and store.get_photo(photo.id).status == Status.UPLOADED
    with pytest.raises(pipeline.PipelineError):
        pipeline.import_ledger(cfg, store, tmp_path / "missing.csv")


def test_existing_ledger_rows_survive_refresh(cfg, store, tmp_path):
    path = pipeline.ledger_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "sha256,dhash,orig_name,submit_name,uploaded_at,batch_id\n"
        "abc,1,k.jpg,k1.jpg,2024-01-01,b\n",
        encoding="utf-8",
    )
    old = tmp_path / "old"
    write_jpeg(old / "x.jpg", "sharp", seed=11)
    pipeline.import_folder(cfg, store, old, mark_uploaded=True)
    shas = {r.sha256 for r in ledger.read_ledger(path)}
    assert "abc" in shas and len(shas) == 2


def test_cli_import_flags_and_ledger(cfg, tmp_path, photos_dir, monkeypatch, capsys):
    monkeypatch.setattr("shuto.cli.load_config", lambda *a, **k: cfg)
    monkeypatch.setattr("shuto.cli.find_config_path", lambda *a, **k: None)
    assert cli.main(["import", str(photos_dir), "--mark-uploaded", "--yes"]) == 0
    assert "Registered 5 photo(s) as already uploaded" in capsys.readouterr().out
    assert cli.main(["import", str(photos_dir)]) == 0
    text = capsys.readouterr().out
    assert "were uploaded before" in text and "already uploaded on" in text
    out_file = tmp_path / "led.csv"
    assert cli.main(["ledger", "export", str(out_file)]) == 0
    assert "Wrote 5" in capsys.readouterr().out
    assert cli.main(["ledger", "export"]) == 0
    assert "uploaded.csv" in capsys.readouterr().out
    assert cli.main(["ledger", "import", str(out_file)]) == 0
    assert "0 added" in capsys.readouterr().out
    assert cli.main(["import", str(photos_dir), "--force"]) == 0
    assert "were uploaded before" not in capsys.readouterr().out
    assert cli.main(["ledger", "import", str(tmp_path / "nope.csv")]) == 1


def test_web_import_shows_message_and_force(cfg, photos_dir, monkeypatch):
    monkeypatch.setattr(pipeline, "ollama_client", lambda c: FakeOllama())
    web = Web(create_app(cfg))
    try:
        job, text = web.run_job("/import", {"path": str(photos_dir), "mark_uploaded": "yes"})
        assert job.state == "done" and "5 remembered as already sent" in text
        job, text = web.run_job("/import", {"path": str(photos_dir)})
        assert "5 look like photos you sent before" in text and "already uploaded on" in text
        assert "Add even photos I sent before" in web.get("/add").text
        job, text = web.run_job("/import", {"path": str(photos_dir), "force": "yes"})
        assert "sent before" not in text and "0 photo(s) added" in text
    finally:
        web.state.runner.shutdown()


def test_force_releases_stored_look_alikes(cfg, store, tmp_path, uploaded):
    new = tmp_path / "new"
    _edited_copy(new)
    first = pipeline.import_folder(cfg, store, new)
    assert len(first.already_uploaded) == 1
    again = pipeline.import_folder(cfg, store, new, force=True)
    assert again.imported == 1 and not again.already_uploaded
    (batch,) = again.batches
    assert pipeline.cull_batch(cfg, store, batch).total == 1


def test_force_with_mark_uploaded_is_rejected(cfg, store, photos_dir, monkeypatch, capsys):
    with pytest.raises(pipeline.PipelineError):
        pipeline.import_folder(cfg, store, photos_dir, force=True, mark_uploaded=True)
    monkeypatch.setattr("shuto.cli.load_config", lambda *a, **k: cfg)
    monkeypatch.setattr("shuto.cli.find_config_path", lambda *a, **k: None)
    assert cli.main(["import", str(photos_dir), "--force", "--mark-uploaded", "--yes"]) == 2
    monkeypatch.setattr("shuto.cli._input", lambda prompt: "n")
    assert cli.main(["import", str(photos_dir), "--mark-uploaded"]) == 1
    assert not store.list_photos()
    web = Web(create_app(cfg))
    try:
        r = web.post("/import", {"path": str(photos_dir), "force": "yes", "mark_uploaded": "yes"})
        assert "not both" in r.text
        assert 'name="mark_uploaded"' in web.get("/add").text
    finally:
        web.state.runner.shutdown()


_SEEDS = {"a.jpg": 1, "b.jpg": 2, "c.jpg": 3}


def test_rejected_wording_and_exact_rejected_stays_known(cfg, store, tmp_path, uploaded):
    photo = store.list_photos(uploaded, [Status.UPLOADED])[0]
    pipeline.export_batch(cfg, store, uploaded)
    pipeline.mark_submitted(store, uploaded)
    pipeline.record_result(store, photo.id, False, "focus")
    again = pipeline.import_folder(cfg, store, tmp_path / "photos")
    assert again.already_known == 3 and len(again.already_uploaded) == 2  # 2 culled + rejected
    assert all(a.name != photo.orig_name for a in again.already_uploaded)
    new = tmp_path / "new"
    write_jpeg(
        new / "copy.jpg", "sharp", size=(1200, 900), seed=_SEEDS[photo.orig_name], quality=60
    )
    (hit,) = pipeline.import_folder(cfg, store, new).already_uploaded
    assert "looks like a photo previously submitted and rejected on" in hit.text()


def test_merge_keeps_dhash_and_corrupt_file_is_backed_up(cfg, store, tmp_path):
    old = [ledger.LedgerRow("a" * 64, 123, "x.jpg")]
    merged = ledger.merge(old, [ledger.LedgerRow("a" * 64, None, "x.jpg", "s.jpg")])
    assert merged[0].dhash == 123 and merged[0].submit_name == "s.jpg"
    path = pipeline.ledger_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\xff\xfe\x00bad\x80")
    folder = tmp_path / "old"
    write_jpeg(folder / "x.jpg", "sharp", seed=11)
    pipeline.import_folder(cfg, store, folder, mark_uploaded=True)
    assert len(ledger.read_ledger(path)) == 1
    assert list(path.parent.glob("uploaded.csv.bad-*"))


def test_formula_cells_are_neutralised_and_round_trip(tmp_path):
    path = tmp_path / "l.csv"
    row = ledger.LedgerRow("b" * 64, 5, "=SUM(A1).jpg", "+x.jpg", "2024-01-01", "@b")
    ledger.write_ledger(path, [row])
    raw = path.read_text(encoding="utf-8")
    assert "'=SUM(A1).jpg" in raw and "'+x.jpg" in raw and "'@b" in raw
    (back,) = ledger.read_ledger(path)
    assert (back.orig_name, back.submit_name, back.batch_id) == ("=SUM(A1).jpg", "+x.jpg", "@b")
