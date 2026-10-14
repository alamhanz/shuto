import pytest

from shuto.core import secrets
from shuto.core.secrets import KNOWN_SECRETS, get_secret, load_dotenv_file, set_secret


class FakeKeyring:
    def __init__(self):
        self.store = {}

    def get_password(self, service, name):
        return self.store.get((service, name))

    def set_password(self, service, name, value):
        self.store[(service, name)] = value


@pytest.fixture
def env(tmp_path, monkeypatch):
    cfgdir = tmp_path / "cfgdir"
    cfgdir.mkdir()
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    monkeypatch.setattr(secrets, "default_config_dir", lambda: cfgdir)
    monkeypatch.setattr(secrets, "_keyring", lambda: None)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    return cwd, cfgdir


def test_known_secrets():
    assert "SHUTO_FTPS_PASSWORD" in KNOWN_SECRETS
    assert "ANTHROPIC_API_KEY" in KNOWN_SECRETS


def test_missing_returns_none(env):
    assert get_secret("GEMINI_API_KEY") is None


def test_lookup_order(env, monkeypatch):
    cwd, cfgdir = env
    kr = FakeKeyring()
    kr.set_password("shuto", "GEMINI_API_KEY", "from-keyring")
    monkeypatch.setattr(secrets, "_keyring", lambda: kr)
    assert get_secret("GEMINI_API_KEY") == "from-keyring"
    (cfgdir / ".env").write_text("GEMINI_API_KEY=from-config-dir\n", encoding="utf-8")
    assert get_secret("GEMINI_API_KEY") == "from-config-dir"
    (cwd / ".env").write_text("GEMINI_API_KEY=from-cwd\n", encoding="utf-8")
    assert get_secret("GEMINI_API_KEY") == "from-cwd"
    monkeypatch.setenv("GEMINI_API_KEY", "from-env")
    assert get_secret("GEMINI_API_KEY") == "from-env"


def test_keyring_error_is_ignored(env, monkeypatch):
    class Broken:
        def get_password(self, *args):
            raise RuntimeError("no backend")

    monkeypatch.setattr(secrets, "_keyring", lambda: Broken())
    assert get_secret("GEMINI_API_KEY") is None


def test_dotenv_parsing(tmp_path):
    p = tmp_path / ".env"
    p.write_text(
        "# comment\n\nA=1\nexport B = two \nC=\"quoted value\"\nD='x'\nbad line\n",
        encoding="utf-8",
    )
    assert load_dotenv_file(p) == {"A": "1", "B": "two", "C": "quoted value", "D": "x"}
    assert load_dotenv_file(tmp_path / "nope") == {}


def test_set_secret(env, monkeypatch):
    with pytest.raises(RuntimeError, match="keyring"):
        set_secret("GEMINI_API_KEY", "x")
    kr = FakeKeyring()
    monkeypatch.setattr(secrets, "_keyring", lambda: kr)
    set_secret("GEMINI_API_KEY", "x")
    assert get_secret("GEMINI_API_KEY") == "x"
