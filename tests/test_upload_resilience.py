"""Upload: real Cancel, waiting for the internet, honest progress text."""

from __future__ import annotations

import ftplib

import pytest

from shuto import deliver, ftps, pipeline
from shuto.models import Status

CREDS = deliver.FtpsCreds("h", "u", "p")


class Ftp:
    def __init__(self, on_store=None, drop=()):
        self.stored: list[str] = []
        self.on_store = on_store
        self.drop = set(drop)

    def storbinary(self, cmd, fh):
        name = cmd.split(" ", 1)[1]
        while fh.read(2):
            pass
        if name in self.drop:
            self.drop.discard(name)
            raise ConnectionResetError("dropped")
        self.stored.append(name)
        if self.on_store:
            self.on_store(name)

    def quit(self):
        pass


def _files(tmp_path, n):
    out = []
    for i in range(n):
        p = tmp_path / f"p{i}.jpg"
        p.write_bytes(b"\xff\xd8\xff\xda")  # a JPEG stub: uploads re-check it for GPS
        out.append(p)
    return out


def _prepared_batch(cfg, store, tmp_path, n, batch="b"):
    store.upsert_batch(batch)
    for i, p in enumerate(_files(tmp_path, n)):
        pid = store.add_photo(
            sha256=f"s{i}", src_path=f"/x/IMG_{i}.jpg", orig_name=f"IMG_{i}.jpg", batch_id=batch
        )
        store.update_photo(pid, prepared_path=str(p), submit_name=p.name)
        store.set_status(pid, Status.PREPARED, force=True)
    return batch


@pytest.fixture
def creds(monkeypatch):
    monkeypatch.setenv("SHUTO_FTPS_USER", "u")
    monkeypatch.setenv("SHUTO_FTPS_PASSWORD", "p")


def test_cancel_stops_after_current_file(cfg, store, tmp_path, creds):
    batch = _prepared_batch(cfg, store, tmp_path, 5)
    ftp = Ftp()
    s = pipeline.upload_batch(
        cfg,
        store,
        batch,
        confirm=True,
        cancel=lambda: len(ftp.stored) >= 2,
        connect=lambda *a, **k: ftp,
    )
    assert len(s.uploaded) == 2 and s.stopped == 3 and not s.failed
    assert len(store.list_photos(batch, [Status.UPLOADED])) == 2
    assert len(store.list_photos(batch, [Status.PREPARED])) == 3
    assert s.stopped_text() == "Upload stopped: 2 of 5 sent. The other 3 are ready to send later."
    rest = pipeline.upload_batch(cfg, store, batch, confirm=True, connect=lambda *a, **k: Ftp())
    assert len(rest.uploaded) == 3 and rest.stopped == 0


def test_cancel_after_last_file_is_not_stopped(cfg, store, tmp_path, creds):
    batch = _prepared_batch(cfg, store, tmp_path, 2)
    ftp = Ftp()
    s = pipeline.upload_batch(
        cfg,
        store,
        batch,
        confirm=True,
        cancel=lambda: len(ftp.stored) >= 2,
        connect=lambda *a, **k: ftp,
    )
    assert len(s.uploaded) == 2 and s.stopped == 0


def test_reconnect_fails_twice_then_resumes(tmp_path):
    first, second = Ftp(drop={"p1.jpg"}), Ftp()
    calls, sleeps, texts = [], [], []

    def connect(*a, **k):
        calls.append(1)
        if len(calls) == 1:
            return first
        if len(calls) <= 3:
            raise OSError("down")
        return second

    results = {}
    deliver.upload_files(
        CREDS,
        _files(tmp_path, 3),
        lambda n, e: results.__setitem__(n, e),
        connect=connect,
        sleep=sleeps.append,
        on_wait=texts.append,
    )
    assert first.stored == ["p0.jpg"] and second.stored == ["p1.jpg", "p2.jpg"]
    assert all(v is None for v in results.values())
    assert texts == [
        "Connection lost. Trying again in 5 s (attempt 1 of 5)",
        "Connection lost. Trying again in 15 s (attempt 2 of 5)",
    ]
    assert sum(sleeps) == pytest.approx(20)


def test_reconnect_always_fails_marks_rest_failed(tmp_path):
    first = Ftp(drop={"p1.jpg"})
    calls, sleeps = [], []

    def connect(*a, **k):
        calls.append(1)
        if len(calls) == 1:
            return first
        raise OSError("down")

    results = {}
    deliver.upload_files(
        CREDS,
        _files(tmp_path, 4),
        lambda n, e: results.__setitem__(n, e),
        connect=connect,
        sleep=sleeps.append,
    )
    assert sum(sleeps) == pytest.approx(230)
    assert results["p0.jpg"] is None
    for name in ("p1.jpg", "p2.jpg", "p3.jpg"):
        assert str(results[name]) == (
            "The internet connection was lost. 3 photos were not sent; press Upload to send them."
        )
    assert "1 photo was not sent" in ftps.lost_message(1)


def test_server_refusal_on_reconnect_is_not_retried(tmp_path):
    first = Ftp(drop={"p0.jpg"})
    calls, sleeps = [], []

    def connect(*a, **k):
        calls.append(1)
        if len(calls) == 1:
            return first
        raise ftplib.error_perm("530 Login incorrect")

    results = {}
    deliver.upload_files(
        CREDS,
        _files(tmp_path, 2),
        lambda n, e: results.__setitem__(n, e),
        connect=connect,
        sleep=sleeps.append,
    )
    assert not sleeps and "530" in str(results["p1.jpg"])


def test_cancel_during_wait_returns_promptly(tmp_path):
    first = Ftp(drop={"p0.jpg"})
    calls, sleeps = [], []

    def connect(*a, **k):
        calls.append(1)
        if len(calls) == 1:
            return first
        raise OSError("down")

    results = {}
    deliver.upload_files(
        CREDS,
        _files(tmp_path, 2),
        lambda n, e: results.__setitem__(n, e),
        connect=connect,
        sleep=sleeps.append,
        cancel=lambda: len(sleeps) >= 3,
    )
    assert len(sleeps) == 3 and sum(sleeps) == pytest.approx(1.5)
    assert results == {}  # nothing reported failed: they stay prepared


def test_upload_progress_text_and_time_left(cfg, store, tmp_path, creds, monkeypatch):
    batch = _prepared_batch(cfg, store, tmp_path, 3)
    clock = [0.0]
    monkeypatch.setattr(pipeline, "_mono", lambda: clock[0])
    texts = []

    def on_store(name):
        clock[0] += 120.0

    pipeline.upload_batch(
        cfg,
        store,
        batch,
        confirm=True,
        progress=lambda d, t, m: texts.append((d, t, m)),
        connect=lambda *a, **k: Ftp(on_store=on_store),
    )
    assert texts[0] == (0, 3, "Uploading photo 1 of 3 · IMG_0.jpg")
    assert any(m == "Uploading photo 2 of 3 · IMG_1.jpg · about 4 min left" for _, _, m in texts)


def test_upload_progress_shows_megabytes(cfg, store, tmp_path, creds, monkeypatch):
    batch = _prepared_batch(cfg, store, tmp_path, 1)
    clock = [0.0]
    monkeypatch.setattr(pipeline, "_mono", lambda: clock[0])
    texts = []

    class Slow(Ftp):
        def storbinary(self, cmd, fh):
            clock[0] += 1
            super().storbinary(cmd, fh)

    pipeline.upload_batch(
        cfg,
        store,
        batch,
        confirm=True,
        progress=lambda d, t, m: texts.append(m),
        connect=lambda *a, **k: Slow(),
    )
    assert any("MB sent" in m for m in texts)


def test_deliver_phase_text_and_bar_never_full_early(cfg, store, photos_dir, creds):
    from shuto.llm.fake import FakeProvider

    imp = pipeline.import_folder(cfg, store, photos_dir)
    (batch,) = imp.batches
    pipeline.cull_batch(cfg, store, batch)
    pipeline.describe_batch(cfg, store, batch, llm=FakeProvider("fake"))
    pipeline.approve(cfg, store, batch_id=batch)
    seen = []
    pipeline.deliver_batch(
        cfg,
        store,
        batch,
        confirm=True,
        progress=lambda d, t, m: seen.append((d, t, m)),
        connect=lambda *a, **k: Ftp(),
    )
    texts = [m for _, _, m in seen]
    assert "Step 1 of 2 · Preparing photos 1/3" in texts
    assert any(m.startswith("Step 2 of 2 · Uploading photo 2 of 3") for m in texts)
    assert all(d < t for d, t, m in seen if m != "Writing the Shutterstock CSV")
    assert seen[-1][0] == seen[-1][1]


def test_upload_clamps_to_250(cfg, store, tmp_path, creds):
    batch = _prepared_batch(cfg, store, tmp_path, pipeline.MAX_BATCH_PHOTOS + 3)
    s = pipeline.upload_batch(cfg, store, batch, confirm=True, connect=lambda *a, **k: Ftp())
    assert len(s.uploaded) == pipeline.MAX_BATCH_PHOTOS and s.over_limit == 3
    assert len(store.list_photos(batch, [Status.PREPARED])) == 3


def test_drop_during_store_then_retry_marks_uploaded_once(cfg, store, tmp_path, creds):
    batch = _prepared_batch(cfg, store, tmp_path, 2)
    first, second = Ftp(drop={"p1.jpg"}), Ftp()
    conns = iter([first, second])
    s = pipeline.upload_batch(
        cfg, store, batch, confirm=True, connect=lambda *a, **k: next(conns), sleep=lambda _: None
    )
    assert first.stored == ["p0.jpg"] and second.stored == ["p1.jpg"]
    assert sorted(s.uploaded) == ["p0.jpg", "p1.jpg"] and not s.failed
    photos = store.list_photos(batch)
    assert {p.status for p in photos} == {Status.UPLOADED}
    for p in photos:
        moves = [
            e
            for e in store.batch_events(batch, ["status"])
            if e["photo_id"] == p.id and e["detail"]["to"] == "uploaded"
        ]
        assert len(moves) == 1


def test_connection_is_closed_even_if_on_result_raises(tmp_path):
    ftps_made = []

    class Closing(Ftp):
        closed = False

        def quit(self):
            self.closed = True

    def connect(*a, **k):
        ftps_made.append(Closing(drop={"p0.jpg"} if not ftps_made else ()))
        return ftps_made[-1]

    def boom(name, err):
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        deliver.upload_files(
            CREDS, _files(tmp_path, 1), boom, connect=connect, sleep=lambda _: None
        )
    assert len(ftps_made) == 2 and all(f.closed for f in ftps_made)
