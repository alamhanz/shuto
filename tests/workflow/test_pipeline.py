from __future__ import annotations

import threading
from pathlib import Path

import pytest

from shuto.ai import benchmark as ai_benchmark
from shuto.ai import catalog as local_models
from shuto.ai import model_choice as ai_choice
from shuto.ai import selection
from shuto.ai.providers.base import LLMError
from shuto.ai.providers.fake import DEFAULT_RESPONSE, FakeProvider
from shuto.core import errors, paths
from shuto.core.jobs import JobBusy, JobRunner
from shuto.core.models import Status
from shuto.workflow import (
    culling,
    delivering,
    describing,
    exporting,
    importing,
    reports,
    resend,
    review,
)
from shuto.workflow import models as workflow_models

from ..helpers import FakeOllama, fake_profile


class FakeFTP:
    def __init__(self):
        self.stored: dict[str, bytes] = {}

    def storbinary(self, cmd, fh):
        self.stored[cmd.split(" ", 1)[1]] = fh.read()

    def quit(self):
        pass

    def close(self):
        pass


def _setup(cfg, store, photos_dir):
    imp = importing.import_folder(cfg, store, photos_dir)
    (batch,) = imp.batches
    culling.cull_batch(cfg, store, batch)
    return batch


def _statuses(store, batch):
    return {p.orig_name: p.status for p in store.list_photos(batch)}


def _to_prepared(cfg, store, photos_dir):
    batch = _setup(cfg, store, photos_dir)
    describing.describe_batch(cfg, store, batch, llm=FakeProvider("fake"))
    review.approve(cfg, store, batch_id=batch)
    prep = delivering.prepare_batch(cfg, store, batch)
    return batch, prep


def test_import_and_cull(cfg, store, photos_dir):
    imp = importing.import_folder(cfg, store, photos_dir)
    assert imp.imported == 5 and imp.duplicates == 0 and not imp.errors
    for p in store.list_photos():
        assert cfg.workspace_path("thumbs", f"{p.sha256}.jpg").exists()
    again = importing.import_folder(cfg, store, photos_dir)
    assert again.imported == 0 and again.already_known == 5
    (batch,) = imp.batches
    summary = culling.cull_batch(cfg, store, batch, explain=True)
    assert summary.ready == 3 and summary.culled == 2
    st = _statuses(store, batch)
    assert st["blurry.jpg"] == Status.CULLED and st["tiny.jpg"] == Status.CULLED
    assert all(d["reasons"] for d in summary.details)


def test_exact_duplicate_copy_is_skipped(cfg, store, photos_dir):
    (photos_dir / "copy.jpg").write_bytes((photos_dir / "a.jpg").read_bytes())
    imp = importing.import_folder(cfg, store, photos_dir)
    assert imp.imported == 5 and imp.duplicates == 1


def test_import_missing_folder(cfg, store, tmp_path):
    with pytest.raises(errors.PipelineError):
        importing.import_folder(cfg, store, tmp_path / "nope")


def test_describe_skips_culled_and_sets_statuses(cfg, store, photos_dir):
    batch = _setup(cfg, store, photos_dir)
    fake = FakeProvider("fake")
    summary = describing.describe_batch(cfg, store, batch, llm=fake)
    assert summary.described == 3 and summary.failed == 0
    assert len(fake.calls) == 3  # culled photos never reached the model
    st = _statuses(store, batch)
    assert st["a.jpg"] == Status.DESCRIBED and st["blurry.jpg"] == Status.CULLED
    photo = store.list_photos(batch, [Status.DESCRIBED])[0]
    assert photo.metadata["keywords"] and photo.metadata_source == "llm:fake/fake"
    assert store.usage_by_batch(batch)["calls"] == 3


def test_blocked_photo_is_never_approved(cfg, store, photos_dir):
    batch = _setup(cfg, store, photos_dir)
    bad = dict(DEFAULT_RESPONSE, description="National Museum of Western Art in Ueno, Tokyo")
    describing.describe_batch(cfg, store, batch, llm=FakeProvider("fake", response=[bad]))
    blocked = [p.id for p in store.list_photos(batch, [Status.BLOCKED])]
    assert blocked
    res = review.approve(cfg, store, photo_ids=blocked)
    assert not res.approved and set(res.refused) == set(blocked)
    assert not store.list_photos(batch, [Status.APPROVED])
    # a blocked photo never reaches prepare/upload either
    assert not delivering.prepare_batch(cfg, store, batch).prepared


def test_full_flow_with_fake_ftp(cfg, store, photos_dir, monkeypatch):
    batch, prep = _to_prepared(cfg, store, photos_dir)
    assert len(prep.prepared) == 3 and not prep.problems
    assert all(paths.batch_dir(cfg, batch).joinpath("prepared", n).exists() for n in prep.prepared)

    with pytest.raises(errors.UploadRefused):
        delivering.upload_batch(cfg, store, batch)
    assert not store.list_photos(batch, [Status.UPLOADED])
    monkeypatch.setenv("SHUTO_FTPS_USER", "u")
    monkeypatch.setenv("SHUTO_FTPS_PASSWORD", "p")
    ftp = FakeFTP()
    up = delivering.upload_batch(cfg, store, batch, confirm=True, connect=lambda *a, **k: ftp)
    assert sorted(up.uploaded) == sorted(prep.prepared) and not up.failed
    assert set(ftp.stored) == set(prep.prepared)

    dry = exporting.export_batch(cfg, store, batch, dry_run=True)
    assert not dry.files and len(dry.rows) == 3
    exp = exporting.export_batch(cfg, store, batch)
    assert len(exp.files) == 1 and exp.files[0].name.endswith("-shutterstock.csv")
    assert exp.files[0].parent == paths.batch_dir(cfg, batch)
    text = "\n".join(f.read_text(encoding="utf-8") for f in exp.files)
    assert all(name in text for name in prep.prepared)
    assert {r["filename"] for r in exp.rows} == set(ftp.stored)
    assert len(store.list_photos(batch, [Status.EXPORTED])) == 3

    assert len(resend.mark_submitted(store, batch)) == 3
    first = review.find_photo(store, prep.prepared[0])
    resend.record_result(store, first.id, False, "focus", "soft")
    resend.record_result(store, review.find_photo(store, prep.prepared[1]).id, True)
    assert store.rejection_stats() == {"focus": 1}
    counts = reports.batch_report(store, batch)[0]["counts"]
    assert counts["rejected"] == 1 and counts["accepted"] == 1
    with pytest.raises(errors.PipelineError):
        resend.record_result(store, first.id, False, "bogus")


def test_upload_allowed_by_config(cfg, store, photos_dir, monkeypatch):
    cfg.deliver.upload_enabled = True
    batch, _ = _to_prepared(cfg, store, photos_dir)
    monkeypatch.setenv("SHUTO_FTPS_USER", "u")
    monkeypatch.setenv("SHUTO_FTPS_PASSWORD", "p")
    up = delivering.upload_batch(cfg, store, batch, connect=lambda *a, **k: FakeFTP())
    assert len(up.uploaded) == 3


def test_missing_ftps_credentials_message(cfg, store, photos_dir):
    cfg.deliver.upload_enabled = True
    batch, _ = _to_prepared(cfg, store, photos_dir)
    with pytest.raises(RuntimeError, match="SHUTO_FTPS_USER"):
        delivering.upload_batch(cfg, store, batch)


def test_rejection_reasons_reach_next_prompt(cfg, store, photos_dir):
    batch = _setup(cfg, store, photos_dir)
    pid = store.list_photos(batch)[0].id
    for status in (
        Status.DESCRIBED,
        Status.APPROVED,
        Status.PREPARED,
        Status.UPLOADED,
        Status.EXPORTED,
    ):
        store.set_status(pid, status, force=status == Status.DESCRIBED)
    resend.record_result(store, pid, False, "focus", "soft focus on edges")
    fake = FakeProvider("fake")
    describing.describe_batch(cfg, store, batch, llm=fake)
    assert fake.calls and "focus" in (fake.calls[0].system + fake.calls[0].prompt).lower()


def test_edit_reruns_guardrails_and_prepared_goes_back(cfg, store, photos_dir):
    batch, _ = _to_prepared(cfg, store, photos_dir)
    photo = store.list_photos(batch, [Status.PREPARED])[0]
    path = photo.prepared_path
    edited = review.update_metadata(
        cfg, store, photo.id, description="A quiet street with a parked bicycle"
    )
    assert edited.status == Status.APPROVED
    assert edited.prepared_path is None and edited.submit_name is None
    assert not Path(path).exists()
    again = review.update_metadata(cfg, store, photo.id, keywords=["japan"])
    assert again.status == Status.NEEDS_REVIEW  # too few keywords
    culled = store.list_photos(batch, [Status.CULLED])[0]
    with pytest.raises(errors.PipelineError):
        review.update_metadata(cfg, store, culled.id, description="x")


def test_describe_stops_when_provider_down(cfg, store, photos_dir):
    batch = _setup(cfg, store, photos_dir)

    def boom(req):
        raise LLMError("Ollama is not running at x", retryable=False)

    summary = describing.describe_batch(cfg, store, batch, llm=FakeProvider("fake", response=boom))
    assert summary.aborted and summary.failed >= 1 and summary.described == 0
    assert store.list_photos(batch, [Status.READY])


def test_redescribe_by_ids(cfg, store, photos_dir):
    batch = _setup(cfg, store, photos_dir)
    describing.describe_batch(cfg, store, batch, llm=FakeProvider("fake"))
    pid = store.list_photos(batch, [Status.DESCRIBED])[0].id
    fake = FakeProvider("fake")
    s = describing.describe_batch(cfg, store, batch, [pid], fake)
    assert s.described == 1 and len(fake.calls) == 1


# ------------------------------------------------------------------ selection and models


def test_auto_requires_explicit_choice(cfg, store):
    cfg.llm.provider = "ollama"
    with pytest.raises(ai_choice.ModelChoiceRequired):
        selection.resolve_selection(cfg, store)
    sel = workflow_models.use_model(cfg, store, "moondream")
    assert (sel.provider, sel.model) == ("ollama", "moondream")
    assert selection.resolve_selection(cfg, store).model == "moondream"
    assert selection.resolve_selection(cfg, store, model="gemma3:4b").model == "gemma3:4b"
    assert selection.resolve_llm(cfg, store).name == "ollama"


def test_api_models_come_from_catalog(cfg, store, monkeypatch):
    for prov in ("anthropic", "gemini", "bedrock"):
        sel = selection.resolve_selection(cfg, store, provider=prov)
        entry = selection.api_model(prov)
        assert entry is not None and sel.model == entry.model
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(errors.PipelineError, match="GEMINI_API_KEY"):
        selection.resolve_llm(cfg, store, provider="gemini")
    sel = workflow_models.use_model(cfg, store, "gemini:")
    assert sel.provider == "gemini" and sel.model == selection.api_model("gemini").model
    assert selection.resolve_selection(cfg, store).provider == "gemini"


def test_estimates(cfg, store, photos_dir):
    batch = _setup(cfg, store, photos_dir)
    assert describing.estimate_describe(cfg, store, batch).n == 3
    cfg.llm.provider = "ollama"
    workflow_models.use_model(cfg, store, "moondream")
    est = describing.estimate_describe(cfg, store, batch, profile=fake_profile())
    assert est.kind == "time" and est.seconds and "3 photos" in est.text
    est = describing.estimate_describe(cfg, store, batch, provider="anthropic")
    assert est.kind == "cost" and est.usd


def test_doctor_and_list_models(cfg, store):
    cfg.llm.provider = "ollama"
    client = FakeOllama(pulled=["moondream:latest"])
    report = workflow_models.doctor(cfg, store, client=client)
    assert report.selected is None and len(report.rows) == len(local_models.load_catalog()[0])
    assert report.ollama.running and report.hardware["tier"] == "cpu"
    rows = workflow_models.list_models(cfg, client=client, n=8)
    assert [r.rank for r in rows] == list(range(1, 9))
    workflow_models.use_model(cfg, store, "moondream")
    assert workflow_models.doctor(cfg, store, client=client).selected == "ollama:moondream"


def test_preflight_and_pull(cfg):
    sel = selection.Selection("ollama", "moondream", "flag")
    down = selection.describe_preflight(cfg, sel, client=FakeOllama(running=False))
    assert "not running" in down[0]
    assert "not downloaded" in selection.describe_preflight(cfg, sel, client=FakeOllama())[0]
    assert not selection.describe_preflight(cfg, sel, client=FakeOllama(["moondream:latest"]))
    client = FakeOllama()
    workflow_models.pull_model(cfg, "moondream", client=client)
    assert client.pull_calls == ["moondream"]


def test_disk_advice_when_too_small(cfg, monkeypatch):
    monkeypatch.setattr(local_models, "free_gb", lambda p: 0.5)
    adv = workflow_models.model_disk_advice(cfg, "moondream", client=FakeOllama())
    assert adv is not None and not adv.fits and adv.api_options
    assert workflow_models.model_disk_advice(cfg, "not-in-catalog", client=FakeOllama()) is None


def test_bench_model(cfg, store, monkeypatch):
    calls = []

    def fake_bench(provider, sample, cache, fp):
        calls.append((provider.model, fp))
        return {"cold_s": 3.0, "warm_s": 1.0, "ok_json": True}

    monkeypatch.setattr(ai_benchmark, "benchmark", fake_bench)
    rec = workflow_models.bench_model(cfg, store, "moondream", client=FakeOllama())
    assert rec["warm_s"] == 1.0 and calls == [("moondream", "test-fp")]


# ------------------------------------------------------------------ jobs


def test_job_runner(store):
    runner = JobRunner(store)
    try:

        def work(ctx):
            ctx.progress(1, 2, "a")
            ctx.progress(2, 2, "b")
            return "ok"

        jid = runner.submit("describe", "b1", work, total=2)
        job = runner.wait(jid)
        assert job.state == "done" and job.done == 2 and runner.result(jid) == "ok"

        def fail(ctx):
            raise ValueError("nope")

        job = runner.wait(runner.submit("x", "b1", fail))
        assert job.state == "failed" and "nope" in job.error

        gate = threading.Event()
        jid = runner.submit("slow", "b2", lambda ctx: gate.wait(5))
        with pytest.raises(JobBusy):
            runner.submit("other", "b2", lambda ctx: None)
        runner.cancel(jid)
        gate.set()
        assert runner.wait(jid).state == "cancelled"
    finally:
        runner.shutdown()
