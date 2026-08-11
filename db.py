"""SQLite persistence for invites + submissions."""
from __future__ import annotations

import json
import os
import sqlite3
import secrets
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


def data_dir() -> Path:
    root = Path(os.getenv("DATA_DIR") or "./data").resolve()
    root.mkdir(parents=True, exist_ok=True)
    (root / "uploads").mkdir(parents=True, exist_ok=True)
    return root


def db_path() -> Path:
    return data_dir() / "onboarding.sqlite3"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def connect():
    path = db_path()
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _table_columns(conn: sqlite3.Connection, table: str) -> set:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {r[1] for r in rows}


def init_db() -> None:
    with connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS invites (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                token TEXT NOT NULL UNIQUE,
                label TEXT NOT NULL,
                products TEXT NOT NULL,
                connect_url TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL,
                submitted_at TEXT
            );

            CREATE TABLE IF NOT EXISTS submissions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                invite_id INTEGER NOT NULL UNIQUE,
                company TEXT NOT NULL,
                phone TEXT NOT NULL,
                city TEXT NOT NULL,
                country TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                files_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(invite_id) REFERENCES invites(id)
            );
            """
        )
        cols = _table_columns(conn, "invites")
        for col in ("connect_url_avatar", "connect_url_vtour", "connect_url_smartgrid"):
            if col not in cols:
                conn.execute(f"ALTER TABLE invites ADD COLUMN {col} TEXT")
        sub_cols = _table_columns(conn, "submissions")
        if "pending_json" not in sub_cols:
            conn.execute("ALTER TABLE submissions ADD COLUMN pending_json TEXT DEFAULT '[]'")
        if "updated_at" not in sub_cols:
            conn.execute("ALTER TABLE submissions ADD COLUMN updated_at TEXT")


def _normalize_connect_urls(d: Dict[str, Any]) -> Dict[str, Any]:
    """Map legacy connect_url → avatar when new columns are empty."""
    avatar = (d.get("connect_url_avatar") or "").strip()
    vtour = (d.get("connect_url_vtour") or "").strip()
    smart = (d.get("connect_url_smartgrid") or "").strip()
    legacy = (d.get("connect_url") or "").strip()
    if not avatar and legacy:
        avatar = legacy
    d["connect_url_avatar"] = avatar or None
    d["connect_url_vtour"] = vtour or None
    d["connect_url_smartgrid"] = smart or None
    return d


def _row_to_dict(row: Optional[sqlite3.Row]) -> Optional[Dict[str, Any]]:
    if row is None:
        return None
    d = dict(row)
    if "products" in d and isinstance(d["products"], str):
        try:
            d["products"] = json.loads(d["products"])
        except json.JSONDecodeError:
            d["products"] = []
    if "payload_json" in d and isinstance(d["payload_json"], str):
        try:
            d["payload"] = json.loads(d["payload_json"])
        except json.JSONDecodeError:
            d["payload"] = {}
    if "files_json" in d and isinstance(d["files_json"], str):
        try:
            d["files"] = json.loads(d["files_json"])
        except json.JSONDecodeError:
            d["files"] = {}
    if "pending_json" in d:
        raw_pending = d.get("pending_json")
        if isinstance(raw_pending, str):
            try:
                d["pending"] = json.loads(raw_pending or "[]")
            except json.JSONDecodeError:
                d["pending"] = []
        elif isinstance(raw_pending, list):
            d["pending"] = raw_pending
        else:
            d["pending"] = []
    if "token" in d or "label" in d:
        d = _normalize_connect_urls(d)
    return d


def create_invite(
    label: str,
    products: Iterable[str],
    connect_url_avatar: Optional[str] = None,
    connect_url_vtour: Optional[str] = None,
    connect_url_smartgrid: Optional[str] = None,
) -> Dict[str, Any]:
    token = secrets.token_urlsafe(24)
    products_list = list(products)
    avatar = (connect_url_avatar or "").strip() or None
    vtour = (connect_url_vtour or "").strip() or None
    smart = (connect_url_smartgrid or "").strip() or None
    with connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO invites (
              token, label, products, connect_url,
              connect_url_avatar, connect_url_vtour, connect_url_smartgrid,
              status, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)
            """,
            (
                token,
                label.strip(),
                json.dumps(products_list),
                avatar,  # legacy column mirrors avatar for old readers
                avatar,
                vtour,
                smart,
                utc_now(),
            ),
        )
        invite_id = cur.lastrowid
    return get_invite(invite_id)


def get_invite(invite_id: int) -> Optional[Dict[str, Any]]:
    with connect() as conn:
        row = conn.execute("SELECT * FROM invites WHERE id = ?", (invite_id,)).fetchone()
    return _row_to_dict(row)


def get_invite_by_token(token: str) -> Optional[Dict[str, Any]]:
    with connect() as conn:
        row = conn.execute("SELECT * FROM invites WHERE token = ?", (token,)).fetchone()
    return _row_to_dict(row)


def list_invites() -> List[Dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT i.*,
                   s.id AS submission_id,
                   s.company AS submission_company,
                   s.pending_json AS submission_pending_json
            FROM invites i
            LEFT JOIN submissions s ON s.invite_id = i.id
            ORDER BY i.id DESC
            """
        ).fetchall()
    out = []
    for row in rows:
        d = _row_to_dict(row)
        if d is None:
            continue
        raw_p = d.get("submission_pending_json")
        if isinstance(raw_p, str):
            try:
                d["submission_pending"] = json.loads(raw_p or "[]")
            except json.JSONDecodeError:
                d["submission_pending"] = []
        else:
            d["submission_pending"] = []
        out.append(d)
    return out


def get_submission_by_invite(invite_id: int) -> Optional[Dict[str, Any]]:
    with connect() as conn:
        row = conn.execute("SELECT * FROM submissions WHERE invite_id = ?", (invite_id,)).fetchone()
    return _row_to_dict(row)


def get_submission(submission_id: int) -> Optional[Dict[str, Any]]:
    with connect() as conn:
        row = conn.execute("SELECT * FROM submissions WHERE id = ?", (submission_id,)).fetchone()
    return _row_to_dict(row)


def save_submission(
    invite_id: int,
    company: str,
    phone: str,
    city: str,
    country: str,
    payload: Dict[str, Any],
    files: Dict[str, str],
    pending: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Upsert submission; merge files (keep previous when field not re-uploaded)."""
    now = utc_now()
    pending_list = list(pending or [])
    status = "submitted" if not pending_list else "partial"
    with connect() as conn:
        existing = conn.execute(
            "SELECT id, payload_json, files_json FROM submissions WHERE invite_id = ?",
            (invite_id,),
        ).fetchone()
        old_payload: Dict[str, Any] = {}
        old_files: Dict[str, str] = {}
        if existing:
            try:
                old_payload = json.loads(existing["payload_json"] or "{}")
            except json.JSONDecodeError:
                old_payload = {}
            try:
                old_files = json.loads(existing["files_json"] or "{}")
            except json.JSONDecodeError:
                old_files = {}

        merged_payload = {**old_payload, **(payload or {})}
        merged_files = {**old_files, **(files or {})}

        if existing:
            conn.execute(
                """
                UPDATE submissions SET
                  company = ?, phone = ?, city = ?, country = ?,
                  payload_json = ?, files_json = ?, pending_json = ?,
                  updated_at = ?
                WHERE invite_id = ?
                """,
                (
                    (company or "").strip(),
                    (phone or "").strip(),
                    (city or "").strip(),
                    (country or "").strip(),
                    json.dumps(merged_payload),
                    json.dumps(merged_files),
                    json.dumps(pending_list),
                    now,
                    invite_id,
                ),
            )
            sid = existing["id"]
        else:
            cur = conn.execute(
                """
                INSERT INTO submissions
                  (invite_id, company, phone, city, country,
                   payload_json, files_json, pending_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    invite_id,
                    (company or "").strip(),
                    (phone or "").strip(),
                    (city or "").strip(),
                    (country or "").strip(),
                    json.dumps(merged_payload),
                    json.dumps(merged_files),
                    json.dumps(pending_list),
                    now,
                    now,
                ),
            )
            sid = cur.lastrowid

        conn.execute(
            "UPDATE invites SET status = ?, submitted_at = ? WHERE id = ?",
            (status, now, invite_id),
        )
    return get_submission(sid)


def upload_dir_for_invite(invite_id: int) -> Path:
    path = data_dir() / "uploads" / str(invite_id)
    path.mkdir(parents=True, exist_ok=True)
    return path