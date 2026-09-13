"""HTML page routes (server-rendered with Jinja2)."""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from .. import config

router = APIRouter()
templates = Jinja2Templates(directory=str(config.TEMPLATES_DIR))


@router.get("/")
def index(request: Request):
    return templates.TemplateResponse(request, "login.html")


@router.get("/dashboard")
def dashboard(request: Request):
    return templates.TemplateResponse(request, "dashboard.html")


@router.get("/admin")
def admin(request: Request):
    return templates.TemplateResponse(request, "admin.html")


@router.get("/toolgroup")
def toolgroup(request: Request):
    if not request.query_params.get("id"):
        return RedirectResponse("/dashboard")
    return templates.TemplateResponse(request, "toolgroup.html")
