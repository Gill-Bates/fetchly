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
from typing import Any, Final

from .utils.fs import get_data_dir

logger = logging.getLogger(__name__)

__all__ = [
    "DB_PATH",
    "get_db",
    "close_db",
    "init_db",
    "insert_job",
    "update_job",
    "update_job_if_status",
    "get_audio_analysis_cache",
    "upsert_audio_analysis_cache",
    "list_completed_bpms",
    "list_jobs_requiring_audio_analysis",
    "list_queued_jobs",
    "claim_next_queued_job",
    "cancel_interrupted_jobs",
    "get_job",
    "find_active_job_for_submission",
    "job_exists",
    "list_jobs",
    "paginate_jobs",
    "get_stats",
    "list_expired_job_ids",
    "get_settings",
    "set_settings",
    "COMPLETED_STATUSES",
    "DOWNLOADABLE_STATUSES",
    "TERMINAL_JOB_STATUSES",
    "utc_timestamp",
]

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
DB_PATH: Final = get_data_dir() / "jobs.db"

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

_MAX_QUERY_LIMIT: Final[int] = 2_000
_JOB_STATUSES: Final[frozenset[str]] = frozenset(
    {
        "queued",
        "processing",
        "downloading",
        "transcoding",
        "analysis",
        "analysis_done",
        "done",
        "error",
        "cancelled",
    }
)

_SETTINGS_DEFAULTS: Final[dict[str, str]] = {
    "retention_days": "7",
    "login_required": "false",
    "session_idle_minutes": "60",
    "download_concurrent_fragments": "3",
    # Default on: the downloaded file is played back in the browser (player,
    # waveform, trim view), and only H.264/AAC in MP4 plays everywhere -
    # VP9/AV1 renditions do not in Safari/iOS. Users who want the highest
    # resolution over compatibility can turn it off on the settings page.
    "download_mp4_preset": "true",
    "lalalaai_email": "",
    "lalalaai_auth_key": "",
    "lalalaai_auth_checked_at": "0",
    "lalalaai_auth_is_valid": "false",
    "lalalaai_auth_last_error": "",
    "lalalaai_duration_guard": "true",
}


def _parse_bool(value: object) -> bool:
    normalized = str(value).strip().lower()
    if normalized in {"true", "1", "yes", "on"}:
        return True
    if normalized in {"false", "0", "no", "off"}:
        return False
    raise ValueError(f"Invalid boolean value: {value!r}")


def _parse_bounded_int(value: object, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        raise ValueError("Boolean is not an integer setting")
    parsed = int(str(value).strip())
    if not minimum <= parsed <= maximum:
        raise ValueError(f"Integer setting outside allowed range: {parsed}")
    return parsed


def _parse_nonnegative_int(value: object) -> int:
    return _parse_bounded_int(value, minimum=0, maximum=2**63 - 1)


def _validate_limit(limit: int) -> int:
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise TypeError("limit must be an integer")
    if not 1 <= limit <= _MAX_QUERY_LIMIT:
        raise ValueError(f"limit must be between 1 and {_MAX_QUERY_LIMIT}")
    return limit


def _validate_status(status: object) -> str:
    if not isinstance(status, str) or status not in _JOB_STATUSES:
        raise ValueError(f"Invalid job status: {status!r}")
    return status


def _in_placeholders(values: frozenset[str] | tuple[str, ...]) -> str:
    return ",".join("?" for _ in values)


def utc_timestamp() -> str:
    """Return a SQLite-compatible UTC timestamp string without microseconds."""
    return datetime.now(UTC).replace(microsecond=0).strftime("%Y-%m-%d %H:%M:%S")


_INTERNAL_SETTINGS_KEYS: Final[frozenset[str]] = frozenset({
    "admin_password_hash",
    "session_version",
    "statistics_reset_at",
})
_SECRET_SETTINGS_KEYS: Final[frozenset[str]] = frozenset({"lalalaai_auth_key"})
# Only user-writable keys.  Internal keys require allow_internal=True in set_settings.
_ALLOWED_SETTINGS_KEYS: Final[frozenset[str]] = frozenset(_SETTINGS_DEFAULTS)

# Statuses that count as "completed" for downloads / stats purposes.
COMPLETED_STATUSES: Final[frozenset[str]] = frozenset({"done", "analysis_done"})
# Downloaded audio may be served while its non-terminal BPM analysis is pending.
DOWNLOADABLE_STATUSES: Final[frozenset[str]] = COMPLETED_STATUSES | frozenset({"analysis"})
# All terminal statuses (no further transitions possible).
TERMINAL_JOB_STATUSES: Final[frozenset[str]] = COMPLETED_STATUSES | frozenset({"error", "cancelled"})
_RECOVERABLE_IN_FLIGHT_STATUSES: Final[frozenset[str]] = frozenset({"processing", "downloading", "transcoding"})

_JOB_MIGRATIONS: Final[dict[str, str]] = {
    "type": "TEXT",
    "quality": "TEXT",
    "status": "TEXT",
    "filename": "TEXT",
    "finished_at": "TIMESTAMP",
    "duration_seconds": "INTEGER",
    "filesize_bytes": "INTEGER",
    "message": "TEXT",
    "codec": "TEXT",
    "bitrate_kbps": "INTEGER",
    "video_title": "TEXT",
    "video_meta_hover": "TEXT",
    "bpm": "INTEGER",
    "bpm_confidence": "REAL",
    "audio_hash": "TEXT",
    "lalal_split_done": "INTEGER NOT NULL DEFAULT 0",
}

_SETTINGS_TYPES: Final[dict[str, Callable[[object], Any]]] = {
    "retention_days": lambda value: _parse_bounded_int(value, minimum=1, maximum=365),
    "statistics_reset_at": str,
    "login_required": _parse_bool,
    "session_idle_minutes": lambda value: _parse_bounded_int(value, minimum=1, maximum=24 * 60),
    "session_version": _parse_nonnegative_int,
    "download_concurrent_fragments": lambda value: _parse_bounded_int(value, minimum=1, maximum=16),
    "download_mp4_preset": _parse_bool,
    "lalalaai_email": str,
    "lalalaai_auth_key": str,
    "lalalaai_auth_checked_at": _parse_nonnegative_int,
    "lalalaai_auth_is_valid": _parse_bool,
    "lalalaai_auth_last_error": str,
    "lalalaai_duration_guard": _parse_bool,
}


# --------------------------------------------------------------------------- #
# Connection management
# --------------------------------------------------------------------------- #
def _configure_connection(con: sqlite3.Connection) -> None:
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("PRAGMA busy_timeout=5000")


def _configure_database(con: sqlite3.Connection) -> None:
    """Configure persistent database pragmas (run once at startup)."""
    row = con.execute("PRAGMA journal_mode=WAL").fetchone()
    mode = str(row[0]).lower() if row else ""
    if mode != "wal":
        raise RuntimeError(f"SQLite WAL mode unavailable; active journal mode is {mode!r}")


def _prepare_database_path() -> None:
    """Create the database path with owner-only permissions."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    DB_PATH.parent.chmod(0o700)
    DB_PATH.touch(exist_ok=True, mode=0o600)
    DB_PATH.chmod(0o600)


@contextmanager
def get_db() -> Generator[sqlite3.Connection, None, None]:
    """Yield a configured SQLite connection.

    This function performs synchronous I/O. Callers running inside an
    async event loop should wrap database access in asyncio.to_thread().
    """
    _prepare_database_path()
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
        # Configure persistent database pragmas
        _configure_database(con)

        con.execute("""
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                url TEXT NOT NULL,
                type TEXT,
                quality TEXT,
                status TEXT NOT NULL CHECK (
                    status IN (
                        'queued', 'processing', 'downloading', 'transcoding',
                        'analysis', 'analysis_done', 'done', 'error', 'cancelled'
                    )
                ),
                filename TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                finished_at TIMESTAMP,
                duration_seconds INTEGER CHECK (duration_seconds IS NULL OR duration_seconds >= 0),
                filesize_bytes INTEGER CHECK (filesize_bytes IS NULL OR filesize_bytes >= 0),
                message TEXT,
                codec TEXT,
                bitrate_kbps INTEGER,
                video_title TEXT,
                video_meta_hover TEXT,
                bpm INTEGER CHECK (bpm IS NULL OR bpm > 0),
                bpm_confidence REAL,
                audio_hash TEXT,
                lalal_split_done INTEGER NOT NULL DEFAULT 0
            )
        """)

        # Lightweight migration: add all columns defined in _JOB_MIGRATIONS.
        existing_cols = {
            row["name"]
            for row in con.execute("PRAGMA table_info(jobs)").fetchall()
        }
        for col, dtype in _JOB_MIGRATIONS.items():
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
        con.execute("CREATE INDEX IF NOT EXISTS idx_jobs_status_created_at ON jobs(status, created_at DESC)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_jobs_audio_analysis ON jobs(type, status, created_at) WHERE filename IS NOT NULL")
        con.execute("CREATE INDEX IF NOT EXISTS idx_jobs_url_type_quality ON jobs(url, type, quality, created_at DESC)")

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
    _validate_status(status)
    with get_db() as con:
        con.execute(
            """
            INSERT INTO jobs (id, url, type, quality, status, video_title, video_meta_hover)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (job_id, url, job_type, quality, status, video_title, video_meta_hover),
        )
        con.commit()


def update_job(job_id: str, **fields: Any) -> bool:
    if not fields:
        return False

    # Validate that all fields are known; raise if any unknown fields are provided.
    unknown_fields = set(fields) - _UPDATEABLE_COLUMNS
    if unknown_fields:
        raise ValueError(f"Unknown fields: {sorted(unknown_fields)}")

    if not all(column.isidentifier() for column in fields):
        raise ValueError("Invalid column name")
    if "status" in fields:
        _validate_status(fields["status"])

    keys = ", ".join(f"{k}=?" for k in fields)
    values = [*fields.values(), job_id]

    with get_db() as con:
        cursor = con.execute(f"UPDATE jobs SET {keys} WHERE id=?", values)
        con.commit()
        return cursor.rowcount == 1


def update_job_if_status(job_id: str, expected_statuses: tuple[str, ...], **fields: Any) -> bool:
    if not expected_statuses:
        raise ValueError("expected_statuses must not be empty")
    if not fields:
        return False

    unknown_fields = set(fields) - _UPDATEABLE_COLUMNS
    if unknown_fields:
        raise ValueError(f"Unknown fields: {sorted(unknown_fields)}")
    if not all(column.isidentifier() for column in fields):
        raise ValueError("Invalid column name")
    safe_fields = fields
    if "status" in safe_fields:
        _validate_status(safe_fields["status"])

    keys = ", ".join(f"{k}=?" for k in safe_fields)
    placeholders = _in_placeholders(expected_statuses)
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
    limit = _validate_limit(limit)
    placeholders = _in_placeholders(COMPLETED_STATUSES)
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
            (*COMPLETED_STATUSES, limit),
        ).fetchall()
    return [int(row["bpm"]) for row in rows if row["bpm"] is not None]


def list_jobs_requiring_audio_analysis(limit: int = 200) -> list[sqlite3.Row]:
    limit = _validate_limit(limit)
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


def list_queued_jobs(limit: int = 500) -> list[sqlite3.Row]:
    limit = _validate_limit(limit)
    with get_db() as con:
        return con.execute(
            """
            SELECT id, url, type, quality
            FROM jobs
            WHERE status='queued'
            ORDER BY created_at ASC, id ASC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()


def claim_next_queued_job() -> sqlite3.Row | None:
    """Atomically claim and transition the next queued job to processing.

    This prevents multiple workers from processing the same job.
    Returns the updated job row or None if no queued jobs exist.
    """
    with get_db() as con:
        row = con.execute(
            """
            UPDATE jobs
            SET status='processing'
            WHERE id = (
                SELECT id
                FROM jobs
                WHERE status='queued'
                ORDER BY created_at ASC, id ASC
                LIMIT 1
            )
            RETURNING *
            """
        ).fetchone()
        con.commit()
        return row


def cancel_interrupted_jobs() -> int:
    """Mark stale in-flight jobs as cancelled after an application restart."""
    placeholders = _in_placeholders(_RECOVERABLE_IN_FLIGHT_STATUSES)
    message = "Cancelled because the application restarted during processing"
    finished_at = utc_timestamp()

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
        return max(cursor.rowcount, 0)


def get_job(job_id: str) -> sqlite3.Row | None:
    with get_db() as con:
        return con.execute(
            "SELECT * FROM jobs WHERE id=?", (job_id,)
        ).fetchone()


def find_active_job_for_submission(url: str, job_type: str, quality: str) -> sqlite3.Row | None:
    """Return the most recent job for the same (url, type, quality), if any.

    Used by POST /api/submit to warn before creating a second job for a
    source that was already downloaded or is currently in flight. A job
    that errored or was cancelled is excluded so a legitimate retry never
    gets flagged as a duplicate.
    """
    with get_db() as con:
        return con.execute(
            """
            SELECT * FROM jobs
            WHERE url = ? AND type = ? AND quality = ? AND status NOT IN ('error', 'cancelled')
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (url, job_type, quality),
        ).fetchone()


def job_exists(job_id: str) -> bool:
    """Check if a job exists in the database."""
    with get_db() as con:
        row = con.execute(
            "SELECT 1 FROM jobs WHERE id=? LIMIT 1",
            (job_id,),
        ).fetchone()
    return row is not None


def list_jobs(limit: int = 100) -> list[sqlite3.Row]:
    return paginate_jobs(limit=limit, offset=0)


def paginate_jobs(limit: int = 50, offset: int = 0) -> list[sqlite3.Row]:
    """Offset-based pagination for infinite scroll."""
    limit = _validate_limit(limit)
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise ValueError("offset must be a non-negative integer")
    with get_db() as con:
        return con.execute(
            "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()


def get_stats() -> dict[str, int]:
    """Aggregated KPIs for jobs that reached a completed state."""
    placeholders = _in_placeholders(COMPLETED_STATUSES)
    with get_db() as con:
        reset_row = con.execute(
            "SELECT value FROM settings WHERE key='statistics_reset_at'"
        ).fetchone()
        reset_at = str(reset_row["value"] or "").strip() if reset_row else ""
        reset_clause = ""
        query_params: tuple[Any, ...] = tuple(COMPLETED_STATUSES)
        if reset_at:
            reset_clause = " AND datetime(finished_at) > datetime(?)"
            query_params += (reset_at,)

        row = con.execute(f"""
            SELECT
                COUNT(*)                        AS total_jobs,
                COALESCE(SUM(duration_seconds) / 60, 0) AS total_minutes,
                COALESCE(SUM(filesize_bytes), 0) AS total_bytes,
                COALESCE(SUM(CASE
                    WHEN type = 'audio' AND lalal_split_done = 1 THEN duration_seconds
                    ELSE 0
                END) / 60, 0) AS total_lalal_minutes
            FROM jobs
            WHERE status IN ({placeholders})
              {reset_clause}
        """, query_params).fetchone()

    return {
        "total_jobs": row["total_jobs"] or 0,
        "total_minutes": row["total_minutes"] or 0,
        "total_bytes": row["total_bytes"] or 0,
        "total_lalal_minutes": row["total_lalal_minutes"] or 0,
    }


def list_expired_job_ids(keep_days: int) -> list[str]:
    """Return terminal job IDs whose filesystem artifacts may be removed.

    Retention is deliberately read-only at the database level. Keeping these
    rows preserves dashboard statistics and the job history after their files
    have been cleaned up.
    """
    if isinstance(keep_days, bool) or not isinstance(keep_days, int):
        raise TypeError("keep_days must be a non-negative integer")
    if keep_days < 0:
        raise ValueError("keep_days must be non-negative")

    statuses = tuple(sorted(TERMINAL_JOB_STATUSES))
    placeholders = _in_placeholders(statuses)
    with get_db() as con:
        rows = con.execute(
            f"""
            SELECT id
            FROM jobs
            WHERE status IN ({placeholders})
              AND finished_at IS NOT NULL
              AND datetime(finished_at) < datetime('now', '-' || ? || ' days')
            """,
            (*statuses, keep_days),
        ).fetchall()
    return [row["id"] for row in rows]


def purge_old_jobs(keep_days: int) -> list[str]:
    """
    Delete completed jobs older than keep_days.
    Returns deleted IDs so callers can clean up filesystem artifacts.
    """
    if isinstance(keep_days, bool) or not isinstance(keep_days, int):
        raise TypeError("keep_days must be a non-negative integer")
    if keep_days < 0:
        raise ValueError("keep_days must be non-negative")

    statuses = tuple(sorted(TERMINAL_JOB_STATUSES))
    placeholders = _in_placeholders(statuses)
    with get_db() as con:
        rows = con.execute(
            f"""
            DELETE FROM jobs
            WHERE status IN ({placeholders})
              AND finished_at IS NOT NULL
              AND datetime(finished_at) < datetime('now', '-' || ? || ' days')
            RETURNING id
            """,
            (*statuses, keep_days),
        ).fetchall()
        con.commit()
        return [row["id"] for row in rows]


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #
def get_settings(
    *,
    include_internal: bool = False,
    include_secrets: bool = False,
) -> dict[str, Any]:
    """Retrieve settings with type coercion.

    Args:
        include_internal: When False (default), exclude internal keys such as
            admin_password_hash and session_version. Set to True only when
            retrieving settings for internal use (e.g., authentication).
            User-facing APIs should always use the default False to prevent
            accidental information disclosure.
        include_secrets: Include stored credentials such as the Lalal.ai auth
            key. Callers must opt in explicitly and must not serialize them.
    """
    allowed = _ALLOWED_SETTINGS_KEYS - _SECRET_SETTINGS_KEYS
    if include_internal:
        allowed |= _INTERNAL_SETTINGS_KEYS
    if include_secrets:
        allowed |= _SECRET_SETTINGS_KEYS

    with get_db() as con:
        rows = con.execute("SELECT key, value FROM settings").fetchall()

    settings: dict[str, Any] = {}

    for row in rows:
        key = row["key"]
        value = row["value"]

        if key not in allowed:
            continue

        parser = _SETTINGS_TYPES.get(key, str)
        try:
            settings[key] = parser(value)
        except (TypeError, ValueError) as exc:
            logger.warning("Invalid stored setting %s=%r: %s; using default", key, value, exc)
            settings[key] = _SETTINGS_DEFAULTS.get(key, value)

    for key, default in _SETTINGS_DEFAULTS.items():
        if key not in allowed:
            continue
        settings.setdefault(key, _SETTINGS_TYPES.get(key, str)(default))

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
        for key, raw_value in filtered.items():
            parser = _SETTINGS_TYPES.get(key, str)
            parsed_value = parser(raw_value)
            value = ("true" if parsed_value else "false") if isinstance(parsed_value, bool) else str(parsed_value)

            con.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                (key, value),
            )
        con.commit()
