"""Digigrey Client Onboarding — admin dashboard + client forms."""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import List, Optional
from urllib.parse import urljoin

from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import auth
import db

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
PRODUCT_OPTIONS = [
    ("avatar-studio", "Avatar Studio"),
    ("lead-flow-call", "LeadFlow"),
    ("market-pulse", "Market Pulse"),
    ("call-pilot", "Call Pilot"),
    ("smart-grid", "Smart Grid"),
]
PRODUCT_LABELS = dict(PRODUCT_OPTIONS)
FILE_FIELDS = {
    "avatar__video",
    "avatar__voice_sample",
    "smartgrid__logo",
    "leadflow__projects_file",
    "callpilot__projects_file",
    "marketpulse__logo",
    "marketpulse__leads",
}
TEXT_PREFIXES = (
    "smartgrid__",
    "leadflow__",
    "callpilot__",
    "marketpulse__",
)

app = FastAPI(title="Digigrey Onboarding")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
static_dir = BASE_DIR / "static"
static_dir.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


@app.on_event("startup")
def _startup() -> None:
    db.init_db()


def app_base_url(request: Request) -> str:
    configured = (os.getenv("APP_BASE_URL") or "").rstrip("/")
    if configured:
        return configured
    return str(request.base_url).rstrip("/")


def client_form_url(request: Request, token: str) -> str:
    return urljoin(app_base_url(request) + "/", f"o/{token}")


def admin_redirect_login() -> RedirectResponse:
    return RedirectResponse("/admin/login", status_code=303)


def safe_filename(name: str) -> str:
    base = Path(name or "file").name
    base = re.sub(r"[^\w.\-]+", "_", base).strip("._") or "file"
    return base[:180]


def client_connect_context(invite: dict) -> dict:
    avatar = (invite.get("connect_url_avatar") or "").strip()
    vtour = (invite.get("connect_url_vtour") or "").strip()
    smart = (invite.get("connect_url_smartgrid") or "").strip()
    return {
        "connect_url_avatar": avatar,
        "connect_url_vtour": vtour,
        "connect_url_smartgrid": smart,
        "show_social": bool(avatar or vtour or smart),
    }


@app.get("/", response_class=HTMLResponse)
def home():
    return RedirectResponse("/admin", status_code=303)


@app.get("/admin/login", response_class=HTMLResponse)
def admin_login_get(request: Request):
    if auth.is_admin(request):
        return RedirectResponse("/admin", status_code=303)
    if not auth.admin_password():
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "error": "Set ADMIN_PASSWORD env var before using admin."},
        )
    return templates.TemplateResponse("login.html", {"request": request, "error": None})


@app.post("/admin/login")
async def admin_login_post(request: Request, password: str = Form(...)):
    if not auth.check_password(password):
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "error": "Incorrect password."},
            status_code=401,
        )
    resp = RedirectResponse("/admin", status_code=303)
    resp.set_cookie(
        auth.COOKIE_NAME,
        auth.make_session_cookie(),
        httponly=True,
        samesite="lax",
        max_age=60 * 60 * 24 * 14,
    )
    return resp


@app.post("/admin/logout")
def admin_logout():
    resp = RedirectResponse("/admin/login", status_code=303)
    resp.delete_cookie(auth.COOKIE_NAME)
    return resp


@app.get("/admin", response_class=HTMLResponse)
def admin_home(request: Request, notice: Optional[str] = None, error: Optional[str] = None):
    if not auth.is_admin(request):
        return admin_redirect_login()
    return templates.TemplateResponse(
        "admin_home.html",
        {
            "request": request,
            "invites": db.list_invites(),
            "product_options": PRODUCT_OPTIONS,
            "notice": notice,
            "error": error,
        },
    )


@app.post("/admin/invites")
async def admin_create_invite(
    request: Request,
    label: str = Form(...),
    connect_url_avatar: str = Form(""),
    connect_url_vtour: str = Form(""),
    connect_url_smartgrid: str = Form(""),
    products: List[str] = Form([]),
):
    if not auth.is_admin(request):
        return admin_redirect_login()

    label = (label or "").strip()
    products = [p for p in products if p in PRODUCT_LABELS]
    avatar = (connect_url_avatar or "").strip()
    vtour = (connect_url_vtour or "").strip()
    smart = (connect_url_smartgrid or "").strip()

    if not label:
        return admin_home(request, error="Client label is required.")
    if not products:
        return admin_home(request, error="Select at least one product.")
    if "avatar-studio" in products and not avatar:
        return admin_home(request, error="Avatar Studio connect link is required when Avatar Studio is selected.")
    if "smart-grid" in products and not smart:
        return admin_home(request, error="Smart Grid connect link is required when Smart Grid is selected.")
    if "smart-grid" not in products:
        smart = ""

    invite = db.create_invite(
        label,
        products,
        connect_url_avatar=avatar or None,
        connect_url_vtour=vtour or None,
        connect_url_smartgrid=smart or None,
    )
    return RedirectResponse(f"/admin/invites/{invite['id']}", status_code=303)


@app.get("/admin/invites/{invite_id}", response_class=HTMLResponse)
def admin_invite_detail(request: Request, invite_id: int):
    if not auth.is_admin(request):
        return admin_redirect_login()
    invite = db.get_invite(invite_id)
    if not invite:
        return RedirectResponse("/admin", status_code=303)
    submission = db.get_submission_by_invite(invite_id)
    return templates.TemplateResponse(
        "invite_detail.html",
        {
            "request": request,
            "invite": invite,
            "submission": submission,
            "client_url": client_form_url(request, invite["token"]),
        },
    )


@app.get("/admin/files/{submission_id}/{field_name}")
def admin_download_file(request: Request, submission_id: int, field_name: str):
    if not auth.is_admin(request):
        return admin_redirect_login()
    submission = db.get_submission(submission_id)
    if not submission:
        return RedirectResponse("/admin", status_code=303)
    files = submission.get("files") or {}
    stored_name = files.get(field_name)
    if not stored_name:
        return RedirectResponse(f"/admin/invites/{submission['invite_id']}", status_code=303)
    path = db.upload_dir_for_invite(submission["invite_id"]) / stored_name
    if not path.is_file():
        return RedirectResponse(f"/admin/invites/{submission['invite_id']}", status_code=303)
    return FileResponse(path, filename=stored_name)


@app.get("/o/{token}", response_class=HTMLResponse)
def client_form_get(request: Request, token: str):
    invite = db.get_invite_by_token(token)
    if not invite:
        return HTMLResponse("<h1>Invalid or expired link</h1>", status_code=404)
    if invite["status"] == "submitted":
        return templates.TemplateResponse("already_submitted.html", {"request": request})
    products = invite.get("products") or []
    ctx = client_connect_context(invite)
    return templates.TemplateResponse(
        "client_form.html",
        {
            "request": request,
            "token": token,
            "products": products,
            "product_labels": PRODUCT_LABELS,
            **ctx,
            "error": None,
        },
    )


@app.post("/o/{token}", response_class=HTMLResponse)
async def client_form_post(request: Request, token: str):
    invite = db.get_invite_by_token(token)
    if not invite:
        return HTMLResponse("<h1>Invalid or expired link</h1>", status_code=404)
    if invite["status"] == "submitted":
        return templates.TemplateResponse("already_submitted.html", {"request": request})

    form = await request.form()
    products = invite.get("products") or []

    company = str(form.get("common__company") or "").strip()
    phone = str(form.get("common__phone") or "").strip()
    city = str(form.get("common__city") or "").strip()
    country = str(form.get("common__country") or "").strip()

    def render_error(msg: str):
        ctx = client_connect_context(invite)
        return templates.TemplateResponse(
            "client_form.html",
            {
                "request": request,
                "token": token,
                "products": products,
                "product_labels": PRODUCT_LABELS,
                **ctx,
                "error": msg,
            },
            status_code=400,
        )

    if not all([company, phone, city, country]):
        return render_error("Please fill all basic information fields.")

    if "market-pulse" in products:
        if not str(form.get("marketpulse__email") or "").strip():
            return render_error("Market Pulse sender email is required.")
        if not str(form.get("marketpulse__app_password") or "").strip():
            return render_error("Market Pulse app password is required.")

    if "avatar-studio" in products:
        av = form.get("avatar__video")
        vs = form.get("avatar__voice_sample")
        if not isinstance(av, UploadFile) or not av.filename:
            return render_error("Avatar video is required.")
        if not isinstance(vs, UploadFile) or not vs.filename:
            return render_error("Voice sample is required.")

    payload = {}
    for key in form.keys():
        if key in FILE_FIELDS or key.startswith("common__"):
            continue
        if any(key.startswith(p) for p in TEXT_PREFIXES):
            val = form.get(key)
            if isinstance(val, UploadFile):
                continue
            payload[key] = str(val or "")

    upload_root = db.upload_dir_for_invite(invite["id"])
    saved_files = {}
    for field in FILE_FIELDS:
        item = form.get(field)
        if not isinstance(item, UploadFile) or not item.filename:
            continue
        fname = f"{field}__{safe_filename(item.filename)}"
        dest = upload_root / fname
        content = await item.read()
        dest.write_bytes(content)
        saved_files[field] = fname

    try:
        db.save_submission(invite["id"], company, phone, city, country, payload, saved_files)
    except ValueError as e:
        return render_error(str(e))

    return templates.TemplateResponse("thank_you.html", {"request": request})
