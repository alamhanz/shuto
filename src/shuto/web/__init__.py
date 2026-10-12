"""Local web app (FastAPI + Jinja2 + htmx)."""

from .app import create_app, serve

__all__ = ["create_app", "serve"]
