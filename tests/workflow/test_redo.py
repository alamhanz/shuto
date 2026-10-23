"""Send back to the AI: which photos go back, and how the AI run picks them up."""

from __future__ import annotations

import pytest

from shuto.ai.providers.base import LLMError
from shuto.ai.providers.fake import DEFAULT_RESPONSE, FakeProvider, vary
from shuto.core.models import Status
from shuto.workflow import check_batch, describing, importing, redo


def _described(cfg, store, photos_dir):
    imp = importing.import_folder(cfg, store, photos_dir)
    (batch,) = imp.batches
    check_batch.check_batch(cfg, store, batch, llm=FakeProvider("fake"))
    return batch


def _ids(store, batch, *statuses):
    return [p.id for p in store.list_photos(batch, list(statuses))]


def _hooked(first):
    """A fake AI that runs `first()` while it answers its first call."""
    state = {"calls": 0}
    base = vary(DEFAULT_RESPONSE)

    def answer(req):
        state["calls"] += 1
        if state["calls"] == 1:
            first()
        return base(req)

    return FakeProvider("fake", response=answer)


def _failing(req):
    raise LLMError("the model used all its tokens on thinking")


def test_send_back_skips_what_shutterstock_has(cfg, store, photos_dir):
    batch = _described(cfg, store, photos_dir)
    a, b, c = _ids(store, batch, Status.DESCRIBED)
    store.set_status(b, Status.EXPORTED, force=True)
    store.set_status(c, Status.SUBMITTED, force=True)
    s = redo.send_back(cfg, store, batch, [a, b, c])
    assert s.sent == [a] and s.on_shutterstock == [b, c] and not s.refused
    assert store.get_photo(a).status == Status.READY
    assert store.get_photo(b).status == Status.EXPORTED
    assert store.get_photo(c).status == Status.SUBMITTED


def test_send_back_twice_changes_nothing_more(cfg, store, photos_dir):
    batch = _described(cfg, store, photos_dir)
    a = _ids(store, batch, Status.DESCRIBED)[0]
    assert redo.send_back(cfg, store, batch, [a, a]).sent == [a]
    events = len(store.events_for(a))
    assert redo.send_back(cfg, store, batch, [a]).sent == [a]
    assert len(store.events_for(a)) == events
    assert store.get_photo(a).status == Status.READY


def test_send_back_refuses_other_batches_and_unknown_ids(cfg, store, photos_dir):
    batch = _described(cfg, store, photos_dir)
    other = store.add_photo(sha256="f" * 64, src_path="/x/o.jpg", orig_name="o.jpg", batch_id="b2")
    s = redo.send_back(cfg, store, batch, [other, 9999])
    assert not s.sent and set(s.refused) == {other, 9999}
    assert store.get_photo(other).status == Status.IMPORTED


def test_describe_waiting_drains_photos_sent_back_while_it_runs(cfg, store, photos_dir):
    batch = _described(cfg, store, photos_dir)
    a, b, _c = _ids(store, batch, Status.DESCRIBED)
    redo.send_back(cfg, store, batch, [a])
    llm = _hooked(lambda: redo.send_back(cfg, store, batch, [b]))
    seen = []
    s = describing.describe_waiting(
        cfg, store, batch, llm=llm, progress=lambda d, t, m="": seen.append((d, t))
    )
    assert len(llm.calls) == 2  # each photo once
    assert s.described == 2
    assert _ids(store, batch, Status.READY) == []
    assert seen[-1] == (2, 2) and max(t for _, t in seen) == 2


def test_describe_waiting_picks_up_a_finished_photo_sent_back_mid_round(cfg, store, photos_dir):
    batch = _described(cfg, store, photos_dir)
    a, b, c = _ids(store, batch, Status.DESCRIBED)
    redo.send_back(cfg, store, batch, [a, b, c])
    cfg.llm.concurrency = 1
    sent = []

    def second_call():
        done = [i for i in (a, b, c) if store.get_photo(i).status != Status.READY]
        sent.extend(done)
        redo.send_back(cfg, store, batch, done)  # a finished one comes back while the round runs

    state = {"calls": 0}
    base = vary(DEFAULT_RESPONSE)

    def answer(req):
        state["calls"] += 1
        if state["calls"] == 2:
            second_call()
        return base(req)

    llm = FakeProvider("fake", response=answer)
    s = describing.describe_waiting(cfg, store, batch, llm=llm)
    assert sent and len(llm.calls) == 3 + len(sent)  # the sent-back photo is written again
    assert s.failed == 0 and _ids(store, batch, Status.READY) == []


def test_describe_waiting_does_not_retry_a_failed_photo(cfg, store, photos_dir):
    batch = _described(cfg, store, photos_dir)
    a = _ids(store, batch, Status.DESCRIBED)[0]
    redo.send_back(cfg, store, batch, [a])
    llm = FakeProvider("fake", response=_failing)
    s = describing.describe_waiting(cfg, store, batch, llm=llm)
    assert s.failed == 1 and len(llm.calls) == 1
    assert store.get_photo(a).status == Status.READY


def test_failed_redo_restores_the_old_text_with_a_note(cfg, store, photos_dir):
    batch = _described(cfg, store, photos_dir)
    a = _ids(store, batch, Status.DESCRIBED)[0]
    old = store.get_photo(a).metadata["description"]
    redo.send_back(cfg, store, batch, [a])
    s = redo.run_redo(
        cfg, store, batch, llm=FakeProvider("fake", response=_failing), explain=lambda e: "oops"
    )
    assert s.failed == 1 and s.restored == [a] and s.done == 0
    photo = store.get_photo(a)
    assert photo.status == Status.NEEDS_REVIEW
    assert photo.metadata["description"] == old
    first = photo.guardrail["messages"][0]
    assert first.startswith("The AI could not write new text this time (oops)")
    assert "old text is kept" in first


def test_failed_redo_without_old_text_stays_waiting(cfg, store, photos_dir):
    batch = _described(cfg, store, photos_dir)
    culled = _ids(store, batch, Status.CULLED)[0]
    redo.send_back(cfg, store, batch, [culled])
    s = redo.run_redo(cfg, store, batch, llm=FakeProvider("fake", response=_failing))
    assert s.failed == 1 and s.restored == []
    assert store.get_photo(culled).status == Status.READY


def test_run_redo_writes_new_text_and_returns_photos(cfg, store, photos_dir):
    batch = _described(cfg, store, photos_dir)
    ids = _ids(store, batch, Status.DESCRIBED)
    redo.send_back(cfg, store, batch, ids)
    s = redo.run_redo(cfg, store, batch, llm=FakeProvider("fake"))
    assert s.done == 3 and s.failed == 0 and not s.aborted
    assert not _ids(store, batch, Status.READY)


def test_cancelled_redo_leaves_the_rest_waiting(cfg, store, photos_dir):
    batch = _described(cfg, store, photos_dir)
    redo.send_back(cfg, store, batch, _ids(store, batch, Status.DESCRIBED))
    s = redo.run_redo(cfg, store, batch, llm=FakeProvider("fake"), cancel=lambda: True)
    assert s.done == 0 and len(_ids(store, batch, Status.READY)) == 3


def test_check_batch_picks_up_photos_sent_back_while_it_runs(cfg, store, photos_dir):
    imp = importing.import_folder(cfg, store, photos_dir)
    (batch,) = imp.batches

    def send_back_a_culled_one():
        culled = _ids(store, batch, Status.CULLED)
        assert culled
        redo.send_back(cfg, store, batch, culled[:1])

    llm = _hooked(send_back_a_culled_one)
    summary = check_batch.check_batch(cfg, store, batch, llm=llm)
    assert summary.describe is not None and summary.describe.described == 4
    assert len(llm.calls) == 4
    assert not _ids(store, batch, Status.READY)


def test_fail_unfinished_jobs_marks_only_open_jobs(store):
    open_ids = [store.create_job("redo", "b"), store.create_job("check", "b")]
    store.update_job(open_ids[1], state="running")
    done = store.create_job("redo", "b")
    store.update_job(done, state="done")
    assert store.fail_unfinished_jobs("shuto was closed before this finished") == 2
    for job_id in open_ids:
        job = store.get_job(job_id)
        assert job.state == "failed" and "closed" in job.error and job.finished_at
    assert store.get_job(done).state == "done"
    assert store.fail_unfinished_jobs("again") == 0


@pytest.mark.parametrize("why", ["thinking.", "thinking"])
def test_restore_note_has_no_double_full_stop(cfg, store, photos_dir, why):
    batch = _described(cfg, store, photos_dir)
    a = _ids(store, batch, Status.DESCRIBED)[0]
    redo.send_back(cfg, store, batch, [a])
    assert redo.restore_failed(cfg, store, [a], why) == [a]
    assert "(thinking)" in store.get_photo(a).guardrail["messages"][0]
