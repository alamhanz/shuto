"""Local web app: FastAPI + Jinja2 + htmx. Calls only the service layer (pipeline, jobs).

Security model: loopback bind by default, Host header allow-list (DNS rebinding), Origin check on
every state-changing request, a per-process CSRF token, autoescaped templates, files served by
photo id only (paths come from the database), no secret values ever rendered.

Routes live in the `routes` package (one router per page group); shared state in `state`.
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles

from shuto.core.config import Config
from shuto.web.routes import (
    accounts,
    batch,
    files,
    folders,
    help,
    home,
    imports,
    jobs,
    legacy,
    models,
    photo,
    preferences,
    review,
    settings,
    updates,
    upload,
    wizard,
)
from shuto.web.state import HERE, LOOPBACK_HOSTS, WebCtx, WebState, hostname
from shuto.web.ux import BUSY_WHAT, friendly_error

# Order is part of the route table: the legacy `/batch/{id}/{action}` catch-all must come after
# every specific `/batch/{id}/x` POST.
ROUTERS = (
    home,
    wizard,
    imports,
    jobs,
    folders,
    updates,
    settings,
    models,
    accounts,
    preferences,
    help,
    batch,
    review,
    upload,
    legacy,
    files,
    photo,
)
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
CSP = (
    "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
    "object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
)


def create_app(
    cfg: Config,
    *,
    config_path: str | None = None,
    allowed_hosts: set[str] | None = None,
) -> FastAPI:
    state = WebState(cfg, config_path, allowed_hosts or set())

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        state.runner.shutdown()

    app = FastAPI(title="shuto", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.web = state
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")

    @app.middleware("http")
    async def guard(request: Request, call_next):
        host = request.headers.get("host", "")
        if hostname(host) not in state.allowed_hosts:
            return PlainTextResponse("Bad Host header.", status_code=400)
        if request.method not in SAFE_METHODS:
            origin = request.headers.get("origin")
            if origin is not None and urlsplit(origin).netloc.lower() != host.strip().lower():
                return PlainTextResponse("Cross-origin request refused.", status_code=403)
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = CSP
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        # Not "no-referrer": with it, browsers send `Origin: null` on plain form posts (issue #7).
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(sqlite3.OperationalError)
    async def database_busy(request: Request, exc: sqlite3.OperationalError):
        """A lock that outlasted the 30 s wait is a plain message, not a crash page."""
        found = friendly_error(exc)
        if found["what"] != BUSY_WHAT:
            raise exc
        return PlainTextResponse(f"{found['what']} {found['todo']}", status_code=503)

    app.state.ctx = WebCtx(state)
    for module in ROUTERS:
        # Added one by one (not `include_router`) so `app.routes` stays one flat, ordered table.
        for route in module.router.routes:
            app.router.routes.append(route)
    return app


def request_shutdown(app: FastAPI) -> None:
    """Ask the running server to stop cleanly (a no-op without a server, as in tests)."""
    server = getattr(app.state, "server", None)
    if server is not None:
        server.should_exit = True


def serve(
    cfg: Config,
    *,
    host: str,
    port: int,
    open_browser: bool = True,
    config_path: str | None = None,
    launch_args: list[str] | None = None,
) -> None:
    """Run the web UI until interrupted. Bind to loopback unless the user asks otherwise."""
    import uvicorn

    app = create_app(
        cfg,
        config_path=config_path,
        allowed_hosts={host} if host not in ("0.0.0.0", "::") else set(),
    )
    url = f"http://{host if ':' not in host else '[' + host + ']'}:{port}"
    if host not in LOOPBACK_HOSTS:
        print(f"WARNING: listening on {host}. Anyone on your network can reach shuto.")
    print(f"shuto web UI: {url}  (press Ctrl+C to stop)")
    if open_browser:
        import webbrowser

        threading.Timer(1.0, webbrowser.open, args=(url,)).start()
    server = uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level="warning"))
    app.state.server = server
    app.state.launch_args = launch_args
    server.run()
