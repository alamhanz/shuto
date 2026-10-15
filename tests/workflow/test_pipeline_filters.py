"""Pipeline integration of the theme check, the user's rules and the per-batch report."""

from __future__ import annotations

import pytest

from shuto.ai import estimate, filters
from shuto.ai.providers.fake import DEFAULT_RESPONSE, FakeProvider, vary
from shuto.core import config, errors, paths
from shuto.core.models import Status
from shuto.deliver import report
from shuto.settings import effective
from shuto.settings import ui as settings_ui
from shuto.workflow import check_batch, culling, describing, importing, reports, resend, review
from shuto.workflow import checks as workflow_checks
from shuto.workflow import models as workflow_models

THEME = "Everyday Japan"


def _cfg(cfg, *, theme=THEME, rules=(), **flt):
    return cfg.model_copy(
        update={
            "style": cfg.style.model_copy(update={"theme": theme}),
            "filters": cfg.filters.model_copy(update={"rules": list(rules), **flt}),
        }
    )


def _rule(kind="avoid", text="people", mode="set_aside", id="r1"):
    return config.FilterRuleConfig(kind=kind, text=text, mode=mode, id=id)


def _ready(cfg, store, photos_dir):
    batch = next(iter(importing.import_folder(cfg, store, photos_dir).batches))
    culling.cull_batch(cfg, store, batch)
    return batch


def _resp(fit=None, checks=None, **extra):
    out = dict(DEFAULT_RESPONSE, **extra)
    if fit is not None:
        out["theme_fit"] = {"reason": f"theme {fit}", "fit": fit}
    if checks is not None:
        out["checks"] = {k: {"reason": f"why {k}", "answer": v} for k, v in checks.items()}
    return vary(out)  # distinct per photo, so the batch duplicate check stays quiet


def _describe(cfg, store, batch, response, **kw):
    fake = FakeProvider("fake", response=[response])
    summary = describing.describe_batch(cfg, store, batch, llm=fake, **kw)
    return summary, fake


def _one(store, batch, status):
    photos = store.list_photos(batch, [status])
    assert photos
    return photos[0]


def test_no_filters_keeps_old_behaviour(cfg, store, photos_dir):
    batch = _ready(cfg, store, photos_dir)
    summary, fake = _describe(cfg, store, batch, None)
    assert summary.described == 3 and summary.set_aside == 0
    assert all(p.filters is None for p in store.list_photos(batch, [Status.DESCRIBED]))
    assert all("theme_fit" not in s["properties"] for s in fake.schemas)


def test_theme_no_is_set_aside_with_one_call_per_photo(cfg, store, photos_dir):
    cfg = _cfg(cfg)
    batch = _ready(cfg, store, photos_dir)
    summary, fake = _describe(cfg, store, batch, _resp(fit="no"))
    assert summary.set_aside == 3 and summary.described == 0
    assert len(fake.calls) == 3
    assert store.usage_by_batch(batch)["calls"] == 3
    photo = _one(store, batch, Status.SET_ASIDE)
    assert photo.filters["set_aside_by"] == ["theme"]
    assert photo.filters["theme"]["fit"] == "no"
    assert photo.filters["model"] == "fake/fake" and photo.filters["at"]
    assert not photo.metadata["warnings"]  # filter messages live in `filters` only


def test_theme_yes_and_partly_pass(cfg, store, photos_dir):
    cfg = _cfg(cfg)
    batch = _ready(cfg, store, photos_dir)
    summary, _ = _describe(cfg, store, batch, _resp(fit="partly"))
    assert summary.described == 3
    photo = _one(store, batch, Status.DESCRIBED)
    assert photo.filters["theme"]["fit"] == "partly" and photo.filters["verdict"] == "ok"


def test_theme_partly_can_be_flagged(cfg, store, photos_dir):
    cfg = _cfg(cfg, theme_partly="flag")
    batch = _ready(cfg, store, photos_dir)
    summary, _ = _describe(cfg, store, batch, _resp(fit="partly"))
    assert summary.needs_review == 3


def test_theme_check_off_or_no_theme_skips_theme(cfg, store, photos_dir):
    off = _cfg(cfg, theme_check=False, rules=[_rule()])
    batch = _ready(off, store, photos_dir)
    _, fake = _describe(off, store, batch, _resp(checks={"r1": "no"}))
    assert all("theme_fit" not in s["properties"] for s in fake.schemas)
    assert all("checks" in s["properties"] for s in fake.schemas)


def test_flag_rule_fail_needs_review_and_set_aside_rule_fail(cfg, store, photos_dir):
    flag = _cfg(cfg, theme="", rules=[_rule(mode="flag")])
    batch = _ready(flag, store, photos_dir)
    summary, _ = _describe(flag, store, batch, _resp(checks={"r1": "yes"}))
    assert summary.needs_review == 3
    photo = _one(store, batch, Status.NEEDS_REVIEW)
    assert photo.filters["rules"][0]["result"] == "fail"

    aside = _cfg(cfg, theme="", rules=[_rule(kind="include", text="a car", mode="set_aside")])
    summary, _ = _describe(aside, store, batch, _resp(checks={"r1": "no"}), only_ids=None)
    assert summary.described == 0  # nothing ready any more
    ids = [p.id for p in store.list_photos(batch, [Status.NEEDS_REVIEW])]
    summary, _ = _describe(aside, store, batch, _resp(checks={"r1": "no"}), only_ids=ids)
    assert summary.set_aside == 3
    photo = _one(store, batch, Status.SET_ASIDE)
    assert photo.filters["set_aside_by"] == ["rule:r1"]


def test_unusable_answer_needs_a_look(cfg, store, photos_dir):
    cfg = _cfg(cfg, theme="", rules=[_rule()])
    batch = _ready(cfg, store, photos_dir)
    summary, fake = _describe(cfg, store, batch, _resp())  # no checks at all
    assert summary.needs_review == 3 and len(fake.calls) == 3
    photo = _one(store, batch, Status.NEEDS_REVIEW)
    assert photo.filters["rules"][0]["result"] == "unknown"
    assert any("didn't answer" in m for m in filters.messages(photo.filters))


def test_guardrail_block_wins_over_set_aside(cfg, store, photos_dir):
    cfg = _cfg(cfg)
    batch = _ready(cfg, store, photos_dir)
    bad = _resp(fit="no", description="National Museum of Western Art in Ueno, Tokyo")
    summary, _ = _describe(cfg, store, batch, bad)
    assert summary.blocked == 3 and summary.set_aside == 0


def test_status_for_precedence(cfg):
    from shuto.metadata.guardrails import GuardrailResult
    from shuto.metadata.model import PhotoMetadata

    meta = PhotoMetadata(filename="a.jpg", description="d", keywords=[], categories=[])
    aside = {"verdict": "set_aside", "theme": {"effect": "set_aside"}, "rules": []}
    flag = {"verdict": "flag", "theme": {"effect": "flag"}, "rules": []}
    ok = GuardrailResult(action="ok")
    block = GuardrailResult(action="block", messages=["x"])
    assert workflow_checks._status_for(meta, block, aside) == Status.BLOCKED
    assert workflow_checks._status_for(meta, ok, aside) == Status.SET_ASIDE
    assert workflow_checks._status_for(meta, ok, flag) == Status.NEEDS_REVIEW
    assert workflow_checks._status_for(meta, ok, None) == Status.DESCRIBED
    assert (
        workflow_checks._status_for(meta, ok, {**aside, "overridden": {"at": "t"}})
        == Status.DESCRIBED
    )


def test_approve_anyway_is_recorded_and_bulk_skips_set_aside(cfg, store, photos_dir):
    cfg = _cfg(cfg)
    batch = _ready(cfg, store, photos_dir)
    _describe(cfg, store, batch, _resp(fit="no"))
    assert not review.approve(cfg, store, batch_id=batch, include_needs_review=True).approved
    photo = _one(store, batch, Status.SET_ASIDE)
    res = review.approve(cfg, store, photo_ids=[photo.id])
    assert res.approved == [photo.id]
    after = store.get_photo(photo.id)
    assert after.status == Status.APPROVED
    assert after.filters["overridden"]["status_was"] == "set_aside"
    assert filters.status_effect(after.filters) == "ok"
    events = store.events_for(photo.id)
    assert any(e["kind"] == "filters_overridden" for e in events)


def test_approve_anyway_still_refuses_blocked(cfg, store, photos_dir):
    cfg = _cfg(cfg)
    batch = _ready(cfg, store, photos_dir)
    _describe(cfg, store, batch, _resp(fit="no"))
    photo = _one(store, batch, Status.SET_ASIDE)
    meta = dict(photo.metadata, description="National Museum of Western Art in Ueno, Tokyo")
    store.update_photo(photo.id, metadata=meta)
    res = review.approve(cfg, store, photo_ids=[photo.id])
    assert photo.id in res.refused and not res.approved
    assert store.get_photo(photo.id).status == Status.BLOCKED


def test_edit_keeps_set_aside_and_never_moves_approved(cfg, store, photos_dir):
    cfg = _cfg(cfg)
    batch = _ready(cfg, store, photos_dir)
    _describe(cfg, store, batch, _resp(fit="no"))
    photo = _one(store, batch, Status.SET_ASIDE)
    edited = review.update_metadata(cfg, store, photo.id, title="New title")
    assert edited.status == Status.SET_ASIDE and edited.metadata["title"] == "New title"
    assert edited.filters == photo.filters
    review.approve(cfg, store, photo_ids=[photo.id])
    again = review.update_metadata(cfg, store, photo.id, title="Another")
    assert again.status == Status.APPROVED


def test_edit_does_not_flip_hand_approved_flagged_photo(cfg, store, photos_dir):
    cfg = _cfg(cfg, theme="", rules=[_rule(mode="flag")])
    batch = _ready(cfg, store, photos_dir)
    _describe(cfg, store, batch, _resp(checks={"r1": "yes"}))
    photo = _one(store, batch, Status.NEEDS_REVIEW)
    review.approve(cfg, store, photo_ids=[photo.id])
    assert review.update_metadata(cfg, store, photo.id, title="T").status == Status.APPROVED


def test_redescribing_set_aside_photo_is_allowed(cfg, store, photos_dir):
    cfg = _cfg(cfg)
    batch = _ready(cfg, store, photos_dir)
    _describe(cfg, store, batch, _resp(fit="no"))
    photo = _one(store, batch, Status.SET_ASIDE)
    summary, _ = _describe(cfg, store, batch, _resp(fit="yes"), only_ids=[photo.id])
    assert summary.described == 1
    assert store.get_photo(photo.id).filters["theme"]["fit"] == "yes"


def test_auto_approve_only_clean(cfg, store, photos_dir):
    cfg = _cfg(cfg)
    cfg = cfg.model_copy(
        update={"review": cfg.review.model_copy(update={"auto_approve_clean": True})}
    )
    batch = _ready(cfg, store, photos_dir)
    summary, _ = _describe(cfg, store, batch, _resp(fit="no"))
    assert summary.approved == 0 and summary.set_aside == 3


def test_ui_filters_override_config(cfg, store):
    base = _cfg(cfg, rules=[_rule(text="from file")])
    assert [r.text for r in filters.filter_spec(base).rules] == ["from file"]
    stored = settings_ui.set_ui_setting(
        store,
        "filters",
        {
            "theme_check": False,
            "theme_partly": "flag",
            "rules": [
                {"kind": "avoid", "text": "people", "mode": "flag"},
                {"kind": "include", "text": "a car"},
            ],
        },
    )
    assert [r["text"] for r in stored["rules"]] == ["people", "a car"]
    assert all(r["id"] for r in stored["rules"])
    applied = effective.effective_config(base, store)
    spec = filters.filter_spec(applied)
    assert [r.text for r in spec.rules] == ["people", "a car"]
    assert not spec.theme_check and spec.theme_partly == "flag" and not spec.theme_active
    assert [r.text for r in settings_ui.get_filter_rules(applied)] == ["people", "a car"]
    settings_ui.set_ui_setting(store, "filters", {"rules": []})
    assert filters.filter_spec(effective.effective_config(base, store)).rules == ()
    assert [r.text for r in filters.filter_spec(base).rules] == ["from file"]


@pytest.mark.parametrize(
    "value",
    [
        "nope",
        {"theme_partly": "maybe"},
        {"rules": [{"kind": "avoid", "text": "x"}]},
        {"rules": [{"kind": "avoid", "text": f"rule {i}"} for i in range(11)]},
        {"rules": [{"kind": "wrong", "text": "people"}]},
        {"rules": [{"kind": "avoid", "text": "a" * 81}]},
        {"rules": [{"kind": "avoid", "text": "bell\x07"}]},
    ],
)
def test_ui_filters_validation(store, value):
    with pytest.raises(errors.PipelineError):
        settings_ui.set_ui_setting(store, "filters", value)
    assert settings_ui.get_ui_setting(store, "filters") is None


def test_ui_filters_newline_is_collapsed(store):
    stored = settings_ui.set_ui_setting(
        store, "filters", {"rules": [{"kind": "avoid", "text": "two\nlines"}]}
    )
    assert stored["rules"][0]["text"] == "two lines"


def test_damaged_saved_filters_do_not_break_the_app(cfg, store):
    store.set_setting("ui.filters", {"rules": "garbage", "theme_check": "x"})
    assert effective.effective_config(cfg, store).filters == cfg.filters
    store.set_setting("ui.filters", {"rules": [{"kind": "x", "text": "bad rule"}]})
    assert effective.effective_config(cfg, store).filters == cfg.filters


def test_active_checks_and_doctor(cfg, store):
    assert workflow_checks.active_checks(cfg) == []
    assert workflow_checks.checks_summary(cfg) == "none"
    full = _cfg(cfg, rules=[_rule(), _rule(kind="include", text="a car", mode="flag", id="r2")])
    lines = workflow_checks.active_checks(full)
    assert lines[0].startswith('Theme "Everyday Japan"')
    assert lines[1:] == ["Must avoid: people (set aside)", "Must include: a car (just flag)"]
    assert (
        workflow_checks.checks_summary(full) == 'theme "Everyday Japan" (set aside if no) + 2 rules'
    )
    rep = workflow_models.doctor(full, store, client=_NoOllama())
    assert rep.checks == lines


class _NoOllama:
    url = "http://127.0.0.1:11434"

    def version(self):
        raise OSError("down")

    def tags(self):
        return []


def test_estimates_account_for_extra_tokens(cfg, store, photos_dir, monkeypatch):
    seen = []
    real = estimate.for_batch

    def spy(*a, **kw):
        seen.append((kw.get("prompt_tokens"), kw.get("output_tokens")))
        return real(*a, **kw)

    monkeypatch.setattr(estimate, "for_batch", spy)
    batch = _ready(cfg, store, photos_dir)
    describing.estimate_describe(cfg, store, batch)
    check_batch.estimate_check(cfg, store, batch)
    full = _cfg(cfg, rules=[_rule(), _rule(text="cars", id="r2")])
    describing.estimate_describe(full, store, batch)
    check_batch.estimate_check(full, store, batch)
    assert seen[:2] == [(1200, 600)] * 2
    assert seen[2:] == [(1200 + 80 + 60, 600 + 150)] * 2


# --------------------------------------------------------------------------- report


def _report_file(cfg, batch):
    return paths.report_path(cfg, batch)


def test_report_written_after_each_step(cfg, store, photos_dir):
    cfg = _cfg(cfg, rules=[_rule()])
    batch = _ready(cfg, store, photos_dir)
    path = _report_file(cfg, batch)
    assert path.is_file()  # cull_batch / import wrote it
    assert path.parent == paths.batch_dir(cfg, batch) and path.name == "report.csv"
    _describe(cfg, store, batch, _resp(fit="no", checks={"r1": "yes"}))
    text = path.read_bytes().decode("utf-8-sig")
    assert "Must avoid: people" in text and "set_aside" in text and "Everyday Japan" in text
    assert path.read_bytes().startswith(b"\xef\xbb\xbf")
    photo = _one(store, batch, Status.SET_ASIDE)
    review.approve(cfg, store, photo_ids=[photo.id])
    text = path.read_bytes().decode("utf-8-sig")
    header, records = reports.build_report(cfg, store, batch)
    row = next(r for r in records if r[header.index("status")] == "approved")
    assert row[header.index("approved_by_hand")] == "yes" and "approved" in text


def test_report_bytes_match_file_and_cover_all_photos(cfg, store, photos_dir):
    cfg = _cfg(cfg)
    batch = _ready(cfg, store, photos_dir)
    _describe(cfg, store, batch, _resp(fit="yes"))
    data = reports.report_bytes(cfg, store, batch)
    assert data == _report_file(cfg, batch).read_bytes()
    header, records = reports.build_report(cfg, store, batch)
    assert len(records) == 5 and header[0] == "file"
    names = {r[0]: r for r in records}
    assert names["blurry.jpg"][header.index("status")] == "culled"
    assert names["a.jpg"][header.index("theme_fit")] == "yes"
    assert names["a.jpg"][header.index("status_label")]


def test_report_failure_does_not_fail_the_step(cfg, store, photos_dir, monkeypatch):
    cfg = _cfg(cfg)
    batch = _ready(cfg, store, photos_dir)

    def boom(*a, **k):
        raise PermissionError("held open by Excel")

    monkeypatch.setattr(report, "write_report", boom)
    summary, _ = _describe(cfg, store, batch, _resp(fit="yes"))
    assert summary.described == 3
    photo = _one(store, batch, Status.DESCRIBED)
    assert review.approve(cfg, store, photo_ids=[photo.id]).approved == [photo.id]
    kinds = [e["kind"] for e in store.batch_events(batch, ["report_failed"])]
    assert kinds and "report_failed" in kinds


def test_report_duplicate_of_and_rejections(cfg, store, photos_dir):
    batch = _ready(cfg, store, photos_dir)
    _describe(cfg, store, batch, None)
    photo = _one(store, batch, Status.DESCRIBED)
    for to in (Status.APPROVED, Status.PREPARED, Status.UPLOADED, Status.EXPORTED):
        store.set_status(photo.id, to)
    resend.mark_submitted(store, batch, cfg=cfg)
    resend.record_result(store, photo.id, False, "focus", "soft", cfg=cfg)
    header, records = reports.build_report(cfg, store, batch)
    row = next(r for r in records if r[0] == photo.orig_name)
    assert row[header.index("status")] == "rejected"
    assert row[header.index("rejection_reason")] == "focus: soft"
    assert row[header.index("rejected_at")]
    assert "rejected" in _report_file(cfg, batch).read_bytes().decode("utf-8-sig")


def test_refresh_report_ignores_missing_batch(cfg, store):
    reports.refresh_report(cfg, store, None)
    reports.refresh_report(cfg, store, "no-such-batch")
    assert _report_file(cfg, "no-such-batch").is_file()
