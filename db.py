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
    return d


def create_invite(label: str, products: Iterable[str], connect_url: Optional[str]) -> Dict[str, Any]:
    token = secrets.token_urlsafe(24)
    products_list = list(products)
    with connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO invites (token, label, products, connect_url, status, created_at)
            VALUES (?, ?, ?, ?, 'pending', ?)
            """,
            (token, label.strip(), json.dumps(products_list), (connect_url or "").strip() or None, utc_now()),
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
            SELECT i.*, s.id AS submission_id, s.company AS submission_company
            FROM invites i
            LEFT JOIN submissions s ON s.invite_id = i.id
            ORDER BY i.id DESC
            """
        ).fetchall()
    out = []
    for row in rows:
        d = _row_to_dict(row)
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
) -> Dict[str, Any]:
    now = utc_now()
    with connect() as conn:
        existing = conn.execute("SELECT id FROM submissions WHERE invite_id = ?", (invite_id,)).fetchone()
        if existing:
            raise ValueError("This invite was already submitted.")
        cur = conn.execute(
            """
            INSERT INTO submissions
              (invite_id, company, phone, city, country, payload_json, files_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                invite_id,
                company.strip(),
                phone.strip(),
                city.strip(),
                country.strip(),
                json.dumps(payload),
                json.dumps(files),
                now,
            ),
        )
        conn.execute(
            "UPDATE invites SET status = 'submitted', submitted_at = ? WHERE id = ?",
            (now, invite_id),
        )
        sid = cur.lastrowid
    return get_submission(sid)


def upload_dir_for_invite(invite_id: int) -> Path:
    path = data_dir() / "uploads" / str(invite_id)
    path.mkdir(parents=True, exist_ok=True)
    return path
