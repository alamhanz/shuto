"""The help page."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from shuto.web.state import Ctx

router = APIRouter()


@router.get("/help", response_class=HTMLResponse)
def help_page(request: Request, ctx: Ctx):
    return ctx.render(request, "help.html")
