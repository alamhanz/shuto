from __future__ import annotations

import pytest
import yaml

from shuto import cli
from shuto import store as store_pkg
from shuto.ai import benchmark as ai_benchmark
from shuto.ai import catalog as local_models
from shuto.ai import model_choice as ai_choice
from shuto.ai import selection
from shuto.cli import build_parser, main
from shuto.core import errors, paths
from shuto.core.config import load_config
from shuto.core.paths import folder_name
from shuto.deliver import send
from shuto.settings import ui as settings_ui

from ..helpers import FakeOllama

PARSE_CASES = [
    ["ui", "--port", "9000", "--no-browser"],
    ["init"],
    ["doctor"],
    ["models"],
    ["models", "list"],
    ["models", "pull", "moondream", "--yes"],
    ["models", "use", "moondream"],
    ["models", "use", "gemini:gpt"],
    ["models", "bench"],
    ["models", "bench", "moondream"],
    ["import", "photos", "--no-recursive", "--notes", "Kyoto"],
    ["cull", "2025-W10", "--explain"],
    ["describe", "2025-W10", "--ids", "1", "2", "--yes", "--provider", "fake", "--model", "m"],
    ["run", "photos", "--yes"],
    ["review", "2025-W10", "--status", "needs_review", "--full"],
    ["edit", "3", "--description", "d", "--keywords", "a,b", "--no-editorial", "--release", "x"],
    ["approve", "2025-W10", "--include-review"],
    ["approve", "--ids", "1", "2"],
    ["prepare", "2025-W10", "--ids", "4"],
    ["upload", "2025-W10", "--yes"],
    ["export", "2025-W10", "--dry-run"],
    ["submitted", "2025-W10"],
    ["status"],
    ["status", "2025-W10"],
    ["reject", "a.jpg", "--reason", "focus", "--note", "soft"],
    ["accept", "a.jpg"],
    ["secrets", "set", "GEMINI_API_KEY"],
    ["check-ftps", "--upload", "f.jpg"],
    ["restrictions", "list", "--level", "prohibited"],
    ["restrictions", "check", "some text"],
    ["rules"],
    ["rules", "list"],
    ["rules", "add", "--avoid", "people"],
    ["rules", "remove", "r1"],
    ["rules", "theme", "off"],
    ["report", "2025-W10"],
]


@pytest.mark.parametrize("argv", PARSE_CASES, ids=lambda a: " ".join(a[:2]))
def test_every_subcommand_parses(argv):
    args = build_parser().parse_args(argv)
    assert args.command == argv[0] and args.command in cli.HANDLERS


def test_global_flags_before_and_after():
    p = build_parser()
    assert p.parse_args(["--config", "a.yaml", "status"]).config == "a.yaml"
    assert p.parse_args(["status", "--config", "b.yaml"]).config == "b.yaml"
    assert p.parse_args([]).command is None


def test_bad_reason_code_rejected():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["reject", "a.jpg", "--reason", "nope"])


@pytest.fixture
def config_file(tmp_path, isolated):
    path = tmp_path / "shuto.yaml"
    path.write_text(
        yaml.safe_dump({"workspace": str(tmp_path / "ws"), "llm": {"provider": "fake"}}),
        encoding="utf-8",
    )
    return path


def run(config_file, *argv):
    return main(["--config", str(config_file), *argv])


def test_end_to_end_cli(config_file, photos_dir, tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("SHUTO_FTPS_USER", "u")
    monkeypatch.setenv("SHUTO_FTPS_PASSWORD", "p")
    uploaded: dict[str, bytes] = {}

    class FTP:
        def storbinary(self, cmd, fh):
            uploaded[cmd.split(" ", 1)[1]] = fh.read()

        def quit(self):
            pass

        def close(self):
            pass

    real = send.upload_files
    monkeypatch.setattr(
        send,
        "upload_files",
        lambda creds, files, cb, **kw: real(creds, files, cb, lambda *a: FTP(), **kw),
    )

    assert run(config_file, "import", str(photos_dir)) == 0
    batch = "2025-W10"
    assert run(config_file, "cull", batch, "--explain") == 0
    out = capsys.readouterr().out
    assert "3 ready, 2 culled" in out and "blurry" in out

    # estimate is printed and a "no" answer sends nothing to the model
    monkeypatch.setattr(cli, "_input", lambda prompt: "n")
    assert run(config_file, "describe", batch) == 1
    out = capsys.readouterr().out
    assert "Estimate:" in out and "Cancelled" in out

    assert run(config_file, "describe", batch, "--yes") == 0
    assert "Described 3" in capsys.readouterr().out
    assert run(config_file, "review", batch) == 0
    assert run(config_file, "approve", batch) == 0
    assert run(config_file, "prepare", batch) == 0
    capsys.readouterr()

    # upload refused without enable/--yes
    assert run(config_file, "upload", batch) == 1
    assert "disabled" in capsys.readouterr().out and not uploaded
    assert run(config_file, "upload", batch, "--yes") == 0
    assert len(uploaded) == 3

    assert run(config_file, "export", batch, "--dry-run") == 0
    assert run(config_file, "export", batch) == 0
    out = capsys.readouterr().out
    csvs = sorted((tmp_path / "ws" / "batches" / folder_name(batch)).glob("*-shutterstock.csv"))
    assert len(csvs) == 1 and "manual" in out and "Upload CSV" in out
    text = csvs[0].read_text(encoding="utf-8")
    assert all(name in text for name in uploaded)

    assert run(config_file, "submitted", batch) == 0
    name = sorted(uploaded)[0]
    assert run(config_file, "reject", name, "--reason", "focus") == 0
    assert run(config_file, "accept", sorted(uploaded)[1]) == 0
    assert run(config_file, "status") == 0
    assert "Rejections: focus=1" in capsys.readouterr().out


def test_run_command_with_yes(config_file, photos_dir, capsys):
    assert run(config_file, "run", str(photos_dir), "--yes") == 0
    out = capsys.readouterr().out
    assert "Imported 5" in out and "Described 3" in out


def test_doctor_without_choice_shows_top8(config_file, capsys, monkeypatch):
    config_file.write_text(
        yaml.safe_dump(
            {"workspace": str(config_file.parent / "ws"), "llm": {"provider": "ollama"}}
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(selection, "ollama_client", lambda cfg: FakeOllama(["moondream:latest"]))
    assert run(config_file, "doctor") == 0
    out = capsys.readouterr().out
    assert "Model:     (not chosen)" in out and "shuto models use" in out
    lines = [ln for ln in out.splitlines() if ln[:1].isdigit() and "GB" in ln]
    assert len(lines) == 8
    assert "download" in out and "est. per image" in out and "fits disk" in out


def test_describe_without_choice_is_friendly(config_file, photos_dir, capsys, monkeypatch):
    config_file.write_text(
        yaml.safe_dump(
            {"workspace": str(config_file.parent / "ws"), "llm": {"provider": "ollama"}}
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(selection, "ollama_client", lambda cfg: FakeOllama())
    run(config_file, "import", str(photos_dir))
    capsys.readouterr()
    assert run(config_file, "describe", "2025-W10", "--yes") == 2
    out = capsys.readouterr().out
    assert "shuto models list" in out and "shuto models use" in out


def test_models_use_pull_confirmation(config_file, capsys, monkeypatch):
    client = FakeOllama()
    monkeypatch.setattr(selection, "ollama_client", lambda cfg: client)
    config_file.write_text(
        yaml.safe_dump(
            {"workspace": str(config_file.parent / "ws"), "llm": {"provider": "ollama"}}
        ),
        encoding="utf-8",
    )
    assert run(config_file, "models", "use", "moondream") == 0
    assert ai_choice.load_choice(paths.model_choice_path()) == "moondream"
    assert not client.pull_calls  # choosing never downloads
    monkeypatch.setattr(cli, "_input", lambda prompt: "n")
    assert run(config_file, "models", "pull", "moondream") == 0
    assert not client.pull_calls
    monkeypatch.setattr(cli, "_input", lambda prompt: "y")
    assert run(config_file, "models", "pull", "moondream") == 0
    assert client.pull_calls == ["moondream"]
    client.pull_calls.clear()
    assert run(config_file, "models", "pull", "moondream", "--yes") == 0
    assert client.pull_calls == ["moondream"]


def test_pull_refused_when_it_does_not_fit(config_file, capsys, monkeypatch):
    client = FakeOllama()
    monkeypatch.setattr(selection, "ollama_client", lambda cfg: client)
    monkeypatch.setattr(local_models, "free_gb", lambda p: 0.2)
    assert run(config_file, "models", "pull", "moondream", "--yes") == 1
    out = capsys.readouterr().out
    assert "OLLAMA_MODELS" in out or "API options" in out
    assert "$" in out and not client.pull_calls


def test_init_asks_before_download(config_file, capsys, monkeypatch):
    client = FakeOllama()
    monkeypatch.setattr(selection, "ollama_client", lambda cfg: client)
    config_file.write_text(
        yaml.safe_dump(
            {"workspace": str(config_file.parent / "ws"), "llm": {"provider": "ollama"}}
        ),
        encoding="utf-8",
    )
    answers = iter(["1", "n"])  # pick row 1, then decline the download
    monkeypatch.setattr(cli, "_input", lambda prompt: next(answers))
    assert run(config_file, "init") == 0
    assert ai_choice.load_choice(paths.model_choice_path())
    assert not client.pull_calls
    out = capsys.readouterr().out
    assert "Saved choice" in out and "Nothing was downloaded" in out


def test_models_bench(config_file, capsys, monkeypatch):
    monkeypatch.setattr(selection, "ollama_client", lambda cfg: FakeOllama())
    monkeypatch.setattr(
        ai_benchmark,
        "benchmark",
        lambda *a: {"cold_s": 2.0, "warm_s": 1.0, "ok_json": True},
    )
    assert run(config_file, "models", "bench", "moondream") == 0
    assert "warm 1.0 s" in capsys.readouterr().out


def test_restrictions_commands(config_file, capsys):
    assert run(config_file, "restrictions", "list", "--level", "prohibited") == 0
    assert "rule(s)" in capsys.readouterr().out
    assert run(config_file, "restrictions", "check", "Le Corbusier Ueno museum") == 0
    assert "Result: block" in capsys.readouterr().out


def test_bad_config_is_a_clean_error(tmp_path, capsys):
    bad = tmp_path / "bad.yaml"
    bad.write_text("llm: {provider: nope}", encoding="utf-8")
    assert main(["--config", str(bad), "status"]) == 1
    assert "error:" in capsys.readouterr().err


def test_ui_without_web_package(config_file, capsys, monkeypatch):
    import sys

    monkeypatch.setitem(sys.modules, "shuto.web", None)
    assert run(config_file, "ui") == 1
    assert "not available" in capsys.readouterr().out


def test_doctor_says_when_the_model_choice_cannot_be_read(config_file, capsys, monkeypatch):
    config_file.write_text(
        yaml.safe_dump(
            {"workspace": str(config_file.parent / "ws"), "llm": {"provider": "ollama"}}
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(selection, "ollama_client", lambda cfg: FakeOllama(["moondream:latest"]))

    def boom(*a, **k):
        raise errors.PipelineError("bad choice")

    monkeypatch.setattr(selection, "resolve_selection", boom)
    assert run(config_file, "doctor") == 0
    out = capsys.readouterr().out
    assert "could not be read" in out
    assert "(not chosen)" not in out and "shuto models use" not in out


# --------------------------------------------------------------------------- rules and report


def _rules_out(config_file, capsys, *argv):
    code = run(config_file, "rules", *argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_rules_parse_cases():
    p = build_parser()
    assert p.parse_args(["rules"]).rules_cmd is None
    assert p.parse_args(["rules", "add", "--avoid", "people", "--flag"]).flag is True
    assert p.parse_args(["rules", "mode", "r1", "set-aside"]).mode == "set-aside"
    assert p.parse_args(["report", "b", "--out", "x.csv"]).out == "x.csv"
    for bad in (["rules", "add"], ["rules", "add", "--avoid", "a", "--include", "b"]):
        with pytest.raises(SystemExit):
            p.parse_args(bad)


def test_rules_add_list_mode_remove(config_file, capsys):
    code, out, _ = _rules_out(config_file, capsys)
    assert code == 0 and "No rules of your own yet" in out

    code, out, _ = _rules_out(config_file, capsys, "add", "--avoid", "people")
    assert code == 0 and "Added rule" in out and "from now on" in out
    code, out, _ = _rules_out(config_file, capsys, "add", "--include", "a car", "--flag")
    assert code == 0
    code, out, _ = _rules_out(config_file, capsys, "list")
    assert "Must avoid: people" in out and "set aside" in out
    assert "Must include: a car" in out and "just flag" in out

    store = store_pkg.open_store(load_config(str(config_file)))
    saved = settings_ui.get_ui_setting(store, "filters")["rules"]
    assert [r["kind"] for r in saved] == ["avoid", "include"]
    avoid_id = saved[0]["id"]

    assert _rules_out(config_file, capsys, "mode", avoid_id, "flag")[0] == 0
    out = _rules_out(config_file, capsys, "list")[1]
    assert out.count("just flag") == 2

    code, out, _ = _rules_out(config_file, capsys, "remove", avoid_id)
    assert code == 0
    out = _rules_out(config_file, capsys, "list")[1]
    assert "people" not in out and "a car" in out

    assert _rules_out(config_file, capsys, "clear")[0] == 0
    assert "No rules of your own yet" in _rules_out(config_file, capsys, "list")[1]


def test_rules_validation_errors(config_file, capsys):
    code, _, err = _rules_out(config_file, capsys, "add", "--avoid", "x")
    assert code == 1 and err.startswith("error:")
    code, _, err = _rules_out(config_file, capsys, "remove", "nope")
    assert code == 1 and "no rule with id" in err
    code, _, err = _rules_out(config_file, capsys, "mode", "nope", "flag")
    assert code == 1 and "no rule with id" in err
    for i in range(10):
        assert _rules_out(config_file, capsys, "add", "--avoid", f"thing {i}")[0] == 0
    code, _, err = _rules_out(config_file, capsys, "add", "--avoid", "one more")
    assert code == 1 and "10" in err
    assert "thing 9" in _rules_out(config_file, capsys, "list")[1]


def test_rules_theme_and_partly(config_file, capsys):
    code, out, _ = _rules_out(config_file, capsys, "theme", "off")
    assert code == 0 and "off" in out
    assert "Theme check: off" in _rules_out(config_file, capsys, "list")[1]
    assert _rules_out(config_file, capsys, "partly", "flag")[0] == 0
    assert "needs a look" in _rules_out(config_file, capsys, "list")[1]
    assert _rules_out(config_file, capsys, "theme", "on")[0] == 0
    assert "Theme check: on" in _rules_out(config_file, capsys, "list")[1]


def test_rules_theme_off_keeps_the_rules(config_file, capsys):
    _rules_out(config_file, capsys, "add", "--avoid", "people")
    assert _rules_out(config_file, capsys, "theme", "off")[0] == 0
    out = _rules_out(config_file, capsys, "list")[1]
    assert "Must avoid: people" in out and "Theme check: off" in out


def test_rules_partly_flag_keeps_the_rules(config_file, capsys):
    _rules_out(config_file, capsys, "add", "--avoid", "people")
    assert _rules_out(config_file, capsys, "partly", "flag")[0] == 0
    out = _rules_out(config_file, capsys, "list")[1]
    assert "Must avoid: people" in out and "needs a look" in out


def test_rules_add_keeps_the_theme_settings(config_file, capsys):
    _rules_out(config_file, capsys, "theme", "off")
    _rules_out(config_file, capsys, "partly", "flag")
    assert _rules_out(config_file, capsys, "add", "--avoid", "people")[0] == 0
    out = _rules_out(config_file, capsys, "list")[1]
    assert "Must avoid: people" in out and "Theme check: off" in out and "needs a look" in out


def test_rules_are_shared_with_the_web_settings(config_file, capsys):
    store = store_pkg.open_store(load_config(str(config_file)))
    settings_ui.set_ui_setting(
        store, "filters", {"rules": [{"kind": "avoid", "text": "logos", "mode": "flag"}]}
    )
    assert "Must avoid: logos" in _rules_out(config_file, capsys, "list")[1]


def test_doctor_shows_checks(config_file, capsys, monkeypatch):
    monkeypatch.setattr(selection, "ollama_client", lambda cfg: FakeOllama([]))
    assert run(config_file, "doctor") == 0
    assert "Checks:    none" in capsys.readouterr().out
    run(config_file, "rules", "add", "--avoid", "people")
    capsys.readouterr()
    assert run(config_file, "doctor") == 0
    out = capsys.readouterr().out
    assert "Checks:    Must avoid: people (set aside)" in out


def test_doctor_shows_default_place(config_file, capsys, monkeypatch):
    monkeypatch.setattr(selection, "ollama_client", lambda cfg: FakeOllama([]))
    assert run(config_file, "doctor") == 0
    assert "Place:     no default place" in capsys.readouterr().out
    data = yaml.safe_load(config_file.read_text(encoding="utf-8"))
    data["style"] = {"default_place": {"city": "Tokyo", "country": "Japan"}}
    config_file.write_text(yaml.safe_dump(data), encoding="utf-8")
    assert run(config_file, "doctor") == 0
    assert "Place:     Tokyo, Japan" in capsys.readouterr().out


def test_report_command(config_file, photos_dir, tmp_path, capsys):
    assert run(config_file, "report", "2025-W10") == 1  # no such batch
    assert "no photos" in capsys.readouterr().err
    assert run(config_file, "import", str(photos_dir)) == 0
    assert run(config_file, "cull", "2025-W10") == 0
    assert run(config_file, "describe", "2025-W10", "--yes") == 0
    assert "set aside 0" in capsys.readouterr().out
    assert run(config_file, "report", "2025-W10") == 0
    out = capsys.readouterr().out
    path = tmp_path / "ws" / "batches" / folder_name("2025-W10") / "report.csv"
    assert str(path) in out and path.read_bytes().startswith(b"\xef\xbb\xbf")
    target = tmp_path / "elsewhere" / "r.csv"
    assert run(config_file, "report", "2025-W10", "--out", str(target)) == 0
    assert target.exists() and "file" in target.read_text(encoding="utf-8-sig")


def test_doctor_warns_when_another_ollama_answers(config_file, capsys, monkeypatch):
    from shuto.ollama import conflict

    class OldOllama(FakeOllama):
        def version(self):
            return "0.22.0"

    monkeypatch.setattr(conflict, "installed_version", lambda *a, **k: "0.40.8")
    monkeypatch.setattr(selection, "ollama_client", lambda cfg: OldOllama())
    assert run(config_file, "doctor") == 0
    out = capsys.readouterr().out
    assert "Warning:" in out and "0.22.0" in out and "3. Come back here" in out
