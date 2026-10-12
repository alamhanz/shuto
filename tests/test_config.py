from pathlib import Path

import pytest

from shuto import config as cfgmod
from shuto.config import Config, ConfigError, load_config

EXAMPLE = Path(__file__).resolve().parent.parent / "shuto.example.yaml"


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.delenv("SHUTO_CONFIG", raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cfgmod, "default_config_dir", lambda: tmp_path / "cfgdir")


def test_defaults_when_no_file():
    cfg = load_config()
    assert cfg.llm.provider == "ollama"
    assert cfg.llm.model == "auto"
    assert cfg.import_.batch_by == "iso_week"
    assert cfg.deliver.upload_enabled is False


def test_example_config_loads():
    cfg = load_config(EXAMPLE)
    assert cfg.style.theme == ""
    assert cfg.style.region_focus == ""
    assert cfg.style.keywords.always_include == []
    assert cfg.style.keywords.romaji_place_names is False
    assert cfg.import_.near_duplicate_distance == 6
    assert cfg.sources[0].recursive is True
    assert "~" not in str(cfg.sources[0].path)
    assert cfg.cull.mean_luma_range == (35, 220)


def test_unknown_key_rejected_with_path(tmp_path):
    p = tmp_path / "bad.yaml"
    p.write_text("llm:\n  providr: ollama\n", encoding="utf-8")
    with pytest.raises(ConfigError, match=r"llm\.providr"):
        load_config(p)


def test_invalid_provider_rejected(tmp_path):
    p = tmp_path / "bad.yaml"
    p.write_text("llm:\n  provider: nope\n", encoding="utf-8")
    with pytest.raises(ConfigError, match=r"llm\.provider"):
        load_config(p)


def test_missing_explicit_path(tmp_path):
    with pytest.raises(ConfigError):
        load_config(tmp_path / "missing.yaml")


def test_lookup_order(tmp_path, monkeypatch):
    (tmp_path / "shuto.yaml").write_text("web: {port: 1111}\n", encoding="utf-8")
    env_file = tmp_path / "env.yaml"
    env_file.write_text("web: {port: 2222}\n", encoding="utf-8")
    explicit = tmp_path / "explicit.yaml"
    explicit.write_text("web: {port: 3333}\n", encoding="utf-8")
    assert load_config().web.port == 1111
    monkeypatch.setenv("SHUTO_CONFIG", str(env_file))
    assert load_config().web.port == 2222
    assert load_config(explicit).web.port == 3333


def test_user_config_dir_fallback(tmp_path):
    d = tmp_path / "cfgdir"
    d.mkdir()
    (d / "shuto.yaml").write_text("web: {port: 4444}\n", encoding="utf-8")
    assert load_config().web.port == 4444


def test_env_vars_and_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("SHUTO_TEST_DIR", str(tmp_path))
    p = tmp_path / "c.yaml"
    p.write_text("workspace: $SHUTO_TEST_DIR/ws\n", encoding="utf-8")
    cfg = load_config(p)
    assert cfg.workspace_path("thumbs", "x.jpg") == tmp_path / "ws" / "thumbs" / "x.jpg"


def test_default_workspace_is_absolute():
    assert Config().workspace_path("shuto.db").name == "shuto.db"
    assert cfgmod.default_workspace().is_absolute()


def test_no_secret_fields():
    def names(model):
        for name, field in model.model_fields.items():
            yield name.lower()
            ann = field.annotation
            if isinstance(ann, type) and hasattr(ann, "model_fields"):
                yield from names(ann)

    assert not [n for n in names(Config) if "password" in n or "api_key" in n or "secret" in n]
