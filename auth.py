"""Simple cookie session auth for Digigrey onboarding admin."""
from __future__ import annotations

import os
from typing import Optional

from fastapi import HTTPException, Request, status
from itsdangerous import BadSignature, URLSafeSerializer

COOKIE_NAME = "dg_onboard_admin"
SESSION_VALUE = "ok"


def _secret() -> str:
    return os.getenv("SESSION_SECRET") or os.getenv("ADMIN_PASSWORD") or "dev-insecure-secret"


def _serializer() -> URLSafeSerializer:
    return URLSafeSerializer(_secret(), salt="digigrey-onboarding-admin")


def admin_password() -> str:
    return (os.getenv("ADMIN_PASSWORD") or "").strip()


def check_password(password: str) -> bool:
    expected = admin_password()
    if not expected:
        return False
    return password == expected


def make_session_cookie() -> str:
    return _serializer().dumps(SESSION_VALUE)


def is_admin(request: Request) -> bool:
    raw = request.cookies.get(COOKIE_NAME)
    if not raw:
        return False
    try:
        return _serializer().loads(raw) == SESSION_VALUE
    except BadSignature:
        return False


def require_admin(request: Request) -> None:
    if not is_admin(request):
        raise HTTPException(
            status_code=status.HTTP_303_SEE_OTHER,
            headers={"Location": "/admin/login"},
        )
