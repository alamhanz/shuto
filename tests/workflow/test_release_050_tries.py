"""0.5.0 hardening: 8 tries, English only, short prompt, HEIC, GPS at upload, place source."""

from __future__ import annotations

import io
import sys
from datetime import datetime

import pytest
from PIL import Image

from shuto.ai import catalog as local_models
from shuto.ai import selection
from shuto.ai.describe import Style, build_prompt, describe_photo, resolve_place
from shuto.ai.providers.fake import DEFAULT_RESPONSE, FakeProvider
from shuto.core.models import Status
from shuto.deliver import csv_export, prepare
from shuto.ingest import imaging
from shuto.ingest.photo_meta import PhotoMeta
from shuto.metadata import embed, guardrails
from shuto.metadata import rules as metadata_rules
from shuto.places import geo
from shuto.places.context import build_context
from shuto.workflow import delivering, describing, exporting, importing, review

from ..ingest.test_photo_meta import SHIBUYA, make_jpeg
from ..web.test_web_batch import web  # noqa: F401  (fixture)

R = guardrails.load_restrictions()
WHEN = datetime(2026, 10, 5, 9, 30)


def _resp(**over):
    return {**DEFAULT_RESPONSE, **over}


def _bad(n: int) -> dict:
    """A different answer each time that still breaks a hard rule (too few words)."""
    return _resp(description=f"Street {n}", keywords=["street", "tokyo"])


def _describe(llm, **kw):
    return describe_photo(llm, b"j", "a.jpg", style=Style(), restrictions=R, captured=WHEN, **kw)


# ------------------------------------------------------------------ up to 8 tries


def test_default_is_eight_tries():
    assert metadata_rules.MAX_TRIES == 8
    llm = FakeProvider(response=[_bad(i) for i in range(20)])
    res = _describe(llm)
    assert len(llm.calls) == 8 and res.llm.retries == 7
    assert any("Could not be fixed after 8 tries" in w for w in res.metadata.warnings)
    assert any("at least 5 words" in w for w in res.metadata.warnings)


def test_stops_as_soon_as_an_answer_is_valid():
    llm = FakeProvider(response=[_bad(1), _bad(2), _bad(3), _resp()])
    res = _describe(llm)
    assert len(llm.calls) == 4 and res.llm.retries == 3
    assert res.metadata.description == DEFAULT_RESPONSE["description"]
    assert not [i for i in res.issues if i.hard]
    assert not any("Could not be fixed" in w for w in res.metadata.warnings)
    assert res.llm.input_tokens == 4 * 100 and res.llm.output_tokens == 4 * 50


def test_same_answer_again_stops_and_flags():
    llm = FakeProvider(response=[_bad(1), _bad(2), _bad(2), _bad(2), _bad(3)])
    res = _describe(llm)
    assert len(llm.calls) == 4  # two repairs in a row repeat the answer: no progress
    assert any("Could not be fixed after 4 tries" in w for w in res.metadata.warnings)


def test_one_repeated_answer_does_not_stop_the_tries():
    llm = FakeProvider(response=[_bad(1), _bad(1), _bad(2), _resp()])
    res = _describe(llm)
    assert len(llm.calls) == 4 and not [i for i in res.issues if i.hard]


def test_every_retry_is_a_little_warmer_up_to_a_cap():
    llm = FakeProvider(response=[_bad(i) for i in range(20)])
    _describe(llm, temperature=0.2)
    temps = [c.temperature for c in llm.calls]
    assert temps[0] == 0.2
    assert temps[1:4] == pytest.approx([0.35, 0.5, 0.65])
    assert max(temps) <= 0.9 and temps == sorted(temps)


def test_max_tries_is_a_setting():
    llm = FakeProvider(response=[_bad(i) for i in range(20)])
    _describe(llm, max_tries=3)
    assert len(llm.calls) == 3
    one = FakeProvider(response=[_bad(i) for i in range(20)])
    _describe(one, max_tries=1)
    assert len(one.calls) == 1


def test_broken_json_retries_count_toward_the_cap():
    llm = FakeProvider(response=["{not json", *[_bad(i) for i in range(20)]])
    res = _describe(llm, retries=1)
    assert len(llm.calls) == 8 and res.llm.retries == 7


def test_best_answer_is_kept():
    worse = _resp(description="Street", keywords=["a"], editorial=False)
    llm = FakeProvider(response=[_bad(1), worse, worse])
    res = _describe(llm)
    assert res.metadata.description == "Street 1"


def test_near_duplicates_get_many_tries_with_warmer_answers():
    seen = metadata_rules.BatchSeen()
    _describe(FakeProvider(response=_resp()), seen=seen)
    same = [
        _resp(
            description=f"{DEFAULT_RESPONSE['description']} {i}",
            keywords=DEFAULT_RESPONSE["keywords"],
        )
        for i in range(20)
    ]
    llm = FakeProvider(response=same)
    res = _describe(llm, seen=seen)
    assert len(llm.calls) == 8
    assert llm.calls[-1].temperature > llm.calls[1].temperature >= 0.5
    assert any(i.code == "dup_keywords" for i in res.issues)
    assert any("Could not be fixed" in w for w in res.metadata.warnings)


def test_repair_off_makes_one_call():
    llm = FakeProvider(response=[_bad(i) for i in range(20)])
    _describe(llm, repair=False)
    assert len(llm.calls) == 1


def test_pipeline_flags_the_photo_and_counts_tries(cfg, store, photos_dir):
    summary = importing.import_folder(cfg, store, photos_dir)
    bid = next(iter(summary.batches))
    pid = store.list_photos(bid)[0].id
    store.set_status(pid, Status.READY, force=True)
    llm = FakeProvider(response=[_bad(i) for i in range(20)])
    describing.describe_batch(cfg, store, bid, [pid], llm=llm)
    photo = store.get_photo(pid)
    assert photo.status == Status.NEEDS_REVIEW
    assert any("Could not be fixed after 8 tries" in w for w in photo.metadata["warnings"])
    assert len(llm.calls) == 8
    assert store.ai_times(bid)[pid][1] == 8  # tries, as the batch report shows them


def test_config_max_tries(cfg, store, photos_dir):
    cfg.llm.max_tries = 2
    bid = next(iter(importing.import_folder(cfg, store, photos_dir).batches))
    pid = store.list_photos(bid)[0].id
    store.set_status(pid, Status.READY, force=True)
    llm = FakeProvider(response=[_bad(i) for i in range(20)])
    describing.describe_batch(cfg, store, bid, [pid], llm=llm)
    assert len(llm.calls) == 2


# ------------------------------------------------------------------ English only


def test_both_prompts_say_english_only():
    for condensed in (False, True):
        _, user = build_prompt(Style(), "", condensed=condensed)
        assert "English only" in user


def test_ascii_fold_keeps_other_scripts_visible():
    assert metadata_rules.ascii_fold("Kyōto café Straße Hœrdt Øl") == "Kyoto cafe Strasse Hoerdt Ol"
    assert metadata_rules.ascii_fold("が 日本") == "が 日本"  # not altered: it must be reported
    assert metadata_rules.has_foreign_script("a quiet 日本 street")
    assert metadata_rules.has_foreign_script("тихая улица")
    assert metadata_rules.has_foreign_script("서울")
    assert not metadata_rules.has_foreign_script("A quiet café in Kyōto, 2026")
    assert (
        metadata_rules.english_only("Photographer 東京 title Shibuya")
        == "Photographer title Shibuya"
    )


def test_japanese_description_is_repaired():
    jp = _resp(description="東京の静かな住宅街の朝、自転車が並ぶ道")
    llm = FakeProvider(response=[jp, _resp()])
    res = _describe(llm)
    assert len(llm.calls) == 2
    assert "not in English" in llm.calls[1].prompt
    assert res.metadata.description == DEFAULT_RESPONSE["description"]
    assert not [i for i in res.issues if i.hard]


def test_japanese_keyword_is_repaired_and_counts_toward_tries():
    kws = [*DEFAULT_RESPONSE["keywords"][:24], "渋谷"]
    llm = FakeProvider(response=[*[_resp(keywords=kws) for _ in range(1)], _resp()])
    res = _describe(llm)
    assert len(llm.calls) == 2
    assert all(metadata_rules.ascii_fold(k).isascii() for k in res.metadata.keywords)


def test_still_japanese_after_all_tries_is_flagged_not_hidden():
    llm = FakeProvider(
        response=[_resp(description=f"東京の静かな住宅街の朝 {i}" * 2) for i in range(20)]
    )
    res = _describe(llm)
    assert len(llm.calls) == 8
    assert any("not in English" in w for w in res.metadata.warnings)
    assert any("Could not be fixed" in w for w in res.metadata.warnings)


def test_accents_are_folded_not_failed():
    answer = _resp(
        description="A quiet café on a Kyōto street at dawn with steam rising from cups",
        keywords=["kyōto", "café", *DEFAULT_RESPONSE["keywords"]],
    )
    llm = FakeProvider(response=answer)
    res = _describe(llm)
    assert len(llm.calls) == 1
    assert "cafe" in res.metadata.description and "Kyoto" in res.metadata.description
    assert "kyoto" in res.metadata.keywords and "cafe" in res.metadata.keywords


def test_pipeline_flags_japanese_photo(cfg, store, photos_dir):
    bid = next(iter(importing.import_folder(cfg, store, photos_dir).batches))
    pid = store.list_photos(bid)[0].id
    store.set_status(pid, Status.READY, force=True)
    llm = FakeProvider(response=_resp(description="東京の静かな住宅街の朝の自転車の道"))
    describing.describe_batch(cfg, store, bid, [pid], llm=llm)
    photo = store.get_photo(pid)
    assert photo.status == Status.NEEDS_REVIEW
    assert any("not in English" in w for w in photo.metadata["warnings"])
    # it cannot be approved, prepared or exported as it is
    assert review.approve(cfg, store, photo_ids=[pid]).refused[pid].startswith("Text is not")
    problems = prepare.release_check(photo.metadata, None, [])
    assert any("not in English" in p for p in problems)


def test_edited_japanese_text_stays_flagged(cfg, store, photos_dir):
    bid = next(iter(importing.import_folder(cfg, store, photos_dir).batches))
    pid = store.list_photos(bid)[0].id
    store.set_status(pid, Status.READY, force=True)
    describing.describe_batch(cfg, store, bid, [pid], llm=FakeProvider(response=_resp()))
    review.update_metadata(cfg, store, pid, keywords=[*DEFAULT_RESPONSE["keywords"], "東京"])
    photo = store.get_photo(pid)
    assert photo.status == Status.NEEDS_REVIEW
    assert "Text is not in English" in photo.metadata["warnings"][0]


def test_file_hints_do_not_leak_japanese():
    meta = PhotoMeta(
        title="東京の朝 Morning in Tokyo",
        caption="渋谷スクランブル交差点",
        keywords=["東京", "street", "渋谷 crossing"],
        place_hint={"city": "渋谷", "country": "Japan"},
    )
    ctx = build_context(meta)
    assert not metadata_rules.has_foreign_script(ctx["hints"])
    assert "Morning in Tokyo" in ctx["hints"] and "street" in ctx["hints"]
    _, user = build_prompt(Style(), "", facts=ctx["hints"])
    assert "東" not in user


def test_place_names_are_ascii():
    import gzip

    text = gzip.decompress(geo.data_file().read_bytes()).decode("utf-8")
    odd = {c for c in text if not c.isascii()}
    assert odd <= {"œ"}  # the only non-ASCII letter in the bundled GeoNames extract
    assert (
        resolve_place({}, known={"city": "Hœrdt", "country": "France", "source": "gps"})["city"]
        == "HOERDT"
    )


def test_csv_never_holds_non_latin_text(cfg, store, photos_dir):
    row = {
        "filename": "a.jpg",
        "description": "日本の街",
        "keywords": ["x"],
        "categories": [],
    }
    with pytest.raises(csv_export.NotEnglishError):
        csv_export.build_csv([row])
    assert "café" in csv_export.build_csv([{**row, "description": "A café"}])


def test_export_leaves_flagged_photo_out(cfg, store, tmp_path):
    store.upsert_batch("b")
    ids = []
    for i, text in enumerate(
        ("A quiet street with parked bicycles at dawn", "日本の静かな街の朝の道")
    ):
        pid = store.add_photo(
            sha256=f"s{i}", src_path=f"/x/{i}.jpg", orig_name=f"{i}.jpg", batch_id="b"
        )
        store.update_photo(
            pid,
            metadata={"description": text, "keywords": ["street"], "categories": []},
            submit_name=f"p{i}.jpg",
        )
        store.set_status(pid, Status.UPLOADED, force=True)
        ids.append(pid)
    s = exporting.export_batch(cfg, store, "b")
    assert [r["filename"] for r in s.rows] == ["p0.jpg"]
    assert s.skipped == {ids[1]: metadata_rules.reason("non_english")}
    assert "日本" not in s.files[0].read_text(encoding="utf-8")


# ------------------------------------------------------------------ short prompt for small models


def test_small_context_decision():
    assert local_models.is_small_context("moondream")
    assert local_models.is_small_context("llava-phi3")
    assert not local_models.is_small_context("gemma3:4b")
    assert not local_models.is_small_context("qwen3-vl:8b")
    assert not local_models.is_small_context("some-unlisted-model:1b")
    assert local_models.SMALL_CONTEXT_TOKENS == 4096
    models, _ = local_models.load_catalog()
    assert all(m.context_tokens > 0 for m in models)


def test_condensed_prompt_is_shorter_and_keeps_the_rules():
    style = Style(theme="Everyday Japan", romaji_place_names=True, always_include=["japan"])
    facts = "Taken 2026-07-31. Place from GPS: Shibuya (ward), Tokyo (prefecture), Japan."
    args = (style, "RESTRICTED: Tokyo Skytree", ["Recent rejections: focus x3."], "notes", WHEN)
    s_full, full = build_prompt(*args, facts=facts)
    s_short, short = build_prompt(*args, facts=facts, condensed=True)
    assert len(short) < 0.75 * len(full)
    assert len(s_short) < len(s_full)
    for must in (
        "Place from GPS: Shibuya",
        "English only",
        "RESTRICTED: Tokyo Skytree",
        "Everyday Japan",
        "Capture date: October 5, 2026",
        "Photographer notes: notes",
        "Return JSON only",
        "Always include: japan",
        '"description"',
        '"keywords"',
        '"categories"',
        '"editorial"',
        '"detected"',
        '"location"',
    ):
        assert must in short, must


def test_short_restrictions_summary():
    full = guardrails.prompt_summary(R, "japan")
    short = guardrails.prompt_summary(R, "japan", short=True)
    assert len(short) < 0.6 * len(full) and "non-exhaustive" in short
    assert "Never put brand names" not in short


def test_describe_photo_uses_the_short_prompt_when_asked():
    llm = FakeProvider(response=_resp())
    _describe(llm, condensed=True)
    _, short = build_prompt(Style(), guardrails.prompt_summary(R, None), condensed=True)
    assert llm.calls[0].prompt.startswith(short.split("\n")[0])
    assert "English only: Latin letters" in llm.calls[0].prompt


def test_pipeline_picks_the_prompt_by_model():
    class Stub:
        def __init__(self, name, model):
            self.name, self.model = name, model

    assert selection.use_condensed_prompt(Stub("ollama", "moondream"))
    assert not selection.use_condensed_prompt(Stub("ollama", "gemma3:12b"))
    assert not selection.use_condensed_prompt(Stub("anthropic", "moondream"))
    assert not selection.use_condensed_prompt(FakeProvider())


def test_describe_batch_sends_the_short_prompt(cfg, store, photos_dir, monkeypatch):
    bid = next(iter(importing.import_folder(cfg, store, photos_dir).batches))
    pid = store.list_photos(bid)[0].id
    store.set_status(pid, Status.READY, force=True)
    monkeypatch.setattr(selection, "use_condensed_prompt", lambda llm: True)
    llm = FakeProvider(response=_resp())
    describing.describe_batch(cfg, store, bid, [pid], llm=llm)
    assert "JSON fields:" in llm.calls[0].prompt


def test_settings_warns_about_a_small_model(web):  # noqa: F811
    store = web.state.store
    store.set_setting("llm.provider", "ollama")
    store.set_setting("llm.model", "moondream")
    page = web.get("/settings").text
    assert "moondream is a small model" in page
    assert "choose a bigger model, for example" in page
    store.set_setting("llm.model", "gemma3:4b")
    assert "is a small model" not in web.get("/settings").text


def test_small_model_warning_names_a_bigger_fitting_model():
    rows = [
        type("R", (), {"tag": "moondream", "fits_hardware": True})(),
        type("R", (), {"tag": "gemma3:12b", "fits_hardware": False})(),
        type("R", (), {"tag": "gemma3:4b", "fits_hardware": True})(),
    ]
    text = local_models.small_model_warning("moondream", rows)
    assert "gemma3:4b" in text and "gemma3:12b" not in text
    assert local_models.small_model_warning("gemma3:4b", rows) is None
    assert "online AI" in local_models.small_model_warning("moondream", [])


# ------------------------------------------------------------------ HEIC


def _heic_photo(store, tmp_path, name="a.heic"):
    store.upsert_batch("b")
    f = tmp_path / name
    f.write_bytes(b"this is not a real heic file")
    pid = store.add_photo(sha256=f"h{name}", src_path=str(f), orig_name=name, batch_id="b")
    store.set_status(pid, Status.READY, force=True)
    return pid


def test_unreadable_heic_without_support_is_flagged_and_the_batch_goes_on(
    cfg, store, tmp_path, monkeypatch
):
    monkeypatch.setitem(sys.modules, "pillow_heif", None)  # as if it were not installed
    monkeypatch.setattr(imaging, "_heif_registered", False)
    bad = _heic_photo(store, tmp_path)
    good = store.add_photo(
        sha256="g", src_path=str(make_jpeg(tmp_path / "ok.jpg")), orig_name="ok.jpg", batch_id="b"
    )
    store.set_status(good, Status.READY, force=True)
    s = describing.describe_batch(cfg, store, "b", [bad, good], llm=FakeProvider(response=_resp()))
    flagged = store.get_photo(bad)
    assert flagged.status == Status.NEEDS_REVIEW
    assert flagged.metadata["warnings"] == ["HEIC photo could not be read - install HEIC support"]
    assert store.get_photo(good).status == Status.DESCRIBED
    assert s.errors == [(bad, "HEIC photo could not be read - install HEIC support")]
    assert not s.failed and s.described == 1


def test_unreadable_heic_with_support_says_damaged(cfg, store, tmp_path, monkeypatch):
    monkeypatch.setattr(imaging, "heic_support", lambda: True)
    pid = _heic_photo(store, tmp_path, "b.HEIF")
    describing.describe_batch(cfg, store, "b", [pid], llm=FakeProvider(response=_resp()))
    assert store.get_photo(pid).metadata["warnings"] == [imaging.HEIC_DAMAGED]


def test_other_unreadable_files_keep_their_error(tmp_path):
    f = tmp_path / "x.jpg"
    f.write_bytes(b"nope")
    assert imaging.unreadable_message(f) is None


def test_import_reports_unreadable_heic(cfg, store, tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "pillow_heif", None)
    monkeypatch.setattr(imaging, "_heif_registered", False)
    d = tmp_path / "in"
    d.mkdir()
    (d / "a.heic").write_bytes(b"not heic")
    s = importing.import_folder(cfg, store, d)
    assert s.imported == 0 and len(s.errors) == 1
    assert s.errors[0].endswith("HEIC photo could not be read - install HEIC support")


# ------------------------------------------------------------------ GPS at upload


class _Capture:
    def __init__(self):
        self.sent: dict[str, bytes] = {}

    def storbinary(self, cmd, fh):
        self.sent[cmd.split(" ", 1)[1]] = fh.read()

    def quit(self):
        pass


def _gps_of(data: bytes) -> dict:
    return dict(Image.open(io.BytesIO(data)).getexif().get_ifd(0x8825))


def _prepared_with_gps(cfg, store, tmp_path):
    """A copy prepared the 0.4 way: location still inside."""
    src = make_jpeg(
        tmp_path / "a.jpg", lat=SHIBUYA[0], lon=SHIBUYA[1], captured="2026:07:31 19:45:10"
    )
    meta = {"description": "A crossing at night", "keywords": ["crossing"]}
    out = prepare.prepare_file(src, tmp_path / "prepared", "p0.jpg", meta, include_gps=True)
    assert _gps_of(out.read_bytes())
    store.upsert_batch("b")
    pid = store.add_photo(sha256="s", src_path=str(src), orig_name="a.jpg", batch_id="b")
    store.update_photo(pid, prepared_path=str(out), submit_name="p0.jpg")
    store.set_status(pid, Status.PREPARED, force=True)
    return pid, out


@pytest.fixture
def creds(monkeypatch):
    monkeypatch.setenv("SHUTO_FTPS_USER", "u")
    monkeypatch.setenv("SHUTO_FTPS_PASSWORD", "p")


def test_upload_strips_gps_when_the_setting_is_off(cfg, store, tmp_path, creds):
    pid, out = _prepared_with_gps(cfg, store, tmp_path)
    ftp = _Capture()
    s = delivering.upload_batch(cfg, store, "b", confirm=True, connect=lambda *a, **k: ftp)
    assert s.uploaded == ["p0.jpg"] and not s.failed
    assert _gps_of(ftp.sent["p0.jpg"]) == {}
    assert _gps_of(out.read_bytes()) == {}
    exif = Image.open(io.BytesIO(ftp.sent["p0.jpg"])).getexif()
    assert exif[0x0110] == "X-T5"  # other EXIF stays
    assert b"A crossing at night" in ftp.sent["p0.jpg"]  # IPTC/XMP stays
    assert store.get_photo(pid).status == Status.UPLOADED


def test_upload_keeps_gps_when_the_owner_allows_it(cfg, store, tmp_path, creds):
    cfg.prepare.include_gps = True
    _prepared_with_gps(cfg, store, tmp_path)
    ftp = _Capture()
    delivering.upload_batch(cfg, store, "b", confirm=True, connect=lambda *a, **k: ftp)
    assert _gps_of(ftp.sent["p0.jpg"])


def test_upload_never_sends_a_file_it_cannot_check(cfg, store, tmp_path, creds):
    pid, out = _prepared_with_gps(cfg, store, tmp_path)
    out.write_bytes(b"not a jpeg")
    ftp = _Capture()
    s = delivering.upload_batch(cfg, store, "b", confirm=True, connect=lambda *a, **k: ftp)
    assert not ftp.sent and not s.uploaded
    assert "not sent" in s.failed["p0.jpg"]
    assert store.get_photo(pid).status == Status.PREPARED


def test_strip_gps_is_a_no_op_without_gps(tmp_path):
    plain = make_jpeg(tmp_path / "n.jpg", captured="2026:07:31 19:45:10").read_bytes()
    assert embed.strip_gps(plain) == plain


# ------------------------------------------------------------------ place source


def test_city_from_ai_when_gps_only_knows_the_country():
    gps = {"city": None, "region": "Tokyo", "country": "Japan", "source": "gps"}
    got = resolve_place({"city": "Shibuya", "region": None, "country": "Japan"}, known=gps)
    assert (got["city"], got["country"], got["source"]) == ("SHIBUYA", "JAPAN", "gps + AI")
    assert got["default"] is False
    file_ = {**gps, "source": "file"}
    assert resolve_place({"city": "Osaka"}, known=file_)["source"] == "file + AI"
    full = {"city": "Shibuya", "country": "Japan", "source": "gps"}
    assert resolve_place({"city": "Osaka", "country": "Japan"}, known=full)["source"] == "gps"
    # Q64: a known prefecture is the city, the default city is not used against it
    got = resolve_place({}, "Kyoto", "Japan", gps)
    assert (got["city"], got["country"], got["source"]) == ("TOKYO", "JAPAN", "gps")
    assert got["default"] is False
