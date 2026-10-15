"""The package layers hold: a package imports only from its own row or rows below it.

Rows, top to bottom (see CONTRIBUTING.md "Code layout"):

    cli  web                       entry points
    workflow                       orchestration
    update  system                 self-update, OS integration
    settings                       saved UI settings
    ai  ollama  deliver            domain services
    ingest  places  metadata       pure domain
    store                          SQLite persistence
    core                           models, config, validation, small helpers

Imports under `if TYPE_CHECKING:` are ignored. Imports inside functions count.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "shuto"

ROWS = [
    ("core",),
    ("store",),
    ("ingest", "places", "metadata"),
    ("ai", "ollama", "deliver"),
    ("settings",),
    ("update", "system"),
    ("workflow",),
    ("cli", "web"),
]
RANK = {pkg: i for i, row in enumerate(ROWS) for pkg in row}

# Imports between packages of the same row. Everything else in a row is forbidden. There is no
# other allow-list: no import in src/shuto goes up a layer.
SAME_ROW_OK = {
    ("places", "ingest"),
    ("places", "metadata"),
    ("ollama", "ai"),  # only ai.hardware, see ONLY_MODULES
    ("cli", "web"),  # only the `ui` command starts the web app
}
ONLY_MODULES = {("ollama", "ai"): {"shuto.ai.hardware"}, ("cli", "web"): {"shuto.web"}}

# The domain rows (pure domain and domain services) never reach up into the app: not the
# store, the saved settings, the workflow or the web app. Rows that sit above them are fine.
NO_STORE = {"ingest", "places", "metadata", "ai", "ollama", "deliver"}

# `cli` starts the web app from `shuto ui`; nothing else outside `web` may import it.
WEB_IMPORTERS = {"shuto.web", "shuto.cli"}


def _is_module(name: str) -> bool:
    p = SRC.parent.joinpath(*name.split("."))
    return p.with_suffix(".py").is_file() or p.joinpath("__init__.py").is_file()


def _module_name(path: Path) -> str:
    rel = path.relative_to(SRC.parent).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _package(module: str) -> str:
    parts = module.split(".")
    return parts[1] if len(parts) > 1 else ""


def _is_type_checking(test: ast.expr) -> bool:
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
        isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
    )


def _imports(path: Path) -> list[tuple[str, int]]:
    """(absolute module name, line) for every import outside `if TYPE_CHECKING:`."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    me = _module_name(path)
    is_pkg = path.name == "__init__.py"
    found: list[tuple[str, int]] = []

    def visit(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.If) and _is_type_checking(child.test):
                for other in child.orelse:
                    visit_one(other)
                continue
            visit_one(child)

    def visit_one(node: ast.AST) -> None:
        if isinstance(node, ast.Import):
            found.extend((a.name, node.lineno) for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = me.split(".") if is_pkg else me.split(".")[:-1]
                base = base[: len(base) - (node.level - 1)]
                target = ".".join(base + ([node.module] if node.module else []))
            else:
                target = node.module or ""
            # `from shuto.ai import hardware` imports the module shuto.ai.hardware
            names = [f"{target}.{a.name}" for a in node.names]
            submodules = [n for n in names if _is_module(n)]
            if len(submodules) < len(names):
                found.append((target, node.lineno))
            found.extend((n, node.lineno) for n in submodules)
        visit(node)

    visit(tree)
    return found


def _sources() -> list[Path]:
    return sorted(p for p in SRC.rglob("*.py") if "__pycache__" not in p.parts)


def _shuto_imports(path: Path) -> list[tuple[str, int]]:
    out = []
    for name, line in _imports(path):
        if name == "shuto" or name.startswith("shuto."):
            out.append((name, line))
    return out


def _resolve(name: str) -> str:
    """The module a dotted name stands for (drops a trailing attribute like `shuto.ai.hardware.x`
    or an imported name that is not a module)."""
    parts = name.split(".")
    while len(parts) > 1:
        if _is_module(".".join(parts)):
            return ".".join(parts)
        parts.pop()
    return "shuto"


def test_every_package_has_a_row():
    packages = {
        p.name
        for p in SRC.iterdir()
        if p.is_dir() and p.name not in {"data", "__pycache__"} and (p / "__init__.py").is_file()
    }
    assert packages == set(RANK)


def test_no_flat_modules_are_left():
    flat = {p.name for p in SRC.glob("*.py")}
    assert flat == {"__init__.py", "__main__.py"}


def test_imports_follow_the_layers():
    problems = []
    for path in _sources():
        me = _module_name(path)
        mine = _package(me)
        if mine not in RANK:
            continue
        for name, line in _shuto_imports(path):
            target = _resolve(name)
            theirs = _package(target)
            if theirs not in RANK or theirs == mine:
                continue
            if RANK[theirs] > RANK[mine]:
                problems.append(f"{me}:{line} imports {target} (a higher layer)")
            elif RANK[theirs] == RANK[mine]:
                if (mine, theirs) not in SAME_ROW_OK:
                    problems.append(f"{me}:{line} imports {target} (same row)")
                elif ONLY_MODULES.get((mine, theirs)) and not any(
                    target == m or target.startswith(m + ".") for m in ONLY_MODULES[(mine, theirs)]
                ):
                    problems.append(
                        f"{me}:{line} imports {target} (only {ONLY_MODULES[mine, theirs]})"
                    )
            elif mine in NO_STORE and theirs in {"store", "settings", "workflow", "web"}:
                problems.append(f"{me}:{line} imports {target} (domain code stays off the app)")
    assert not problems, "\n".join(problems)


def test_only_web_and_the_ui_command_import_the_web_app():
    problems = []
    for path in _sources():
        me = _module_name(path)
        if me == "shuto.web" or me.startswith("shuto.web."):
            continue
        for name, line in _shuto_imports(path):
            if _resolve(name) == "shuto.web" or _resolve(name).startswith("shuto.web."):
                if not any(me == w or me.startswith(w + ".") for w in WEB_IMPORTERS):
                    problems.append(f"{me}:{line} imports {name}")
    assert not problems, "\n".join(problems)


def test_the_update_helper_is_stdlib_only():
    path = SRC / "update" / "_helper.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    import sys

    stdlib = set(sys.stdlib_module_names)
    names = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names += [a.name.split(".")[0] for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "the helper is copied alone: no relative imports"
            names.append((node.module or "").split(".")[0])
    assert set(names) <= stdlib, set(names) - stdlib
