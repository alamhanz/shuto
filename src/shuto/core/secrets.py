"""Secret lookup: environment, then .env files, then the OS keyring. Never stored in config."""

from __future__ import annotations

import os
from pathlib import Path

from shuto.core.config import default_config_dir

KEYRING_SERVICE = "shuto"
FTPS_USER = "SHUTO_FTPS_USER"
FTPS_PASSWORD = "SHUTO_FTPS_PASSWORD"
ANTHROPIC_API_KEY = "ANTHROPIC_API_KEY"
GEMINI_API_KEY = "GEMINI_API_KEY"
# The provider registry (ai.providers.REGISTRY) takes its key names from the two API constants,
# so this list and the registry cannot drift; tests/ai/test_registry.py checks it.
KNOWN_SECRETS = (FTPS_USER, FTPS_PASSWORD, ANTHROPIC_API_KEY, GEMINI_API_KEY)


def _keyring():
    try:
        import keyring
    except ImportError:
        return None
    return keyring


def load_dotenv_file(path: str | os.PathLike[str]) -> dict[str, str]:
    """Parse a simple KEY=VALUE file. A missing file gives {}. Does not touch os.environ."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return {}
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            out[key] = value
    return out


def get_secret(name: str) -> str | None:
    value = os.environ.get(name)
    if value:
        return value
    for env_file in (Path.cwd() / ".env", default_config_dir() / ".env"):
        value = load_dotenv_file(env_file).get(name)
        if value:
            return value
    kr = _keyring()
    if kr is not None:
        try:
            value = kr.get_password(KEYRING_SERVICE, name)
        except Exception:
            value = None
        if value:
            return value
    return None


def keyring_available() -> bool:
    """True when the OS keyring can be used (the package is installed and has a backend)."""
    kr = _keyring()
    if kr is None:
        return False
    try:
        from keyring.backends import fail

        return not isinstance(kr.get_keyring(), fail.Keyring)
    except Exception:
        return False


def set_secret(name: str, value: str) -> None:
    kr = _keyring()
    if kr is None:
        raise RuntimeError(
            "Cannot save the password: the keyring add-on is missing. "
            "Reinstall shuto (pip install --upgrade shuto) or put it in a .env file instead."
        )
    try:
        kr.set_password(KEYRING_SERVICE, name, value)
    except Exception as exc:
        raise RuntimeError(
            f"Cannot save the password: this computer's keyring refused it ({exc}). "
            "You can put it in a .env file instead."
        ) from exc


def delete_secret(name: str) -> None:
    """Remove a stored secret from the keyring. Not stored or no keyring is not an error."""
    kr = _keyring()
    if kr is None:
        return
    try:
        kr.delete_password(KEYRING_SERVICE, name)
    except Exception as exc:
        if type(exc).__name__ == "PasswordDeleteError":
            return
        raise RuntimeError(f"Cannot remove the saved password ({exc}).") from exc
