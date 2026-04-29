#!/usr/bin/env python3
#
# app/db.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

import logging
import sqlite3
from collections.abc import Callable, Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
DB_PATH: Final = Path(__file__).parent.parent / "data" / "jobs.db"

# Whitelist of updatable columns to prevent SQL injection via column names.
_UPDATEABLE_COLUMNS: Final[frozenset[str]] = frozenset(
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
        "bpm",
        "bpm_confidence",
        "audio_hash",
        "lalal_split_done",
    }
)

_SETTINGS_DEFAULTS: Final[dict[str, str]] = {
    "retention_days": "7",
    "login_required": "false",
    "session_idle_minutes": "60",
    "lalalaai_email": "",
    "lalalaai_auth_key": "",
    "lalalaai_auth_checked_at": "0",
    "lalalaai_auth_is_valid": "false",
    "lalalaai_auth_last_error": "",
}


def _int_parser(default: int) -> Callable[[str], int]:
    return lambda v: int(v) if str(v).isdigit() else default


_INTERNAL_SETTINGS_KEYS: Final[frozenset[str]] = frozenset({
    "admin_password_hash",
    "session_version",
})
# Only user-writable keys.  Internal keys require allow_internal=True in set_settings.
_ALLOWED_SETTINGS_KEYS: Final[frozenset[str]] = frozenset(_SETTINGS_DEFAULTS)

# Statuses that count as "completed" for audio analysis purposes.
_COMPLETED_STATUSES: Final[frozenset[str]] = frozenset({"done", "analysis", "analysis_done"})
_RECOVERABLE_IN_FLIGHT_STATUSES: Final[frozenset[str]] = frozenset({"processing", "downloading", "transcoding"})

_SETTINGS_TYPES: Final[dict[str, Callable[[str], Any]]] = {
    "retention_days": _int_parser(7),
    "login_required": lambda v: v.lower() in ("true", "1", "yes"),
    "session_idle_minutes": _int_parser(60),
    "session_version": _int_parser(0),
    "lalalaai_email": str,
    "lalalaai_auth_key": str,
    "lalalaai_auth_checked_at": _int_parser(0),
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
    """Yield a configured SQLite connection.

    This function performs synchronous I/O. Callers running inside an
    async event loop should wrap database access in asyncio.to_thread().
    """
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
# Schema – idempotent (CREATE … IF NOT EXISTS + lightweight migrations)
# --------------------------------------------------------------------------- #
def init_db() -> None:
    """Idempotent schema creation and lightweight column migration."""
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
                filesize_bytes INTEGER,
                message TEXT,
                codec TEXT,
                bitrate_kbps INTEGER,
                video_title TEXT,
                video_meta_hover TEXT,
                bpm INTEGER,
                bpm_confidence REAL,
                audio_hash TEXT,
                lalal_split_done INTEGER NOT NULL DEFAULT 0
            )
        """)

        # Lightweight migration: add audio analysis columns to existing rows.
        existing_cols = {
            row["name"]
            for row in con.execute("PRAGMA table_info(jobs)").fetchall()
        }
        for col, dtype in (
            ("bpm", "INTEGER"),
            ("bpm_confidence", "REAL"),
            ("audio_hash", "TEXT"),
            ("lalal_split_done", "INTEGER NOT NULL DEFAULT 0"),
        ):
            if col not in existing_cols:
                con.execute(f"ALTER TABLE jobs ADD COLUMN {col} {dtype}")

        con.execute("""
            CREATE TABLE IF NOT EXISTS audio_analysis_cache (
                hash TEXT PRIMARY KEY,
                bpm INTEGER,
                bpm_confidence REAL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        con.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)

        con.execute("CREATE INDEX IF NOT EXISTS idx_jobs_created_at ON jobs(created_at DESC)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_jobs_finished_at ON jobs(finished_at)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_jobs_status_finished_at ON jobs(status, finished_at)")

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


def update_job_if_status(job_id: str, expected_statuses: tuple[str, ...], **fields: Any) -> bool:
    if not expected_statuses:
        raise ValueError("expected_statuses must not be empty")
    if not fields:
        return False

    safe_fields = {k: v for k, v in fields.items() if k in _UPDATEABLE_COLUMNS}
    if not safe_fields:
        raise ValueError("No valid fields to update")

    keys = ", ".join(f"{k}=?" for k in safe_fields)
    placeholders = ", ".join("?" for _ in expected_statuses)
    values = [*safe_fields.values(), job_id, *expected_statuses]

    with get_db() as con:
        cursor = con.execute(
            f"UPDATE jobs SET {keys} WHERE id=? AND status IN ({placeholders})",
            values,
        )
        con.commit()
        return bool(cursor.rowcount)


def get_audio_analysis_cache(hash_value: str) -> sqlite3.Row | None:
    with get_db() as con:
        return con.execute(
            """
            SELECT hash, bpm, bpm_confidence, created_at
            FROM audio_analysis_cache
            WHERE hash=?
            """,
            (hash_value,),
        ).fetchone()


def upsert_audio_analysis_cache(
    hash_value: str,
    *,
    bpm: int | None,
    bpm_confidence: float | None,
) -> None:
    with get_db() as con:
        con.execute(
            """
            INSERT INTO audio_analysis_cache (hash, bpm, bpm_confidence)
            VALUES (?, ?, ?)
            ON CONFLICT(hash) DO UPDATE SET
                bpm=excluded.bpm,
                bpm_confidence=excluded.bpm_confidence,
                created_at=CURRENT_TIMESTAMP
            """,
            (hash_value, bpm, bpm_confidence),
        )
        con.commit()


def list_completed_bpms(limit: int = 1000) -> list[int]:
    placeholders = ",".join("?" for _ in _COMPLETED_STATUSES)
    with get_db() as con:
        rows = con.execute(
            f"""
            SELECT bpm
            FROM jobs
            WHERE bpm IS NOT NULL
              AND status IN ({placeholders})
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (*_COMPLETED_STATUSES, limit),
        ).fetchall()
    return [int(row["bpm"]) for row in rows if row["bpm"] is not None]


def list_jobs_requiring_audio_analysis(limit: int = 200) -> list[sqlite3.Row]:
    with get_db() as con:
        return con.execute(
            """
            SELECT id, filename, duration_seconds
            FROM jobs
            WHERE type='audio'
              AND status='analysis'
              AND filename IS NOT NULL
            ORDER BY created_at ASC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()


def list_queued_jobs() -> list[sqlite3.Row]:
    with get_db() as con:
        return con.execute(
            """
            SELECT id, url, type, quality
            FROM jobs
            WHERE status='queued'
            ORDER BY created_at ASC
            """
        ).fetchall()


def cancel_interrupted_jobs() -> int:
    """Mark stale in-flight jobs as cancelled after an application restart."""
    placeholders = ",".join("?" for _ in _RECOVERABLE_IN_FLIGHT_STATUSES)
    message = "Cancelled because the application restarted during processing"
    finished_at = datetime.now(UTC).isoformat()

    with get_db() as con:
        cursor = con.execute(
            f"""
            UPDATE jobs
            SET status=?,
                message=?,
                finished_at=?
            WHERE status IN ({placeholders})
            """,
            ("cancelled", message, finished_at, *_RECOVERABLE_IN_FLIGHT_STATUSES),
        )
        con.commit()
        return int(cursor.rowcount or 0)


def get_job(job_id: str) -> sqlite3.Row | None:
    with get_db() as con:
        return con.execute(
            "SELECT * FROM jobs WHERE id=?", (job_id,)
        ).fetchone()


def job_exists(job_id: str) -> bool:
    """Check if a job exists in the database."""
    return get_job(job_id) is not None


def list_jobs(limit: int = 100) -> list[sqlite3.Row]:
    return paginate_jobs(limit=limit, offset=0)


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
                COALESCE(SUM(filesize_bytes), 0) AS total_bytes,
                COALESCE(SUM(CASE
                    WHEN type = 'audio' AND lalal_split_done = 1 THEN duration_seconds
                    ELSE 0
                END) / 60, 0) AS total_lalal_minutes
            FROM jobs
            WHERE status = 'done'
        """).fetchone()

    return {
        "total_jobs": row["total_jobs"] or 0,
        "total_minutes": row["total_minutes"] or 0,
        "total_bytes": row["total_bytes"] or 0,
        "total_lalal_minutes": row["total_lalal_minutes"] or 0,
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


def set_settings(data: dict[str, Any], *, allow_internal: bool = False) -> None:
    """Update settings. Unknown keys are ignored.

    Args:
        data: Key/value pairs to persist.
        allow_internal: When ``True``, internal keys such as
            ``admin_password_hash`` are also accepted.  Callers that receive
            data directly from user input must leave this as ``False`` so that
            the password hash cannot be overwritten without explicit
            verification.
    """
    allowed = _ALLOWED_SETTINGS_KEYS | _INTERNAL_SETTINGS_KEYS if allow_internal else _ALLOWED_SETTINGS_KEYS
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
