"""AgentAssist+ client onboarding: admin dashboard + client forms."""
from __future__ import annotations

import json
import os
import re
import shutil
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin

from dotenv import load_dotenv
from fastapi import Body, FastAPI, Form, Header, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

import auth
import db

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
# Starlette defaults max_part_size to 1MB; that silently kills Mac/phone videos.
MAX_PART_SIZE = int(os.getenv("MAX_UPLOAD_PART_BYTES", str(512 * 1024 * 1024)))
ONBOARDING_HTML_API_KEY = (os.getenv("ONBOARDING_HTML_API_KEY") or "").strip()
CONTACT_URL = (
    os.getenv("CONTACT_URL") or "https://agentassistplus.com/contact"
).strip()
MAX_FILES_PER_FIELD = 3
MAX_PROJECT_FILES = MAX_FILES_PER_FIELD
PRODUCT_OPTIONS = [
    ("avatar-studio", "Avatar Studio"),
    ("lead-flow-call", "LeadFlow"),
    ("market-pulse", "Market Pulse"),
    ("call-pilot", "Call Pilot"),
    ("smart-grid", "Smart Grid"),
]
PRODUCT_LABELS = dict(PRODUCT_OPTIONS)
PACKAGE_LABELS = {
    "basic": "Basic",
    "growth": "Growth",
    "premium": "Premium",
}


def normalize_package(raw: Optional[str]) -> str:
    key = (raw or "").strip().lower()
    if key in PACKAGE_LABELS:
        return key
    # Accept display names too.
    for slug, label in PACKAGE_LABELS.items():
        if key == label.lower():
            return slug
    return ""


def file_slots(base: str) -> List[str]:
    """Canonical Contabo key first, then _2 / _3 extras."""
    return [base] + [f"{base}_{i}" for i in range(2, MAX_FILES_PER_FIELD + 1)]


FILE_FIELDS = {
    *file_slots("avatar__video"),
    *file_slots("avatar__voice_sample"),
    *file_slots("common__logo"),
    *file_slots("smartgrid__logo"),
    *file_slots("marketpulse__logo"),
    *file_slots("marketpulse__leads"),
    "leadflow__projects_file",
    "callpilot__projects_file",
    *(f"common__projects_file_{i}" for i in range(1, MAX_PROJECT_FILES + 1)),
    *(f"leadflow__projects_file_{i}" for i in range(2, MAX_PROJECT_FILES + 1)),
    *(f"callpilot__projects_file_{i}" for i in range(2, MAX_PROJECT_FILES + 1)),
}
TEXT_PREFIXES = (
    "common__",
    "avatar__",
    "smartgrid__",
    "leadflow__",
    "callpilot__",
    "marketpulse__",
)

app = FastAPI(title="AgentAssist+ Onboarding")
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


def require_api_key(x_onboarding_api_key: Optional[str] = None) -> Optional[JSONResponse]:
    expected = ONBOARDING_HTML_API_KEY
    provided = (x_onboarding_api_key or "").strip()
    if not expected:
        return JSONResponse(
            {"error": "ONBOARDING_HTML_API_KEY is not configured on this service"},
            status_code=503,
        )
    if not provided or provided != expected:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    return None


def create_invite_from_payload(
    *,
    label: str,
    products: List[str],
    connect_url_avatar: str = "",
    connect_url_vtour: str = "",
    connect_url_smartgrid: str = "",
    package: str = "",
) -> tuple[Optional[dict], Optional[str]]:
    label = (label or "").strip()
    products = [p for p in products if p in PRODUCT_LABELS]
    avatar = (connect_url_avatar or "").strip()
    vtour = (connect_url_vtour or "").strip()
    smart = (connect_url_smartgrid or "").strip()
    pkg = normalize_package(package)

    if not label:
        return None, "Client label is required."
    if not products:
        return None, "Select at least one product."
    if "avatar-studio" in products and not avatar:
        return None, "Avatar Studio connect link is required when Avatar Studio is selected."
    if "smart-grid" in products and not smart:
        # One Connect Hub link is enough; reuse avatar connect URL when smart-grid needs it.
        smart = avatar
    if "smart-grid" not in products:
        smart = ""
    if "smart-grid" in products and not smart:
        return None, "Smart Grid connect link is required when Smart Grid is selected."

    invite = db.create_invite(
        label,
        products,
        connect_url_avatar=avatar or None,
        connect_url_vtour=vtour or None,
        connect_url_smartgrid=smart or None,
        package=pkg or None,
    )
    return invite, None


def admin_redirect_login() -> RedirectResponse:
    return RedirectResponse("/admin/login", status_code=303)


def notify_contabo_submitted(invite: dict, form_url: str) -> None:
    """Tell Contabo the onboarding form is done so the portal unlocks the dashboard."""
    base = (
        os.getenv("CONTABO_API_URL")
        or os.getenv("AGENTASSIST_API_URL")
        or ""
    ).strip().rstrip("/")
    key = ONBOARDING_HTML_API_KEY
    if not base or not key:
        print("[onboarding] Contabo notify skipped: CONTABO_API_URL or API key missing")
        return
    payload = json.dumps(
        {
            # formUrl is the unique matcher; inviteId alone can collide across Contabo users.
            "formUrl": form_url,
            "inviteId": invite.get("id"),
            "label": invite.get("label"),
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        f"{base}/api/onboarding/external-complete",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "X-Onboarding-Api-Key": key,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=12) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            print(f"[onboarding] Contabo notify ok ({resp.status}): {body[:200]}")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        print(f"[onboarding] Contabo notify failed: {exc}")


def safe_filename(name: str) -> str:
    base = Path(name or "file").name
    base = re.sub(r"[^\w.\-]+", "_", base).strip("._") or "file"
    return base[:180]


async def save_upload_stream(upload: UploadFile, dest: Path) -> int:
    """Write upload to disk in chunks (avoid loading whole video into RAM)."""
    written = 0
    with dest.open("wb") as out:
        while True:
            block = await upload.read(1024 * 1024)
            if not block:
                break
            out.write(block)
            written += len(block)
    return written


def chunk_dir(invite_id: int, upload_id: str) -> Path:
    path = db.upload_dir_for_invite(invite_id) / "_chunks" / upload_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def resolve_invite_for_upload(token: str):
    invite = db.get_invite_by_token(token)
    if not invite:
        return None, JSONResponse({"ok": False, "error": "Invalid link"}, status_code=404)
    if invite["status"] == "submitted":
        return None, JSONResponse({"ok": False, "error": "Already submitted"}, status_code=400)
    return invite, None


def _with_embed_param(url: str) -> str:
    """Append embed=1 so Avatar Studio connect UI can compact itself in an iframe."""
    u = (url or "").strip()
    if not u:
        return ""
    joiner = "&" if "?" in u else "?"
    if "embed=" in u:
        return u
    return f"{u}{joiner}embed=1"


def client_connect_context(invite: dict) -> dict:
    avatar = (invite.get("connect_url_avatar") or "").strip()
    vtour = (invite.get("connect_url_vtour") or "").strip()
    smart = (invite.get("connect_url_smartgrid") or "").strip()
    # One primary link (Avatar Studio Copy Connect Link) drives the embed.
    primary = avatar or smart or vtour
    return {
        "connect_url_avatar": avatar,
        "connect_url_vtour": vtour,
        "connect_url_smartgrid": smart,
        "connect_embed_url": _with_embed_param(primary),
        "connect_primary_url": primary,
        "show_social": bool(primary),
    }


def display_filename(stored: str) -> str:
    """Strip field prefix from stored upload names for UI."""
    name = Path(stored or "").name
    for field in FILE_FIELDS:
        prefix = f"{field}__"
        if name.startswith(prefix):
            return name[len(prefix) :]
    return name


def _collect_slots(files: dict, base: str) -> List[str]:
    out = []
    for key in file_slots(base):
        val = files.get(key)
        if val:
            out.append(val)
    return out[:MAX_FILES_PER_FIELD]


def _has_any_slot(files: dict, base: str) -> bool:
    return bool(_collect_slots(files, base))


def _project_text(payload: dict) -> str:
    return (
        str(payload.get("common__projects") or "").strip()
        or str(payload.get("leadflow__projects") or "").strip()
        or str(payload.get("callpilot__projects") or "").strip()
    )


def _has_project_file(files: dict) -> bool:
    if files.get("leadflow__projects_file") or files.get("callpilot__projects_file"):
        return True
    if files.get("common__projects_file_1"):
        return True
    for i in range(1, MAX_PROJECT_FILES + 1):
        if files.get(f"leadflow__projects_file_{i}") or files.get(f"callpilot__projects_file_{i}"):
            return True
        if files.get(f"common__projects_file_{i}"):
            return True
    return False


def mirror_shared_fields(products: list, payload: dict, files: dict) -> tuple[dict, dict]:
    """Keep product-specific keys Contabo/admin already know, from shared UI fields."""
    payload = dict(payload or {})
    files = dict(files or {})
    products = products or []

    tagline = (
        str(payload.get("common__tagline") or "").strip()
        or str(payload.get("smartgrid__tagline") or "").strip()
    )
    if tagline:
        payload["common__tagline"] = tagline
        if "smart-grid" in products:
            payload["smartgrid__tagline"] = tagline

    logo_slots = []
    for key in [*file_slots("common__logo"), *file_slots("smartgrid__logo"), *file_slots("marketpulse__logo")]:
        val = files.get(key)
        if val and val not in logo_slots:
            logo_slots.append(val)
    logo_slots = logo_slots[:MAX_FILES_PER_FIELD]
    for idx, stored in enumerate(logo_slots):
        common_key = "common__logo" if idx == 0 else f"common__logo_{idx + 1}"
        files[common_key] = stored
        if "smart-grid" in products:
            sg_key = "smartgrid__logo" if idx == 0 else f"smartgrid__logo_{idx + 1}"
            files[sg_key] = stored
        if "market-pulse" in products:
            mp_key = "marketpulse__logo" if idx == 0 else f"marketpulse__logo_{idx + 1}"
            files[mp_key] = stored

    projects = _project_text(payload)
    if projects:
        payload["common__projects"] = projects
        if "lead-flow-call" in products:
            payload["leadflow__projects"] = projects
        if "call-pilot" in products:
            payload["callpilot__projects"] = projects

    project_slots = []
    for i in range(1, MAX_PROJECT_FILES + 1):
        key = f"common__projects_file_{i}"
        legacy = "leadflow__projects_file" if i == 1 else f"leadflow__projects_file_{i}"
        legacy_cp = "callpilot__projects_file" if i == 1 else f"callpilot__projects_file_{i}"
        stored = files.get(key) or files.get(legacy) or files.get(legacy_cp)
        if stored:
            project_slots.append(stored)
    for idx, stored in enumerate(project_slots[:MAX_PROJECT_FILES], start=1):
        files[f"common__projects_file_{idx}"] = stored
        if "lead-flow-call" in products:
            dest = "leadflow__projects_file" if idx == 1 else f"leadflow__projects_file_{idx}"
            files[dest] = stored
        if "call-pilot" in products:
            dest = "callpilot__projects_file" if idx == 1 else f"callpilot__projects_file_{idx}"
            files[dest] = stored

    return payload, files


def split_phone(phone: str, payload: Optional[dict] = None) -> tuple[str, str]:
    """Return (dial_code, national_number) for UI prefill."""
    payload = payload or {}
    dial = str(payload.get("common__phone_dial") or "").strip()
    national = str(payload.get("common__phone_national") or "").strip()
    if dial or national:
        return dial or "+1", national
    raw = (phone or "").strip()
    if not raw:
        return "+1", ""
    m = re.match(r"^\+(\d{1,4})\s*(.*)$", raw)
    if m:
        return f"+{m.group(1)}", m.group(2).strip()
    return "+1", raw


def compose_phone(dial: str, national: str) -> str:
    dial = (dial or "").strip() or "+1"
    national = (national or "").strip()
    if not national:
        return ""
    if not dial.startswith("+"):
        dial = f"+{dial}"
    return f"{dial} {national}".strip()


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
    payload, files = mirror_shared_fields(products, payload, files)

    if "avatar-studio" in products:
        if not _has_any_slot(files, "avatar__video"):
            pending.append("Avatar video")
        if not _has_any_slot(files, "avatar__voice_sample"):
            pending.append("Voice sample")

    # Smart Grid content sources are optional (no required asterisk on the form).

    if "smart-grid" in products or "market-pulse" in products:
        if not _has_any_slot(files, "common__logo") and not _has_any_slot(files, "smartgrid__logo") and not _has_any_slot(files, "marketpulse__logo"):
            pending.append("Business logo")
    primary_color = (
        str(payload.get("common__brand_color") or "").strip()
        or str(payload.get("common__brand_color_1") or "").strip()
    )
    if not primary_color:
        pending.append("Brand color")
    if not str(payload.get("common__website") or "").strip():
        pending.append("Website")

    if "market-pulse" in products:
        if not str(payload.get("marketpulse__email") or "").strip():
            pending.append("Market Pulse sender email")
        if not str(payload.get("marketpulse__app_password") or "").strip():
            pending.append("Market Pulse app password")

    needs_units = "lead-flow-call" in products or "call-pilot" in products
    if needs_units:
        if not _project_text(payload) and not _has_project_file(files):
            pending.append("Units for sale")

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
    files = dict((submission or {}).get("files") or {})
    payload = dict((submission or {}).get("payload") or {})
    payload, files = mirror_shared_fields(products, payload, files)
    if pending is None:
        pending = list((submission or {}).get("pending") or [])
    uploaded = {k: display_filename(v) for k, v in files.items() if v}
    phone_raw = (submission or {}).get("phone") or ""
    dial, national = split_phone(phone_raw, payload)
    client_name = (invite.get("label") or "").strip()
    package_slug = normalize_package(invite.get("package") or "")
    package_label = PACKAGE_LABELS.get(package_slug, "")
    show_logo = "smart-grid" in products or "market-pulse" in products
    show_projects = "lead-flow-call" in products or "call-pilot" in products
    show_brand = True
    project_uploads = []
    for i in range(1, MAX_PROJECT_FILES + 1):
        key = f"common__projects_file_{i}"
        if uploaded.get(key):
            project_uploads.append({"field": key, "name": uploaded[key]})

    def slot_uploads(base: str) -> List[dict]:
        rows = []
        for key in file_slots(base):
            if uploaded.get(key):
                rows.append({"field": key, "name": uploaded[key]})
        return rows

    logo_uploads = slot_uploads("common__logo")
    if not logo_uploads:
        logo_uploads = slot_uploads("smartgrid__logo") or slot_uploads("marketpulse__logo")
    return {
        "request": request,
        "token": token,
        "products": products,
        "product_labels": PRODUCT_LABELS,
        "package_slug": package_slug,
        "package_label": package_label,
        **client_connect_context(invite),
        "error": error,
        "pending": pending,
        "submission": submission,
        "payload": payload,
        "uploaded_files": uploaded,
        "company": (submission or {}).get("company") or "",
        "phone": phone_raw,
        "phone_dial": dial,
        "phone_national": national,
        "city": (submission or {}).get("city") or "",
        "country": (submission or {}).get("country") or "",
        "client_name": client_name,
        "contact_url": CONTACT_URL,
        "show_shared_logo": show_logo,
        "show_shared_projects": show_projects,
        "show_brand_assets": show_brand,
        "logo_uploads": logo_uploads,
        "avatar_video_uploads": slot_uploads("avatar__video"),
        "avatar_voice_uploads": slot_uploads("avatar__voice_sample"),
        "leads_uploads": slot_uploads("marketpulse__leads"),
        "project_uploads": project_uploads,
        "max_files_per_field": MAX_FILES_PER_FIELD,
        "max_project_files": MAX_PROJECT_FILES,
        "invite_status": invite.get("status") or "pending",
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
    package: str = Form(""),
    products: List[str] = Form([]),
):
    if not auth.is_admin(request):
        return admin_redirect_login()

    invite, error = create_invite_from_payload(
        label=label,
        products=products,
        connect_url_avatar=connect_url_avatar,
        connect_url_vtour=connect_url_vtour,
        connect_url_smartgrid=connect_url_smartgrid,
        package=package,
    )
    if error:
        return admin_home(request, error=error)
    return RedirectResponse(f"/admin/invites/{invite['id']}", status_code=303)


class InviteCreateBody(BaseModel):
    label: str = Field(..., min_length=1)
    products: List[str] = Field(default_factory=list)
    connect_url_avatar: str = ""
    connect_url_vtour: str = ""
    connect_url_smartgrid: str = ""
    package: str = ""


@app.post("/api/invites")
async def api_create_invite(
    request: Request,
    payload: InviteCreateBody = Body(...),
    x_onboarding_api_key: Optional[str] = Header(default=None, alias="X-Onboarding-Api-Key"),
):
    """Machine endpoint for Contabo / automations to mint a client form URL."""
    auth_error = require_api_key(x_onboarding_api_key)
    if auth_error:
        return auth_error

    products = payload.products or []
    if isinstance(products, str):
        products = [products]

    invite, error = create_invite_from_payload(
        label=str(payload.label or ""),
        products=list(products),
        connect_url_avatar=str(payload.connect_url_avatar or ""),
        connect_url_vtour=str(payload.connect_url_vtour or ""),
        connect_url_smartgrid=str(payload.connect_url_smartgrid or ""),
        package=str(payload.package or ""),
    )
    if error:
        return JSONResponse({"error": error}, status_code=400)

    token = invite["token"]
    return {
        "inviteId": invite["id"],
        "token": token,
        "formUrl": client_form_url(request, token),
        "label": invite.get("label"),
        "products": invite.get("products") or [],
        "package": invite.get("package"),
    }


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


@app.get("/api/invites/{invite_id}")
async def api_get_invite(
    invite_id: int,
    x_onboarding_api_key: Optional[str] = Header(default=None, alias="X-Onboarding-Api-Key"),
):
    """Machine endpoint: Contabo polls Digigrey submit status for dashboard unlock."""
    auth_error = require_api_key(x_onboarding_api_key)
    if auth_error:
        return auth_error
    invite = db.get_invite(invite_id)
    if not invite:
        return JSONResponse({"error": "Invite not found"}, status_code=404)
    return {
        "inviteId": invite["id"],
        "status": invite.get("status"),
        "label": invite.get("label"),
        "token": invite.get("token"),
    }


@app.get("/api/invites/by-token/{token}")
async def api_get_invite_by_token(
    token: str,
    x_onboarding_api_key: Optional[str] = Header(default=None, alias="X-Onboarding-Api-Key"),
):
    auth_error = require_api_key(x_onboarding_api_key)
    if auth_error:
        return auth_error
    invite = db.get_invite_by_token(token)
    if not invite:
        return JSONResponse({"error": "Invite not found"}, status_code=404)
    return {
        "inviteId": invite["id"],
        "status": invite.get("status"),
        "label": invite.get("label"),
        "token": invite.get("token"),
    }


SENSITIVE_PAYLOAD_KEYS = (
    "app_password",
    "password",
    "secret",
    "token",
    "api_key",
    "apikey",
)


def _mask_payload(payload: dict) -> dict:
    out = {}
    for key, value in (payload or {}).items():
        key_l = str(key).lower()
        if any(s in key_l for s in SENSITIVE_PAYLOAD_KEYS):
            out[key] = "••••••••" if value else ""
        else:
            out[key] = value
    return out


def _submission_public_payload(invite: dict, submission: Optional[dict]) -> dict:
    products = invite.get("products") or []
    product_labels = [PRODUCT_LABELS.get(p, p) for p in products]
    submitted_at = invite.get("submitted_at") or (submission or {}).get("updated_at") or (submission or {}).get("created_at")
    files = (submission or {}).get("files") or {}
    return {
        "inviteId": invite.get("id"),
        "status": invite.get("status"),
        "label": invite.get("label"),
        "products": products,
        "productLabels": product_labels,
        "submittedAt": submitted_at,
        "createdAt": (submission or {}).get("created_at"),
        "updatedAt": (submission or {}).get("updated_at"),
        "company": (submission or {}).get("company") or "",
        "phone": (submission or {}).get("phone") or "",
        "city": (submission or {}).get("city") or "",
        "country": (submission or {}).get("country") or "",
        "payload": _mask_payload((submission or {}).get("payload") or {}),
        "files": {k: v for k, v in files.items() if v},
        "pending": (submission or {}).get("pending") or [],
    }


@app.get("/api/invites/by-token/{token}/submission")
async def api_get_submission_by_token(
    token: str,
    x_onboarding_api_key: Optional[str] = Header(default=None, alias="X-Onboarding-Api-Key"),
):
    """Machine endpoint: Contabo reads a client's submitted Digigrey form (read-only)."""
    auth_error = require_api_key(x_onboarding_api_key)
    if auth_error:
        return auth_error
    invite = db.get_invite_by_token(token)
    if not invite:
        return JSONResponse({"error": "Invite not found"}, status_code=404)
    submission = db.get_submission_by_invite(invite["id"])
    return _submission_public_payload(invite, submission)


@app.get("/api/invites/by-token/{token}/files/{field_name}")
async def api_download_submission_file(
    token: str,
    field_name: str,
    x_onboarding_api_key: Optional[str] = Header(default=None, alias="X-Onboarding-Api-Key"),
):
    """Machine endpoint: Contabo proxies a submitted upload for the client portal."""
    auth_error = require_api_key(x_onboarding_api_key)
    if auth_error:
        return auth_error
    invite = db.get_invite_by_token(token)
    if not invite:
        return JSONResponse({"error": "Invite not found"}, status_code=404)
    submission = db.get_submission_by_invite(invite["id"])
    if not submission:
        return JSONResponse({"error": "Submission not found"}, status_code=404)
    files = submission.get("files") or {}
    stored_name = files.get(field_name)
    if not stored_name:
        return JSONResponse({"error": "File not found"}, status_code=404)
    path = db.upload_dir_for_invite(invite["id"]) / Path(stored_name).name
    if not path.is_file():
        return JSONResponse({"error": "File missing on disk"}, status_code=404)
    return FileResponse(path, filename=path.name)


@app.get("/o/{token}", response_class=HTMLResponse)
def client_form_get(request: Request, token: str):
    invite = db.get_invite_by_token(token)
    if not invite:
        return HTMLResponse("<h1>Invalid or expired link</h1>", status_code=404)
    # Only fully submitted invites are locked; partial can resume.
    if invite["status"] == "submitted":
        # Re-notify Contabo in case the original webhook was missed / mismatched.
        try:
            notify_contabo_submitted(invite, client_form_url(request, token))
        except Exception as exc:
            print(f"[onboarding] Contabo re-notify failed: {exc}")
        return templates.TemplateResponse("already_submitted.html", {"request": request})
    return templates.TemplateResponse(
        "client_form.html",
        form_context(request, token, invite),
    )


@app.post("/o/{token}/upload-chunk")
async def client_upload_chunk(request: Request, token: str):
    """Accept large Mac/phone videos as raw binary chunks (no multipart)."""
    invite, err = resolve_invite_for_upload(token)
    if err:
        return err

    q = request.query_params
    field = str(q.get("field") or "").strip()
    upload_id = str(q.get("upload_id") or "").strip()
    filename = str(q.get("filename") or "file").strip()
    try:
        chunk_index = int(q.get("chunk_index"))
        total_chunks = int(q.get("total_chunks"))
    except (TypeError, ValueError):
        return JSONResponse({"ok": False, "error": "Invalid chunk numbers"}, status_code=400)

    if field not in FILE_FIELDS:
        return JSONResponse({"ok": False, "error": "Invalid field"}, status_code=400)
    if total_chunks < 1 or total_chunks > 5000:
        return JSONResponse({"ok": False, "error": "Invalid chunk count"}, status_code=400)
    if chunk_index < 0 or chunk_index >= total_chunks:
        return JSONResponse({"ok": False, "error": "Invalid chunk index"}, status_code=400)
    upload_id = re.sub(r"[^a-zA-Z0-9_\-]", "", upload_id)[:64]
    if not upload_id:
        return JSONResponse({"ok": False, "error": "Invalid upload id"}, status_code=400)

    dest = chunk_dir(invite["id"], upload_id) / f"{chunk_index:06d}.part"
    written = 0
    try:
        with dest.open("wb") as out:
            async for block in request.stream():
                if not block:
                    continue
                out.write(block)
                written += len(block)
                # Hard cap per chunk (~8MB) to protect memory/disk abuse.
                if written > 8 * 1024 * 1024:
                    out.close()
                    dest.unlink(missing_ok=True)
                    return JSONResponse({"ok": False, "error": "Chunk too large"}, status_code=413)
    except Exception:
        dest.unlink(missing_ok=True)
        return JSONResponse({"ok": False, "error": "Chunk write failed"}, status_code=500)

    if written <= 0:
        dest.unlink(missing_ok=True)
        return JSONResponse({"ok": False, "error": "Empty chunk"}, status_code=400)

    if chunk_index == total_chunks - 1:
        parts_root = chunk_dir(invite["id"], upload_id)
        for i in range(total_chunks):
            part = parts_root / f"{i:06d}.part"
            if not part.is_file() or part.stat().st_size <= 0:
                return JSONResponse(
                    {"ok": False, "error": f"Missing chunk {i}"},
                    status_code=400,
                )
        stored = f"{field}__{safe_filename(filename)}"
        final_path = db.upload_dir_for_invite(invite["id"]) / stored
        with final_path.open("wb") as out:
            for i in range(total_chunks):
                part = parts_root / f"{i:06d}.part"
                with part.open("rb") as src:
                    shutil.copyfileobj(src, out, length=1024 * 1024)
        shutil.rmtree(parts_root, ignore_errors=True)

        # Register file immediately so admin sees it even if later form-save fails.
        products = invite.get("products") or []
        prior = db.get_submission_by_invite(invite["id"]) or {}
        company = (prior.get("company") or "").strip()
        phone = (prior.get("phone") or "").strip()
        city = (prior.get("city") or "").strip()
        country = (prior.get("country") or "").strip()
        payload = dict(prior.get("payload") or {})
        file_patch = {field: stored}
        payload, file_patch = mirror_shared_fields(products, payload, file_patch)
        merged_files = {**(prior.get("files") or {}), **file_patch}
        pending = compute_pending(
            products, company, phone, city, country, payload, merged_files
        )
        db.save_submission(
            invite["id"],
            company,
            phone,
            city,
            country,
            payload,
            file_patch,
            pending=pending,
            finalize=False,
        )

        return JSONResponse(
            {
                "ok": True,
                "done": True,
                "field": field,
                "stored_name": stored,
                "display_name": display_filename(stored),
                "bytes": final_path.stat().st_size,
                "pending": pending,
            }
        )

    return JSONResponse(
        {
            "ok": True,
            "done": False,
            "chunk_index": chunk_index,
            "total_chunks": total_chunks,
            "bytes": written,
        }
    )


async def parse_client_form(request: Request):
    """Parse multipart/form with large-part support; fall back if kw unsupported."""
    try:
        return await request.form(max_part_size=MAX_PART_SIZE)
    except TypeError:
        return await request.form()


def extract_text_fields(
    source: Any,
    *,
    prior_payload: dict,
    prior_company: str = "",
    prior_phone: str = "",
    prior_city: str = "",
    prior_country: str = "",
) -> tuple[str, str, str, str, dict]:
    """Pull company/phone/city/country + payload text from form mapping or JSON dict."""
    def get(key: str, default: str = "") -> str:
        if hasattr(source, "get"):
            val = source.get(key)
        else:
            return default
        if isinstance(val, UploadFile):
            return default
        if val is None:
            return default
        return str(val)

    company = get("common__company", "").strip() or prior_company
    city = get("common__city", "").strip() or prior_city
    country = get("common__country", "").strip() or prior_country

    dial = get("common__phone_dial", "").strip()
    national = get("common__phone_national", "").strip()
    phone_full = get("common__phone", "").strip()
    if dial or national:
        phone = compose_phone(dial, national) or prior_phone
    else:
        phone = phone_full or prior_phone

    payload = dict(prior_payload or {})
    if dial:
        payload["common__phone_dial"] = dial
    if national or dial:
        payload["common__phone_national"] = national

    keys = []
    if hasattr(source, "keys"):
        keys = list(source.keys())
    elif isinstance(source, dict):
        keys = list(source.keys())

    for key in keys:
        if key in FILE_FIELDS or key.startswith("preuploaded__"):
            continue
        if key.startswith("common__") and key in {
            "common__company",
            "common__phone",
            "common__phone_dial",
            "common__phone_national",
            "common__city",
            "common__country",
        }:
            continue
        if not any(key.startswith(p) for p in TEXT_PREFIXES):
            continue
        new_val = get(key, "")
        if key == "marketpulse__app_password" and not new_val.strip():
            continue
        payload[key] = new_val

    return company, phone, city, country, payload


@app.post("/o/{token}/autosave")
async def client_form_autosave(request: Request, token: str):
    """Debounced draft save for text fields. Never finalizes / notifies Contabo."""
    invite = db.get_invite_by_token(token)
    if not invite:
        return JSONResponse({"ok": False, "error": "Invalid link"}, status_code=404)
    if invite["status"] == "submitted":
        return JSONResponse({"ok": False, "error": "Already submitted"}, status_code=400)

    products = invite.get("products") or []
    prior = db.get_submission_by_invite(invite["id"]) or {}
    prior_payload = dict(prior.get("payload") or {})
    prior_files = dict(prior.get("files") or {})

    try:
        incoming = await request.json()
    except Exception:
        incoming = {}
    if not isinstance(incoming, dict):
        incoming = {}

    merged_source = {
        "common__company": prior.get("company") or "",
        "common__phone": prior.get("phone") or "",
        "common__city": prior.get("city") or "",
        "common__country": prior.get("country") or "",
        **prior_payload,
    }
    for key, val in incoming.items():
        if key.startswith("preuploaded__"):
            continue
        merged_source[key] = "" if val is None else val

    company, phone, city, country, payload = extract_text_fields(
        merged_source,
        prior_payload=prior_payload,
        prior_company=(prior.get("company") or ""),
        prior_phone=(prior.get("phone") or ""),
        prior_city=(prior.get("city") or ""),
        prior_country=(prior.get("country") or ""),
    )
    if "common__company" in incoming:
        company = str(incoming.get("common__company") or "").strip()
    if "common__city" in incoming:
        city = str(incoming.get("common__city") or "").strip()
    if "common__country" in incoming:
        country = str(incoming.get("common__country") or "").strip()
    if "common__phone_dial" in incoming or "common__phone_national" in incoming:
        dial = str(incoming.get("common__phone_dial") or payload.get("common__phone_dial") or "")
        national = str(
            incoming.get("common__phone_national")
            if "common__phone_national" in incoming
            else payload.get("common__phone_national") or ""
        )
        phone = compose_phone(dial, national)
        payload["common__phone_dial"] = dial.strip()
        payload["common__phone_national"] = national.strip()

    payload, files = mirror_shared_fields(products, payload, prior_files)
    pending = compute_pending(products, company, phone, city, country, payload, files)
    db.save_submission(
        invite["id"],
        company,
        phone,
        city,
        country,
        payload,
        {},
        pending=pending,
        finalize=False,
    )
    return {
        "ok": True,
        "pending": pending,
        "complete": not pending,
        "savedAt": db.utc_now(),
    }


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
        form = await parse_client_form(request)
    except Exception:
        prior = db.get_submission_by_invite(invite["id"]) or prior
        return templates.TemplateResponse(
            "client_form.html",
            form_context(
                request,
                token,
                invite,
                error="Could not save form fields. Files already uploaded are kept. Fill text fields and try Submit again.",
                pending=list((prior or {}).get("pending") or []),
                submission=prior or None,
            ),
            status_code=400,
        )

    company, phone, city, country, payload = extract_text_fields(
        form,
        prior_payload=prior_payload,
        prior_company=(prior.get("company") or ""),
        prior_phone=(prior.get("phone") or ""),
        prior_city=(prior.get("city") or ""),
        prior_country=(prior.get("country") or ""),
    )

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
                    "payload": payload,
                    "files": prior_files,
                },
            ),
            status_code=status,
        )

    upload_root = db.upload_dir_for_invite(invite["id"])
    saved_files: dict = {}

    for field in FILE_FIELDS:
        pre = str(form.get(f"preuploaded__{field}") or "").strip()
        if not pre:
            continue
        pre_name = Path(pre).name
        if not pre_name.startswith(f"{field}__"):
            continue
        if (upload_root / pre_name).is_file():
            saved_files[field] = pre_name

    for field in FILE_FIELDS:
        if field in saved_files:
            continue
        item = form.get(field)
        if not isinstance(item, UploadFile) or not item.filename:
            continue
        try:
            fname = f"{field}__{safe_filename(item.filename)}"
            dest = upload_root / fname
            written = await save_upload_stream(item, dest)
            if written <= 0:
                continue
            saved_files[field] = fname
        except Exception:
            if field == "avatar__video":
                extra_notes.append("Avatar video (upload failed; try again)")
            elif field == "avatar__voice_sample":
                extra_notes.append("Voice sample (upload failed; try again)")
            else:
                extra_notes.append(f"{field} (upload failed)")

    payload, saved_files = mirror_shared_fields(products, payload, saved_files)
    merged_files = {**prior_files, **saved_files}
    payload, merged_files = mirror_shared_fields(products, payload, merged_files)
    pending = compute_pending(
        products, company, phone, city, country, payload, merged_files, extra_notes
    )

    db.save_submission(
        invite["id"],
        company,
        phone,
        city,
        country,
        payload,
        saved_files,
        pending=pending,
        finalize=True,
    )

    form_url = client_form_url(request, token)
    if not pending:
        notify_contabo_submitted(invite, form_url)

    return templates.TemplateResponse(
        "thank_you.html",
        {
            "request": request,
            "pending": pending,
            "is_partial": bool(pending),
            "token": token,
            "form_url": form_url,
            "client_name": (invite.get("label") or "").strip(),
            "contact_url": CONTACT_URL,
        },
    )