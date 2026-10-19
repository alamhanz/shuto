"""End to end outputs on the fake model: statuses, stored metadata, report, CSV, IPTC/XMP, names.

Photo bytes differ between platforms (JPEG encoders), so hashes, sizes and cull numbers are not
recorded; everything else a user sees or uploads is.
"""

from __future__ import annotations

import base64
import copy
import re
from datetime import datetime
from pathlib import Path

import pytest

from ..helpers import write_jpeg
from . import check_json, check_text

FIXED_ISO = "2025-06-01T12:00:00"
FIXED_UTC = "2025-06-01T12:00:00+00:00"
TIME_COLUMNS = (
    "sha256",
    "checked_at",
    "approved_at",
    "uploaded_at",
    "submitted_at",
    "accepted_at",
    "rejected_at",
    "ai_seconds",
)
TIME_KEYS = ("at", "time", "seconds", "checked_at", "described_at", "elapsed")


def _seam():
    from shuto.ai.providers.fake import DEFAULT_RESPONSE, FakeProvider
    from shuto.core import paths
    from shuto.core import text as core_text
    from shuto.core.models import Status
    from shuto.deliver import csv_export, report, send
    from shuto.metadata import embed
    from shuto.settings import effective
    from shuto.settings import ui as settings_ui
    from shuto.store import db as store_mod
    from shuto.workflow import (
        culling,
        delivering,
        describing,
        exporting,
        importing,
        reports,
        review,
    )

    return {
        "core_text": core_text,
        "paths": paths,
        "settings_ui": settings_ui,
        "effective": effective,
        "importing": importing,
        "culling": culling,
        "describing": describing,
        "review": review,
        "delivering": delivering,
        "exporting": exporting,
        "reports": reports,
        "store_mod": store_mod,
        "embed": embed,
        "deliver": send,
        "report": report,
        "csv_export": csv_export,
        "FakeProvider": FakeProvider,
        "DEFAULT_RESPONSE": DEFAULT_RESPONSE,
        "Status": Status,
    }


class _FakeFTP:
    def __init__(self):
        self.stored: dict[str, bytes] = {}

    def storbinary(self, cmd, fh):
        self.stored[cmd.split(" ", 1)[1]] = fh.read()

    def quit(self):
        pass

    def close(self):
        pass


def _responder(base: dict, text: dict):
    """A different answer per photo, keyed on the capture date in the prompt (so the order the
    photos are described in does not matter)."""

    def respond(req):
        found = re.search(r"Capture date: (\w+ \d+, \d{4})", req.prompt)
        day = found.group(1) if found else "unknown"
        return {
            **base,
            "description": f"{text['description']} on {day}",
            "caption_sentence": text["caption"],
            "keywords": [*text["keywords"], *(f"{text['extra']} {day} {i}" for i in range(6))],
        }

    return respond


def _photos(root: Path) -> Path:
    """Three good photos taken on different days, one blurry, one too small."""
    for name, kind, seed, day in (
        ("a.jpg", "sharp", 1, 4),
        ("b.jpg", "sharp", 2, 5),
        ("c.jpg", "sharp", 3, 6),
        ("blurry.jpg", "blurred", 4, 4),
        ("tiny.jpg", "small", 5, 4),
    ):
        write_jpeg(root / name, kind, seed=seed, captured=datetime(2025, 3, day, 10, 0, 0))
    return root


ENGLISH = {
    "description": "A quiet residential street in Tokyo with parked bicycles and utility poles",
    "caption": "A quiet residential street with parked bicycles.",
    "keywords": [
        "japan", "tokyo", "street", "bicycle", "residential", "urban", "quiet", "morning",
        "city", "asphalt", "utility pole", "houses", "neighborhood", "travel", "daily life",
        "asia", "road", "outdoor", "architecture", "wires",
    ],
    "extra": "extra",
}  # fmt: skip
JAPANESE = {
    "description": "東京の 静かな 住宅街の 通りに 駐輪された 自転車と 電柱",
    "caption": "自転車が並ぶ静かな住宅街。",
    "keywords": [
        "日本", "東京", "通り", "自転車", "住宅街", "都会", "静か", "朝", "街", "道路", "電柱",
        "家", "近所", "旅行", "日常", "アジア", "路地", "屋外", "建築", "電線",
    ],
    "extra": "追加",
}  # fmt: skip


def _scrub(value):
    """Remove time values (any key that names a time) from nested stored data."""
    if isinstance(value, dict):
        return {k: _scrub(v) for k, v in value.items() if k not in TIME_KEYS}
    if isinstance(value, list):
        return [_scrub(v) for v in value]
    return value


def _embedded(embed, meta: dict, blob: bytes) -> bool:
    """The prepared copy carries exactly the IPTC and XMP blocks built from its metadata."""
    description = str(meta.get("description") or "")
    title = str(meta.get("title") or description[:64])
    keywords = list(meta.get("keywords") or [])
    return (
        embed.build_iptc(title, description, keywords) in blob
        and embed.build_xmp(title, description, keywords) in blob
    )


def _run(cfg, store, tmp_path, monkeypatch, text, language) -> dict:
    photos_dir = _photos(tmp_path / "shots")
    s = _seam()
    monkeypatch.setattr(s["core_text"], "now_iso", lambda: FIXED_ISO)
    monkeypatch.setattr(s["store_mod"], "_now", lambda: FIXED_UTC)
    monkeypatch.setenv("SHUTO_FTPS_USER", "u")
    monkeypatch.setenv("SHUTO_FTPS_PASSWORD", "p")
    if language:
        s["settings_ui"].set_ui_setting(store, "language", language)
    s["settings_ui"].set_ui_setting(store, "max_tries", 3)
    run_cfg = s["effective"].effective_config(cfg, store)
    run_cfg.deliver.upload_enabled = True

    (batch,) = s["importing"].import_folder(run_cfg, store, photos_dir).batches
    s["culling"].cull_batch(run_cfg, store, batch)
    llm = s["FakeProvider"]("fake", response=_responder(s["DEFAULT_RESPONSE"], text))
    s["describing"].describe_batch(run_cfg, store, batch, llm=llm)
    s["review"].approve(run_cfg, store, batch_id=batch)
    prep = s["delivering"].prepare_batch(run_cfg, store, batch)
    ftp = _FakeFTP()
    s["delivering"].upload_batch(run_cfg, store, batch, confirm=True, connect=lambda *a, **k: ftp)
    export = s["exporting"].export_batch(run_cfg, store, batch)

    photos = store.list_photos(batch)
    after_export = {p.orig_name: p.status.value for p in photos}
    header, records = s["reports"].build_report(run_cfg, store, batch)
    keep = [i for i, name in enumerate(header) if name not in TIME_COLUMNS]
    report_rows = [{header[i]: row[i] for i in keep} for row in sorted(records, key=lambda r: r[0])]
    csv_text = "\n".join(f.read_bytes().decode("utf-8") for f in export.files)
    named = {p.orig_name: p for p in photos}
    meta = {
        name: {
            "status": p.status.value,
            "metadata": _scrub(copy.deepcopy(p.metadata)),
            "metadata_source": p.metadata_source,
            "guardrail": _scrub(copy.deepcopy(p.guardrail)),
            "filters": _scrub(copy.deepcopy(p.filters)),
            "detected": p.detected,
            "submit_name": p.submit_name,
        }
        for name, p in sorted(named.items())
    }
    first = next((p for p in photos if p.submit_name), None)
    blob = b""
    if first:
        blob = (
            s["paths"]
            .batch_dir(run_cfg, batch)
            .joinpath("prepared", first.submit_name)
            .read_bytes()
        )
    return {
        "batch": batch,
        "statuses_after_export": after_export,
        "prepared_names": sorted(prep.prepared),
        "uploaded_names": sorted(ftp.stored),
        "export_files": [f.name for f in export.files],
        "export_rows": sorted(export.rows, key=lambda r: r["filename"]),
        "csv": csv_text,
        "report_header": [header[i] for i in keep],
        "report_rows": report_rows,
        "photos": meta,
        "prepared_has_iptc_and_xmp": bool(first) and _embedded(s["embed"], first.metadata, blob),
    }


def test_strict_english_flow_golden(cfg, store, tmp_path, monkeypatch):
    out = _run(cfg, store, tmp_path, monkeypatch, ENGLISH, None)
    assert out["prepared_has_iptc_and_xmp"] is True
    check_json("outputs_english.json", {k: v for k, v in out.items() if k != "csv"})
    check_text("outputs_english.csv", out["csv"])


def test_japanese_language_flow_golden(cfg, store, tmp_path, monkeypatch):
    out = _run(
        cfg,
        store,
        tmp_path,
        monkeypatch,
        JAPANESE,
        {"english_only": False, "language": "Japanese"},
    )
    check_json("outputs_japanese.json", {k: v for k, v in out.items() if k != "csv"})
    check_text("outputs_japanese.csv", out["csv"])


def test_strict_english_refuses_japanese_text(cfg, store, tmp_path, monkeypatch):
    """Strict English never lets Japanese text reach the prepared files or the CSV."""
    out = _run(cfg, store, tmp_path, monkeypatch, JAPANESE, None)
    assert out["export_rows"] == []
    assert out["prepared_names"] == []
    assert "日本" not in out["csv"]


IPTC_CASES = {
    "plain": ("Quiet street", "A quiet street in Tokyo", ["tokyo", "street"]),
    "escaped": ("Tokyo & Kyoto <old>", 'Cafe "A" & B', ["a&b", "x<y"]),
    "japanese": ("渋谷の交差点", "渋谷のスクランブル交差点", ["渋谷", "交差点"]),
    "long": ("T" * 100, "D" * 2100, [f"kw{i}" for i in range(3)]),
}


@pytest.mark.parametrize("case", sorted(IPTC_CASES))
def test_iptc_xmp_blocks_golden(case):
    embed = _seam()["embed"]
    title, description, keywords = IPTC_CASES[case]
    iptc = embed.build_iptc(title, description, keywords)
    xmp = embed.build_xmp(title, description, keywords)
    check_json(
        f"iptc_xmp_{case}.json",
        {"iptc_b64": base64.b64encode(iptc).decode(), "xmp": xmp.decode("utf-8")},
    )
