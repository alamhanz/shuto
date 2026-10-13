"""Characterization tests: behaviour recorded on shuto 0.5.0 (commit 7b9fbc7).

They test through public entry points so the 0.5.1 module moves do not change them. Each test
file resolves the module paths it needs in a `_seam()` function at the top; only those lines
change when a module moves.

Goldens live in `golden/`. To re-record them (only when a change is meant to alter behaviour):
`SHUTO_REGEN_GOLDEN=1 pytest tests/characterization`.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

GOLDEN = Path(__file__).parent / "golden"
REGEN = os.environ.get("SHUTO_REGEN_GOLDEN") == "1"


def check_json(name: str, value) -> None:
    """Compare `value` with `golden/<name>` (JSON, sorted keys, stable text)."""
    text = json.dumps(value, indent=2, ensure_ascii=False, sort_keys=False) + "\n"
    check_text(name, text)


def check_text(name: str, text: str) -> None:
    path = GOLDEN / name
    if REGEN:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))
        return
    assert path.exists(), f"missing golden {name}; run with SHUTO_REGEN_GOLDEN=1"
    expected = path.read_bytes().decode("utf-8")
    assert text.replace("\r\n", "\n") == expected.replace("\r\n", "\n"), f"golden {name} differs"
