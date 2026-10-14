"""One folder per batch: where the files land, old locations, the Open folder helper."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

from shuto.ai.providers.fake import FakeProvider
from shuto.cli import main
from shuto.core import paths
from shuto.core.models import Status
from shuto.system import openfolder
from shuto.workflow import culling, delivering, describing, importing, review

from ..deliver.test_prepare_deliver import FakeFtp


def _approved(cfg, store, photos_dir):
    (batch,) = importing.import_folder(cfg, store, photos_dir).batches
    culling.cull_batch(cfg, store, batch)
    describing.describe_batch(cfg, store, batch, llm=FakeProvider("fake"))
    review.approve(cfg, store, batch_id=batch)
    return batch


def test_files_land_in_the_batch_folder(cfg, store, photos_dir, monkeypatch):
    monkeypatch.setenv("SHUTO_FTPS_USER", "u")
    monkeypatch.setenv("SHUTO_FTPS_PASSWORD", "p")
    batch = _approved(cfg, store, photos_dir)
    delivering.deliver_batch(cfg, store, batch, confirm=True, connect=lambda *a, **k: FakeFtp())
    folder = paths.batch_dir(cfg, batch)
    assert folder.parent == cfg.workspace_path("batches")
    assert (folder / "report.csv").is_file()
    assert list(folder.glob("*-shutterstock.csv"))
    assert len(list((folder / "prepared").glob("*.jpg"))) == 3
    for old in ("prepared", "exports", "reports"):
        assert not cfg.workspace_path(old).exists()
    assert not list(photos_dir.glob("*.csv")) and not (photos_dir / "prepared").exists()


def test_old_prepared_folder_is_kept_and_its_names_are_not_reused(
    cfg, store, photos_dir, monkeypatch
):
    batch = _approved(cfg, store, photos_dir)
    old = cfg.workspace_path("prepared", delivering._safe_dir(batch))
    old.mkdir(parents=True)
    legacy = old / "20250304-old-001.jpg"
    legacy.write_bytes(b"x")
    delivering.prepare_batch(cfg, store, batch)
    assert legacy.read_bytes() == b"x"  # never touched or moved
    names = {p.submit_name for p in store.list_photos(batch, [Status.PREPARED])}
    assert len(names) == 3 and "20250304-old-001.jpg" not in names


def test_path_like_batch_ids_cannot_escape(cfg):
    for evil in ("../../etc", "..", "a/../../b", "..\\..\\x", "C:\\Windows", "/abs", ""):
        folder = paths.batch_dir(cfg, evil)
        assert folder.parent == cfg.workspace_path("batches")
        assert folder.name not in ("", ".", "..")
        assert not set(folder.name) & set("/\\:")
    assert paths.batch_dir(cfg, "2025-w10").name == "2025-w10"
    assert paths.batch_dir(cfg, "a b").name != paths.batch_dir(cfg, "a_b").name


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


def test_folder_names_are_readable_and_safe():
    from shuto.deliver.report import folder_name

    assert folder_name("trip (2)") == "trip (2)"
    assert folder_name("2025-w10") == "2025-w10"
    assert folder_name("a  b").startswith("a b-")  # repeated spaces collapse (name changed)
    assert folder_name("  .x. ").startswith("x-")


def test_folder_names_differing_only_by_case_do_not_collide():
    from shuto.deliver.report import folder_name

    a, b = folder_name("Kyoto"), folder_name("kyoto")
    assert a != b and a.lower() != b.lower() and a.startswith("Kyoto-") and b == "kyoto"
    assert folder_name("Kyoto") == a  # stable


@pytest.mark.parametrize("bad", ["nul", "NUL", "con.txt", "COM1", "lpt9", "Aux", "prn.tar.gz"])
def test_windows_reserved_names_are_prefixed(bad):
    from shuto.deliver.report import folder_name

    name = folder_name(bad)
    assert name.startswith("_") and name.split(".")[0].split("-")[0].upper() not in {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        "COM1",
        "LPT9",
    }
    assert folder_name("nullable") == "nullable" and folder_name("com10") == "com10"


def test_folder_names_unicode_dots_empty_and_long():
    from shuto.deliver.report import folder_name

    uni = folder_name("京都 trip")
    assert uni.isascii() and uni.startswith("__ trip-")
    assert folder_name("京都") != folder_name("大阪")  # the hash keeps them apart
    for odd in ("..", "...", "", " ", "/", "a/b"):
        n = folder_name(odd)
        assert n and n not in (".", "..") and not set(n) & set("/\\:")
    assert folder_name("").startswith("batch-") and folder_name("..").startswith("batch-")
    long = folder_name("x" * 200)
    assert long.startswith("x" * 80 + "-") and len(long) == 80 + 9
    assert folder_name("y" * 79 + " " + "z" * 20).startswith("y" * 79 + "-")
