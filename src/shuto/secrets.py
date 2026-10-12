"""Secret lookup: environment, then .env files, then the OS keyring. Never stored in config."""

from __future__ import annotations

import os
from pathlib import Path

from .config import default_config_dir

KEYRING_SERVICE = "shuto"
KNOWN_SECRETS = (
    "SHUTO_FTPS_USER",
    "SHUTO_FTPS_PASSWORD",
    "ANTHROPIC_API_KEY",
    "GEMINI_API_KEY",
)


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


def set_secret(name: str, value: str) -> None:
    kr = _keyring()
    if kr is None:
        raise RuntimeError("keyring is not installed; run: pip install 'shuto[keyring]'")
    kr.set_password(KEYRING_SERVICE, name, value)
