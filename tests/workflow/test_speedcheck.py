# ruff: noqa: F811
"""Live speed check: maths, cadence, changes applied to the photos not started yet."""

from __future__ import annotations

import itertools
import threading

import pytest

from shuto.ai import hardware, speedcheck
from shuto.ai.providers.fake import DEFAULT_RESPONSE, FakeProvider, vary
from shuto.ai.speedcheck import LiveRun, ModelOption
from shuto.core import errors
from shuto.core.models import Status
from shuto.settings import effective
from shuto.settings import ui as settings_ui
from shuto.workflow import check_batch, describing, importing, reports, review, speed

from ..web.test_web_batch import batch, web  # noqa: F401  (fixtures)


def _live(expected=10.0, tries=8, options=None, factory=None, model="fake"):
    return LiveRun(
        model=model, max_tries=tries, expected_s=expected, options=options, llm_factory=factory
    )


def _feed(live, seconds, n):
    last = None
    for _ in range(n):
        last = live.record(seconds)
    return last


def _fast_models(*items):
    return lambda model: (10.0, list(items))


# ------------------------------------------------------------------ maths


def test_running_average():
    assert speedcheck.running_average([]) == 0.0
    assert speedcheck.running_average([10, 20, 30]) == 20.0


def test_slow_threshold_is_30_percent_over_expected():
    assert speedcheck.SLOW_FACTOR == 1.3 and speedcheck.CHECK_EVERY == 5
    assert speedcheck.is_slow(13.0, 10.0)
    assert speedcheck.is_slow(20.0, 10.0)
    assert not speedcheck.is_slow(12.99, 10.0)
    assert not speedcheck.is_slow(5.0, 10.0)
    assert not speedcheck.is_slow(5.0, 0.0)


def test_check_is_due_every_five_photos():
    due = [n for n in range(0, 21) if speedcheck.is_check_due(n)]
    assert due == [5, 10, 15, 20]


def test_suggested_tries():
    assert speedcheck.suggested_tries(8) == 3
    assert speedcheck.suggested_tries(4) == 3
    assert speedcheck.suggested_tries(3) == 2
    assert speedcheck.suggested_tries(2) == 1
    assert speedcheck.suggested_tries(1) is None


# ------------------------------------------------------------------ cadence + suggestion


def test_only_looks_at_every_fifth_photo():
    live = _live()
    assert _feed(live, 20.0, 4) is None
    first = live.record(20.0)  # the 5th
    assert first is not None and first.average == 20.0 and first.expected == 10.0
    assert first.tries == 3 and first.current_tries == 8
    for _ in range(4):  # 6th to 9th: the same suggestion stays
        assert live.record(20.0) is first
    assert live.record(20.0) is not first  # the 10th is looked at again


def test_not_slow_enough_gives_no_suggestion():
    live = _live()
    assert _feed(live, 12.9, 5) is None
    assert live.suggestion() is None


def test_no_suggestion_when_nothing_can_be_changed():
    live = _live(tries=1)
    assert _feed(live, 50.0, 5) is None


def test_keep_going_asks_again_only_when_worse():
    live = _live()
    assert _feed(live, 20.0, 5) is not None
    live.dismiss()
    assert live.suggestion() is None
    assert _feed(live, 20.0, 5) is None  # same speed as when dismissed: quiet
    again = _feed(live, 80.0, 5)  # much slower: asked again
    assert again is not None and again.average > 20.0


def test_options_installed_first_and_missing_one_flagged():
    live = _live(
        options=_fast_models(
            ModelOption("not-here:1b", 2.0, False),
            ModelOption("have-b:2b", 6.0, True),
            ModelOption("have-a:3b", 8.0, True),
        )
    )
    s = _feed(live, 30.0, 5)
    assert [m.tag for m in s.models] == ["have-b:2b", "have-a:3b", "not-here:1b"]
    assert [m.tag for m in s.installed_models] == ["have-b:2b", "have-a:3b"]
    assert [m.tag for m in s.missing_models] == ["not-here:1b"]


def test_option_lookup_failure_still_offers_fewer_tries():
    def boom(model):
        raise RuntimeError("ollama is not running")

    s = _feed(_live(options=boom), 30.0, 5)
    assert s is not None and s.tries == 3 and s.models == ()


# ------------------------------------------------------------------ choices apply at once


def test_fewer_tries_applies_and_restarts_the_window():
    live = _live()
    _feed(live, 30.0, 5)
    live.set_max_tries(3)
    assert live.snapshot() == ("fake", 3)
    assert live.suggestion() is None and "up to 3 tries" in live.notice
    assert _feed(live, 30.0, 4) is None  # photos 6-9
    assert live.record(30.0) is not None  # the 10th, with a fresh window, is slow again


def test_model_switch_only_to_an_installed_offered_model():
    live = _live(
        options=_fast_models(ModelOption("have:2b", 5.0, True), ModelOption("miss:1b", 2.0, False))
    )
    _feed(live, 30.0, 5)
    assert not live.switch_model("miss:1b")  # must be downloaded first
    assert not live.switch_model("other:7b")  # not offered / does not fit
    assert live.snapshot()[0] == "fake"
    assert live.switch_model("have:2b")
    assert live.snapshot()[0] == "have:2b" and live.expected_s == 5.0


def test_llm_for_builds_once_and_defaults():
    built = []

    def factory(tag):
        built.append(tag)
        return FakeProvider(model=tag)

    live = _live(factory=factory)
    base = FakeProvider(model="fake")
    assert live.llm_for("fake", base) is base
    a = live.llm_for("small", base)
    assert a.model == "small" and live.llm_for("small", base) is a and built == ["small"]


def test_record_is_thread_safe():
    live = _live()
    threads = [
        threading.Thread(target=lambda: [live.record(1.0) for _ in range(50)]) for _ in range(8)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert live._described == 400


# ------------------------------------------------------------------ describe_batch


def _bad(n):
    return {**DEFAULT_RESPONSE, "description": f"Street {n}", "keywords": ["street", "tokyo"]}


@pytest.fixture
def ready(cfg, store, photos_dir):
    cfg = cfg.model_copy(update={"llm": cfg.llm.model_copy(update={"concurrency": 1})})
    bid = next(iter(importing.import_folder(cfg, store, photos_dir).batches))
    for p in store.list_photos(bid):
        store.set_status(p.id, Status.READY, force=True)
    return cfg, store, bid


def test_changed_tries_apply_to_the_remaining_photos_and_are_recorded(ready):
    cfg, store, bid = ready
    live = _live(tries=4)
    counter = itertools.count(1)
    calls = []

    def answer(req):
        n = next(counter)
        calls.append(n)
        if n == 4:  # the first photo has used its 4 tries: ask for fewer from now on
            live.set_max_tries(2)
        return _bad(n)

    describing.describe_batch(cfg, store, bid, llm=FakeProvider(response=answer), live=live)
    photos = store.list_photos(bid)
    tries = sorted(p.metadata["ai_run"]["max_tries"] for p in photos)
    assert tries == [2] * (len(photos) - 1) + [4]
    assert len(calls) == 4 + 2 * (len(photos) - 1)
    assert all(p.metadata["ai_run"]["model"] == "fake/fake" for p in photos)


def test_model_switch_applies_to_remaining_photos_and_is_recorded(ready):
    cfg, store, bid = ready
    used = []

    def small_answer(req):
        used.append("small:1b")
        return vary(DEFAULT_RESPONSE)(req)

    live = _live(
        options=_fast_models(ModelOption("small:1b", 3.0, True)),
        factory=lambda tag: FakeProvider(model=tag, response=small_answer),
        expected=1.0,
    )
    first_done = []

    def first_answer(req):
        if not first_done:
            first_done.append(True)
            _feed(live, 50.0, 5)  # the speed check fires during the first photo
            assert live.switch_model("small:1b")
        return vary(DEFAULT_RESPONSE)(req)

    base = FakeProvider(response=first_answer)
    describing.describe_batch(cfg, store, bid, llm=base, live=live)
    photos = store.list_photos(bid)
    models = [p.metadata["ai_run"]["model"] for p in photos]
    assert models.count("fake/fake") == 1 and models.count("fake/small:1b") == len(photos) - 1
    assert sum(p.metadata_source == "llm:fake/small:1b" for p in photos) == len(photos) - 1
    assert len(used) == len(photos) - 1  # the new model really answered them


def test_without_live_the_config_value_is_used_and_recorded(ready):
    cfg, store, bid = ready
    cfg = cfg.model_copy(update={"llm": cfg.llm.model_copy(update={"max_tries": 5})})
    describing.describe_batch(cfg, store, bid, llm=FakeProvider(response=None))
    assert {p.metadata["ai_run"]["max_tries"] for p in store.list_photos(bid)} == {5}


def test_report_has_model_and_max_tries_per_photo(ready):
    cfg, store, bid = ready
    describing.describe_batch(cfg, store, bid, llm=FakeProvider(response=None))
    header, rows = reports.build_report(cfg, store, bid)
    i, j = header.index("ai_max_tries"), header.index("ai_model")
    assert {r[i] for r in rows} == {str(cfg.llm.max_tries)} and all(r[j] for r in rows)
    pid = store.list_photos(bid)[0].id  # an edit keeps the record
    review.update_metadata(cfg, store, pid, title="Morning street")
    assert store.get_photo(pid).metadata["ai_run"]["max_tries"] == cfg.llm.max_tries


# ------------------------------------------------------------------ local only, same estimate


class _Api:
    name = "anthropic"
    model = "claude-x"


def _local(cfg, model):
    return cfg.model_copy(
        update={"llm": cfg.llm.model_copy(update={"provider": "ollama", "model": model})}
    )


def test_api_and_fake_runs_have_no_speed_check(cfg, store):
    assert speed.make_live_run(cfg, store) is None  # fake provider
    assert speed.make_live_run(cfg, store, _Api()) is None


def test_local_run_gets_a_live_check_with_the_estimate_shown_before(cfg, store):
    local = _local(cfg, "gemma3:4b")
    live = speed.make_live_run(local, store)
    assert live is not None and live.model == "gemma3:4b" and live.max_tries == 8
    bid = "b"
    store.upsert_batch(bid)
    store.add_photo(sha256="0" * 64, src_path="/x.jpg", orig_name="x.jpg", batch_id=bid)
    est = check_batch.estimate_check(local, store, bid)
    assert est.per_image == pytest.approx(live.expected_s)


def test_unknown_model_has_no_expected_time_so_no_check(cfg, store):
    assert speed.make_live_run(_local(cfg, "mystery:1b"), store) is None


def test_faster_options_fit_and_installed_first(cfg, monkeypatch):
    class Found:
        pulled = ["moondream:latest"]

    monkeypatch.setattr(hardware, "ollama_status", lambda c: Found())
    current, options = speed.faster_model_options(cfg, "gemma3:12b")
    assert current > 0 and options
    assert all(o.seconds < current for o in options)
    flags = [o.installed for o in options]
    assert flags == sorted(flags, reverse=True)  # installed ones first
    assert all(o.tag != "gemma3:12b" for o in options)


# ------------------------------------------------------------------ settings


def test_max_tries_setting_validates_and_reaches_the_config(cfg, store):
    assert settings_ui.set_ui_setting(store, "max_tries", "3") == 3
    assert effective.effective_config(cfg, store).llm.max_tries == 3
    for bad in ("0", "9", "x", None, True):
        with pytest.raises(errors.PipelineError):
            settings_ui.set_ui_setting(store, "max_tries", bad)
    assert settings_ui.get_ui_setting(store, "max_tries") == 3


def test_settings_page_saves_max_tries(web):
    page = web.get("/settings").text
    assert 'name="max_tries"' in page and "Fewer tries is faster" in page
    r = web.post("/settings/tries", {"max_tries": "2"})
    assert r.status_code == 303 and web.state.cfg.llm.max_tries == 2
    assert 'value="2" selected' in web.get("/settings").text
    web.post("/settings/tries", {"max_tries": "12"})
    assert web.state.cfg.llm.max_tries == 2


# ------------------------------------------------------------------ web panel


@pytest.fixture
def running(web):
    """A job that stays active, with a live check that already has a suggestion."""
    gate = threading.Event()
    web.state.store.upsert_batch("b1")
    job_id = web.state.runner.submit("check", "b1", lambda ctx: gate.wait(10))
    live = _live(
        options=_fast_models(ModelOption("have:2b", 5.0, True), ModelOption("miss:1b", 2.0, False))
    )
    _feed(live, 30.0, 5)
    web.state.live["b1"] = live
    yield web, live, job_id
    gate.set()
    web.state.runner.wait(job_id)


def test_job_panel_shows_the_suggestion_in_plain_words(running):
    web, live, job_id = running
    text = web.get(f"/jobs/{job_id}").text
    assert "This is slower than expected" in text and "about 30 s per photo" in text
    assert "Use up to 3 tries" in text and "Switch to have:2b" in text
    assert "miss:1b would be faster" in text and "must be downloaded first" in text
    assert "Keep going" in text


def test_no_panel_without_a_suggestion_or_a_live_check(running):
    web, live, job_id = running
    live.dismiss()
    assert "slower than expected" not in web.get(f"/jobs/{job_id}").text
    web.state.live.clear()
    assert web.get(f"/jobs/{job_id}").status_code == 200


def test_click_fewer_tries_applies_now(running):
    web, live, job_id = running
    r = web.post("/batch/b1/speed/tries")
    assert r.status_code == 200 and live.snapshot() == ("fake", 3)
    assert "up to 3 tries" in r.text and "slower than expected" not in r.text


def test_click_switch_model_and_refused_model(running):
    web, live, job_id = running
    r = web.post("/batch/b1/speed/model", {"model": "miss:1b"})
    assert "cannot be used now" in r.text and live.snapshot()[0] == "fake"
    r = web.post("/batch/b1/speed/model", {"model": "have:2b"})
    assert live.snapshot()[0] == "have:2b" and "now uses have:2b" in r.text


def test_click_keep_going_hides_the_panel(running):
    web, live, job_id = running
    r = web.post("/batch/b1/speed/keep")
    assert "slower than expected" not in r.text and live.snapshot() == ("fake", 8)


def test_speed_route_rejects_unknown_choice_and_needs_csrf(running):
    web, live, job_id = running
    assert web.post("/batch/b1/speed/nope").status_code == 404
    assert web.client.post("/batch/b1/speed/tries").status_code == 403


def test_web_check_runs_with_a_live_check_and_cleans_up(web, batch, monkeypatch):
    made = []

    def make(cfg, store, llm=None):
        live = _live(expected=0.0001)
        made.append(live)
        return live

    monkeypatch.setattr(speed, "make_live_run", make)
    job = web.run_job(f"/batch/{batch}/check")
    assert job.state == "done", job.error
    assert made and made[0]._described > 0
    assert web.state.live == {}


def test_web_check_goes_on_when_the_speed_check_fails(web, batch, monkeypatch):
    def broken(cfg, store, llm=None):
        raise RuntimeError("hardware detection failed")

    monkeypatch.setattr(speed, "make_live_run", broken)
    job = web.run_job(f"/batch/{batch}/check")
    assert job.state == "done", job.error
    assert web.state.live == {}


def test_max_tries_is_one_to_eight_everywhere(web):
    from pydantic import ValidationError

    from shuto.core.config import LLMConfig

    with pytest.raises(ValidationError):
        LLMConfig(max_tries=12)
    page = web.get("/settings").text
    assert 'value="8" selected' in page
