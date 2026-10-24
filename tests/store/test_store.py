import sqlite3
import threading

import pytest

from shuto.core.models import REASON_CODES, Job, LLMCall, Status
from shuto.store import InvalidTransition, Store


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "data" / "shuto.db")


def _photo(store, n=1, batch="2026-W01", **kw):
    return store.add_photo(
        sha256=f"sha{n}", src_path=f"/x/{n}.jpg", orig_name=f"{n}.jpg", batch_id=batch, **kw
    )


def test_migrate_idempotent(store):
    store.migrate()
    store.migrate()
    with sqlite3.connect(store.path) as c:
        assert c.execute("PRAGMA user_version").fetchone()[0] == 3
        assert c.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    Store(store.path)  # reopen


def test_sha_unique_returns_existing(store):
    a = _photo(store, 1)
    assert _photo(store, 1) == a
    assert len(store.list_photos()) == 1
    assert store.find_by_sha("sha1").id == a


def test_json_roundtrip_and_defaults(store):
    pid = _photo(
        store,
        1,
        has_gps=True,
        cull={"reasons": ["blurry"]},
        metadata={"title": "Tokyo ü", "keywords": ["a", "b"]},
    )
    p = store.get_photo(pid)
    assert p.status is Status.IMPORTED
    assert p.has_gps is True
    assert p.cull == {"reasons": ["blurry"]}
    assert p.metadata["title"] == "Tokyo ü"
    assert p.release_ids == []
    assert p.guardrail is None
    store.update_photo(pid, guardrail={"blocked": False}, release_ids=["r1"], width=4000)
    p = store.get_photo(pid)
    assert p.guardrail == {"blocked": False}
    assert p.release_ids == ["r1"]
    assert p.width == 4000
    assert p.updated_at


def test_add_photo_from_dataclass(store):
    from shuto.core.models import Photo

    pid = store.add_photo(
        Photo(sha256="s", src_path="/a", orig_name="a.jpg", batch_id="b", metadata={"t": 1})
    )
    p = store.get_photo(pid)
    assert p.batch_id == "b" and p.metadata == {"t": 1} and p.status is Status.IMPORTED
    with pytest.raises(ValueError):
        store.add_photo(sha256="x")


def test_update_photo_validation(store):
    pid = _photo(store)
    with pytest.raises(ValueError):
        store.update_photo(pid, status="ready")
    with pytest.raises(ValueError):
        store.update_photo(pid, bogus=1)
    with pytest.raises(KeyError):
        store.update_photo(999, width=1)


def test_submit_name_unique_and_lookup(store):
    a = _photo(store, 1)
    b = _photo(store, 2)
    store.update_photo(a, submit_name="tokyo_1.jpg")
    assert store.find_by_submit_name("tokyo_1.jpg").id == a
    assert store.find_by_submit_name("nope.jpg") is None
    with pytest.raises(sqlite3.IntegrityError):
        store.update_photo(b, submit_name="tokyo_1.jpg")


def test_status_transitions(store):
    pid = _photo(store)
    store.set_status(pid, Status.READY)
    with pytest.raises(InvalidTransition):
        store.set_status(pid, Status.UPLOADED)
    assert store.get_photo(pid).status is Status.READY
    store.set_status(pid, "needs_review")
    store.set_status(pid, Status.APPROVED)
    store.set_status(pid, Status.PREPARED)
    store.set_status(pid, Status.APPROVED)  # edit after prepare
    store.set_status(pid, Status.READY)  # re-describe
    seen = [e["detail"]["to"] for e in store.events_for(pid) if e["kind"] == "status"]
    assert seen == ["ready", "needs_review", "approved", "prepared", "approved", "ready"]


def test_overrides_and_blocked(store):
    pid = _photo(store, status=Status.CULLED)
    store.set_status(pid, Status.READY)
    store.set_status(pid, Status.BLOCKED)
    with pytest.raises(InvalidTransition):
        store.set_status(pid, Status.APPROVED)


def test_no_return_to_ready_after_upload(store):
    pid = _photo(store, status=Status.UPLOADED)
    with pytest.raises(InvalidTransition):
        store.set_status(pid, Status.READY)


def test_force_is_logged(store):
    pid = _photo(store)
    store.set_status(pid, Status.ACCEPTED, force=True, detail={"why": "manual"})
    ev = [e for e in store.events_for(pid) if e["kind"] == "status"][-1]
    assert ev["detail"]["forced"] is True
    assert ev["detail"]["detail"] == {"why": "manual"}
    with pytest.raises(KeyError):
        store.set_status(999, Status.READY)


def test_batches(store):
    store.upsert_batch("b1", notes="hello", source="/pics")
    store.upsert_batch("b1")
    assert store.get_batch("b1").notes == "hello"
    store.upsert_batch("b1", notes="changed")
    assert store.get_batch("b1").notes == "changed"
    assert store.get_batch("b1").source == "/pics"
    a = _photo(store, 1, batch="b1")
    _photo(store, 2, batch="b1")
    store.set_status(a, Status.READY)
    _photo(store, 3, batch="b2")  # batch auto-created
    rows = {b.id: counts for b, counts in store.list_batches()}
    assert rows["b1"] == {Status.READY: 1, Status.IMPORTED: 1}
    assert rows["b2"] == {Status.IMPORTED: 1}
    assert store.get_batch("zzz") is None


def test_list_photos_filters(store):
    a = _photo(store, 1, batch="b1")
    b = _photo(store, 2, batch="b2")
    store.set_status(b, Status.READY)
    assert [p.id for p in store.list_photos()] == [a, b]
    assert [p.id for p in store.list_photos("b2")] == [b]
    assert [p.id for p in store.list_photos(statuses=["ready"])] == [b]
    assert store.list_photos(statuses=[]) == []
    assert store.list_photos("b1", [Status.READY]) == []


def test_rejections(store):
    a = _photo(store, 1)
    b = _photo(store, 2)
    store.update_photo(a, metadata={"title": "Shibuya"})
    store.add_rejection(a, "focus", "soft edges")
    store.add_rejection(b, "focus")
    store.add_rejection(b, "title", "too generic")
    with pytest.raises(ValueError):
        store.add_rejection(a, "not_a_code")
    assert "focus" in REASON_CODES
    assert store.rejection_stats() == {"focus": 2, "title": 1}
    assert store.rejection_stats(since_days=-1) == {}
    ex = store.recent_rejection_examples(limit=2)
    assert [e["note"] for e in ex] == ["too generic", "soft edges"]
    assert ex[1]["title"] == "Shibuya"
    assert len(store.list_rejections(b)) == 2


def test_llm_usage(store):
    a = _photo(store, 1, batch="b1")
    b = _photo(store, 2, batch="b2")
    store.log_llm_call(LLMCall("ollama", "m", a, 10, 5, 2.5, repaired=True))
    store.log_llm_call(LLMCall("ollama", "m", a, 20, 6, 1.5, ok=False, error="x"))
    store.log_llm_call(LLMCall("ollama", "m", b, 1, 1, 1.0))
    assert store.usage_by_batch("b1") == {
        "calls": 2,
        "input_tokens": 30,
        "output_tokens": 11,
        "seconds": 4.0,
    }
    assert store.usage_by_batch("none")["calls"] == 0


def test_jobs(store):
    jid = store.create_job("describe", "b1", total=5)
    job = store.get_job(jid)
    assert isinstance(job, Job) and job.state == "queued" and job.total == 5
    store.update_job(jid, state="running", done=2)
    assert store.get_job(jid).done == 2
    with pytest.raises(ValueError):
        store.update_job(jid, nope=1)
    assert store.get_job(999) is None


def test_settings_roundtrip(store):
    assert store.get_setting("llm.model") is None
    assert store.get_setting("llm.model", "auto") == "auto"
    store.set_setting("llm.model", "qwen2.5vl:7b")
    store.set_setting("opts", {"a": [1, 2], "b": None})
    store.set_setting("llm.model", "other")
    assert store.get_setting("llm.model") == "other"
    assert store.get_setting("opts") == {"a": [1, 2], "b": None}


def test_concurrent_writers(store):
    errors = []

    def work(t):
        try:
            for i in range(10):
                pid = store.add_photo(
                    sha256=f"t{t}-{i}", src_path="/x", orig_name="x.jpg", batch_id="b"
                )
                store.set_status(pid, Status.READY)
                store.set_setting("k", t)
                store.log_llm_call(LLMCall("fake", "m", pid))
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=work, args=(t,)) for t in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert len(store.list_photos(statuses=[Status.READY])) == 40
    assert store.usage_by_batch("b")["calls"] == 40


# -- schema v2 / filters / set_aside -----------------------------------------------------


def _make_v1_db(path):
    """A database exactly as the previous release created it (v1 DDL, one photo)."""
    from shuto.store.schema import SCHEMA_V1

    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    for stmt in SCHEMA_V1.split(";"):
        if stmt.strip():
            conn.execute(stmt)
    conn.execute("PRAGMA user_version=1")
    conn.execute("INSERT INTO batches(id, created_at) VALUES ('b1', 'x')")
    conn.execute(
        "INSERT INTO photos(batch_id, sha256, src_path, orig_name, status, guardrail_json,"
        " created_at, updated_at) VALUES ('b1','abc','/x/a.jpg','a.jpg','described',"
        " '{\"action\": \"ok\"}', 't', 't')"
    )
    conn.commit()
    conn.close()


def _columns(db):
    with sqlite3.connect(db) as c:
        return [r[1] for r in c.execute("PRAGMA table_info(photos)")]


def _version(db):
    with sqlite3.connect(db) as c:
        return c.execute("PRAGMA user_version").fetchone()[0]


def test_v1_database_migrates_in_place(tmp_path):
    db = tmp_path / "old" / "shuto.db"
    _make_v1_db(db)
    assert "filters_json" not in _columns(db)
    store = Store(db)
    photo = store.find_by_sha("abc")
    assert photo.status is Status.DESCRIBED
    assert photo.guardrail == {"action": "ok"}
    assert photo.filters is None
    store.update_photo(photo.id, filters={"v": 1, "verdict": "ok"})
    assert store.get_photo(photo.id).filters == {"v": 1, "verdict": "ok"}
    store.migrate()  # no-op the second time
    Store(db)
    assert _version(db) == 3
    assert _columns(db).count("filters_json") == 1
    assert _columns(db).count("draft_json") == 1


def test_migration_is_atomic(tmp_path, monkeypatch):
    db = tmp_path / "old" / "shuto.db"
    _make_v1_db(db)
    real_connect = sqlite3.connect

    class Boom(sqlite3.Connection):
        def execute(self, sql, *a):
            if sql.startswith("PRAGMA user_version=2"):
                raise sqlite3.OperationalError("disk gone")
            return super().execute(sql, *a)

    monkeypatch.setattr(
        "shuto.store.db.sqlite3.connect",
        lambda *a, **kw: real_connect(*a, factory=Boom, **kw),
    )
    with pytest.raises(sqlite3.OperationalError):
        Store(db)
    monkeypatch.undo()
    assert _version(db) == 1
    assert "filters_json" not in _columns(db)
    assert Store(db).find_by_sha("abc") is not None  # a later start migrates fine
    assert _version(db) == 3


def test_migrate_with_column_already_present(tmp_path):
    db = tmp_path / "old" / "shuto.db"
    _make_v1_db(db)
    with sqlite3.connect(db) as c:
        c.execute("ALTER TABLE photos ADD COLUMN filters_json TEXT")
    Store(db)
    assert _version(db) == 3


def test_newer_database_refused(tmp_path):
    db = tmp_path / "new" / "shuto.db"
    db.parent.mkdir()
    with sqlite3.connect(db) as c:
        c.execute("PRAGMA user_version=4")
    with pytest.raises(RuntimeError, match="newer"):
        Store(db)


def _make_v2_db(path):
    """A database as 0.6.3 left it: v2 (filters column, no draft column), with real rows."""
    _make_v1_db(path)
    with sqlite3.connect(path) as c:
        c.execute("ALTER TABLE photos ADD COLUMN filters_json TEXT")
        c.execute("PRAGMA user_version=2")
        c.execute("UPDATE photos SET filters_json='{\"v\": 1}'")
        c.execute('UPDATE photos SET metadata_json=\'{"description": "x"}\'')
        c.execute("INSERT INTO settings(key, value_json) VALUES ('onboarded', 'true')")


def test_v2_database_gets_the_draft_column_and_keeps_everything(tmp_path):
    db = tmp_path / "old" / "shuto.db"
    _make_v2_db(db)
    assert "draft_json" not in _columns(db)
    store = Store(db)
    assert _version(db) == 3 and _columns(db).count("draft_json") == 1
    photo = store.find_by_sha("abc")
    assert photo.status is Status.DESCRIBED and photo.draft is None
    assert photo.filters == {"v": 1} and photo.metadata == {"description": "x"}
    assert store.get_setting("onboarded") is True
    store.update_photo(photo.id, draft={"state": "waiting"})
    assert store.get_photo(photo.id).draft == {"state": "waiting"}
    store.update_photo(photo.id, draft=None)
    assert store.get_photo(photo.id).draft is None
    Store(db)  # opening again changes nothing
    assert _version(db) == 3 and _columns(db).count("draft_json") == 1


def test_v2_to_v3_migration_is_atomic(tmp_path, monkeypatch):
    db = tmp_path / "old" / "shuto.db"
    _make_v2_db(db)
    real_connect = sqlite3.connect

    class Boom(sqlite3.Connection):
        def execute(self, sql, *a):
            if sql.startswith("PRAGMA user_version=3"):
                raise sqlite3.OperationalError("disk gone")
            return super().execute(sql, *a)

    monkeypatch.setattr(
        "shuto.store.db.sqlite3.connect",
        lambda *a, **kw: real_connect(*a, factory=Boom, **kw),
    )
    with pytest.raises(sqlite3.OperationalError):
        Store(db)
    monkeypatch.undo()
    assert _version(db) == 2 and "draft_json" not in _columns(db)
    assert Store(db).find_by_sha("abc") is not None
    assert _version(db) == 3


def test_v2_database_with_the_column_already_there(tmp_path):
    db = tmp_path / "old" / "shuto.db"
    _make_v2_db(db)
    with sqlite3.connect(db) as c:
        c.execute("ALTER TABLE photos ADD COLUMN draft_json TEXT")
    Store(db)
    assert _version(db) == 3 and _columns(db).count("draft_json") == 1


def test_upgrade_keeps_a_backup_of_the_old_database(tmp_path):
    db = tmp_path / "old" / "shuto.db"
    _make_v2_db(db)
    Store(db)
    bak = db.with_name("shuto.db.v2.bak")
    assert bak.is_file() and _version(bak) == 2 and "draft_json" not in _columns(bak)
    assert _version(db) == 3
    assert Store(bak.with_name("copy.db")).find_by_sha("abc") is None  # a separate file
    # an older shuto can use the backup as its database
    with sqlite3.connect(bak) as c:
        assert c.execute("SELECT count(*) FROM photos").fetchone()[0] == 1


def test_v1_upgrade_is_named_after_the_old_version(tmp_path):
    db = tmp_path / "old" / "shuto.db"
    _make_v1_db(db)
    Store(db)
    assert db.with_name("shuto.db.v1.bak").is_file()


def test_backup_is_made_once_and_never_overwritten(tmp_path):
    db = tmp_path / "old" / "shuto.db"
    _make_v2_db(db)
    bak = db.with_name("shuto.db.v2.bak")
    bak.write_bytes(b"older backup")
    Store(db)
    assert bak.read_bytes() == b"older backup"
    assert _version(db) == 3


def test_no_backup_for_new_or_current_databases(tmp_path):
    db = tmp_path / "new" / "shuto.db"
    Store(db)
    Store(db)
    assert list(db.parent.glob("*.bak")) == []


def test_failed_backup_stops_the_upgrade(tmp_path, monkeypatch):
    db = tmp_path / "old" / "shuto.db"
    _make_v2_db(db)

    real_connect = sqlite3.connect

    class NoBackup(sqlite3.Connection):
        def backup(self, *a, **kw):
            raise sqlite3.OperationalError("disk full")

    monkeypatch.setattr(
        "shuto.store.db.sqlite3.connect",
        lambda *a, **kw: real_connect(*a, factory=NoBackup, **kw),
    )
    with pytest.raises(RuntimeError, match="backup of your database.*Nothing was changed"):
        Store(db)
    assert _version(db) == 2 and "draft_json" not in _columns(db)
    assert list(db.parent.glob("*.bak*")) == []


def test_filters_roundtrip_and_none_default(store):
    pid = _photo(store)
    assert store.get_photo(pid).filters is None
    data = {"v": 1, "theme": {"text": "日本", "fit": "no"}, "rules": []}
    store.update_photo(pid, filters=data)
    assert store.get_photo(pid).filters == data
    store.update_photo(pid, filters=None)
    assert store.get_photo(pid).filters is None


def test_set_aside_transitions(store):
    pid = _photo(store)
    store.set_status(pid, Status.READY)
    store.set_status(pid, Status.SET_ASIDE)
    store.set_status(pid, Status.NEEDS_REVIEW)  # describe results move between each other
    store.set_status(pid, Status.SET_ASIDE)
    store.set_status(pid, Status.APPROVED)  # approve anyway
    store.set_status(pid, Status.READY)
    store.set_status(pid, Status.SET_ASIDE)
    store.set_status(pid, Status.READY)  # write again
    for bad in (Status.PREPARED, Status.UPLOADED, Status.SUBMITTED):
        store.set_status(pid, Status.SET_ASIDE)
        with pytest.raises(InvalidTransition):
            store.set_status(pid, bad)
        store.set_status(pid, Status.READY)
    # only a describe can lead into set_aside
    with pytest.raises(InvalidTransition):
        store.set_status(_photo(store, n=2), Status.SET_ASIDE)
    with pytest.raises(InvalidTransition):
        store.set_status(_photo(store, n=3, status=Status.UPLOADED), Status.SET_ASIDE)


def test_status_times_latest_per_status(store):
    a = _photo(store, n=1)
    b = _photo(store, n=2)
    _photo(store, n=3, batch="other")
    store.set_status(a, Status.READY)
    store.set_status(a, Status.NEEDS_REVIEW)
    store.set_status(a, Status.APPROVED)
    store.set_status(a, Status.READY)
    store.set_status(a, Status.NEEDS_REVIEW)
    store.set_status(b, Status.READY)
    times = store.status_times("2026-W01")
    assert set(times) == {a, b}
    assert set(times[a]) == {"ready", "needs_review", "approved"}
    assert set(times[b]) == {"ready"}
    assert times[a]["needs_review"] >= times[a]["approved"]
    assert store.status_times("nope") == {}


def test_batch_events_filters_kind_and_batch(store):
    a = _photo(store, n=1)
    c = _photo(store, n=3, batch="other")
    store.set_status(a, Status.READY)
    store.set_status(c, Status.READY)
    store.log_event("note", photo_id=a, batch_id="2026-W01", detail={"x": 1})
    evs = store.batch_events("2026-W01", ["status", "note"])
    assert [e["kind"] for e in evs].count("note") == 1
    assert all(e["batch_id"] == "2026-W01" for e in evs)
    assert [e["detail"] for e in store.batch_events("2026-W01", ["note"])] == [{"x": 1}]
    assert store.batch_events("2026-W01", []) == []
    assert store.batch_events("2026-W01", ("nothing",)) == []
