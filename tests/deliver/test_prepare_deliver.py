import codecs
import csv
import ftplib
import io
from datetime import datetime

import pytest
from PIL import Image

from shuto.deliver import ftps, prepare, send

from ..helpers import write_jpeg

META = {
    "title": "Fuji at dawn",
    "description": "Mount Fuji at dawn",
    "keywords": ["fuji", "dawn", "japan"],
}


def test_safe_filename_unicode_and_collisions():
    taken: set[str] = set()
    pat = "{date:%Y%m%d}-{slug}-{n:03d}"
    when = datetime(2026, 4, 1)
    a = prepare.safe_filename(when, "Café 東京 Ōsaka!", 1, pat, 5, taken)
    assert a == "20260401-cafe-osaka-001.jpg"
    b = prepare.safe_filename(when, "Café 東京 Ōsaka!", 1, pat, 5, taken)
    assert b == "20260401-cafe-osaka-001-002.jpg"
    c = prepare.safe_filename(when, "x", 1, "FIXED", 5, {"fixed.jpg"})
    assert c == "fixed-002.jpg"
    long = prepare.safe_filename(None, "a " * 100, 1, "{slug}-{date:%Y}", 100, set())
    assert (
        len(long) <= 80
        and long.endswith(".jpg")
        and "nodate" in prepare.safe_filename(None, "t", 1, "{date:%Y}-{slug}", 3, set())
    )


def test_release_check():
    ok = {**META, "editorial": False}
    assert prepare.release_check(ok, {}, []) == []
    assert prepare.release_check(ok, {"recognizable_people": True}, [])
    assert prepare.release_check(ok, {"recognizable_people": True}, ["m1"]) == []
    assert prepare.release_check(ok, {"logos": True}, ["m1"])
    ed = {**META, "editorial": True, "description": "Mount Fuji at dawn"}
    assert prepare.release_check(ed, {"recognizable_people": True}, [])
    ed["description"] = "TOKYO, JAPAN - APRIL 1, 2026: Crowd at a crossing"
    assert prepare.release_check(ed, {"recognizable_people": True}, []) == []
    # Y3: UNKNOWN parts and lower-case captions are refused at prepare.
    ed["description"] = "TOKYO, JAPAN - UNKNOWN: Crowd at a crossing"
    assert any("UNKNOWN" in p for p in prepare.release_check(ed, {}, []))
    ed["description"] = "Tokyo, Japan - April 1, 2026: Crowd at a crossing"
    assert prepare.release_check(ed, {}, [])
    ed["description"] = "ZÜRICH, SWITZERLAND - MAY 1, 2026: A blue tram waits at the stop"
    assert prepare.release_check(ed, {}, []) == []
    # G10: a property release does not clear logos (they are trademarks).
    assert prepare.release_check(ok, {"logos": True}, ["property:p1"])
    assert prepare.release_check({"description": "", "keywords": []}, {}, [])


def test_prepare_file_jpeg_and_png(tmp_path):
    src = write_jpeg(tmp_path / "a.jpg", captured=datetime(2026, 4, 1, 8, 30), size=(400, 300))
    before = src.read_bytes()
    out = prepare.prepare_file(src, tmp_path / "out", "x.jpg", META)
    assert src.read_bytes() == before
    assert out.name == "x.jpg" and not list((tmp_path / "out").glob("*.tmp"))
    with Image.open(out) as im:
        assert im.format == "JPEG"
        assert im.getexif().get_ifd(0x8769).get(0x9003) == "2026:04:01 08:30:00"
        assert b"Fuji at dawn" in out.read_bytes()
        assert im.info.get("photoshop")

    png = tmp_path / "b.png"
    with Image.open(src) as im:
        im.save(png, exif=im.getexif().tobytes())
    out2 = prepare.prepare_file(png, tmp_path / "out", "y.jpg", META)
    with Image.open(out2) as im:
        assert im.format == "JPEG"
        assert im.getexif().get_ifd(0x8769).get(0x9003) == "2026:04:01 08:30:00"
    assert b"Mount Fuji at dawn" in out2.read_bytes()


class FakeFtp:
    def __init__(self, fail=(), drop=(), log=None):
        self.fail, self.drop = set(fail), set(drop)
        self.stored: list[str] = []
        self.closed = False

    def storbinary(self, cmd, fh):
        name = cmd.split(" ", 1)[1]
        fh.read()
        if name in self.drop:
            self.drop.discard(name)
            raise ConnectionResetError("dropped")
        if name in self.fail:
            raise ftplib.error_perm("550 no")
        self.stored.append(name)

    def quit(self):
        self.closed = True


def _files(tmp_path, names):
    out = []
    for n in names:
        p = tmp_path / n
        p.write_bytes(b"data")
        out.append(p)
    return out


def test_upload_partial_failure_continues(tmp_path):
    fake = FakeFtp(fail={"b.jpg"})
    results = {}
    send.upload_files(
        send.FtpsCreds("h", "u", "p"),
        _files(tmp_path, ["a.jpg", "b.jpg", "c.jpg"]),
        lambda n, e: results.__setitem__(n, e),
        connect=lambda *a, **k: fake,
    )
    assert fake.stored == ["a.jpg", "c.jpg"]
    assert results["a.jpg"] is None and isinstance(results["b.jpg"], ftplib.error_perm)
    assert results["c.jpg"] is None and fake.closed


def test_upload_reconnects_once_on_drop(tmp_path):
    first, second = FakeFtp(drop={"b.jpg"}), FakeFtp()
    conns = iter([first, second])
    results = {}
    send.upload_files(
        send.FtpsCreds("h", "u", "p"),
        _files(tmp_path, ["a.jpg", "b.jpg", "c.jpg"]),
        lambda n, e: results.__setitem__(n, e),
        connect=lambda *a, **k: next(conns),
    )
    assert first.stored == ["a.jpg"] and second.stored == ["b.jpg", "c.jpg"]
    assert all(v is None for v in results.values())


def test_upload_reconnect_failure_marks_rest_failed(tmp_path):
    first = FakeFtp(drop={"a.jpg"})
    calls = []

    def connect(*a, **k):
        calls.append(1)
        if len(calls) > 1:
            raise OSError("down")
        return first

    results = {}
    send.upload_files(
        send.FtpsCreds("h", "u", "p"),
        _files(tmp_path, ["a.jpg", "b.jpg"]),
        lambda n, e: results.__setitem__(n, e),
        connect=connect,
        sleep=lambda _s: None,
    )
    assert all(isinstance(v, Exception) for v in results.values())


def test_ftps_close_never_raises():
    class Bad:
        def quit(self):
            raise OSError

        def close(self):
            raise OSError

    ftps.close(Bad())


def test_creds_missing_names_both(monkeypatch):
    monkeypatch.setattr(send, "get_secret", lambda name: None)
    with pytest.raises(RuntimeError) as ei:
        send.creds_from_secrets("h")
    assert "SHUTO_FTPS_USER" in str(ei.value) and "SHUTO_FTPS_PASSWORD" in str(ei.value)
    monkeypatch.setattr(send, "get_secret", lambda name: "v")
    c = send.creds_from_secrets("h")
    assert (c.host, c.username, c.password) == ("h", "v", "v")
    assert "v" not in repr(c).replace("host", "")


def test_write_csv_is_one_file_per_batch(tmp_path):
    rows = [
        {
            "filename": "a-001.jpg",
            "description": "Nihon, café",
            "keywords": ["x"],
            "categories": [],
        },
        {
            "filename": "b-002.jpg",
            "description": "TOKYO, JAPAN - OCTOBER 5, 2026: Commuters wait on the platform",
            "keywords": ["y", "z w"],
            "categories": ["Nature", "Travel"],
            "editorial": True,
        },
    ]
    path = send.write_csv("2026-W14/x", rows, tmp_path / "csv")
    assert path.name == send.csv_filename("2026-W14/x")
    assert path.name.endswith("-shutterstock.csv") and "/" not in path.name
    assert list((tmp_path / "csv").iterdir()) == [path]
    raw = path.read_bytes()
    assert not raw.startswith(codecs.BOM_UTF8) and b"\r\r" not in raw  # no BOM, like the sample
    assert raw.decode("utf-8").splitlines()[0] == (
        "Filename,Description,Keywords,Categories,Editorial,Mature content,illustration"
    )
    parsed = list(csv.reader(io.StringIO(raw.decode("utf-8"), newline="")))
    assert parsed[1] == ["a-001.jpg", "Nihon, café", "x", "", "no", "no", "no"]
    assert parsed[2] == [
        "b-002.jpg",
        "TOKYO, JAPAN - OCTOBER 5, 2026: Commuters wait on the platform",
        "y,z w",
        "Nature,Travel",
        "yes",
        "no",
        "no",
    ]
    assert '"y,z w"' in raw.decode("utf-8")  # keywords stay one quoted cell
    again = send.write_csv("2026-W14/x", rows[:1], tmp_path / "csv")
    assert again == path and len(path.read_text(encoding="utf-8").splitlines()) == 2
