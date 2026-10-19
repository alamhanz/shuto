"""The folder browser used by the Add photos page."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from shuto.web import browse
from shuto.web.state import Ctx

router = APIRouter()


@router.get("/browse", response_class=HTMLResponse)
def browse_folder(request: Request, ctx: Ctx, path: str = ""):
    try:
        listing = browse.list_dir(path, ctx.cfg.import_.extensions)
        problem = None
    except browse.BrowseError as exc:
        listing, problem = None, str(exc)
    return ctx.render(
        request,
        "_browse.html",
        listing=listing,
        problem=problem,
        roots=browse.roots(),
    )
