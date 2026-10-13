import csv
import io
from pathlib import Path

import pytest

from shuto import ledger, report
from shuto.models import Photo, Status


def _flt(rules, theme=None, aside=(), overridden=None):
    return {
        "v": 1,
        "at": "2026-10-13T10:00:00+00:00",
        "model": "ollama/qwen",
        "verdict": "set_aside" if aside else "ok",
        "theme": theme,
        "rules": rules,
        "set_aside_by": list(aside),
        "overridden": overridden,
    }


def _rule(rid, kind, text, result, reason=""):
    return {
        "id": rid,
        "kind": kind,
        "text": text,
        "mode": "set_aside",
        "answer": "yes",
        "result": result,
        "reason": reason,
    }


def _photos():
    p1 = Photo(
        sha256="s1",
        src_path="/x/1.jpg",
        orig_name="築地市場.jpg",
        batch_id="b",
        status=Status.SET_ASIDE,
        id=1,
        cull={"ok": True, "reasons": []},
        guardrail={"action": "review", "hits": [{"name": "Logo", "kind": "logo"}]},
        metadata={"title": '=HYPERLINK("http://evil","x")', "keywords": ["a", "b"]},
        filters=_flt(
            [
                _rule("r1", "avoid", "people", "fail", "a crowd"),
                _rule("r2", "include", "car", "pass"),
            ],
            theme={"text": "Everyday Japan", "fit": "no", "reason": "a beach"},
            aside=["theme", "rule:r1"],
        ),
    )
    p2 = Photo(
        sha256="s2",
        src_path="/x/2.jpg",
        orig_name="2.jpg",
        batch_id="b",
        status=Status.APPROVED,
        id=2,
        cull={"ok": False, "reasons": ["blurry", "dark"]},
        metadata_source="llm:ollama/qwen",
        filters={
            **_flt(
                [_rule("r9", "avoid", "dogs", "unknown", "no answer")],
                overridden={"at": "t", "status_was": "set_aside"},
            ),
            "model": "",
        },
    )
    p3 = Photo(sha256="s3", src_path="/x/3.jpg", orig_name="3.jpg", batch_id="b", id=3)
    return [p1, p2, p3]


def _build(photos=None):
    return report.build_rows(
        photos or _photos(),
        times={2: {"approved": "2026-10-14"}, 1: {"set_aside": "z"}},
        duplicate_of={3: "1.jpg"},
        labels=lambda s: f"L:{s}",
        rejections={3: ("2026-11-01", "ip: logo")},
    )


def test_header_has_union_of_rule_columns_in_first_seen_order():
    header, records = _build()
    i = header.index("Must avoid: people")
    assert header[i : i + 3] == ["Must avoid: people", "Must include: car", "Must avoid: dogs"]
    assert header[:3] == ["file", "batch", "sha256"]
    assert header[-1] == "rejection_reason"
    assert all(len(r) == len(header) for r in records)


def test_cells():
    header, records = _build()
    a, b, c = (dict(zip(header, r, strict=True)) for r in records)
    assert a["Must avoid: people"] == "fail" and a["Must include: car"] == "pass"
    assert a["Must avoid: dogs"] == ""  # not checked with that rule
    assert b["Must avoid: dogs"] == "unknown" and b["Must avoid: people"] == ""
    assert a["theme_fit"] == "no" and a["theme_reason"] == "a beach"
    assert a["set_aside_by"] == "Theme; Must avoid: people"
    assert a["your_rules_reasons"] == "Must avoid: people: a crowd"
    assert a["rules_hit"] == "Logo" and a["shutterstock_rules"] == "review"
    assert a["keyword_count"] == "2" and a["status_label"] == "L:set_aside"
    assert a["checked_at"] == "2026-10-13T10:00:00+00:00" and a["ai_model"] == "ollama/qwen"
    assert b["quality"] == "failed" and b["quality_reasons"] == "blurry; dark"
    assert b["approved_by_hand"] == "yes" and b["approved_at"] == "2026-10-14"
    assert b["your_rules_reasons"] == "Must avoid: dogs: no answer"
    assert b["ai_model"] == "ollama/qwen"  # falls back to the describe model
    assert c["duplicate_of"] == "1.jpg" and c["quality"] == "" and c["theme"] == ""
    assert c["rejected_at"] == "2026-11-01" and c["rejection_reason"] == "ip: logo"
    assert c["approved_by_hand"] == "" and c["checked_at"] == "" and c["ai_model"] == ""


def test_checked_at_without_theme_or_rules_falls_back_to_the_describe_time():
    p = Photo(
        sha256="s4", src_path="/x/4.jpg", orig_name="4.jpg", batch_id="b", id=4,
        status=Status.DESCRIBED, metadata_source="llm:ollama/qwen",
    )  # fmt: skip
    header, records = report.build_rows(
        [p],
        times={4: {"described": "2026-10-15T08:00:00"}},
        duplicate_of={},
        labels=str,
        rejections={},
    )
    assert dict(zip(header, records[0], strict=True))["checked_at"] == "2026-10-15T08:00:00"


def test_empty_batch_has_header_only():
    header, records = report.build_rows([], times={}, duplicate_of={}, labels=str, rejections={})
    assert records == [] and "file" in header
    assert report.render(header, records).startswith(b"\xef\xbb\xbf")


def test_render_bom_crlf_and_formula_guard():
    header, records = _build()
    data = report.render(header, records)
    assert data.startswith(b"\xef\xbb\xbf")
    assert b"\r\n" in data
    rows = list(csv.reader(io.StringIO(data.decode("utf-8-sig"), newline="")))
    assert rows[1][header.index("title")].startswith("'=HYPERLINK(")
    assert rows[1][0] == "築地市場.jpg"
    for row in rows:
        for cell in row:
            assert not cell.startswith(("=", "+", "-", "@"))


@pytest.mark.parametrize("cell", ["=1+1", "+1", "-2", "@SUM(A1)"])
def test_each_formula_prefix_guarded(cell):
    data = report.render(["h"], [[cell]]).decode("utf-8-sig")
    assert data.splitlines()[1] == "'" + cell


def test_header_is_guarded_too():
    data = report.render(["=bad"], [["x"]]).decode("utf-8-sig")
    assert data.splitlines()[0] == "'=bad"


def test_write_report_roundtrip_and_overwrite(tmp_path):
    header, records = _build()
    path = tmp_path / "reports" / "b.csv"
    assert report.write_report(path, header, records) == path
    assert path.read_bytes() == report.render(header, records)
    with path.open(encoding="utf-8-sig", newline="") as fh:
        assert next(csv.DictReader(fh))["file"] == "築地市場.jpg"
    report.write_report(path, header, records[:1])
    assert len(path.read_bytes().splitlines()) == 2
    assert [p.name for p in path.parent.iterdir()] == ["b.csv"]


def test_write_report_leaves_no_temp_file_on_error(tmp_path, monkeypatch):
    path = tmp_path / "reports" / "b.csv"
    report.write_report(path, ["h"], [["old"]])

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(report.os, "replace", boom)
    with pytest.raises(OSError):
        report.write_report(path, ["h"], [["new"]])
    assert [p.name for p in path.parent.iterdir()] == ["b.csv"]
    assert b"old" in path.read_bytes()


def test_report_filename():
    assert report.report_filename("2026-W41") == "2026-W41.csv"
    for weird in ("a/b", "..\\x", "日本", "", "a b", "CON:"):
        name = report.report_filename(weird)
        assert name.endswith(".csv")
        assert not set(name) & set("/\\: ")
        assert not name.startswith(".")
        assert Path(name).name == name
    assert report.report_filename("a/b") != report.report_filename("a_b")
    assert report.report_filename("a/b") == report.report_filename("a/b")


def test_ledger_alias():
    assert ledger.safe_cell is ledger._safe
    assert ledger.safe_cell("=x") == "'=x" and ledger.safe_cell("ok") == "ok"
