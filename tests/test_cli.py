from __future__ import annotations

import pytest
import yaml

from shuto import cli, deliver, local_models, pipeline
from shuto.cli import build_parser, main

from .conftest import FakeOllama

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

    real = deliver.upload_files
    monkeypatch.setattr(
        deliver, "upload_files", lambda creds, files, cb: real(creds, files, cb, lambda *a: FTP())
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
    csvs = sorted((tmp_path / "ws" / "exports").glob("*.csv"))
    assert csvs and "manual" in out
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
    monkeypatch.setattr(pipeline, "ollama_client", lambda cfg: FakeOllama(["moondream:latest"]))
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
    monkeypatch.setattr(pipeline, "ollama_client", lambda cfg: FakeOllama())
    run(config_file, "import", str(photos_dir))
    capsys.readouterr()
    assert run(config_file, "describe", "2025-W10", "--yes") == 2
    out = capsys.readouterr().out
    assert "shuto models list" in out and "shuto models use" in out


def test_models_use_pull_confirmation(config_file, capsys, monkeypatch):
    client = FakeOllama()
    monkeypatch.setattr(pipeline, "ollama_client", lambda cfg: client)
    config_file.write_text(
        yaml.safe_dump(
            {"workspace": str(config_file.parent / "ws"), "llm": {"provider": "ollama"}}
        ),
        encoding="utf-8",
    )
    assert run(config_file, "models", "use", "moondream") == 0
    assert local_models.load_choice(pipeline.model_choice_path()) == "moondream"
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
    monkeypatch.setattr(pipeline, "ollama_client", lambda cfg: client)
    monkeypatch.setattr(local_models, "free_gb", lambda p: 0.2)
    assert run(config_file, "models", "pull", "moondream", "--yes") == 1
    out = capsys.readouterr().out
    assert "OLLAMA_MODELS" in out or "API options" in out
    assert "$" in out and not client.pull_calls


def test_init_asks_before_download(config_file, capsys, monkeypatch):
    client = FakeOllama()
    monkeypatch.setattr(pipeline, "ollama_client", lambda cfg: client)
    config_file.write_text(
        yaml.safe_dump(
            {"workspace": str(config_file.parent / "ws"), "llm": {"provider": "ollama"}}
        ),
        encoding="utf-8",
    )
    answers = iter(["1", "n"])  # pick row 1, then decline the download
    monkeypatch.setattr(cli, "_input", lambda prompt: next(answers))
    assert run(config_file, "init") == 0
    assert local_models.load_choice(pipeline.model_choice_path())
    assert not client.pull_calls
    out = capsys.readouterr().out
    assert "Saved choice" in out and "Nothing was downloaded" in out


def test_models_bench(config_file, capsys, monkeypatch):
    monkeypatch.setattr(pipeline, "ollama_client", lambda cfg: FakeOllama())
    monkeypatch.setattr(
        local_models,
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
    monkeypatch.setattr(pipeline, "ollama_client", lambda cfg: FakeOllama(["moondream:latest"]))

    def boom(*a, **k):
        raise pipeline.PipelineError("bad choice")

    monkeypatch.setattr(pipeline, "resolve_selection", boom)
    assert run(config_file, "doctor") == 0
    out = capsys.readouterr().out
    assert "could not be read" in out
    assert "(not chosen)" not in out and "shuto models use" not in out
