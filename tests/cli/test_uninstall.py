from __future__ import annotations

import pytest

from shuto import cli
from shuto.core import paths
from shuto.system import launcher


@pytest.fixture
def env(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    (workspace / "thumbs").mkdir(parents=True)
    (workspace / "thumbs" / "a.jpg").write_bytes(b"x")
    (workspace / "shuto.db").write_bytes(b"db")
    (workspace / "mine.txt").write_text("not shuto's")
    photos = tmp_path / "photos"
    photos.mkdir()
    (photos / "IMG_1.jpg").write_bytes(b"photo")
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "model_choice.json").write_text("{}")
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    cfg_file = tmp_path / "shuto.yaml"
    cfg_file.write_text(f"workspace: '{workspace.as_posix()}'\n", encoding="utf-8")
    monkeypatch.setenv("SHUTO_CONFIG", str(cfg_file))
    monkeypatch.setattr(cli, "default_config_dir", lambda: config_dir)
    monkeypatch.setattr(paths, "benchmark_cache_path", lambda: cache_dir / "b.json")
    monkeypatch.setattr(launcher, "is_running", lambda *a, **k: False)
    lnk = tmp_path / "shuto.lnk"
    lnk.write_text("x")
    monkeypatch.setattr(launcher, "_shortcut_paths", lambda: [lnk])
    deleted: list[str] = []
    monkeypatch.setattr(cli, "delete_secret", deleted.append)
    monkeypatch.setattr(cli, "keyring_available", lambda: True)
    answers: list[str] = []
    monkeypatch.setattr(cli, "_input", lambda prompt: answers.pop(0))
    return {
        "workspace": workspace,
        "photos": photos,
        "config": config_dir,
        "cache": cache_dir,
        "lnk": lnk,
        "deleted": deleted,
        "answers": answers,
    }


def test_declined_changes_nothing(env, capsys):
    env["answers"].append("n")
    assert cli.main(["uninstall"]) == 1
    assert "Nothing was changed" in capsys.readouterr().out
    assert env["lnk"].exists()
    assert env["deleted"] == []


def test_yes_removes_shortcuts_and_login_keeps_data(env, capsys):
    assert cli.main(["uninstall", "--yes"]) == 0
    text = capsys.readouterr().out
    assert not env["lnk"].exists()
    assert env["deleted"] == ["SHUTO_FTPS_USER", "SHUTO_FTPS_PASSWORD"]
    assert (env["workspace"] / "shuto.db").exists()
    assert env["config"].exists()
    assert "uv tool uninstall shuto" in text
    assert "ollama rm" in text


def test_delete_data_removes_only_shuto_files(env):
    assert cli.main(["uninstall", "--yes", "--delete-data"]) == 0
    ws = env["workspace"]
    assert not (ws / "shuto.db").exists()
    assert not (ws / "thumbs").exists()
    assert (ws / "mine.txt").exists()  # unknown file in a custom workspace is left alone
    assert not env["config"].exists()
    assert not env["cache"].exists()
    assert (env["photos"] / "IMG_1.jpg").read_bytes() == b"photo"


def test_second_prompt_controls_data(env):
    env["answers"].extend(["y", "n"])
    assert cli.main(["uninstall"]) == 0
    assert (env["workspace"] / "shuto.db").exists()
    env["answers"].extend(["y", "y"])
    assert cli.main(["uninstall"]) == 0
    assert not (env["workspace"] / "shuto.db").exists()
    assert (env["photos"] / "IMG_1.jpg").exists()


def test_keep_login(env):
    assert cli.main(["uninstall", "--yes", "--keep-login"]) == 0
    assert env["deleted"] == []
    assert not env["lnk"].exists()


def test_refuses_while_running(env, monkeypatch, capsys):
    monkeypatch.setattr(launcher, "is_running", lambda *a, **k: True)
    assert cli.main(["uninstall", "--yes"]) == 1
    assert "Close it first" in capsys.readouterr().out
    assert env["lnk"].exists()
    assert env["deleted"] == []


def test_no_keyring_is_fine(env, monkeypatch, capsys):
    monkeypatch.setattr(cli, "keyring_available", lambda: False)
    assert cli.main(["uninstall", "--yes"]) == 0
    assert env["deleted"] == []
    assert "No keyring" in capsys.readouterr().out
    assert not env["lnk"].exists()


def test_keyring_error_reported_and_continues(env, monkeypatch, capsys):
    def boom(name):
        raise RuntimeError("Cannot remove the saved password (locked).")

    monkeypatch.setattr(cli, "delete_secret", boom)
    assert cli.main(["uninstall", "--yes"]) == 0
    assert "locked" in capsys.readouterr().out


def test_broken_config_does_not_block(env, tmp_path, monkeypatch):
    bad = tmp_path / "bad.yaml"
    bad.write_text("workspace: [unclosed\n", encoding="utf-8")
    monkeypatch.setenv("SHUTO_CONFIG", str(bad))
    assert cli.main(["uninstall", "--yes"]) == 0
    assert not env["lnk"].exists()


def test_delete_data_cache_inside_config_dir(env, monkeypatch, capsys):
    nested = env["config"] / "Cache"
    nested.mkdir()
    (nested / "b.json").write_text("{}")
    monkeypatch.setattr(paths, "benchmark_cache_path", lambda: nested / "b.json")
    assert cli.main(["uninstall", "--yes", "--delete-data"]) == 0
    assert not env["config"].exists()
    assert "Could not remove" not in capsys.readouterr().out


def test_delete_data_symlinked_entry_keeps_target(env):
    ws = env["workspace"]
    (ws / "thumbs" / "a.jpg").unlink()
    (ws / "thumbs").rmdir()
    try:
        (ws / "thumbs").symlink_to(env["photos"], target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not available")
    assert cli.main(["uninstall", "--yes", "--delete-data"]) == 0
    assert not (ws / "thumbs").exists()
    assert (env["photos"] / "IMG_1.jpg").read_bytes() == b"photo"


def test_login_removed_message_not_shown_on_error(env, monkeypatch, capsys):
    def boom(name):
        raise RuntimeError("locked")

    monkeypatch.setattr(cli, "delete_secret", boom)
    cli.main(["uninstall", "--yes"])
    assert "Removed the saved Shutterstock login" not in capsys.readouterr().out
