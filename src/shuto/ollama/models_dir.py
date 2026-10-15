"""Where Ollama keeps its models: choose a folder, restart Ollama, optionally move the models.

Ollama reads OLLAMA_MODELS only when it starts, so changing the folder means (1) saving the
setting where new programs will find it, (2) restarting Ollama, (3) checking that it came back.
Windows and macOS are handled here without administrator rights. On Linux the service is
configured with systemd, which needs administrator rights, so the Help page explains it.

Safety of a move: files are copied under a temporary name, every copy is size-checked, Ollama
is restarted on the new folder and must still list every model it listed before. Only then are
the old `blobs/` and `manifests/` files removed. Any failure leaves the old files untouched.
"""

from __future__ import annotations

import os
import plistlib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from shuto.ai import hardware
from shuto.core import platform as core_platform
from shuto.ollama import env, service
from shuto.ollama import move as moving
from shuto.ollama.env import (  # alias until T9
    ENV_NAME,
    LINUX_TEXT,
    MACHINE_RESET_TEXT,
    MACHINE_TEXT,
    _win_read,  # tests/conftest.py patches models_dir._win_read (kept as a seam)
    reset_models_dir,
    set_models_dir,
)
from shuto.ollama.errors import MoveCancelled, StoreError, StoreUnsupported
from shuto.ollama.move import _gb, freeable_bytes, models_size_bytes  # noqa: F401  alias until T9


@dataclass
class Check:
    path: Path
    warnings: list[str] = field(default_factory=list)


def os_name() -> str:  # alias until T9
    return core_platform.os_name()


def supported(name: str | None = None) -> bool:
    return (name or core_platform.os_name()) in ("windows", "macos")


def current_models_dir() -> Path:
    """Where Ollama keeps its models, from the saved setting rather than this process's copy."""
    os_ = core_platform.os_name()
    if supported(os_):
        value = saved_value(os_) or machine_any_value(os_)
        if value:
            return Path(value).expanduser()
        return hardware.ollama_models_dir(env={})
    return hardware.ollama_models_dir()


def default_models_dir(home: Path | None = None) -> Path:
    return (home or Path.home()) / ".ollama" / "models"


def is_custom() -> bool:
    os_ = core_platform.os_name()
    if supported(os_):
        return bool(saved_value(os_) or machine_any_value(os_))
    return bool(os.environ.get(ENV_NAME))


def machine_any_value(os_name: str | None = None) -> str | None:
    """The whole-computer OLLAMA_MODELS on Windows, whether or not this user also has one."""
    if (os_name or core_platform.os_name()) != "windows":
        return None
    try:
        return _win_read(True)
    except Exception:
        return None


def machine_wide_value(os_name: str | None = None) -> str | None:
    """The whole-computer OLLAMA_MODELS on Windows when this user has no value of their own
    (a user value would win over it). shuto cannot change it without administrator rights."""
    if (os_name or core_platform.os_name()) != "windows":
        return None
    try:
        if _win_read(False):
            return None
    except Exception:
        return None
    return machine_any_value(os_name)


def saved_value(os_name: str | None = None) -> str | None:
    """OLLAMA_MODELS as saved for this user (registry / launchctl), not as this process
    happened to inherit it. Where nothing is saved (or on Linux) None, or the process value."""
    os_ = os_name or core_platform.os_name()
    if os_ == "windows":
        try:
            return _win_read(False)
        except Exception:
            return None
    if os_ == "macos":
        value = str(
            getattr(env._run(["launchctl", "getenv", ENV_NAME]), "stdout", "") or ""
        ).strip()
        if value:
            return value
        try:
            args = plistlib.loads(env._plist_path().read_bytes()).get("ProgramArguments", [])
            return str(args[3]) if len(args) > 3 else None
        except Exception:
            return None
    return os.environ.get(ENV_NAME) or None


# ---------------------------------------------------------------- the folder you pick


def validate_target(raw: str, current: Path | None = None) -> Check:
    """Check a folder chosen by the user. Raises StoreError in plain words.

    Nothing is created here; the folder is made (and tested) when the change runs.
    """
    raw = (raw or "").strip().strip('"')
    if not raw or "\x00" in raw:
        raise StoreError("Choose a folder first.")
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise StoreError("Use the full location of the folder, for example D:\\ollama\\models.")
    path = Path(os.path.abspath(path))
    moving.check_distinct(current or current_models_dir(), path)
    if path.exists() and not path.is_dir():
        raise StoreError("That location is a file, not a folder.")
    warnings = []
    if path.parent == path:
        warnings.append(
            "That is the top of a drive. A folder inside it, such as ollama\\models, is tidier."
        )
    return Check(path, warnings)


def _names(client: Any) -> set[str] | None:
    """Models Ollama lists right now; None when it could not be asked."""
    try:
        return {
            hardware.normalize_tag(m.get("name") or m.get("model", ""))
            for m in client.tags()
            if m.get("name") or m.get("model")
        }
    except Exception:
        return None


def login_order_warning(client: Any, os_name: str | None = None) -> str | None:
    """macOS only: after a restart Ollama can start before the folder setting is applied, and
    then it looks in the default folder and lists nothing. Say so when that seems to be the case."""
    if (os_name or core_platform.os_name()) != "macos" or not is_custom():
        return None
    folder = current_models_dir()
    names = _names(client)
    if (
        names
        or names is None
        or not any(n.startswith("manifests") for n, _ in moving._model_files(folder))
    ):
        return None
    return (
        f"Ollama is running but shows no models, although {folder} has some. On a Mac, Ollama "
        "can start at login before the setting is applied. Choose Quit Ollama in the menu bar, "
        "open it again from Applications, and your models will be back."
    )


def change_models_dir(
    target: Path | None,
    *,
    move: bool,
    client: Any,
    progress: moving.Progress | None = None,
    cancelled: Callable[[], bool] = lambda: False,
    os_name: str | None = None,
) -> str:
    """Point Ollama at `target` (None = back to the default folder) and restart it.

    Returns a plain-language result. Raises StoreError if something went wrong; in that case
    the previous setting is restored and no model file has been deleted.
    """
    os_ = os_name or core_platform.os_name()
    if not supported(os_):
        raise StoreUnsupported(LINUX_TEXT)
    if machine_wide_value(os_):
        raise StoreError(MACHINE_TEXT)
    if target is None and machine_any_value(os_):
        raise StoreError(MACHINE_RESET_TEXT)
    old = current_models_dir()
    new = default_models_dir() if target is None else Path(os.path.abspath(target))
    moving.check_distinct(old, new)
    prev = saved_value(os_)
    was_up = service._is_up(client)
    installed = was_up or service.can_start(os_)

    def save() -> None:
        reset_models_dir(os_) if target is None else set_models_dir(new, os_)

    if not installed:
        save()
        return f"Saved. Ollama will keep its models in {new} once it is installed."

    if move:  # a first look, while Ollama is still running: stop early if the move cannot work
        moving._check_room(moving.plan_move(old, new), new)

    created = moving.Created()
    restart = False
    plan: moving.MovePlan | None = None
    keep_old = False
    try:
        moving.prepare_folder(new, created)
        if was_up:
            restart = True
            service.stop_ollama(client, os_)
        if move:  # planned after Ollama stopped, so a finishing download is not missed
            plan = moving.plan_move(old, new)
            if plan.conflicts:
                raise StoreError(moving.conflict_message(plan))
            if not plan.files:
                plan = None
            else:
                moving._check_room(plan, new)
                moving.copy_models(plan, progress, cancelled, created)
        save()
        restart = True
        service.start_ollama(client, os_)
        if plan:
            expected = moving.manifest_names(plan.files)
            seen = _names(client)
            if seen is None:
                raise StoreError("Ollama restarted but shuto could not ask it for its models.")
            missing = expected - seen
            if missing:
                raise StoreError(
                    "Ollama restarted but cannot see " + ", ".join(sorted(missing)) + " in "
                    "the new folder."
                )
            keep_old = not expected  # nothing to check the copy against: do not delete
    except MoveCancelled:
        stopped_text = f"Stopped. Nothing was changed. Your models are still in {old}."
        return _undo(client, os_, prev, restart, created, new, stopped_text)
    except StoreError as exc:
        _undo(client, os_, prev, restart, created, new, "")
        raise StoreError(
            f"{exc} The setting was put back and your models are still in {old}."
        ) from None
    except Exception as exc:
        _undo(client, os_, prev, restart, created, new, "")
        raise StoreError(
            f"Something unexpected went wrong ({type(exc).__name__}). "
            f"The setting was put back and your models are still in {old}."
        ) from None

    where = f"Ollama now keeps its models in {new}."
    if plan and keep_old:
        return f"{where} The models were copied; the originals in {old} were left in place."
    if plan:
        try:
            freed = moving.delete_old_models(plan)
        except StoreError as exc:
            return f"{where} The models were copied, but the originals in {old} were kept: {exc}"
        return f"{where} {len(plan.files)} model file(s) were moved; {_gb(freed)} freed in {old}."
    return where + (
        " Models you downloaded before stay in the old folder."
        if moving.models_size_bytes(old)
        else ""
    )


def _undo(
    client: Any,
    os_: str,
    prev: str | None,
    restart: bool,
    created: moving.Created,
    new: Path,
    message: str,
) -> str:
    """Put the old setting back, bring Ollama back, then remove only what this run created."""
    _recover(client, os_, prev, restart)
    left = created.remove()
    if left:
        message += f" Some copied files could not be removed from {new}."
    return message


def _recover(client: Any, os_: str, prev: str | None, restart: bool) -> None:
    """Best effort: restore the previous setting and bring Ollama back on it.

    Ollama may already be running on the new folder, so it is always stopped and started again.
    """
    try:
        if prev:
            set_models_dir(prev, os_)
        else:
            reset_models_dir(os_)
    except StoreError:
        pass
    if restart:
        try:
            service.stop_ollama(client, os_)
        except StoreError:
            pass
        try:
            service.start_ollama(client, os_)
        except StoreError:
            pass
