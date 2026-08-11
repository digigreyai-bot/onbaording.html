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


def display_filename(stored: str) -> str:
    """Strip field prefix from stored upload names for UI."""
    name = Path(stored or "").name
    for field in FILE_FIELDS:
        prefix = f"{field}__"
        if name.startswith(prefix):
            return name[len(prefix) :]
    return name


def compute_pending(
    products: list,
    company: str,
    phone: str,
    city: str,
    country: str,
    payload: dict,
    files: dict,
    extra_notes: Optional[List[str]] = None,
) -> List[str]:
    """Checklist of missing required items for selected products."""
    pending: List[str] = []
    if not (company or "").strip():
        pending.append("Company name")
    if not (phone or "").strip():
        pending.append("Phone number")
    if not (city or "").strip():
        pending.append("City")
    if not (country or "").strip():
        pending.append("Country")

    payload = payload or {}
    files = files or {}

    if "avatar-studio" in products:
        if not files.get("avatar__video"):
            pending.append("Avatar video")
        if not files.get("avatar__voice_sample"):
            pending.append("Voice sample")

    if "market-pulse" in products:
        if not str(payload.get("marketpulse__email") or "").strip():
            pending.append("Market Pulse sender email")
        if not str(payload.get("marketpulse__app_password") or "").strip():
            pending.append("Market Pulse app password")

    if "smart-grid" in products:
        if not files.get("smartgrid__logo") and not str(payload.get("smartgrid__tagline") or "").strip():
            # soft: logo OR tagline/sources optional — only flag empty sources+logo as mild?
            pass  # Smart Grid has no hard required beyond basics

    if "lead-flow-call" in products:
        has_projects = bool(str(payload.get("leadflow__projects") or "").strip())
        has_file = bool(files.get("leadflow__projects_file"))
        if not has_projects and not has_file:
            pending.append("LeadFlow projects")

    if "call-pilot" in products:
        has_projects = bool(str(payload.get("callpilot__projects") or "").strip())
        has_file = bool(files.get("callpilot__projects_file"))
        if not has_projects and not has_file:
            pending.append("Call Pilot projects")

    if extra_notes:
        for note in extra_notes:
            if note and note not in pending:
                pending.append(note)
    return pending


def form_context(
    request: Request,
    token: str,
    invite: dict,
    *,
    error: Optional[str] = None,
    pending: Optional[List[str]] = None,
    submission: Optional[dict] = None,
) -> dict:
    products = invite.get("products") or []
    submission = submission or db.get_submission_by_invite(invite["id"])
    files = (submission or {}).get("files") or {}
    payload = (submission or {}).get("payload") or {}
    if pending is None:
        pending = list((submission or {}).get("pending") or [])
    uploaded = {k: display_filename(v) for k, v in files.items() if v}
    return {
        "request": request,
        "token": token,
        "products": products,
        "product_labels": PRODUCT_LABELS,
        **client_connect_context(invite),
        "error": error,
        "pending": pending,
        "submission": submission,
        "payload": payload,
        "uploaded_files": uploaded,
        "company": (submission or {}).get("company") or "",
        "phone": (submission or {}).get("phone") or "",
        "city": (submission or {}).get("city") or "",
        "country": (submission or {}).get("country") or "",
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
    # Only fully submitted invites are locked; partial can resume.
    if invite["status"] == "submitted":
        return templates.TemplateResponse("already_submitted.html", {"request": request})
    return templates.TemplateResponse(
        "client_form.html",
        form_context(request, token, invite),
    )


@app.post("/o/{token}", response_class=HTMLResponse)
async def client_form_post(request: Request, token: str):
    invite = db.get_invite_by_token(token)
    if not invite:
        return HTMLResponse("<h1>Invalid or expired link</h1>", status_code=404)
    if invite["status"] == "submitted":
        return templates.TemplateResponse("already_submitted.html", {"request": request})

    products = invite.get("products") or []
    prior = db.get_submission_by_invite(invite["id"]) or {}
    prior_files = dict(prior.get("files") or {})
    prior_payload = dict(prior.get("payload") or {})
    extra_notes: List[str] = []

    try:
        form = await request.form()
    except Exception:
        # Multipart parse failure (often oversized upload via proxy)
        return templates.TemplateResponse(
            "client_form.html",
            form_context(
                request,
                token,
                invite,
                error="Upload failed (file may be too large). Try a smaller file, then submit again — progress is kept.",
                pending=list(prior.get("pending") or [])
                + (["Avatar video (upload failed — try smaller file)"] if "avatar-studio" in products else []),
                submission=prior or None,
            ),
            status_code=413,
        )

    company = str(form.get("common__company") or "").strip()
    phone = str(form.get("common__phone") or "").strip()
    city = str(form.get("common__city") or "").strip()
    country = str(form.get("common__country") or "").strip()

    def render_form(msg: str, pending: Optional[List[str]] = None, status: int = 400):
        return templates.TemplateResponse(
            "client_form.html",
            form_context(
                request,
                token,
                invite,
                error=msg,
                pending=pending,
                submission={
                    **(prior or {}),
                    "company": company or (prior or {}).get("company") or "",
                    "phone": phone or (prior or {}).get("phone") or "",
                    "city": city or (prior or {}).get("city") or "",
                    "country": country or (prior or {}).get("country") or "",
                    "payload": prior_payload,
                    "files": prior_files,
                },
            ),
            status_code=status,
        )

    # Soft-require at least company OR phone so empty spam is avoided.
    if not company and not phone:
        return render_form("Please enter at least a company name or phone number before saving.")

    payload = dict(prior_payload)
    for key in form.keys():
        if key in FILE_FIELDS or key.startswith("common__"):
            continue
        if any(key.startswith(p) for p in TEXT_PREFIXES):
            val = form.get(key)
            if isinstance(val, UploadFile):
                continue
            # Keep previous secret if user left password blank on resume
            new_val = str(val or "")
            if key == "marketpulse__app_password" and not new_val.strip():
                continue
            payload[key] = new_val

    upload_root = db.upload_dir_for_invite(invite["id"])
    saved_files: dict = {}
    for field in FILE_FIELDS:
        item = form.get(field)
        if not isinstance(item, UploadFile) or not item.filename:
            continue
        try:
            fname = f"{field}__{safe_filename(item.filename)}"
            dest = upload_root / fname
            content = await item.read()
            if not content:
                continue
            dest.write_bytes(content)
            saved_files[field] = fname
        except Exception:
            if field == "avatar__video":
                extra_notes.append("Avatar video (upload failed — try smaller file)")
            elif field == "avatar__voice_sample":
                extra_notes.append("Voice sample (upload failed — try again)")
            else:
                extra_notes.append(f"{field} (upload failed)")

    merged_files = {**prior_files, **saved_files}
    pending = compute_pending(
        products, company, phone, city, country, payload, merged_files, extra_notes
    )

    submission = db.save_submission(
        invite["id"],
        company,
        phone,
        city,
        country,
        payload,
        saved_files,
        pending=pending,
    )

    return templates.TemplateResponse(
        "thank_you.html",
        {
            "request": request,
            "pending": pending,
            "is_partial": bool(pending),
            "token": token,
            "form_url": client_form_url(request, token),
        },
    )