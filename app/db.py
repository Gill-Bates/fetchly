#!/usr/bin/env python3
#
# app/db.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

import logging
import math
import secrets
import sqlite3
from collections.abc import Callable, Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any, Final

from .utils.credentials import normalize_admin_username
from .utils.duration import round_seconds
from .utils.fs import get_data_dir
from .utils.public_url import normalize_public_hostname

logger = logging.getLogger(__name__)

__all__ = [
    "COMPLETED_STATUSES",
    "DB_PATH",
    "DOWNLOADABLE_STATUSES",
    "TERMINAL_JOB_STATUSES",
    "cancel_interrupted_jobs",
    "claim_next_queued_job",
    "close_db",
    "consume_share_link",
    "create_share_link",
    "delete_jobs_and_share_links",
    "delete_share_links_for_jobs",
    "find_active_job_for_submission",
    "get_audio_analysis_cache",
    "get_db",
    "get_job",
    "get_settings",
    "get_share_link",
    "get_stats",
    "init_db",
    "insert_job",
    "job_exists",
    "list_completed_bpms",
    "list_expired_job_ids",
    "list_job_ids",
    "list_jobs",
    "list_jobs_requiring_audio_analysis",
    "list_queued_jobs",
    "paginate_jobs",
    "set_settings",
    "update_job",
    "update_job_if_status",
    "upsert_audio_analysis_cache",
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

# Bumped only when a schema change needs data migration. init_db() stamps this
# into the file (PRAGMA user_version) and refuses to open a file stamped higher,
# so rolling a deployment back to an older image fails fast instead of silently
# writing against a schema it does not understand.
_SCHEMA_VERSION: Final[int] = 1

# job_id deletions are chunked so a large retention sweep never trips SQLite's
# bound-parameter ceiling (SQLITE_MAX_VARIABLE_NUMBER) or holds one oversized
# write transaction.
_DELETE_BATCH_SIZE: Final[int] = 900
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
_JOB_STATUS_SQL_VALUES: Final[str] = ", ".join(
    f"'{status}'" for status in sorted(_JOB_STATUSES)
)

_SETTINGS_DEFAULTS: Final[dict[str, str]] = {
    # 0 means unlimited: job files are retained until explicitly removed.
    "retention_days": "0",
    "login_required": "false",
    # Off on a fresh install, and no credentials exist to go with it. The admin
    # account is created in Settings -> Security; authentication cannot be
    # switched on before a username and password are stored (see
    # app/routes/api.py::api_set_settings).
    "enable_authentication": "false",
    "admin_username": "",
    "session_idle_minutes": "60",
    # 0 means "Automatic": sized per download from the host's CPU quota and
    # free memory (app/governor.py::recommended_concurrent_fragments).
    "download_concurrent_fragments": "0",
    # Startup-only worker-pool size for the next application boot. 0 means
    # "auto-detect from CPU quota".
    "download_worker_count": "0",
    "download_timeout_minutes": "60",
    "transcode_timeout_minutes": "120",
    "download_max_filesize_gib": "4",
    # Default on: the downloaded file is played back in the browser (player,
    # waveform, trim view), and only H.264/AAC in MP4 plays everywhere -
    # VP9/AV1 renditions do not in Safari/iOS. Users who want the highest
    # resolution over compatibility can turn it off on the settings page.
    "download_mp4_preset": "true",
    # Burns the fetchly logo into the bottom-right corner of every downloaded
    # video, with the public hostname underneath when one is configured. On by
    # default; costs nothing on the capped qualities (the overlay rides along
    # in the transcode that already runs) but adds an encoder pass to "max".
    # See app/utils/watermark.py.
    "video_watermark": "true",
    # 0 disables the duration gate so any track length is analyzed.
    "audio_analysis_max_minutes": "15",
    "audio_analysis_timeout_minutes": "5",
    "lalalaai_email": "",
    "lalalaai_auth_key": "",
    "lalalaai_auth_checked_at": "0",
    "lalalaai_auth_is_valid": "false",
    "lalalaai_auth_last_error": "",
    # Processing minutes the Lalal.ai account had left at the last validation,
    # cached alongside it so the settings tile can name a balance without a
    # request per page view. -1 means "not known yet".
    "lalalaai_minutes_left": "-1",
    "lalalaai_duration_guard": "true",
    "lalal_max_download_gib": "4",
    # How often a generated share link may be used. 0 means unlimited; the
    # value is snapshotted onto each link at creation time so changing it
    # later never retroactively re-opens or closes links already handed out.
    "share_link_max_uses": "0",
    # Public hostname (or IP) that share links are built from. Empty means
    # "use the host of the request that created the link". Set this behind a
    # reverse proxy that does not forward X-Forwarded-Host. See
    # app/utils/public_url.py.
    "public_hostname": "",
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


def _parse_minutes_left(value: object) -> float:
    """Parse a cached Lalal.ai balance, normalizing anything unusable to -1."""
    if isinstance(value, bool):
        raise ValueError("Boolean is not a minutes value")
    parsed = float(str(value).strip())
    if not math.isfinite(parsed) or parsed < 0:
        return -1.0
    return parsed


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

_SETTINGS_TYPES: Final[dict[str, Callable[[object], Any]]] = {
    "retention_days": lambda value: _parse_bounded_int(value, minimum=0, maximum=365),
    "statistics_reset_at": str,
    "login_required": _parse_bool,
    "enable_authentication": _parse_bool,
    "admin_username": lambda value: normalize_admin_username(value if isinstance(value, str) else ""),
    "session_idle_minutes": lambda value: _parse_bounded_int(value, minimum=1, maximum=24 * 60),
    "session_version": _parse_nonnegative_int,
    "download_concurrent_fragments": lambda value: _parse_bounded_int(value, minimum=0, maximum=16),
    "download_worker_count": lambda value: _parse_bounded_int(value, minimum=0, maximum=8),
    "download_timeout_minutes": lambda value: _parse_bounded_int(value, minimum=1, maximum=240),
    "transcode_timeout_minutes": lambda value: _parse_bounded_int(value, minimum=1, maximum=480),
    "download_max_filesize_gib": lambda value: _parse_bounded_int(value, minimum=1, maximum=100),
    "download_mp4_preset": _parse_bool,
    "video_watermark": _parse_bool,
    "audio_analysis_max_minutes": lambda value: _parse_bounded_int(value, minimum=0, maximum=240),
    "audio_analysis_timeout_minutes": lambda value: _parse_bounded_int(value, minimum=1, maximum=60),
    "lalalaai_email": str,
    "lalalaai_auth_key": str,
    "lalalaai_auth_checked_at": _parse_nonnegative_int,
    "lalalaai_auth_is_valid": _parse_bool,
    "lalalaai_minutes_left": _parse_minutes_left,
    "lalalaai_auth_last_error": str,
    "lalalaai_duration_guard": _parse_bool,
    "lalal_max_download_gib": lambda value: _parse_bounded_int(value, minimum=1, maximum=100),
    "share_link_max_uses": lambda value: _parse_bounded_int(value, minimum=0, maximum=10000),
    "public_hostname": lambda value: normalize_public_hostname(value if isinstance(value, str) else ""),
}


# --------------------------------------------------------------------------- #
# Connection management
# --------------------------------------------------------------------------- #
def _configure_connection(con: sqlite3.Connection) -> None:
    con.row_factory = sqlite3.Row
    # Enforce share_links.job_id -> jobs(id) ON DELETE CASCADE. SQLite defaults
    # foreign-key enforcement off and it is per-connection, so it has to be set
    # on every connection before any statement runs.
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("PRAGMA busy_timeout=30000")


def _configure_database(con: sqlite3.Connection) -> None:
    """Configure persistent database pragmas (run once at startup)."""
    row = con.execute("PRAGMA journal_mode=WAL").fetchone()
    mode = str(row[0]).lower() if row else ""
    if mode != "wal":
        raise RuntimeError(f"SQLite WAL mode unavailable; active journal mode is {mode!r}")


_database_path_prepared: str | None = None


def _prepare_database_path() -> None:
    """Create the database file and its directory with owner-only permissions.

    Memoised per resolved path: the mkdir/chmod/touch/chmod sequence runs once
    per process instead of on every get_db() call (which also stops Path.touch()
    from bumping the DB file's mtime on every read and waking file watchers).
    It re-runs only if DB_PATH is repointed, which the tests do. Deliberately
    lock-free - every step is idempotent, so a startup race just repeats
    harmless work.
    """
    global _database_path_prepared
    target = str(DB_PATH)
    if _database_path_prepared == target:
        return
    DB_PATH.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    DB_PATH.parent.chmod(0o700)
    DB_PATH.touch(exist_ok=True, mode=0o600)
    DB_PATH.chmod(0o600)
    _database_path_prepared = target


@contextmanager
def get_db() -> Generator[sqlite3.Connection]:
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
    """Checkpoint the WAL and truncate it on shutdown so the on-disk database is
    self-contained after a graceful stop or ``docker restart`` (best-effort).

    Called from the FastAPI lifespan shutdown (app/main.py) after the workers
    have stopped, so no other connection should hold the WAL at this point.
    """
    if not DB_PATH.exists():
        return
    con: sqlite3.Connection | None = None
    try:
        con = sqlite3.connect(DB_PATH)
        _configure_connection(con)
        # row = (busy, wal_pages, checkpointed_pages); busy != 0 means another
        # connection still held the WAL so the -wal file was left for the next
        # start to replay - safe for durability, but worth surfacing.
        row = con.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if row is not None and row[0] != 0:
            logger.warning("WAL checkpoint on shutdown was incomplete: %s", tuple(row))
        else:
            logger.debug("WAL checkpoint on shutdown complete")
    except Exception as exc:
        logger.warning("WAL checkpoint failed: %s", exc)
    finally:
        if con is not None:
            con.close()


# --------------------------------------------------------------------------- #
# Schema – idempotent initial creation
# --------------------------------------------------------------------------- #
def init_db() -> None:
    """Create the database schema for a fresh deployment and stamp its version.

    There is no versioned migration runner: the project ships a single schema
    and every table is created with ``IF NOT EXISTS``. The only cross-version
    safety here is the ``PRAGMA user_version`` guard below, which stops an older
    build from opening a database a newer build has already written.
    """
    with get_db() as con:
        # Configure persistent database pragmas
        _configure_database(con)

        schema_version = int(con.execute("PRAGMA user_version").fetchone()[0])
        if schema_version > _SCHEMA_VERSION:
            raise RuntimeError(
                f"jobs.db was written by a newer Fetchly (schema v{schema_version}; "
                f"this build supports v{_SCHEMA_VERSION}). Refusing to start so a "
                "rolled-back deployment cannot write against a schema it does not "
                "understand."
            )

        con.execute(f"""
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                url TEXT NOT NULL,
                type TEXT,
                quality TEXT,
                status TEXT NOT NULL CHECK (
                    status IN ({_JOB_STATUS_SQL_VALUES})
                ),
                filename TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                finished_at TEXT,
                duration_seconds REAL CHECK (duration_seconds IS NULL OR duration_seconds >= 0),
                filesize_bytes INTEGER CHECK (filesize_bytes IS NULL OR filesize_bytes >= 0),
                message TEXT,
                codec TEXT,
                bitrate_kbps INTEGER CHECK (bitrate_kbps IS NULL OR bitrate_kbps >= 0),
                video_title TEXT,
                video_meta_hover TEXT,
                bpm INTEGER CHECK (bpm IS NULL OR bpm > 0),
                bpm_confidence REAL CHECK (bpm_confidence IS NULL OR bpm_confidence >= 0),
                audio_hash TEXT,
                lalal_split_done INTEGER NOT NULL DEFAULT 0 CHECK (lalal_split_done IN (0, 1))
            )
        """)

        con.execute("""
            CREATE TABLE IF NOT EXISTS audio_analysis_cache (
                hash TEXT PRIMARY KEY,
                bpm INTEGER CHECK (bpm IS NULL OR bpm > 0),
                bpm_confidence REAL CHECK (bpm_confidence IS NULL OR bpm_confidence >= 0),
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)

        con.execute("""
            CREATE TABLE IF NOT EXISTS share_links (
                token TEXT PRIMARY KEY,
                job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                max_uses INTEGER NOT NULL DEFAULT 0 CHECK (max_uses >= 0),
                use_count INTEGER NOT NULL DEFAULT 0 CHECK (use_count >= 0)
            )
        """)

        con.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)

        # One-off normalisation: earlier builds wrote finished_at via
        # datetime.isoformat() ("...T...+00:00" with microseconds), which does
        # not sort lexically against the "YYYY-MM-DD HH:MM:SS" form the rest of
        # the code (and CURRENT_TIMESTAMP) uses. Rewrite the stragglers so the
        # retention/stats queries below can range-scan finished_at directly
        # instead of wrapping it in datetime(). No-op once every row is
        # canonical, so it is safe to run on every start.
        con.execute(
            "UPDATE jobs SET finished_at = strftime('%Y-%m-%d %H:%M:%S', finished_at) "
            "WHERE finished_at IS NOT NULL AND finished_at LIKE '%T%'"
        )

        con.execute("CREATE INDEX IF NOT EXISTS idx_jobs_created_at_id ON jobs(created_at DESC, id DESC)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_jobs_status_finished_at ON jobs(status, finished_at)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_jobs_status_created_at ON jobs(status, created_at DESC)")
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_jobs_audio_analysis "
            "ON jobs(type, status, created_at) WHERE filename IS NOT NULL"
        )
        con.execute("CREATE INDEX IF NOT EXISTS idx_jobs_url_type_quality ON jobs(url, type, quality, created_at DESC)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_share_links_job_id ON share_links(job_id, created_at DESC)")
        # Redundant with the composite indexes above: every query that filters
        # `status` also constrains a column covered by (status, ...); retention
        # and stats always filter `status` alongside `finished_at`; and no query
        # touches `created_at` without also ordering by it. Dropped where an
        # older database still carries them.
        con.execute("DROP INDEX IF EXISTS idx_jobs_status")
        con.execute("DROP INDEX IF EXISTS idx_jobs_finished_at")
        con.execute("DROP INDEX IF EXISTS idx_jobs_created_at")

        if schema_version != _SCHEMA_VERSION:
            con.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")

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
    duration_seconds: float | None = None,
) -> None:
    """Insert a queued job row.

    ``duration_seconds`` is the runtime the source reported at submit time, so
    the job list can show a length while the download is still running. The
    worker overwrites it with the ffprobe reading of the finished file.
    """
    _validate_status(status)
    # The column's CHECK constraint rejects negatives, and an unusable reading
    # is stored as "unknown" rather than aborting the submission.
    duration = round_seconds(duration_seconds)
    if duration is None or duration <= 0:
        duration = None
    with get_db() as con:
        con.execute(
            """
            INSERT INTO jobs (id, url, type, quality, status, video_title, video_meta_hover, duration_seconds)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (job_id, url, job_type, quality, status, video_title, video_meta_hover, duration),
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
        cursor = con.execute(f"UPDATE jobs SET {keys} WHERE id=?", values)  # noqa: S608  # identifiers are allow-listed above; values stay bound
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
            f"UPDATE jobs SET {keys} WHERE id=? AND status IN ({placeholders})",  # noqa: S608  # identifiers are allow-listed above; values stay bound
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
            """,  # noqa: S608  # identifiers are allow-listed above; values stay bound
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
            """,  # noqa: S608  # identifiers are allow-listed above; values stay bound
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
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """,
            (url, job_type, quality),
        ).fetchone()


def create_share_link(job_id: str, max_uses: int) -> str:
    """Return a share token for *job_id*, reusing a still-usable one if present.

    Reuse keeps repeated "Share" clicks on the same job from both littering the
    table and burning quota on links the user never handed out. A link is only
    reused when its snapshotted ``max_uses`` still matches the current setting,
    so changing the setting always yields a fresh link under the new limit.
    """
    if isinstance(max_uses, bool) or not isinstance(max_uses, int) or max_uses < 0:
        raise ValueError("max_uses must be a non-negative integer")

    with get_db() as con:
        existing = con.execute(
            """
            SELECT token FROM share_links
            WHERE job_id = ?
              AND max_uses = ?
              AND (max_uses = 0 OR use_count < max_uses)
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (job_id, max_uses),
        ).fetchone()
        if existing is not None:
            return str(existing["token"])

        # 6 random bytes render as exactly 8 URL-safe characters (48 bits).
        # Short enough to paste into a chat, and brute-forcing it is bounded by
        # the SlowAPI rate limit on the redeem route rather than by length.
        # Retry on the (vanishingly rare) primary-key collision instead of
        # letting the insert raise.
        for _ in range(8):
            token = secrets.token_urlsafe(6)
            try:
                con.execute(
                    "INSERT INTO share_links (token, job_id, max_uses) VALUES (?, ?, ?)",
                    (token, job_id, max_uses),
                )
            except sqlite3.IntegrityError:
                continue
            con.commit()
            return token
        raise RuntimeError("Could not allocate a unique share token")


def get_share_link(token: str) -> sqlite3.Row | None:
    with get_db() as con:
        return con.execute(
            "SELECT * FROM share_links WHERE token = ?", (token,)
        ).fetchone()


def consume_share_link(token: str) -> bool:
    """Atomically count one use of *token*.

    Returns False when the token is unknown or its use limit is exhausted. The
    guard lives in the UPDATE's WHERE clause so two concurrent requests cannot
    both read a below-limit count and then both write.
    """
    with get_db() as con:
        cursor = con.execute(
            """
            UPDATE share_links
            SET use_count = use_count + 1
            WHERE token = ? AND (max_uses = 0 OR use_count < max_uses)
            """,
            (token,),
        )
        con.commit()
        return cursor.rowcount > 0


def delete_share_links_for_jobs(job_ids: list[str]) -> int:
    """Drop share links for jobs whose artifacts housekeeping removed.

    Chunked so a large retention sweep cannot exceed SQLite's bound-parameter
    limit or hold one oversized write transaction. (Fresh databases also get
    ON DELETE CASCADE from share_links.job_id, but retention only deletes files,
    not the job rows, so this explicit cleanup is still required.)
    """
    if not job_ids:
        return 0
    deleted = 0
    with get_db() as con:
        for start in range(0, len(job_ids), _DELETE_BATCH_SIZE):
            batch = job_ids[start:start + _DELETE_BATCH_SIZE]
            placeholders = ",".join("?" * len(batch))
            cursor = con.execute(
                f"DELETE FROM share_links WHERE job_id IN ({placeholders})",  # noqa: S608  # identifiers are allow-listed above; values stay bound
                tuple(batch),
            )
            deleted += max(cursor.rowcount, 0)
        con.commit()
    return deleted


def list_job_ids() -> list[str]:
    """Return a stable snapshot of every job ID for an explicit bulk removal."""
    with get_db() as con:
        rows = con.execute("SELECT id FROM jobs").fetchall()
    return [str(row["id"]) for row in rows]


def delete_jobs_and_share_links(job_ids: list[str]) -> tuple[int, int]:
    """Delete selected jobs and invalidate their share links atomically.

    The explicit link deletion supports databases created before the foreign-key
    cascade was added. Callers clean the corresponding filesystem artifacts
    before invoking this function, so an unsuccessful cleanup leaves the job
    records and their links intact for a safe retry.
    """
    if not job_ids:
        return (0, 0)

    job_ids = list(dict.fromkeys(job_ids))
    jobs_deleted = 0
    links_deleted = 0
    with get_db() as con:
        for start in range(0, len(job_ids), _DELETE_BATCH_SIZE):
            batch = job_ids[start:start + _DELETE_BATCH_SIZE]
            placeholders = ",".join("?" * len(batch))
            links_cursor = con.execute(
                f"DELETE FROM share_links WHERE job_id IN ({placeholders})",  # noqa: S608  # identifiers are allow-listed above; values stay bound
                tuple(batch),
            )
            jobs_cursor = con.execute(
                f"DELETE FROM jobs WHERE id IN ({placeholders})",  # noqa: S608  # identifiers are allow-listed above; values stay bound
                tuple(batch),
            )
            links_deleted += max(links_cursor.rowcount, 0)
            jobs_deleted += max(jobs_cursor.rowcount, 0)
        con.commit()
    return (jobs_deleted, links_deleted)


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
    """Offset-based pagination for infinite scroll.

    ``id`` is a deterministic tie-breaker: ``created_at`` only has one-second
    resolution, so without it two jobs sharing a timestamp can order
    differently between requests and land on two pages or none. It does not
    fix the inherent offset-drift when rows are inserted mid-scroll - the
    client de-duplicates by job id for that - but it makes each page stable.
    """
    limit = _validate_limit(limit)
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise ValueError("offset must be a non-negative integer")
    with get_db() as con:
        return con.execute(
            "SELECT * FROM jobs ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()


def get_stats() -> dict[str, int | float]:
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
            # finished_at is stored canonically ("YYYY-MM-DD HH:MM:SS"), so the
            # column is compared directly - no datetime() wrapper - to let the
            # planner range-scan idx_jobs_status_finished_at. The bound value is
            # normalised once via datetime() in case an operator ever hand-edits
            # statistics_reset_at into another accepted format.
            reset_clause = " AND finished_at > datetime(?)"
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
        """, query_params).fetchone()  # noqa: S608  # identifiers are allow-listed above; values stay bound

    return {
        "total_jobs": row["total_jobs"] or 0,
        "total_minutes": round(float(row["total_minutes"] or 0), 1),
        "total_bytes": row["total_bytes"] or 0,
        "total_lalal_minutes": round(float(row["total_lalal_minutes"] or 0), 1),
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
    if keep_days == 0:
        return []

    statuses = tuple(sorted(TERMINAL_JOB_STATUSES))
    placeholders = _in_placeholders(statuses)
    with get_db() as con:
        rows = con.execute(
            f"""
            SELECT id
            FROM jobs
            WHERE status IN ({placeholders})
              AND finished_at IS NOT NULL
              AND finished_at < datetime('now', '-' || ? || ' days')
            """,  # noqa: S608  # identifiers are allow-listed above; values stay bound
            (*statuses, keep_days),
        ).fetchall()
    return [row["id"] for row in rows]


def purge_old_jobs(keep_days: int, *, batch_size: int = _MAX_QUERY_LIMIT) -> list[str]:
    """Delete terminal jobs older than *keep_days*, in bounded batches.

    Returns deleted IDs so callers can clean up filesystem artifacts. Fresh
    databases cascade the delete to share_links; older ones rely on the caller
    also invoking delete_share_links_for_jobs().

    Not currently wired into housekeeping - retention is deliberately read-only
    at the database level (see list_expired_job_ids) - but kept correct and
    batch-bounded for callers that opt into hard deletion.
    """
    if isinstance(keep_days, bool) or not isinstance(keep_days, int):
        raise TypeError("keep_days must be a non-negative integer")
    if keep_days < 0:
        raise ValueError("keep_days must be non-negative")
    batch_size = _validate_limit(batch_size)

    statuses = tuple(sorted(TERMINAL_JOB_STATUSES))
    placeholders = _in_placeholders(statuses)
    deleted: list[str] = []
    with get_db() as con:
        while True:
            rows = con.execute(
                f"""
                DELETE FROM jobs
                WHERE id IN (
                    SELECT id
                    FROM jobs
                    WHERE status IN ({placeholders})
                      AND finished_at IS NOT NULL
                      AND finished_at < datetime('now', '-' || ? || ' days')
                    LIMIT ?
                )
                RETURNING id
                """,  # noqa: S608  # identifiers are allow-listed above; values stay bound
                (*statuses, keep_days, batch_size),
            ).fetchall()
            con.commit()
            if not rows:
                break
            deleted.extend(row["id"] for row in rows)
            if len(rows) < batch_size:
                break
    return deleted


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
            default_value = _SETTINGS_DEFAULTS.get(key, value)
            settings[key] = parser(default_value)

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
