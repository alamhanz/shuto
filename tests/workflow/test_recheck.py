"""Check a sent photo again with the AI: the draft, using or dropping it, what never changes."""

from __future__ import annotations

import csv
from pathlib import Path

from shuto.ai.providers.base import LLMError
from shuto.ai.providers.fake import DEFAULT_RESPONSE, FakeProvider, vary
from shuto.core.models import UPLOADED_STATES, Status
from shuto.workflow import describing, recheck, redo, resend

from .test_uploaded_memory import uploaded  # noqa: F401 (fixture)

NEW = {**DEFAULT_RESPONSE, "description": "A brand new quiet street in Tokyo at dawn"}


def _sent(store, batch):
    return store.list_photos(batch, [Status.UPLOADED, Status.EXPORTED])


def _csv_rows(path):
    with Path(path).open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def _queue_and_describe(cfg, store, batch, pid, llm):
    assert recheck.queue(cfg, store, batch, [pid]).queued == [pid]
    return describing.describe_waiting(cfg, store, batch, llm=llm)


def test_queue_only_takes_uploaded_and_exported_photos(cfg, store, uploaded):  # noqa: F811
    a, b, c = _sent(store, uploaded)
    store.set_status(c.id, Status.SUBMITTED, force=True)
    s = recheck.queue(cfg, store, uploaded, [a.id, c.id, 9999])
    assert s.queued == [a.id] and set(s.refused) == {c.id, 9999}
    assert "can only be changed on the Shutterstock site" in s.refused[c.id]
    assert store.get_photo(a.id).draft == {"state": "waiting"}
    assert store.get_photo(c.id).draft is None


def test_queue_refuses_a_photo_that_was_not_sent(cfg, store, uploaded):  # noqa: F811
    a = _sent(store, uploaded)[0]
    store.set_status(a.id, Status.APPROVED, force=True)
    s = recheck.queue(cfg, store, uploaded, [a.id])
    assert not s.queued and "not been sent" in s.refused[a.id]


def test_describing_writes_a_draft_and_nothing_else(cfg, store, uploaded):  # noqa: F811
    a = _sent(store, uploaded)[0]
    before = store.get_photo(a.id)
    s = _queue_and_describe(cfg, store, uploaded, a.id, FakeProvider("fake", response=NEW))
    assert s.described + s.needs_review == 1 and not s.failed
    after = store.get_photo(a.id)
    assert after.draft["state"] == "ready"
    assert after.draft["metadata"]["description"] == NEW["description"]
    assert after.draft["status_hint"] in {"described", "needs_review"}
    assert after.draft["metadata_source"] == "llm:fake/fake"
    for name in ("status", "metadata", "guardrail", "submit_name", "prepared_path", "uploaded_at"):
        assert getattr(after, name) == getattr(before, name), name
    assert after.status in UPLOADED_STATES
    assert Path(after.prepared_path).is_file()


def test_a_check_job_drains_waiting_drafts_after_the_ready_photos(cfg, store, uploaded):  # noqa: F811
    a, b, _c = _sent(store, uploaded)
    recheck.queue(cfg, store, uploaded, [a.id, b.id])
    seen = []
    llm = FakeProvider("fake", response=vary(NEW))
    s = describing.describe_waiting(
        cfg, store, uploaded, llm=llm, progress=lambda d, t, m="": seen.append((d, t))
    )
    assert len(llm.calls) == 2
    assert s.described + s.needs_review == 2
    assert {store.get_photo(p).draft["state"] for p in (a.id, b.id)} == {"ready"}
    assert seen[-1] == (2, 2)


def test_redo_run_also_drains_drafts(cfg, store, uploaded):  # noqa: F811
    a = _sent(store, uploaded)[0]
    recheck.queue(cfg, store, uploaded, [a.id])
    out = redo.run_redo(cfg, store, uploaded, llm=FakeProvider("fake", response=NEW))
    assert out.done == 1 and not out.failed
    assert store.get_photo(a.id).draft["state"] == "ready"


def test_a_failed_ai_keeps_the_old_text(cfg, store, uploaded):  # noqa: F811
    a = _sent(store, uploaded)[0]
    old = store.get_photo(a.id)

    def boom(_req):
        raise LLMError("the model used all its tokens on thinking")

    s = _queue_and_describe(cfg, store, uploaded, a.id, FakeProvider("fake", response=boom))
    assert s.failed == 1
    now = store.get_photo(a.id)
    assert now.draft["state"] == "failed" and "tokens" in now.draft["error"]
    assert now.metadata == old.metadata and now.status == old.status
    assert recheck.queue(cfg, store, uploaded, [a.id]).queued == [a.id]  # Try again


def test_use_draft_replaces_the_text_and_rewrites_the_csv(cfg, store, uploaded):  # noqa: F811
    a = _sent(store, uploaded)[0]
    old_prepared = a.prepared_path
    before_ids = {p.id for p in store.list_photos(statuses=list(UPLOADED_STATES))}
    _queue_and_describe(cfg, store, uploaded, a.id, FakeProvider("fake", response=NEW))
    res = recheck.use_draft(cfg, store, a.id)
    assert res.ok and res.csv_path and not res.messages
    now = store.get_photo(a.id)
    assert now.draft is None and now.status == Status.EXPORTED
    assert now.metadata["description"] == NEW["description"]
    assert now.submit_name == a.submit_name and now.prepared_path is None
    assert not Path(old_prepared).exists()
    rows = {r["Filename"]: r for r in _csv_rows(res.csv_path)}
    assert rows[a.submit_name]["Description"] == NEW["description"]
    assert len(rows) == 3
    assert {p.id for p in store.list_photos(statuses=list(UPLOADED_STATES))} == before_ids
    (ev,) = [e for e in store.events_for(a.id) if e["kind"] == "recheck_applied"]
    assert ev["detail"]["old_metadata"]["description"] == a.metadata["description"]


def test_owner_edits_win_over_the_draft(cfg, store, uploaded):  # noqa: F811
    a = _sent(store, uploaded)[0]
    _queue_and_describe(cfg, store, uploaded, a.id, FakeProvider("fake", response=NEW))
    edits = {"description": "A calm street in Tokyo with bicycles", "keywords": None}
    res = recheck.use_draft(cfg, store, a.id, edits)
    assert res.ok
    now = store.get_photo(a.id)
    assert now.metadata["description"] == edits["description"]
    assert now.metadata_source == "manual"


def test_a_draft_with_non_english_text_is_refused(cfg, store, uploaded):  # noqa: F811
    a = _sent(store, uploaded)[0]
    _queue_and_describe(cfg, store, uploaded, a.id, FakeProvider("fake", response=NEW))
    res = recheck.use_draft(cfg, store, a.id, {"description": "東京の静かな通り"})
    assert not res.ok and res.messages
    now = store.get_photo(a.id)
    assert now.draft["state"] == "ready" and now.metadata["description"] != "東京の静かな通り"


def test_a_blocked_draft_is_refused(cfg, store, uploaded):  # noqa: F811
    a = _sent(store, uploaded)[0]
    bad = {**NEW, "description": "National Museum of Western Art in Ueno, Tokyo"}
    _queue_and_describe(cfg, store, uploaded, a.id, FakeProvider("fake", response=bad))
    assert store.get_photo(a.id).draft["status_hint"] == "blocked"
    res = recheck.use_draft(cfg, store, a.id)
    assert not res.ok and res.messages
    now = store.get_photo(a.id)
    assert now.draft["state"] == "ready" and now.metadata == a.metadata


def test_a_submitted_photo_cannot_take_the_new_text(cfg, store, uploaded):  # noqa: F811
    a = _sent(store, uploaded)[0]
    _queue_and_describe(cfg, store, uploaded, a.id, FakeProvider("fake", response=NEW))
    store.set_status(a.id, Status.SUBMITTED, force=True)
    res = recheck.use_draft(cfg, store, a.id)
    assert not res.ok and "Shutterstock site" in res.messages[0]
    assert store.get_photo(a.id).draft["state"] == "ready"


def test_use_draft_without_a_draft(cfg, store, uploaded):  # noqa: F811
    a = _sent(store, uploaded)[0]
    assert recheck.use_draft(cfg, store, a.id).messages == ["There is no new text to use."]


def test_keep_the_old_text_drops_the_draft(cfg, store, uploaded):  # noqa: F811
    a = _sent(store, uploaded)[0]
    _queue_and_describe(cfg, store, uploaded, a.id, FakeProvider("fake", response=vary(NEW)))
    recheck.drop_draft(store, a.id)
    now = store.get_photo(a.id)
    assert now.draft is None and now.metadata == a.metadata and now.status == a.status


def test_a_resend_after_new_text_makes_a_fresh_copy(cfg, store, uploaded):  # noqa: F811
    a = _sent(store, uploaded)[0]
    _queue_and_describe(cfg, store, uploaded, a.id, FakeProvider("fake", response=NEW))
    assert recheck.use_draft(cfg, store, a.id).ok
    s = resend.reupload_photos(cfg, store, [a.id], batch_id=uploaded)
    assert s.moved == {a.id: Status.APPROVED}
    assert store.get_photo(a.id).metadata["description"] == NEW["description"]
