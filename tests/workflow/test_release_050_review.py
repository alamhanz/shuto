"""Fixes from the 0.5.0 review (shuto-133): decoding, hints, place order, privacy, num_ctx."""

from __future__ import annotations

import struct
from types import SimpleNamespace

import pytest

from shuto import cli
from shuto.ai import catalog as local_models
from shuto.ai import machine, selection
from shuto.ai.hardware import HardwareProfile
from shuto.ai.prompts import Style, build_prompt
from shuto.ai.providers.base import LLMRequest
from shuto.ai.providers.fake import FakeProvider
from shuto.ai.providers.ollama import OllamaProvider
from shuto.core.models import Status
from shuto.ingest.photo_meta import PhotoMeta, read_photo_meta
from shuto.metadata import embed
from shuto.metadata.rules import has_foreign_script
from shuto.places.context import build_context
from shuto.places.resolve import resolve_place
from shuto.settings import ui as settings_ui
from shuto.workflow import delivering

from ..ingest.test_photo_meta import SHIBUYA, _segment, make_jpeg
from .test_release_050 import NO_LOC, _run
from .test_release_050_tries import _Capture, _prepared_with_gps, creds  # noqa: F401

SHIFT_JIS_SHIBUYA = "渋谷".encode("cp932")


def _iptc_raw(fields: list[tuple[int, int, bytes]]) -> bytes:
    iim = b"".join(embed._iim(rec, ds, raw) for rec, ds, raw in fields)
    if len(iim) % 2:
        iim += b"\x00"
    res = b"8BIM" + struct.pack(">H", 0x0404) + b"\x00\x00" + struct.pack(">I", len(iim)) + iim
    return _segment(0xED, embed.PS_HEADER + res)


# ------------------------------------------------------------------ 1: decoding


def test_shift_jis_iptc_is_decoded_not_garbled(tmp_path):
    seg = _iptc_raw([(2, 90, SHIFT_JIS_SHIBUYA), (2, 25, "東京".encode("cp932"))])
    meta = read_photo_meta(make_jpeg(tmp_path / "a.jpg", segments=seg))
    assert meta.place_hint == {"city": "渋谷"}
    assert meta.keywords == ["東京"]


def test_shift_jis_place_gives_no_file_place_and_no_garbage_hint(tmp_path):
    seg = _iptc_raw([(2, 90, SHIFT_JIS_SHIBUYA), (2, 25, "東京".encode("cp932"))])
    meta = read_photo_meta(make_jpeg(tmp_path / "a.jpg", segments=seg))
    ctx = build_context(meta)
    assert ctx["place"] is None
    assert ctx["hints"] == "Lens: XF35mmF1.4 R."
    assert not any(ord(c) < 32 or 0x7F <= ord(c) < 0xA0 for c in ctx["hints"])


def test_undecodable_iptc_bytes_are_dropped(tmp_path):
    seg = _iptc_raw([(2, 90, b"\x8f\xff\x92"), (2, 25, b"\x8f\xff"), (2, 5, b"Plain title")])
    meta = read_photo_meta(make_jpeg(tmp_path / "a.jpg", segments=seg))
    assert meta.place_hint is None and meta.keywords == []
    assert meta.title == "Plain title"


def test_declared_latin1_and_utf8_charsets_are_honoured(tmp_path):
    latin = _iptc_raw([(1, 90, b"\x1b-A"), (2, 90, "Zürich".encode("latin-1"))])
    meta = read_photo_meta(make_jpeg(tmp_path / "a.jpg", segments=latin))
    assert meta.place_hint == {"city": "Zürich"}
    utf8 = _iptc_raw([(1, 90, b"\x1b%G"), (2, 90, "渋谷".encode("cp932"))])
    meta = read_photo_meta(make_jpeg(tmp_path / "b.jpg", segments=utf8))
    assert meta.place_hint is None  # declared UTF-8 but not valid UTF-8


def test_control_characters_never_reach_the_prompt(tmp_path):
    seg = _iptc_raw(
        [
            (2, 5, b"Cross\x85ing\x1b\xe2\x80\xaeevil"),
            (2, 25, b"night\x9b"),
            (2, 90, b"Kyo\x07to"),
        ]
    )
    meta = read_photo_meta(make_jpeg(tmp_path / "a.jpg", segments=seg))
    ctx = build_context(meta)
    _, user = build_prompt(Style(), facts=ctx["hints"])
    for ch in user:
        assert ch == "\n" or ord(ch) >= 32 and not 0x7F <= ord(ch) < 0xA0
    assert "‮" not in user and "Kyoto" in ctx["hints"]


def test_file_place_parts_are_sanity_checked():
    meta = PhotoMeta(place_hint={"city": "渋谷", "region": "Tokyo", "country": "J"})
    place = build_context(meta)["place"]
    assert place["city"] is None and place["region"] == "Tokyo" and place["country"] is None
    junk = PhotoMeta(place_hint={"city": "A1 !!", "country": "x" * 80})
    assert build_context(junk)["place"] is None


def test_metadata_is_length_capped(tmp_path):
    seg = _iptc_raw([(2, 25, b"k" * 300), (2, 5, b"t" * 400)])
    meta = read_photo_meta(make_jpeg(tmp_path / "a.jpg", segments=seg))
    assert len(meta.keywords[0]) == 64 and len(meta.title) == 200


# ------------------------------------------------------------------ 2: empty hint sentences


def test_japanese_only_title_and_keywords_leave_no_empty_sentences():
    meta = PhotoMeta(title="渋谷の夜", keywords=["東京", "夜"], caption="交差点")
    hints = build_context(meta)["hints"]
    assert hints == ""
    mixed = build_context(PhotoMeta(keywords=["東京", "night"]))["hints"]
    assert mixed == "Photographer keywords: night."


# ------------------------------------------------------------------ 3: GPS + file place


def test_non_latin_file_city_keeps_the_gps_region_and_country():
    gps = {"city": None, "region": "Tokyo", "country": "Japan", "confidence": "low"}
    meta = PhotoMeta(place_hint={"city": "渋谷"})
    ctx = build_context(meta, gps)
    assert ctx["place"]["source"] == "gps" and ctx["place"]["region"] == "Tokyo"
    assert "Tokyo" in ctx["hints"] and "Japan" in ctx["hints"]


def test_usable_file_city_fills_the_gps_city():
    gps = {"city": None, "region": "Kanagawa", "country": "Japan"}
    ctx = build_context(PhotoMeta(place_hint={"city": "Kamakura"}), gps)
    assert ctx["place"]["city"] == "Kamakura" and ctx["place"]["region"] == "Kanagawa"


# ------------------------------------------------------------------ 4: default city (Q64)


def test_prefecture_beats_default_city_and_default_never_contradicts():
    gps = {"city": None, "region": "Tokyo", "country": "Japan", "source": "gps"}
    got = resolve_place({}, "Kyoto", "Japan", gps)
    assert (got["city"], got["country"], got["source"], got["default"]) == (
        "TOKYO",
        "JAPAN",
        "gps",
        False,
    )
    other = {"city": None, "region": None, "country": "France", "source": "file"}
    got = resolve_place({}, "Kyoto", "Japan", other)
    assert got["city"] == "" and got["country"] == "FRANCE"
    same = {"city": None, "region": None, "country": "Japan", "source": "file"}
    assert resolve_place({}, "Kyoto", "Japan", same)["city"] == "KYOTO"
    assert resolve_place({"city": "Osaka"}, "Kyoto", "Japan", gps)["city"] == "OSAKA"
    assert resolve_place({}, "Kyoto", "Japan")["source"] == "default"


# ------------------------------------------------------------------ Q63: neighbourhood


def _hood_place():
    return {
        "city": "Shibuya",
        "region": "Tokyo",
        "country": "Japan",
        "neighborhood": "Sakuragaokacho",
        "ward": "Shibuya",
        "confidence": "high",
    }


def test_neighbourhood_only_when_gps_is_kept():
    on = build_context(PhotoMeta(), _hood_place())
    assert "Sakuragaokacho" in on["hints"] and "neighborhood may be a keyword" in on["hints"]
    off = build_context(PhotoMeta(), _hood_place(), neighborhood=False)
    assert "Sakuragaokacho" not in off["hints"] and "neighborhood" not in off["hints"]
    assert "Shibuya (ward)" in off["hints"] and off["place"]["neighborhood"] is None


def test_describe_run_follows_the_privacy_setting(cfg, store, tmp_path):
    folder = tmp_path / "shots"
    make_jpeg(folder / "a.jpg", lat=SHIBUYA[0], lon=SHIBUYA[1])
    _, _, fake = _run(cfg, store, folder, NO_LOC)  # include_gps is off
    assert "Sakuragaokacho" not in fake.calls[0].prompt
    assert "Shibuya (ward)" in fake.calls[0].prompt


# ------------------------------------------------------------------ 5: CLI privacy


@pytest.mark.parametrize("command", ["prepare", "upload"])
def test_cli_honours_the_web_privacy_setting(cfg, store, monkeypatch, command):
    cfg.prepare.include_gps = True  # the yaml says keep it
    settings_ui.set_ui_setting(store, "include_gps", False)  # the owner switched it off in Settings
    monkeypatch.setattr("shuto.cli.load_config", lambda *a, **k: cfg)
    monkeypatch.setattr("shuto.cli.find_config_path", lambda *a, **k: None)
    seen = []

    def fake(c, *a, **k):
        seen.append(c.prepare.include_gps)
        return SimpleNamespace(
            prepared=[], problems={}, warnings={}, notes={}, uploaded=[], failed={}
        )

    monkeypatch.setattr(delivering, f"{command}_batch", fake)
    args = [command, "b"] + (["--yes"] if command == "upload" else [])
    assert cli.main(args) == 0
    assert seen == [False]


# ------------------------------------------------------------------ 7: upload English check


def test_upload_refuses_a_non_english_copy_made_before_the_guard(cfg, store, tmp_path, creds):  # noqa: F811
    pid, out = _prepared_with_gps(cfg, store, tmp_path)
    store.update_photo(pid, metadata={"title": "Crossing", "keywords": ["渋谷", "night"]})
    ftp = _Capture()
    s = delivering.upload_batch(cfg, store, "b", confirm=True, connect=lambda *a, **k: ftp)
    assert not ftp.sent and not s.uploaded
    assert "edit it in Review" in s.failed["p0.jpg"]
    assert store.get_photo(pid).status == Status.PREPARED


def test_upload_sends_non_latin_text_when_strict_english_is_off(cfg, store, tmp_path, creds):  # noqa: F811
    cfg.style.english_only = False
    cfg.style.metadata_language = "Japanese"
    pid, _ = _prepared_with_gps(cfg, store, tmp_path)
    store.update_photo(pid, metadata={"title": "渋谷", "keywords": ["渋谷"]})
    ftp = _Capture()
    s = delivering.upload_batch(cfg, store, "b", confirm=True, connect=lambda *a, **k: ftp)
    assert s.uploaded == ["p0.jpg"] and not s.failed


# ------------------------------------------------------------------ 6: num_ctx


class _Client:
    def __init__(self):
        self.options = None

    def supports_thinking(self, model):
        return False

    def always_thinks(self, model):
        return False

    def chat(self, model, messages, format=None, options=None, keep_alive=None, timeout=None):
        self.options = options
        return {"message": {"content": "{}"}}


def _complete(provider):
    req = LLMRequest(system="s", prompt="p", image_jpeg=b"x", max_tokens=1500, temperature=0.2)
    provider.complete(req, {})


def test_ollama_request_carries_num_ctx():
    client = _Client()
    _complete(OllamaProvider("gemma3:12b", client=client))
    assert client.options == {"temperature": 0.2, "num_predict": 1500, "num_ctx": 8192}
    client = _Client()
    _complete(OllamaProvider("moondream", client=client, num_ctx=2048))
    assert client.options["num_ctx"] == 2048


def _profile(ram_gb: float, gpu_gb: float = 0.0) -> HardwareProfile:
    return HardwareProfile(
        os="Windows",
        arch="x86_64",
        cpu="cpu",
        cores=8,
        ram_gb=ram_gb,
        gpus=[],
        apple_silicon=False,
        usable_gpu_gb=gpu_gb,
        tier="cpu",
        fingerprint="t",
    )


def test_effective_num_ctx_is_capped_by_catalogue_and_machine():
    catalog = local_models.load_catalog()[0]
    small = next(m for m in catalog if 0 < m.context_tokens <= 4096)
    big = next(m for m in catalog if m.context_tokens > 8192 and m.min_ram_gb <= 16)
    assert local_models.effective_num_ctx(small.tag, None, catalog) == small.context_tokens
    assert local_models.effective_num_ctx(big.tag, None, catalog) == 8192
    assert local_models.effective_num_ctx(big.tag, _profile(128), catalog) == 8192
    tight = _profile(big.min_ram_gb + 4.5)  # under 1 GB to spare
    assert local_models.effective_num_ctx(big.tag, tight, catalog) == 4096
    assert local_models.effective_num_ctx("not-in-catalogue:1b", tight, catalog) == 8192


def test_short_prompt_follows_the_num_ctx_actually_sent():
    big = OllamaProvider("gemma3:12b", client=_Client())
    assert not selection.use_condensed_prompt(big)
    assert selection.use_condensed_prompt(
        OllamaProvider("gemma3:12b", client=_Client(), num_ctx=4096)
    )
    assert not selection.use_condensed_prompt(FakeProvider())


def test_resolve_llm_sends_a_num_ctx(cfg, store, monkeypatch):
    monkeypatch.setattr(machine, "hardware_profile", lambda *a, **k: _profile(128))
    llm = selection.resolve_llm(cfg, store, provider="ollama", model="gemma3:12b")
    assert llm.num_ctx == 8192


# ------------------------------------------------------------------ 8: Latin letters


def test_more_latin_letters_are_not_foreign():
    for word in ("Łódź", "Đà Nẵng", "Þingvellir", "Reykjavík ð", "Ħamrun", "ı"):
        assert not has_foreign_script(word), word


# ------------------------------------------------------------------ shuto-138: Latin-1 vs Shift-JIS


@pytest.mark.parametrize(
    "city", ["Köln", "España", "São Paulo", "Kraków", "Zürich", "Málaga", "Genève"]
)
def test_latin1_iptc_without_a_marker_is_never_shortened(tmp_path, city):
    seg = _iptc_raw([(2, 90, city.encode("latin-1"))])
    meta = read_photo_meta(make_jpeg(tmp_path / "a.jpg", segments=seg))
    got = (meta.place_hint or {}).get("city")
    assert got in (None, city)


def test_latin1_text_that_is_not_letters_is_dropped(tmp_path):
    seg = _iptc_raw([(2, 90, b"A\xa4\xa4@")])
    assert read_photo_meta(make_jpeg(tmp_path / "a.jpg", segments=seg)).place_hint is None


def test_cp932_half_width_katakana_or_private_use_is_dropped(tmp_path):
    seg = _iptc_raw([(2, 90, "ﾄｳｷｮｳ".encode("cp932")), (2, 95, b"\x81\x61\xfd\xfe")])
    assert read_photo_meta(make_jpeg(tmp_path / "a.jpg", segments=seg)).place_hint is None


def test_xmp_place_wins_over_a_dropped_or_doubtful_iptc_place(tmp_path):
    from ..ingest.test_photo_meta import xmp_segment

    iptc = _iptc_raw([(2, 90, b"\x8f\xff\x92")])  # undecodable
    meta = read_photo_meta(make_jpeg(tmp_path / "a.jpg", segments=iptc + xmp_segment()))
    assert meta.place_hint["city"] == "Kamakura"
    both = _iptc_raw([(2, 90, "Köln".encode("latin-1"))])
    meta = read_photo_meta(make_jpeg(tmp_path / "b.jpg", segments=both + xmp_segment()))
    assert meta.place_hint["city"] == "Kamakura"
