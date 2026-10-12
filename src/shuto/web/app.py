"""Local web app: FastAPI + Jinja2 + htmx. Calls only the service layer (pipeline, jobs).

Security model: loopback bind by default, Host header allow-list (DNS rebinding), Origin check on
every state-changing request, a per-process CSRF token, autoescaped templates, files served by
photo id only (paths come from the database), no secret values ever rendered.

Routes live in `routes_setup` and `routes_batch`; shared helpers in `common`.
"""

from __future__ import annotations

import threading
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles

from ..config import Config
from .common import HERE, LOOPBACK_HOSTS, WebCtx, WebState, hostname
from .routes_batch import register_batch_routes
from .routes_setup import register_setup_routes

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
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        return response

    ctx = WebCtx(state)
    register_setup_routes(app, ctx)
    register_batch_routes(app, ctx)
    return app


def serve(
    cfg: Config,
    *,
    host: str,
    port: int,
    open_browser: bool = True,
    config_path: str | None = None,
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
    uvicorn.run(app, host=host, port=port, log_level="warning")
