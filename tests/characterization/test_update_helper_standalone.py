"""The update helper is copied alone into a temp folder and run with `python -I`, so it may
import only the standard library and must start without the shuto package."""

from __future__ import annotations

import ast
import shutil
import subprocess
import sys
from pathlib import Path


def _seam() -> Path:
    from shuto import updates

    return updates.HELPER_SCRIPT


def test_helper_file_exists_next_to_the_updater():
    helper = _seam()
    assert helper.is_file()
    assert helper.name in ("_update_helper.py", "_helper.py")


def test_helper_imports_only_the_standard_library():
    tree = ast.parse(_seam().read_text(encoding="utf-8"))
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "the helper must not use relative imports"
            found.add((node.module or "").split(".")[0])
    assert found
    assert found <= set(sys.stdlib_module_names), sorted(found - set(sys.stdlib_module_names))


def test_helper_copy_runs_alone_with_isolated_python(tmp_path):
    helper = _seam()
    folder = tmp_path / "shuto-update-test"
    folder.mkdir()
    copy = folder / helper.name
    shutil.copyfile(helper, copy)
    run = subprocess.run(
        [sys.executable, "-I", str(copy), "--help"],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=folder,
    )
    assert run.returncode == 0, run.stderr
    assert "usage" in run.stdout.lower()
    assert Path(copy).is_file()
