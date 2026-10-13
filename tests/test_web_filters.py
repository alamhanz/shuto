"""Web side of the theme check, your own rules, Approve anyway and the batch report."""

from __future__ import annotations

import csv
import html
import io
import re
from urllib.parse import unquote

import pytest
from fastapi.testclient import TestClient

from shuto import pipeline
from shuto.llm.fake import DEFAULT_RESPONSE, FakeProvider
from shuto.models import LLMCall, Status
from shuto.web import create_app, ux

from .conftest import FakeOllama

BASE = "http://127.0.0.1:8765"
JARGON = (
    r"dHash|IPTC|CSRF|FTPS|cull|guardrail|env|keyring|terminal|setx|systemctl"
    r"|JSON|schema|prompt|LLM|verdict|fingerprint"
)


class Web:
    def __init__(self, app):
        self.state = app.state.web
        self.client = TestClient(app, base_url=BASE, follow_redirects=False)

    def get(self, path, **kw):
        return self.client.get(path, **kw)

    def post(self, path, data=None, token=True, **kw):
        data = dict(data or {})
        if token:
            data["csrf_token"] = self.state.csrf
        return self.client.post(path, data=data, **kw)


def text_of(page: str) -> str:
    page = re.sub(r"<(script|style)\b.*?</\1>", " ", page, flags=re.S | re.I)
    return html.unescape(re.sub(r"<[^>]+>", " ", page))


def no_jargon(page: str, where: str) -> None:
    found = re.findall(JARGON, text_of(page), flags=re.I)
    assert not found, (where, found)


@pytest.fixture
def web(cfg, monkeypatch):
    monkeypatch.setattr(pipeline, "ollama_client", lambda c: FakeOllama(pulled=["gemma3:4b"]))
    w = Web(create_app(cfg))
    pipeline.set_ui_setting(w.state.store, "onboarded", True)
    yield w
    w.state.runner.shutdown()


def _save_rules(web, rules=(), theme_check=True, partly="pass", **extra):
    data = {"theme_partly": partly, **extra}
    if theme_check:
        data["theme_check"] = "yes"
    for n, (kind, text, mode) in enumerate(rules):
        data.update({f"rule_kind_{n}": kind, f"rule_text_{n}": text, f"rule_mode_{n}": mode})
    return web.post("/settings/rules", data)


def _notice(resp, key):
    assert resp.status_code == 303, resp.text
    match = re.search(rf"{key}=([^&]*)", resp.headers["location"])
    return unquote(match.group(1)) if match else ""


@pytest.fixture
def checked(web, photos_dir):
    """A batch of 5 photos, 3 of them described with theme 'no' (so set aside)."""
    web.post("/settings/style", {"theme": "Everyday Japan", "tone": "", "always_include": ""})
    store, cfg = web.state.store, web.state.cfg
    batch = next(iter(pipeline.import_folder(cfg, store, photos_dir).batches))
    pipeline.cull_batch(cfg, store, batch)
    response = dict(DEFAULT_RESPONSE, theme_fit={"reason": "It is a desert.", "fit": "no"})
    fake = FakeProvider("fake", response=[response])
    summary = pipeline.describe_batch(cfg, store, batch, llm=fake)
    assert summary.set_aside == 3
    return batch


def _aside(web, batch):
    return web.state.store.list_photos(batch, [Status.SET_ASIDE])


# ------------------------------------------------------------------ settings


def test_settings_shows_the_rules_section(web):
    text = web.get("/settings").text
    assert 'id="rules"' in text and "Your photo rules" in text
    assert "Must include" in text and "Must avoid" in text
    assert "Set aside" in text and "Just flag" in text
    assert "At most 10 rules" in text
    no_jargon(text, "/settings")


def test_save_rules_roundtrip(web):
    r = _save_rules(web, [("avoid", "people", "set_aside"), ("include", "a car", "flag")])
    assert "saved" in _notice(r, "notice")
    stored = pipeline.get_ui_setting(web.state.store, "filters")
    assert [(x["kind"], x["text"], x["mode"]) for x in stored["rules"]] == [
        ("avoid", "people", "set_aside"),
        ("include", "a car", "flag"),
    ]
    assert all(x["id"] for x in stored["rules"])
    assert [x.text for x in web.state.cfg.filters.rules] == ["people", "a car"]
    page = web.get("/settings").text
    assert 'value="people"' in page and 'value="a car"' in page and "2 saved" in page


def test_rule_ids_survive_an_edit_and_remove_works(web):
    _save_rules(web, [("avoid", "people", "set_aside"), ("avoid", "cars", "flag")])
    first, second = pipeline.get_ui_setting(web.state.store, "filters")["rules"]
    data = {
        "rule_id_0": first["id"],
        "rule_kind_0": "avoid",
        "rule_text_0": "crowds",
        "rule_mode_0": "flag",
        "rule_id_1": second["id"],
        "rule_kind_1": "avoid",
        "rule_text_1": "cars",
        "rule_mode_1": "flag",
        "rule_remove_1": "yes",
        "theme_check": "yes",
        "theme_partly": "pass",
    }
    web.post("/settings/rules", data)
    (kept,) = pipeline.get_ui_setting(web.state.store, "filters")["rules"]
    assert kept["id"] == first["id"] and kept["text"] == "crowds" and kept["mode"] == "flag"


@pytest.mark.parametrize(
    ("rules", "expect"),
    [
        ([("avoid", f"thing {n}", "flag") for n in range(11)], "at most 10"),
        ([("avoid", "x" * 81, "flag")], "at most 80"),
        ([("avoid", "x", "flag")], "at least 2"),
        ([("sideways", "people", "flag")], "Must include"),
        ([("avoid", "people", "maybe")], "Just flag"),
    ],
)
def test_bad_rules_give_plain_errors_and_save_nothing(web, rules, expect):
    r = _save_rules(web, rules)
    assert expect in _notice(r, "error")
    assert pipeline.get_ui_setting(web.state.store, "filters") is None
    assert web.state.cfg.filters.rules == []


def test_ten_rules_are_allowed(web):
    r = _save_rules(web, [("avoid", f"thing {n}", "flag") for n in range(10)])
    assert _notice(r, "error") == ""
    page = web.get("/settings").text
    assert "10 saved" in page and "reached the limit" in page


def test_theme_check_and_partly_settings(web):
    _save_rules(web, theme_check=False, partly="flag")
    assert web.state.cfg.filters.theme_check is False
    assert web.state.cfg.filters.theme_partly == "flag"
    page = web.get("/settings").text
    assert re.search(r'name="theme_check"[^>]*value="yes"(?![^>]*checked)', page)
    assert re.search(r'<option value="flag" selected>', page)
    bad = _save_rules(web, partly="sometimes")
    assert "partly" in _notice(bad, "error")


def test_rules_post_needs_csrf(web):
    assert web.post("/settings/rules", {"theme_partly": "pass"}, token=False).status_code == 403
    assert pipeline.get_ui_setting(web.state.store, "filters") is None


def test_wizard_style_step_mentions_theme_check(web):
    text = web.get("/welcome?step=5").text
    assert "also used to check each photo" in text
    assert 'form="wizard-form"' in text
    no_jargon(text, "/welcome?step=5")


# ------------------------------------------------------------------ review


def test_label_and_bucket_know_set_aside():
    assert ux.label("set_aside") == "Set aside: doesn't fit"
    assert ux.label(Status.SET_ASIDE) == "Set aside: doesn't fit"
    assert ux.bucket(Status.SET_ASIDE) == "aside"


def test_ai_time_text():
    assert ux.ai_time_text(0) == "0 s"
    assert ux.ai_time_text(4.24) == "4.2 s"
    assert ux.ai_time_text(45) == "45 s"
    assert ux.ai_time_text(192) == "3 min 12 s"
    assert ux.ai_time_text(3900) == "1 h 5 min"


def test_set_aside_photos_show_layer_and_reason(web, checked):
    text = text_of(web.get(f"/batch/{checked}/review?show=aside").text)
    assert "Set aside: doesn't fit" in text
    assert "Theme" in text and "It is a desert." in text
    nothing = web.get(f"/batch/{checked}/review?show=good").text
    assert "It is a desert." not in nothing
    no_jargon(text, "review aside")


def test_why_sub_filter_counts_and_whitelist(web, checked):
    page = web.get(f"/batch/{checked}/review?show=aside").text
    assert re.search(r"Your theme\s*<span class=\"chip-count\">3<", page)
    assert re.search(r"Quality\s*<span class=\"chip-count\">2<", page)
    assert web.get(f"/batch/{checked}/review?show=aside&why=theme").text.count("<article") == 3
    assert web.get(f"/batch/{checked}/review?show=aside&why=quality").text.count("<article") == 2
    assert web.get(f"/batch/{checked}/review?show=aside&why=rules").text.count("<article") == 0
    weird = web.get(f"/batch/{checked}/review?show=aside&why=%3Cb%3E")
    assert weird.status_code == 200 and weird.text.count("<article") == 5


def test_rule_failure_names_your_rule(web, photos_dir):
    _save_rules(web, [("avoid", "people", "set_aside")], theme_check=False)
    store, cfg = web.state.store, web.state.cfg
    batch = next(iter(pipeline.import_folder(cfg, store, photos_dir).batches))
    pipeline.cull_batch(cfg, store, batch)
    response = dict(DEFAULT_RESPONSE, checks={"r1": {"reason": "Two people.", "answer": "yes"}})
    pipeline.describe_batch(cfg, store, batch, llm=FakeProvider("fake", response=[response]))
    page = web.get(f"/batch/{batch}/review?show=aside&why=rules").text
    assert page.count("<article") == 3
    assert "Your rule" in page and "Two people." in page


def test_unusable_answer_goes_to_needs_a_look(web, photos_dir):
    _save_rules(web, [("avoid", "people", "set_aside")], theme_check=False)
    store, cfg = web.state.store, web.state.cfg
    batch = next(iter(pipeline.import_folder(cfg, store, photos_dir).batches))
    pipeline.cull_batch(cfg, store, batch)
    pipeline.describe_batch(
        cfg, store, batch, llm=FakeProvider("fake", response=[dict(DEFAULT_RESPONSE)])
    )
    page = web.get(f"/batch/{batch}/review?show=attention").text
    assert page.count("<article") == 3 and "didn't answer" in text_of(page)


# ------------------------------------------------------------------ photo page + Approve anyway


def test_photo_page_lists_your_checks(web, checked):
    photo = _aside(web, checked)[0]
    page = web.get(f"/photo/{photo.id}").text
    text = text_of(page)
    assert "Your checks" in text and "Doesn't fit" in text
    assert "It is a desert." in text and "Approve anyway" in text
    no_jargon(page, "photo page")


def test_photo_page_without_checks(web, photos_dir):
    store, cfg = web.state.store, web.state.cfg
    batch = next(iter(pipeline.import_folder(cfg, store, photos_dir).batches))
    pipeline.cull_batch(cfg, store, batch)
    pipeline.describe_batch(cfg, store, batch, llm=FakeProvider("fake"))
    photo = store.list_photos(batch, [Status.DESCRIBED])[0]
    text = web.get(f"/photo/{photo.id}").text
    assert "Your checks" in text and "Not checked with a theme" in text
    assert "Approve anyway" not in text


def test_older_rules_note_is_quiet_and_only_when_changed(web, checked):
    photo = _aside(web, checked)[0]
    assert "earlier theme or rules" not in web.get(f"/photo/{photo.id}").text
    _save_rules(web, [("avoid", "people", "flag")])
    text = web.get(f"/photo/{photo.id}").text
    assert "earlier theme or rules" in text
    assert "recheck" not in text.lower() and "check again" not in text.lower()


def test_approve_anyway(web, checked):
    photo = _aside(web, checked)[0]
    assert web.post(f"/photo/{photo.id}/approve", token=False).status_code == 403
    assert web.state.store.get_photo(photo.id).status == Status.SET_ASIDE
    r = web.post(f"/photo/{photo.id}/approve", {"back": "aside"})
    assert r.status_code == 303 and "error=" not in r.headers["location"]
    after = web.state.store.get_photo(photo.id)
    assert (
        after.status == Status.APPROVED and after.filters["overridden"]["status_was"] == "set_aside"
    )
    assert "You approved this photo" in web.get(f"/photo/{photo.id}").text


def test_approve_anyway_with_the_edit_form_keeps_going(web, checked):
    photo = _aside(web, checked)[0]
    meta = photo.metadata
    form = {
        "description": meta["description"],
        "title": "",
        "keywords": ", ".join(meta["keywords"]),
        "editorial": "no",
    }
    r = web.post(f"/photo/{photo.id}/approve", form)
    assert r.status_code == 303 and "error=" not in r.headers["location"]
    assert web.state.store.get_photo(photo.id).status == Status.APPROVED


def test_approve_anyway_is_refused_for_a_shutterstock_block(web, checked):
    photo = _aside(web, checked)[0]
    store = web.state.store
    meta = dict(photo.metadata, description="National Museum of Western Art in Ueno, Tokyo")
    store.update_photo(photo.id, metadata=meta)
    r = web.post(f"/photo/{photo.id}/approve")
    assert r.status_code == 303 and "error=" in r.headers["location"]
    assert store.get_photo(photo.id).status == Status.BLOCKED
    again = web.post(f"/photo/{photo.id}/approve")  # a blocked photo stays blocked
    assert "error=" in again.headers["location"]
    assert store.get_photo(photo.id).status == Status.BLOCKED


def test_never_moves_approved_photos_when_rules_change(web, checked):
    photo = _aside(web, checked)[0]
    web.post(f"/photo/{photo.id}/approve")
    _save_rules(web, [("avoid", "people", "set_aside")])
    assert web.state.store.get_photo(photo.id).status == Status.APPROVED


def test_check_page_lists_active_checks(web, batch_new):
    _save_rules(web, [("avoid", "people", "set_aside")])
    text = web.get(f"/batch/{batch_new}/check").text
    assert "Must avoid: people" in text and "Everyday Japan" in text
    no_jargon(text, "check page")


@pytest.fixture
def batch_new(web, photos_dir):
    web.post("/settings/style", {"theme": "Everyday Japan", "tone": "", "always_include": ""})
    store, cfg = web.state.store, web.state.cfg
    return next(iter(pipeline.import_folder(cfg, store, photos_dir).batches))


# ------------------------------------------------------------------ report


def _log_times(web, batch, seconds=2.5, retries=1):
    for p in web.state.store.list_photos(batch):
        if p.status in (Status.SET_ASIDE, Status.DESCRIBED, Status.NEEDS_REVIEW):
            web.state.store.log_llm_call(
                LLMCall("fake", "fake", photo_id=p.id, seconds=seconds, retries=retries)
            )


def _rows(resp):
    body = resp.content.decode("utf-8-sig")
    return list(csv.DictReader(io.StringIO(body)))


def test_report_download_headers_bom_and_columns(web, checked):
    _log_times(web, checked)
    r = web.get(f"/batch/{checked}/report.csv")
    assert r.status_code == 200
    assert r.content.startswith(b"\xef\xbb\xbf")
    assert r.headers["content-type"].startswith("text/csv")
    assert "charset=utf-8" in r.headers["content-type"]
    assert r.headers["content-disposition"].startswith('attachment; filename="shuto-report-')
    assert r.headers["content-disposition"].endswith('.csv"')
    assert r.headers["x-content-type-options"] == "nosniff"
    rows = _rows(r)
    assert len(rows) == 5  # one row per photo
    assert "ai_seconds" in rows[0] and "ai_tries" in rows[0]
    asides = [x for x in rows if x["status"] == "set_aside"]
    assert len(asides) == 3
    assert all(x["ai_seconds"] == "2.5" and x["ai_tries"] == "2" for x in asides)
    assert all(x["theme_fit"] == "no" and x["theme_reason"] == "It is a desert." for x in asides)
    culled = [x for x in rows if x["status"] == "culled"]
    assert all(x["ai_seconds"] == "" and x["ai_tries"] == "" for x in culled)


def test_report_uses_the_latest_describe(web, checked):
    _log_times(web, checked, seconds=9.0, retries=0)
    _log_times(web, checked, seconds=1.0, retries=2)
    asides = [x for x in _rows(web.get(f"/batch/{checked}/report.csv")) if x["ai_seconds"]]
    assert {(x["ai_seconds"], x["ai_tries"]) for x in asides} == {("1.0", "3")}


def test_report_unknown_or_path_like_batch_ids_are_404(web, checked):
    for bad in ("nope", "..%2F..%2Fetc%2Fpasswd", "..", "%2e%2e%2freports"):
        assert web.get(f"/batch/{bad}/report.csv").status_code == 404, bad


def test_report_is_read_only_get_and_has_no_post(web, checked):
    assert web.post(f"/batch/{checked}/report.csv").status_code in (404, 405)


def test_review_and_submit_show_download_and_ai_time(web, checked):
    _log_times(web, checked, seconds=2.5, retries=0)
    for page in ("review", "submit"):
        text = web.get(f"/batch/{checked}/{page}").text
        assert f'href="/batch/{checked}/report.csv"' in text, page
        assert "Download report" in text, page
        assert (
            "AI time: 7.5 s in total, 2.5 s per photo on average (3 photos)"
            in text_of(text).replace("\n", " ").replace("  ", " ")
            or "7.5 s in total" in text
        ), page
        no_jargon(text, page)


def test_no_ai_time_before_the_ai_has_run(web, photos_dir):
    store, cfg = web.state.store, web.state.cfg
    batch = next(iter(pipeline.import_folder(cfg, store, photos_dir).batches))
    text = web.get(f"/batch/{batch}/review?show=all").text
    assert "Download report" in text and "AI time" not in text


def test_report_file_is_refreshed_with_cfg_at_web_call_sites(web, checked):
    path = pipeline.report_path(web.state.cfg, checked)
    path.unlink(missing_ok=True)
    culled = web.state.store.list_photos(checked, [Status.CULLED])[0]
    r = web.post(f"/photo/{culled.id}/ready")
    assert r.status_code == 303
    assert path.is_file()


def test_ai_time_summary(web, checked):
    assert pipeline.ai_time_summary(web.state.store, checked)["photos"] == 3
    _log_times(web, checked, seconds=4.0)
    s = pipeline.ai_time_summary(web.state.store, checked)
    assert s["photos"] == 3 and s["total"] == pytest.approx(12.0)
    assert s["average"] == pytest.approx(4.0)


# ------------------------------------------------------------------ partly, all set aside, shortcut


def _partly_batch(web, photos_dir):
    web.post("/settings/style", {"theme": "Everyday Japan", "tone": "", "always_include": ""})
    store, cfg = web.state.store, web.state.cfg
    batch = next(iter(pipeline.import_folder(cfg, store, photos_dir).batches))
    pipeline.cull_batch(cfg, store, batch)
    response = dict(DEFAULT_RESPONSE, theme_fit={"reason": "Only a bit rural.", "fit": "partly"})
    pipeline.describe_batch(cfg, store, batch, llm=FakeProvider("fake", response=[response]))
    return batch


def test_partly_photo_that_passes_shows_reason_and_theme_chip(web, photos_dir):
    batch = _partly_batch(web, photos_dir)
    store = web.state.store
    assert store.list_photos(batch, [Status.DESCRIBED])
    page = web.get(f"/batch/{batch}/review?show=good").text
    assert "Only a bit rural." in page and "Only partly fits your theme" in text_of(page)
    assert "Theme" in text_of(page)
    photo = store.list_photos(batch, [Status.DESCRIBED])[0]
    assert "Only a bit rural." in web.get(f"/photo/{photo.id}").text


def test_batch_with_everything_set_aside_links_to_them(web, checked):
    store = web.state.store
    for p in store.list_photos(checked, [Status.CULLED]):
        store.set_status(p.id, Status.SET_ASIDE, force=True)
    assert len(_aside(web, checked)) == 5
    counts = {"set_aside": 5}
    nxt = ux.next_action(checked, counts)
    assert nxt["label"] == "Review 5 set-aside photos" and nxt["href"].endswith("?show=aside")
    home = web.get("/").text
    assert "Review 5 set-aside photos" in home and f"/batch/{checked}/review?show=aside" in home
    assert web.get(nxt["href"]).status_code == 200


def test_a_shortcut_does_not_approve_a_set_aside_photo(web, checked):
    aside = _aside(web, checked)[0]
    page = web.get(f"/photo/{aside.id}").text
    assert 'id="approve-form"' not in page and "Approve anyway" in page
    store = web.state.store
    good = store.list_photos(checked, [Status.CULLED])[0]
    store.set_status(good.id, Status.NEEDS_REVIEW, force=True)
    assert 'id="approve-form"' in web.get(f"/photo/{good.id}").text
