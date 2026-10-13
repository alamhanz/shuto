"""One folder per batch: where the files land, old locations, the Open folder helper."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

from shuto import openfolder, pipeline
from shuto.cli import main
from shuto.llm.fake import FakeProvider
from shuto.models import Status

from .test_prepare_deliver import FakeFtp


def _approved(cfg, store, photos_dir):
    (batch,) = pipeline.import_folder(cfg, store, photos_dir).batches
    pipeline.cull_batch(cfg, store, batch)
    pipeline.describe_batch(cfg, store, batch, llm=FakeProvider("fake"))
    pipeline.approve(cfg, store, batch_id=batch)
    return batch


def test_files_land_in_the_batch_folder(cfg, store, photos_dir, monkeypatch):
    monkeypatch.setenv("SHUTO_FTPS_USER", "u")
    monkeypatch.setenv("SHUTO_FTPS_PASSWORD", "p")
    batch = _approved(cfg, store, photos_dir)
    pipeline.deliver_batch(cfg, store, batch, confirm=True, connect=lambda *a, **k: FakeFtp())
    folder = pipeline.batch_dir(cfg, batch)
    assert folder.parent == cfg.workspace_path("batches")
    assert (folder / "report.csv").is_file()
    assert list(folder.glob("shutterstock-*.csv"))
    assert len(list((folder / "prepared").glob("*.jpg"))) == 3
    for old in ("prepared", "exports", "reports"):
        assert not cfg.workspace_path(old).exists()
    assert not list(photos_dir.glob("*.csv")) and not (photos_dir / "prepared").exists()


def test_old_prepared_folder_is_kept_and_its_names_are_not_reused(
    cfg, store, photos_dir, monkeypatch
):
    batch = _approved(cfg, store, photos_dir)
    old = cfg.workspace_path("prepared", pipeline._safe_dir(batch))
    old.mkdir(parents=True)
    legacy = old / "20250304-old-001.jpg"
    legacy.write_bytes(b"x")
    pipeline.prepare_batch(cfg, store, batch)
    assert legacy.read_bytes() == b"x"  # never touched or moved
    names = {p.submit_name for p in store.list_photos(batch, [Status.PREPARED])}
    assert len(names) == 3 and "20250304-old-001.jpg" not in names


def test_path_like_batch_ids_cannot_escape(cfg):
    for evil in ("../../etc", "..", "a/../../b", "..\\..\\x", "C:\\Windows", "/abs", ""):
        folder = pipeline.batch_dir(cfg, evil)
        assert folder.parent == cfg.workspace_path("batches")
        assert folder.name not in ("", ".", "..")
        assert not set(folder.name) & set("/\\:")
    assert pipeline.batch_dir(cfg, "2025-W10").name == "2025-W10"
    assert pipeline.batch_dir(cfg, "a b").name != pipeline.batch_dir(cfg, "a_b").name


@pytest.mark.parametrize(
    ("platform", "argv"),
    [("darwin", ["open"]), ("linux", ["/usr/bin/xdg-open"])],
)
def test_open_folder_commands(monkeypatch, tmp_path, platform, argv):
    calls = []
    monkeypatch.setattr(openfolder.sys, "platform", platform)
    monkeypatch.setattr(openfolder.shutil, "which", lambda n: "/usr/bin/xdg-open")
    monkeypatch.setattr(subprocess, "Popen", lambda cmd, **k: calls.append(cmd))
    assert openfolder.open_folder(tmp_path) is None
    assert calls == [[*argv, str(tmp_path)]]


def test_open_folder_windows_and_no_desktop(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(openfolder.sys, "platform", "win32")
    monkeypatch.setattr(openfolder.os, "startfile", seen.append, raising=False)
    assert openfolder.open_folder(tmp_path) is None and seen == [str(tmp_path)]
    monkeypatch.setattr(openfolder.sys, "platform", "linux")
    monkeypatch.setattr(openfolder.shutil, "which", lambda n: None)
    assert openfolder.open_folder(tmp_path) == openfolder.NO_DESKTOP


def test_cli_prints_the_batch_folder(tmp_path, isolated, photos_dir, capsys):
    conf = tmp_path / "shuto.yaml"
    conf.write_text(
        yaml.safe_dump({"workspace": str(tmp_path / "ws"), "llm": {"provider": "fake"}}),
        encoding="utf-8",
    )
    assert main(["--config", str(conf), "import", str(photos_dir)]) == 0
    (line,) = [x for x in capsys.readouterr().out.splitlines() if x.startswith("  batch ")]
    batch = line.split()[1].rstrip(":")
    assert main(["--config", str(conf), "report", batch]) == 0
    out = capsys.readouterr().out
    assert "report.csv" in out and "Batch folder: " in out
    assert str(Path(tmp_path / "ws" / "batches")) in out
