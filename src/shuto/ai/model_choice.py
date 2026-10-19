"""The explicit local-model choice (`model_choice.json`): `model: auto` only works after it."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path


class ModelChoiceRequired(Exception):
    """`model: auto` was used but the user never explicitly chose a model."""


def load_choice(path: Path) -> str | None:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8")).get("model") or None
    except (OSError, ValueError, AttributeError):
        return None


def save_choice(path: Path, tag: str) -> None:
    """Record that the user explicitly chose this model (enables `model: auto`)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"model": tag, "at": datetime.now(UTC).isoformat()}), encoding="utf-8"
    )


def clear_choice(path: Path) -> None:
    """Forget the explicit model choice (its model was removed)."""
    try:
        Path(path).unlink()
    except FileNotFoundError:
        pass


def resolve_model(configured: str, choice_path: Path) -> str:
    """An explicit model tag is used as-is; `auto` only works after an explicit choice."""
    if configured and configured != "auto":
        return configured
    chosen = load_choice(choice_path)
    if chosen:
        return chosen
    raise ModelChoiceRequired(
        "No local model chosen yet. Run `shuto models` to see the top 8 for this machine "
        "and pick one with `shuto models use <tag>`."
    )
