#!/usr/bin/env python3
#
# app/db.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable, Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
DB_PATH: Path = Path(__file__).parent.parent / "data" / "jobs.db"

# Whitelist of updatable columns to prevent SQL injection via column names.
_UPDATEABLE_COLUMNS: frozenset[str] = frozenset(
    {
        "url",
        "type",
        "quality",
        "status",
        "filename",
        "finished_at",
        "filesize_bytes",
        "duration_seconds",
        "message",
        "codec",
        "bitrate_kbps",
        "video_title",
        "video_meta_hover",
    }
)

_SETTINGS_DEFAULTS: dict[str, str] = {
    "retention_days": "7",
    "login_required": "false",
    "session_idle_minutes": "60",
    "lalalaai_email": "",
    "lalalaai_auth_key": "",
    "lalalaai_auth_requested_at": "0",
    "lalalaai_auth_checked_at": "0",
    "lalalaai_auth_is_valid": "false",
    "lalalaai_auth_last_error": "",
}

_SETTINGS_TYPES: dict[str, Callable[[str], Any]] = {
    "retention_days": lambda v: int(v) if v.isdigit() else 7,
    "login_required": lambda v: v.lower() in ("true", "1", "yes"),
    "session_idle_minutes": lambda v: int(v) if str(v).isdigit() else 60,
    "lalalaai_email": str,
    "lalalaai_auth_key": str,
    "lalalaai_auth_requested_at": lambda v: int(v) if str(v).isdigit() else 0,
    "lalalaai_auth_checked_at": lambda v: int(v) if str(v).isdigit() else 0,
    "lalalaai_auth_is_valid": lambda v: str(v).lower() in ("true", "1", "yes"),
    "lalalaai_auth_last_error": str,
}


# --------------------------------------------------------------------------- #
# Connection management
# --------------------------------------------------------------------------- #
def _configure_connection(con: sqlite3.Connection) -> None:
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("PRAGMA busy_timeout=5000")


@contextmanager
def get_db() -> Generator[sqlite3.Connection, None, None]:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    _configure_connection(con)
    try:
        yield con
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def close_db() -> None:
    """Checkpoint WAL and truncate on shutdown (best-effort)."""
    if not DB_PATH.exists():
        return
    con: sqlite3.Connection | None = None
    try:
        con = sqlite3.connect(DB_PATH)
        _configure_connection(con)
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except Exception as exc:
        logger.warning("WAL checkpoint failed: %s", exc)
    finally:
        if con is not None:
            con.close()


# --------------------------------------------------------------------------- #
# Schema (fresh install only - no migration guards)
# --------------------------------------------------------------------------- #
def init_db() -> None:
    with get_db() as con:
        tables = {
            row["name"]
            for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        indexes = {
            row["name"]
            for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            ).fetchall()
        }

        if "jobs" not in tables:
            con.execute("""
                CREATE TABLE jobs (
                    id TEXT PRIMARY KEY,
                    url TEXT NOT NULL,
                    type TEXT,
                    quality TEXT,
                    status TEXT,
                    filename TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    finished_at TIMESTAMP,
                    duration_seconds INTEGER,
                    filesize_bytes INTEGER,
                    message TEXT,
                    codec TEXT,
                    bitrate_kbps INTEGER,
                    video_title TEXT,
                    video_meta_hover TEXT
                )
            """)

        if "idx_jobs_created_at" not in indexes:
            con.execute("""
                CREATE INDEX idx_jobs_created_at
                ON jobs(created_at DESC)
            """)

        if "settings" not in tables:
            con.execute("""
                CREATE TABLE settings (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
            """)
        con.commit()


# --------------------------------------------------------------------------- #
# CRUD
# --------------------------------------------------------------------------- #
def insert_job(
    job_id: str,
    url: str,
    job_type: str,
    quality: str,
    status: str,
    video_title: str | None = None,
    video_meta_hover: str | None = None,
) -> None:
    with get_db() as con:
        con.execute(
            """
            INSERT INTO jobs (id, url, type, quality, status, video_title, video_meta_hover)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (job_id, url, job_type, quality, status, video_title, video_meta_hover),
        )
        con.commit()


def update_job(job_id: str, **fields: Any) -> None:
    if not fields:
        return

    safe_fields = {k: v for k, v in fields.items() if k in _UPDATEABLE_COLUMNS}
    if not safe_fields:
        raise ValueError("No valid fields to update")

    keys = ", ".join(f"{k}=?" for k in safe_fields)
    values = [*safe_fields.values(), job_id]

    with get_db() as con:
        con.execute(f"UPDATE jobs SET {keys} WHERE id=?", values)
        con.commit()


def get_job(job_id: str) -> sqlite3.Row | None:
    with get_db() as con:
        return con.execute(
            "SELECT * FROM jobs WHERE id=?", (job_id,)
        ).fetchone()


def list_jobs(limit: int = 100) -> list[sqlite3.Row]:
    with get_db() as con:
        return con.execute(
            "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()


def paginate_jobs(limit: int = 50, offset: int = 0) -> list[sqlite3.Row]:
    """Offset-based pagination for infinite scroll."""
    with get_db() as con:
        return con.execute(
            "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()


def get_stats() -> dict[str, int]:
    """Aggregated KPIs for completed jobs."""
    with get_db() as con:
        row = con.execute("""
            SELECT
                COUNT(*)                        AS total_jobs,
                COALESCE(SUM(duration_seconds) / 60, 0) AS total_minutes,
                COALESCE(SUM(filesize_bytes), 0) AS total_bytes
            FROM jobs
            WHERE status = 'done'
        """).fetchone()
    return {
        "total_jobs": row["total_jobs"] or 0,
        "total_minutes": row["total_minutes"] or 0,
        "total_bytes": row["total_bytes"] or 0,
    }


def purge_old_jobs(keep_days: int) -> list[str]:
    """
    Delete completed jobs older than keep_days.
    Returns deleted IDs so callers can clean up filesystem artifacts.
    """
    if keep_days < 0:
        raise ValueError("keep_days must be non-negative")

    with get_db() as con:
        rows = con.execute(
            """
            DELETE FROM jobs
            WHERE status IN ('done', 'error')
              AND finished_at < datetime('now', '-' || ? || ' days')
            RETURNING id
            """,
            (keep_days,),
        ).fetchall()
        con.commit()
        return [row["id"] for row in rows]


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #
def get_settings() -> dict[str, Any]:
    """Retrieve settings with type coercion."""
    with get_db() as con:
        rows = con.execute("SELECT key, value FROM settings").fetchall()

    settings: dict[str, Any] = {}
    for key, value in ((row["key"], row["value"]) for row in rows):
        parser = _SETTINGS_TYPES.get(key, str)
        try:
            settings[key] = parser(value)
        except Exception:
            settings[key] = _SETTINGS_DEFAULTS.get(key, value)

    for key, default in _SETTINGS_DEFAULTS.items():
        if key not in settings:
            parser = _SETTINGS_TYPES.get(key, str)
            settings[key] = parser(default)

    return settings


def set_settings(data: dict[str, Any]) -> None:
    """Update settings. Unknown keys are ignored."""
    # Allow default settings and admin_password_hash (for GUI password changes)
    allowed = set(_SETTINGS_DEFAULTS) | {"admin_password_hash"}
    filtered = {k: v for k, v in data.items() if k in allowed}

    with get_db() as con:
        for key, value in filtered.items():
            if isinstance(value, bool):
                value = "true" if value else "false"
            else:
                value = str(value)

            con.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                (key, value),
            )
        con.commit()
