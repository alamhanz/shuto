"""The web route table: methods, paths, order, and the templates the routes render."""

from __future__ import annotations

import re
from pathlib import Path

from . import check_json


def _seam():
    from shuto.web import app as web_app

    return web_app.create_app, Path(web_app.__file__).parent


def _routes(cfg) -> list[dict]:
    create_app, _ = _seam()
    app = create_app(cfg)
    out = []
    for route in app.routes:
        methods = sorted(getattr(route, "methods", None) or [])
        if "HEAD" in methods and "GET" in methods:
            methods.remove("HEAD")
        out.append({"methods": methods, "path": route.path})
    return out


def test_route_table_golden(cfg):
    check_json("routes.json", _routes(cfg))


def test_generic_batch_action_route_comes_after_the_specific_ones(cfg):
    paths = [(tuple(r["methods"]), r["path"]) for r in _routes(cfg)]
    generic = paths.index((("POST",), "/batch/{batch_id}/{action}"))
    for i, (methods, path) in enumerate(paths):
        if methods == ("POST",) and path.startswith("/batch/{batch_id}/") and i != generic:
            if path.count("/") == 3:
                assert i < generic, path
    assert (("POST",), "/batch/{batch_id}/speed/{choice}") in paths


def test_updater_polled_urls_exist(cfg):
    """An open 0.5.0 page keeps polling and posting these after the in-app update reloads it."""
    have = {r["path"] for r in _routes(cfg)}
    for path in (
        "/healthz",
        "/updates/banner",
        "/updates/panel",
        "/jobs/{job_id}",
        "/jobs/{job_id}/cancel",
        "/downloads/bar",
        "/model/banner",
        "/batch/{batch_id}/speed/{choice}",
    ):
        assert path in have, path


def test_every_rendered_template_exists():
    _, web_dir = _seam()
    templates = web_dir / "templates"
    names = set()
    for py in web_dir.rglob("*.py"):
        text = py.read_text(encoding="utf-8")
        names.update(re.findall(r'render\(\s*\w+,\s*"([\w/.-]+\.html)"', text))
        names.update(re.findall(r'TemplateResponse\(\s*\w+,\s*"([\w/.-]+\.html)"', text))
    assert names, "no template names found"
    for name in sorted(names):
        assert (templates / name).is_file(), name
