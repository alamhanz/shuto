import sqlite3
import threading

import pytest

from shuto.models import REASON_CODES, Job, LLMCall, Status
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
        assert c.execute("PRAGMA user_version").fetchone()[0] == 1
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
    from shuto.models import Photo

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
            for i in range(25):
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
    assert len(store.list_photos(statuses=[Status.READY])) == 100
    assert store.usage_by_batch("b")["calls"] == 100
