#!/usr/bin/env python3
#
# app/db.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

from __future__ import annotations

import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

# --------------------------------------------------------------------------- #
# Konfiguration
# --------------------------------------------------------------------------- #
DB_PATH: Path = Path(__file__).parent.parent / "data" / "jobs.db"

# Whitelist erlaubter Spalten – schützt update_job vor SQL-Injection via Spaltennamen
_UPDATEABLE_COLUMNS: frozenset[str] = frozenset(
    {"url", "type", "quality", "status", "filename", "finished_at", "filesize_bytes", "duration_seconds", "message"}
)


# --------------------------------------------------------------------------- #
# Zentraler Connection-Manager
# --------------------------------------------------------------------------- #
@contextmanager
def get_db() -> Generator[sqlite3.Connection, None, None]:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")  # WAL: NORMAL ist safe und schneller als FULL
    con.execute("PRAGMA busy_timeout=5000")   # 5s warten statt sofort SQLITE_BUSY
    try:
        yield con
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def close_db() -> None:
    """WAL-Checkpoint + Shrink beim Shutdown – sichert alle Writes in die Hauptdatei."""
    if not DB_PATH.exists():
        return
    try:
        con = sqlite3.connect(DB_PATH)
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        con.close()
    except Exception:
        pass  # Best-effort – Prozess beendet sich ohnehin


# --------------------------------------------------------------------------- #
# Schema
# --------------------------------------------------------------------------- #
def init_db() -> None:
    with get_db() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                url TEXT NOT NULL,
                type TEXT,
                quality TEXT,
                status TEXT,
                filename TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                finished_at TIMESTAMP,
                duration_seconds INTEGER,
                filesize_bytes INTEGER
            )
        """)
        # Spalten nachträglich ergänzen (idempotent für bestehende DBs)
        for col, typedef in (
            ("duration_seconds", "INTEGER"),
            ("filesize_bytes", "INTEGER"),
            ("message", "TEXT"),
        ):
            try:
                con.execute(f"ALTER TABLE jobs ADD COLUMN {col} {typedef}")
            except sqlite3.OperationalError:
                pass  # Spalte existiert bereits
        con.execute("""
            CREATE INDEX IF NOT EXISTS idx_jobs_created_at
            ON jobs(created_at DESC)
        """)
        
        # Settings table
        con.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        
        # Initialize default settings if not exist
        con.execute("""
            INSERT OR IGNORE INTO settings (key, value) VALUES ('retention_days', '7')
        """)
        con.execute("""
            INSERT OR IGNORE INTO settings (key, value) VALUES ('login_required', 'false')
        """)
        con.execute("""
            INSERT OR IGNORE INTO settings (key, value) VALUES ('lalalaai_api_key', '')
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
) -> None:
    with get_db() as con:
        con.execute(
            "INSERT INTO jobs (id, url, type, quality, status) VALUES (?, ?, ?, ?, ?)",
            (job_id, url, job_type, quality, status),
        )
        con.commit()


def update_job(job_id: str, **fields: Any) -> None:
    if not fields:
        return

    safe_fields = {k: v for k, v in fields.items() if k in _UPDATEABLE_COLUMNS}
    if not safe_fields:
        raise ValueError(
            f"No valid fields to update. Allowed: {_UPDATEABLE_COLUMNS}"
        )

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


def iter_jobs(limit: int = 100) -> Generator[sqlite3.Row, None, None]:
    with get_db() as con:
        yield from con.execute(
            "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)
        )


def list_jobs(limit: int = 100) -> list[sqlite3.Row]:
    return list(iter_jobs(limit))


def paginate_jobs(limit: int = 50, offset: int = 0) -> list[sqlite3.Row]:
    """Cursor-basierte Pagination für Infinity Scroll."""
    with get_db() as con:
        return con.execute(
            "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()


def get_stats() -> dict[str, int]:
    """Aggregierte KPIs für die Hero-Stats-Anzeige."""
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
        "total_jobs":    row[0] or 0,
        "total_minutes": row[1] or 0,
        "total_bytes":   row[2] or 0,
    }


def purge_old_jobs(keep_days: int) -> list[str]:
    """
    Delete completed jobs older than keep_days.
    Returns list of deleted job IDs for directory cleanup.
    """
    with get_db() as con:
        # First, get the IDs of jobs to delete
        rows = con.execute(
            """
            SELECT id FROM jobs
            WHERE status IN ('done', 'error')
              AND finished_at < datetime('now', ? || ' days')
            """,
            (f"-{keep_days}",),
        ).fetchall()
        
        job_ids = [row["id"] for row in rows]
        
        if job_ids:
            # Delete the jobs
            con.execute(
                f"""
                DELETE FROM jobs
                WHERE id IN ({','.join('?' * len(job_ids))})
                """,
                job_ids,
            )
            con.commit()
        
        return job_ids


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #
def get_settings() -> dict[str, Any]:
    """Retrieve all settings from database."""
    with get_db() as con:
        rows = con.execute("SELECT key, value FROM settings").fetchall()
    
    settings = {}
    for row in rows:
        key = row["key"]
        value = row["value"]
        
        # Parse boolean values
        if key == "login_required":
            settings[key] = value.lower() in ("true", "1", "yes")
        # Parse integer values
        elif key == "retention_days":
            try:
                settings[key] = int(value)
            except (ValueError, TypeError):
                settings[key] = 30
        else:
            settings[key] = value
    
    return settings


def set_settings(data: dict[str, Any]) -> None:
    """Update settings in database."""
    with get_db() as con:
        for key, value in data.items():
            # Convert boolean to string for storage
            if isinstance(value, bool):
                value = "true" if value else "false"
            else:
                value = str(value)
            
            con.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                (key, value),
            )
        con.commit()
