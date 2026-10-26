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

from .auth.credentials import normalize_admin_username
from .utils.boolean import parse_bool_text
from .utils.duration import round_seconds
from .utils.fs import get_data_dir
from .utils.public_url import normalize_public_hostname

logger = logging.getLogger(__name__)

# Shared with app/auth/session.py and app/routes/api.py so the allowed range for
# session_max_days is defined once. Lives here, not in auth/session.py, because
# auth/session.py already imports get_settings from this module - the reverse
# import would be circular.
SESSION_MAX_DAYS_MIN: Final = 1
SESSION_MAX_DAYS_MAX: Final = 7

__all__ = [
    "ACTIVE_JOB_STATUSES",
    "CANCELLABLE_JOB_STATUSES",
    "COMPLETED_STATUSES",
    "DB_PATH",
    "DOWNLOADABLE_STATUSES",
    "DOWNLOAD_OUTPUT_MODES",
    "PROGRESS_STATUSES",
    "RETRYABLE_JOB_STATUSES",
    "SETTING_INT_BOUNDS",
    "TERMINAL_JOB_STATUSES",
    "WORKER_OWNED_STATUSES",
    "DatabaseSchemaError",
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
    "list_active_jobs",
    "list_completed_bpms",
    "list_expired_job_ids",
    "list_job_ids",
    "list_jobs",
    "list_jobs_requiring_audio_analysis",
    "list_queued_jobs",
    "now_iso",
    "paginate_jobs",
    "set_settings",
    "set_settings_and_bump_session_version",
    "setting_default",
    "update_job",
    "update_job_if_status",
    "upsert_audio_analysis_cache",
    "utc_timestamp",
    "with_finished_at",
]

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
_JOB_STATUS_SQL_VALUES: Final[str] = ", ".join(f"'{status}'" for status in sorted(_JOB_STATUSES))

# Allowed values of the "download_output_mode" setting, in the order the
# settings slider presents them. app/workers/worker.py maps each to a format-selection
# preference and a post-processing target; app/static/js/settings.js mirrors
# this list for the slider ticks.
DOWNLOAD_OUTPUT_MODES: Final[tuple[str, ...]] = ("source", "universal", "av1")

_SETTINGS_DEFAULTS: Final[dict[str, str]] = {
    # 0 means unlimited: job files are retained until explicitly removed.
    "retention_days": "0",
    # New jobs are listed in the dashboard history unless this is disabled.
    # Each job snapshots the value at submission time, so changing it never
    # changes the visibility of an existing job.
    "enable_job_history": "true",
    # On by default: a finished download starts automatically in the browser
    # instead of waiting for the user to click Download. Applies to newly
    # submitted jobs only, mirroring enable_job_history above.
    "download_auto_start": "true",
    "login_required": "false",
    # Off on a fresh install, and no credentials exist to go with it. The admin
    # account is created in Settings -> Security; authentication cannot be
    # switched on before a username and password are stored (see
    # app/routes/api.py::api_set_settings).
    "enable_authentication": "false",
    "admin_username": "",
    # Absolute session lifetime in days, counted from login. The session is
    # invalidated once it elapses - there is no sliding renewal past it.
    "session_max_days": "7",
    # 0 means "Automatic": sized per download from the host's CPU quota and
    # free memory (app/runtime/governor.py::recommended_concurrent_fragments).
    "download_concurrent_fragments": "0",
    # Startup-only worker-pool size for the next application boot. 0 means
    # "auto-detect from CPU quota".
    "download_worker_count": "0",
    "download_timeout_minutes": "60",
    "transcode_timeout_minutes": "120",
    "download_max_filesize_gib": "4",
    # What the finished "max" download is muxed or encoded into. One of
    # DOWNLOAD_OUTPUT_MODES:
    #   "source"    - the source rendition is passed through untouched: best
    #                 codec regardless of type, muxed (losslessly) into
    #                 whatever container fits (.webm for VP9/Opus, .mkv for
    #                 AV1). No encoder runs unless the watermark forces one
    #                 (see below).
    #   "universal" - H.264/AAC in MP4, which plays on Safari, iOS, TVs and in
    #                 editing software. Preferred at format-selection time, so
    #                 a source that already offers it is never re-encoded.
    #   "av1"       - AV1 video, preferred at format-selection time; a source
    #                 that already is AV1 is never re-encoded.
    # The watermark burns an overlay in, which always costs a video encode, but
    # that encode targets the mode's own codec - for "source" the codec the
    # download arrived in - so the watermark never changes the stored mode's
    # promise. See app/workers/worker.py::_output_mode and ::_target_video_codec.
    "download_output_mode": "universal",
    # Burns the fetchly logo into the bottom-right corner of every downloaded
    # video, with the public hostname underneath when one is configured. Off by
    # default; costs nothing on the capped qualities (the overlay rides along
    # in the transcode that already runs) but adds an encoder pass to "max".
    # See app/utils/watermark.py.
    "video_watermark": "false",
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
    parsed = parse_bool_text(str(value))
    if parsed is None:
        raise ValueError(f"Invalid boolean value: {value!r}")
    return parsed


def _parse_bounded_int(value: object, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        raise ValueError("Boolean is not an integer setting")
    parsed = int(str(value).strip())
    if not minimum <= parsed <= maximum:
        raise ValueError(f"Integer setting outside allowed range: {parsed}")
    return parsed


def _parse_nonnegative_int(value: object) -> int:
    return _parse_bounded_int(value, minimum=0, maximum=2**63 - 1)


def _parse_output_mode(value: object) -> str:
    """Parse the "download_output_mode" setting against its allowed vocabulary."""
    normalized = str(value).strip().lower()
    if normalized not in DOWNLOAD_OUTPUT_MODES:
        raise ValueError(f"Invalid download output mode: {value!r}")
    return normalized


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


def to_iso_utc(value: object) -> str | None:
    """Return a stored timestamp as ISO-8601 UTC with a ``Z`` suffix.

    Stored values are naive ``YYYY-MM-DD HH:MM:SS`` text that is UTC by
    convention, so a missing offset is read as UTC; an explicit offset is
    converted. Empty or unparseable input returns ``None``.
    """
    if value is None or value == "":
        return None
    try:
        parsed = datetime.fromisoformat(str(value).strip())
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")


def now_iso() -> str:
    """Return the current UTC timestamp as ISO-8601 with an offset.

    For status-event payloads only (informational, never persisted) - stored
    timestamps always go through utc_timestamp() above, whose plain
    "YYYY-MM-DD HH:MM:SS" format sorts correctly as text in SQLite. Mixing the
    two formats in a stored column previously caused finished_at to sort
    incorrectly; keep them distinct and never persist this one.
    """
    return datetime.now(UTC).isoformat()


_INTERNAL_SETTINGS_KEYS: Final[frozenset[str]] = frozenset(
    {
        "admin_password_hash",
        "session_version",
        "statistics_reset_at",
    }
)
_SECRET_SETTINGS_KEYS: Final[frozenset[str]] = frozenset({"lalalaai_auth_key"})
# User-writable keys only; internal keys need allow_internal=True in set_settings.
_ALLOWED_SETTINGS_KEYS: Final[frozenset[str]] = frozenset(_SETTINGS_DEFAULTS)

# Statuses that count as "completed" for downloads / stats purposes.
COMPLETED_STATUSES: Final[frozenset[str]] = frozenset({"done", "analysis_done"})
# Downloaded audio may be served while its non-terminal BPM analysis is pending.
DOWNLOADABLE_STATUSES: Final[frozenset[str]] = COMPLETED_STATUSES | frozenset({"analysis"})
# All terminal statuses (no further transitions possible).
TERMINAL_JOB_STATUSES: Final[frozenset[str]] = COMPLETED_STATUSES | frozenset({"error", "cancelled"})
# Every non-terminal status: the job is still queued or being worked on.
ACTIVE_JOB_STATUSES: Final[frozenset[str]] = _JOB_STATUSES - TERMINAL_JOB_STATUSES
# The statuses a worker thread owns: while the row holds one of these, that
# thread is the only writer, so a conditional update on them tells "still my
# job" from "cancelled or retried in the meantime". Also what a restart strands.
WORKER_OWNED_STATUSES: Final[tuple[str, ...]] = (
    "processing",
    "downloading",
    "transcoding",
)
# A job can be cancelled before a worker ever picks it up, or while one owns it.
CANCELLABLE_JOB_STATUSES: Final[tuple[str, ...]] = ("queued", *WORKER_OWNED_STATUSES)
# Terminal without a result: the user may submit the job again.
RETRYABLE_JOB_STATUSES: Final[frozenset[str]] = TERMINAL_JOB_STATUSES - COMPLETED_STATUSES
# Statuses whose SSE events carry a live progress percentage; any other status
# drops it. app/workers/worker.py::_PROGRESS_BEARING_STATUSES is the writer and
# mirrors this set (it may not import a new db constant, so the agreement is
# guarded by tests/test_status_mapping_parity.py).
PROGRESS_STATUSES: Final[frozenset[str]] = frozenset({"downloading", "transcoding"})


def with_finished_at(status: str, extra: dict[str, Any]) -> dict[str, Any]:
    """Add ``finished_at`` to *extra* when *status* is terminal.

    Centralizes the invariant "a terminal status write must carry a
    finished_at timestamp" so worker modules do not each have to remember it
    per call site. A caller-supplied ``finished_at`` is left untouched.
    """
    if status in TERMINAL_JOB_STATUSES and "finished_at" not in extra:
        return {**extra, "finished_at": utc_timestamp()}
    return extra


# Inclusive (minimum, maximum) of every integer setting. The settings API
# validates against this table and the stored-value parsers are built from it.
SETTING_INT_BOUNDS: Final[dict[str, tuple[int, int]]] = {
    "retention_days": (0, 365),
    "session_max_days": (SESSION_MAX_DAYS_MIN, SESSION_MAX_DAYS_MAX),
    "download_concurrent_fragments": (0, 16),
    "download_worker_count": (0, 8),
    "download_timeout_minutes": (1, 240),
    "transcode_timeout_minutes": (1, 480),
    "download_max_filesize_gib": (1, 100),
    "audio_analysis_max_minutes": (0, 240),
    "audio_analysis_timeout_minutes": (1, 60),
    "lalal_max_download_gib": (1, 100),
    "share_link_max_uses": (0, 10000),
}


def _bounded_int_parser(key: str) -> Callable[[object], int]:
    minimum, maximum = SETTING_INT_BOUNDS[key]
    return lambda value: _parse_bounded_int(value, minimum=minimum, maximum=maximum)


_SETTINGS_TYPES: Final[dict[str, Callable[[object], Any]]] = {
    "retention_days": _bounded_int_parser("retention_days"),
    "enable_job_history": _parse_bool,
    "download_auto_start": _parse_bool,
    "statistics_reset_at": str,
    "login_required": _parse_bool,
    "enable_authentication": _parse_bool,
    "admin_username": lambda value: normalize_admin_username(value if isinstance(value, str) else ""),
    "session_max_days": _bounded_int_parser("session_max_days"),
    "session_version": _parse_nonnegative_int,
    "download_concurrent_fragments": _bounded_int_parser("download_concurrent_fragments"),
    "download_worker_count": _bounded_int_parser("download_worker_count"),
    "download_timeout_minutes": _bounded_int_parser("download_timeout_minutes"),
    "transcode_timeout_minutes": _bounded_int_parser("transcode_timeout_minutes"),
    "download_max_filesize_gib": _bounded_int_parser("download_max_filesize_gib"),
    "download_output_mode": _parse_output_mode,
    "video_watermark": _parse_bool,
    "audio_analysis_max_minutes": _bounded_int_parser("audio_analysis_max_minutes"),
    "audio_analysis_timeout_minutes": _bounded_int_parser("audio_analysis_timeout_minutes"),
    "lalalaai_email": str,
    "lalalaai_auth_key": str,
    "lalalaai_auth_checked_at": _parse_nonnegative_int,
    "lalalaai_auth_is_valid": _parse_bool,
    "lalalaai_minutes_left": _parse_minutes_left,
    "lalalaai_auth_last_error": str,
    "lalalaai_duration_guard": _parse_bool,
    "lalal_max_download_gib": _bounded_int_parser("lalal_max_download_gib"),
    "share_link_max_uses": _bounded_int_parser("share_link_max_uses"),
    "public_hostname": lambda value: normalize_public_hostname(value if isinstance(value, str) else ""),
}


def setting_default(key: str) -> Any:
    """The parsed default of a setting, for callers that must fall back when the table is unreadable."""
    return _SETTINGS_TYPES.get(key, str)(_SETTINGS_DEFAULTS[key])


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
    # Shutdown checkpoint failure must not mask shutdown.
    except Exception as exc:  # noqa: BLE001
        logger.warning("WAL checkpoint failed: %s", exc)
    finally:
        if con is not None:
            con.close()


# SQLITE_CORRUPT and SQLITE_NOTADB (primary result codes).
_UNREADABLE_SQLITE_CODES: Final[frozenset[int]] = frozenset({11, 26})
_MAX_LISTED_MISSING_COLUMNS: Final = 5


class DatabaseSchemaError(RuntimeError):
    """The existing database file does not match the schema this release expects."""


def _create_tables(con: sqlite3.Connection) -> None:
    con.execute(
        f"""
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
            include_in_history INTEGER NOT NULL DEFAULT 1 CHECK (include_in_history IN (0, 1)),
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
            lalal_split_done INTEGER NOT NULL DEFAULT 0 CHECK (lalal_split_done IN (0, 1)),
            client_ip TEXT
        )
    """
    )

    con.execute(
        """
        CREATE TABLE IF NOT EXISTS audio_analysis_cache (
            hash TEXT PRIMARY KEY,
            bpm INTEGER CHECK (bpm IS NULL OR bpm > 0),
            bpm_confidence REAL CHECK (bpm_confidence IS NULL OR bpm_confidence >= 0),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """
    )

    con.execute(
        """
        CREATE TABLE IF NOT EXISTS share_links (
            token TEXT PRIMARY KEY,
            job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            max_uses INTEGER NOT NULL DEFAULT 0 CHECK (max_uses >= 0),
            use_count INTEGER NOT NULL DEFAULT 0 CHECK (use_count >= 0)
        )
    """
    )

    con.execute(
        """
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """
    )


def _create_indexes(con: sqlite3.Connection) -> None:
    con.execute("CREATE INDEX IF NOT EXISTS idx_jobs_created_at_id ON jobs(created_at DESC, id DESC)")
    con.execute(
        "CREATE INDEX IF NOT EXISTS idx_jobs_history_created_at "
        "ON jobs(created_at DESC, id DESC) WHERE include_in_history = 1"
    )
    con.execute("CREATE INDEX IF NOT EXISTS idx_jobs_status_finished_at ON jobs(status, finished_at)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_jobs_status_created_at ON jobs(status, created_at DESC)")
    con.execute(
        "CREATE INDEX IF NOT EXISTS idx_jobs_audio_analysis "
        "ON jobs(type, status, created_at) WHERE filename IS NOT NULL"
    )
    con.execute("CREATE INDEX IF NOT EXISTS idx_jobs_url_type_quality ON jobs(url, type, quality, created_at DESC)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_share_links_job_id ON share_links(job_id, created_at DESC)")


def _table_columns(con: sqlite3.Connection) -> dict[str, list[str]]:
    tables = [
        r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")
    ]
    return {t: [r[1] for r in con.execute(f'PRAGMA table_info("{t}")')] for t in tables}


def _verify_schema(con: sqlite3.Connection) -> None:
    """Fail with an actionable error when an existing database predates this schema.

    The expected columns come from the CREATE statements themselves (applied to
    a throwaway in-memory database), so they cannot drift from ``_create_tables``.
    Only tables already present are checked (a missing one is simply created
    afterwards); extra tables or columns in the file are tolerated.
    """
    reference = sqlite3.connect(":memory:")
    try:
        _create_tables(reference)
        expected = _table_columns(reference)
    finally:
        reference.close()
    actual = _table_columns(con)
    problems = [
        f"{table}.{column}"
        for table, columns in expected.items()
        if table in actual
        for column in columns
        if column not in actual[table]
    ]
    if problems:
        shown = ", ".join(problems[:_MAX_LISTED_MISSING_COLUMNS])
        if len(problems) > _MAX_LISTED_MISSING_COLUMNS:
            shown += f", ... and {len(problems) - _MAX_LISTED_MISSING_COLUMNS} more"
        raise DatabaseSchemaError(
            "The schema of the existing database does not match this version of fetchly: "
            f"missing {shown}. Databases from older releases are no longer migrated. "
            f"{_remove_database_hint()}"
        )


def _remove_database_hint() -> str:
    return (
        f"Database file: {DB_PATH.resolve()}. "
        "Remove the database file (jobs.db, including jobs.db-wal and jobs.db-shm) and restart the app; "
        "it will be recreated. Note: jobs and settings are lost - back them up first if needed."
    )


def _is_unreadable_database(exc: sqlite3.DatabaseError) -> bool:
    """True for "not a database"/corruption, not for locked, busy or I/O errors."""
    if isinstance(exc, sqlite3.OperationalError):
        return False
    code = getattr(exc, "sqlite_errorcode", None)
    if isinstance(code, int):
        return (code & 0xFF) in _UNREADABLE_SQLITE_CODES
    message = str(exc).lower()
    return "file is not a database" in message or "malformed" in message


def init_db() -> None:
    """Create the schema on a fresh deployment and verify an existing one.

    Every table and index is created with ``IF NOT EXISTS``. There is no
    migration path: a database written by an older release is not upgraded and
    raises :class:`DatabaseSchemaError` instead of failing later in SQL. A file
    that is not a SQLite database or is corrupt raises it too; it is never
    modified or removed automatically.
    """
    try:
        with get_db() as con:
            _configure_database(con)
            # Verify before creating anything, so a rejected database is left untouched.
            _verify_schema(con)
            _create_tables(con)
            _create_indexes(con)
            con.commit()
    except sqlite3.DatabaseError as exc:
        if not _is_unreadable_database(exc):
            raise
        raise DatabaseSchemaError(
            f"The database file is not a valid SQLite database ({exc}). {_remove_database_hint()}"
        ) from exc


def insert_job(
    job_id: str,
    url: str,
    job_type: str,
    quality: str,
    status: str,
    video_title: str | None = None,
    video_meta_hover: str | None = None,
    duration_seconds: float | None = None,
    include_in_history: bool = True,
    client_ip: str | None = None,
) -> None:
    """Insert a queued job row.

    ``duration_seconds`` is the runtime the source reported at submit time, so
    the job list can show a length while the download is still running. The
    worker overwrites it with the ffprobe reading of the finished file.

    ``client_ip`` is the requester's IP as resolved by
    ``app.auth.rate_limit.get_rate_limit_ip`` at submission time (already
    proxy-aware); stored as-is and never updated afterwards. Country/ASN are
    not persisted - they are resolved from this address at render time via
    ``app.utils.geoip``.
    """
    _insert_job_row(
        job_id,
        url,
        job_type,
        quality,
        status,
        video_title=video_title,
        video_meta_hover=video_meta_hover,
        duration_seconds=duration_seconds,
        include_in_history=include_in_history,
        client_ip=client_ip,
    )


def _insert_job_row(
    job_id: str,
    url: str,
    job_type: str,
    quality: str,
    status: str,
    *,
    video_title: str | None,
    video_meta_hover: str | None,
    duration_seconds: float | None,
    include_in_history: bool,
    client_ip: str | None,
    unless_duplicate: bool = False,
) -> sqlite3.Row | None:
    """Insert one job row; with *unless_duplicate*, check and insert atomically.

    The duplicate lookup and the INSERT share one ``BEGIN IMMEDIATE`` write
    transaction, so two concurrent submitters cannot both pass the check. Returns
    the blocking job when the insert was skipped, otherwise ``None``.
    """
    _validate_status(status)
    # The column's CHECK constraint rejects negatives, and an unusable reading
    # is stored as "unknown" rather than aborting the submission.
    duration = round_seconds(duration_seconds)
    if duration is None or duration <= 0:
        duration = None
    with get_db() as con:
        if unless_duplicate:
            con.execute("BEGIN IMMEDIATE")
            existing = _find_active_duplicate(con, url, job_type, quality)
            if existing is not None:
                con.rollback()
                return existing
        con.execute(
            """
            INSERT INTO jobs (
                id, url, type, quality, status, video_title, video_meta_hover,
                duration_seconds, include_in_history, client_ip
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job_id,
                url,
                job_type,
                quality,
                status,
                video_title,
                video_meta_hover,
                duration,
                int(include_in_history),
                client_ip,
            ),
        )
        con.commit()
    return None


def insert_job_unless_active_duplicate(
    job_id: str,
    url: str,
    job_type: str,
    quality: str,
    status: str,
    *,
    allow_duplicate: bool = False,
    video_title: str | None = None,
    video_meta_hover: str | None = None,
    duration_seconds: float | None = None,
    include_in_history: bool = True,
    client_ip: str | None = None,
) -> sqlite3.Row | None:
    """Insert a job unless an equivalent one is already active or finished.

    Returns ``None`` when the row was inserted, or the existing job (see
    :func:`find_active_job_for_submission`) when the insert was refused.
    ``allow_duplicate`` skips the check, for an explicitly confirmed duplicate.
    """
    return _insert_job_row(
        job_id,
        url,
        job_type,
        quality,
        status,
        video_title=video_title,
        video_meta_hover=video_meta_hover,
        duration_seconds=duration_seconds,
        include_in_history=include_in_history,
        client_ip=client_ip,
        unless_duplicate=not allow_duplicate,
    )


def _job_assignments(fields: dict[str, Any]) -> tuple[str, list[Any]]:
    """Validate *fields* and return the ``SET`` clause with its bound values.

    Column names are allow-listed here, which is what makes it safe to put
    them into the SQL text; values always stay bound.
    """
    unknown_fields = set(fields) - _UPDATEABLE_COLUMNS
    if unknown_fields:
        raise ValueError(f"Unknown fields: {sorted(unknown_fields)}")
    if not all(column.isidentifier() for column in fields):
        raise ValueError("Invalid column name")
    if "status" in fields:
        _validate_status(fields["status"])
    return ", ".join(f"{k}=?" for k in fields), list(fields.values())


def update_job(job_id: str, **fields: Any) -> bool:
    if not fields:
        return False

    keys, values = _job_assignments(fields)

    with get_db() as con:
        cursor = con.execute(
            f"UPDATE jobs SET {keys} WHERE id=?",  # noqa: S608  # identifiers are allow-listed above; values stay bound
            (*values, job_id),
        )
        con.commit()
        return cursor.rowcount == 1


def update_job_if_status(job_id: str, expected_statuses: tuple[str, ...], **fields: Any) -> bool:
    if not expected_statuses:
        raise ValueError("expected_statuses must not be empty")
    if not fields:
        return False

    keys, values = _job_assignments(fields)
    placeholders = _in_placeholders(expected_statuses)

    with get_db() as con:
        cursor = con.execute(
            f"UPDATE jobs SET {keys} WHERE id=? AND status IN ({placeholders})",  # noqa: S608  # identifiers are allow-listed above; values stay bound
            (*values, job_id, *expected_statuses),
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
    """Atomically move the next queued job to processing so two workers cannot
    claim the same one. Returns the updated row, or None if none are queued.
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
    placeholders = _in_placeholders(WORKER_OWNED_STATUSES)
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
            ("cancelled", message, finished_at, *WORKER_OWNED_STATUSES),
        )
        con.commit()
        return max(cursor.rowcount, 0)


def get_job(job_id: str) -> sqlite3.Row | None:
    with get_db() as con:
        return con.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()


def find_active_job_for_submission(url: str, job_type: str, quality: str) -> sqlite3.Row | None:
    """Return the most recent job for the same (url, type, quality), if any.

    Used by POST /api/submit to warn before creating a second job for a
    source that was already downloaded or is currently in flight. A job
    that errored or was cancelled is excluded so a legitimate retry never
    gets flagged as a duplicate.
    """
    with get_db() as con:
        return _find_active_duplicate(con, url, job_type, quality)


def _find_active_duplicate(con: sqlite3.Connection, url: str, job_type: str, quality: str) -> sqlite3.Row | None:
    retryable = tuple(sorted(RETRYABLE_JOB_STATUSES))
    return con.execute(
        f"""
        SELECT * FROM jobs
        WHERE url = ? AND type = ? AND quality = ? AND status NOT IN ({_in_placeholders(retryable)})
        ORDER BY created_at DESC, id DESC
        LIMIT 1
        """,  # noqa: S608  # identifiers are allow-listed above; values stay bound
        (url, job_type, quality, *retryable),
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
        con.execute("BEGIN IMMEDIATE")
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

        # 16 random bytes render as 22 URL-safe characters (128 bits). The token
        # is the only authorization a public share download has, so its length -
        # not the SlowAPI rate limit on the redeem route - has to make guessing
        # infeasible. Still short enough to paste into a chat.
        # Retry on the (vanishingly rare) primary-key collision instead of
        # letting the insert raise.
        for _ in range(8):
            token = secrets.token_urlsafe(16)
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
        return con.execute("SELECT * FROM share_links WHERE token = ?", (token,)).fetchone()


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
            batch = job_ids[start : start + _DELETE_BATCH_SIZE]
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


def delete_jobs_and_share_links(job_ids: list[str], *, terminal_only: bool = True) -> tuple[list[str], int]:
    """Delete selected jobs and invalidate their share links atomically.

    Return ``(deleted_ids, links_deleted)``. With ``terminal_only`` (default) a
    job that is no longer terminal - for example one retried after the caller
    selected it - is skipped and keeps its row and share links; only the IDs
    actually deleted are returned. ``terminal_only=False`` is for an explicit
    bulk removal that has already cancelled the workers.

    The explicit link deletion supports databases created before the foreign-key
    cascade was added. Callers clean the corresponding filesystem artifacts
    before invoking this function, so an unsuccessful cleanup leaves the job
    records and their links intact for a safe retry.
    """
    if not job_ids:
        return ([], 0)

    job_ids = list(dict.fromkeys(job_ids))
    statuses = tuple(sorted(TERMINAL_JOB_STATUSES))
    status_clause = f" AND status IN ({_in_placeholders(statuses)})" if terminal_only else ""
    status_params: tuple[Any, ...] = statuses if terminal_only else ()
    deleted_ids: list[str] = []
    links_deleted = 0
    with get_db() as con:
        for start in range(0, len(job_ids), _DELETE_BATCH_SIZE):
            batch = tuple(job_ids[start : start + _DELETE_BATCH_SIZE])
            placeholders = ",".join("?" * len(batch))
            # The first write takes the write lock, so the status cannot change
            # between the link delete and the job delete below.
            owned_ids = f"SELECT id FROM jobs WHERE id IN ({placeholders}){status_clause}"  # noqa: S608  # identifiers are allow-listed above; values stay bound
            links_cursor = con.execute(
                f"DELETE FROM share_links WHERE job_id IN ({owned_ids})",  # noqa: S608  # identifiers are allow-listed above; values stay bound
                (*batch, *status_params),
            )
            rows = con.execute(
                f"DELETE FROM jobs WHERE id IN ({placeholders}){status_clause} RETURNING id",  # noqa: S608  # identifiers are allow-listed above; values stay bound
                (*batch, *status_params),
            ).fetchall()
            links_deleted += max(links_cursor.rowcount, 0)
            deleted_ids.extend(str(row["id"]) for row in rows)
        con.commit()
    return (deleted_ids, links_deleted)


def job_exists(job_id: str) -> bool:
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
            """
            SELECT * FROM jobs
            WHERE include_in_history = 1
            ORDER BY created_at DESC, id DESC
            LIMIT ? OFFSET ?
            """,
            (limit, offset),
        ).fetchall()


def list_active_jobs(limit: int = 50) -> list[sqlite3.Row]:
    """Non-terminal jobs, newest first, regardless of ``include_in_history``.

    Lets a reloaded dashboard find a running job even when job history was
    disabled at submission and paginate_jobs() therefore skips it.
    """
    limit = _validate_limit(limit)
    statuses = tuple(sorted(ACTIVE_JOB_STATUSES))
    with get_db() as con:
        return con.execute(
            f"""
            SELECT * FROM jobs
            WHERE status IN ({_in_placeholders(statuses)})
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,  # noqa: S608  # placeholders only; status values stay bound
            (*statuses, limit),
        ).fetchall()


def get_stats() -> dict[str, int | float]:
    """Aggregated KPIs for jobs that reached a completed state."""
    placeholders = _in_placeholders(COMPLETED_STATUSES)
    with get_db() as con:
        reset_row = con.execute("SELECT value FROM settings WHERE key='statistics_reset_at'").fetchone()
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

        row = con.execute(
            f"""
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
        """,  # noqa: S608 - identifiers are allow-listed above; values stay bound
            query_params,
        ).fetchone()

    return {
        "total_jobs": row["total_jobs"] or 0,
        "total_minutes": round(float(row["total_minutes"] or 0), 1),
        "total_bytes": row["total_bytes"] or 0,
        "total_lalal_minutes": round(float(row["total_lalal_minutes"] or 0), 1),
    }


def _validate_keep_days(keep_days: int) -> int:
    if isinstance(keep_days, bool) or not isinstance(keep_days, int):
        raise TypeError("keep_days must be a non-negative integer")
    if keep_days < 0:
        raise ValueError("keep_days must be non-negative")
    return keep_days


def list_expired_job_ids(keep_days: int) -> list[str]:
    """Return terminal job IDs whose retention window has elapsed.

    Read-only: does not delete rows or files. Superseded by purge_old_jobs()
    for the housekeeping sweep, which deletes the DB rows outright; this is
    kept for callers that only need to inspect what would expire.
    """
    if _validate_keep_days(keep_days) == 0:
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


def delete_expired_job_if_eligible(
    job_id: str,
    keep_days: int,
    delete_directory: Callable[[], bool],
) -> tuple[bool, int]:
    """Delete one expired job's files, row, and links as one serialized decision.

    The initial retention scan is only a candidate snapshot. ``BEGIN IMMEDIATE``
    rechecks eligibility and holds SQLite's single-writer lock until the
    directory callback and row/link deletion finish. A retry that commits first
    changes the status and this function skips the directory; if retention
    claims the writer slot first, the retry cannot report success before the
    job is removed. A failed filesystem deletion leaves the database untouched
    so a later sweep can retry it.

    ``delete_directory`` must only touch this job's filesystem artifacts and
    must not perform database writes.
    """
    _validate_keep_days(keep_days)
    if keep_days == 0:
        return False, 0

    statuses = tuple(sorted(TERMINAL_JOB_STATUSES))
    placeholders = _in_placeholders(statuses)
    eligible_sql = f"""
        SELECT 1
        FROM jobs
        WHERE id = ?
          AND status IN ({placeholders})
          AND finished_at IS NOT NULL
          AND finished_at < datetime('now', '-' || ? || ' days')
    """  # noqa: S608  # generated placeholders only; all values remain bound

    with get_db() as con:
        con.execute("BEGIN IMMEDIATE")
        eligible = con.execute(eligible_sql, (job_id, *statuses, keep_days)).fetchone()
        if eligible is None:
            exists = con.execute("SELECT 1 FROM jobs WHERE id = ?", (job_id,)).fetchone()
            # The row may have been removed after the initial candidate
            # snapshot. With no row to retry, its UUID directory is now an
            # orphan; keep the writer lock through its cleanup as well.
            if exists is None and delete_directory():
                con.rollback()
                return True, 0
            con.rollback()
            return False, 0

        if not delete_directory():
            con.rollback()
            return False, 0

        links_cursor = con.execute("DELETE FROM share_links WHERE job_id = ?", (job_id,))
        deleted = con.execute(
            f"""
            DELETE FROM jobs
            WHERE id = ?
              AND status IN ({placeholders})
              AND finished_at IS NOT NULL
              AND finished_at < datetime('now', '-' || ? || ' days')
            RETURNING id
            """,  # noqa: S608  # status values are bound; only generated placeholders enter SQL
            (job_id, *statuses, keep_days),
        ).fetchone()
        if deleted is None:
            # BEGIN IMMEDIATE prevents another connection from changing this
            # row after the eligibility check; fail loudly if that invariant
            # is ever broken rather than silently leaving files without a row.
            raise RuntimeError(f"Expired job {job_id} changed during its cleanup transaction")

        con.commit()
        return True, max(links_cursor.rowcount, 0)


def purge_old_jobs(keep_days: int, *, batch_size: int = _MAX_QUERY_LIMIT) -> list[str]:
    """Delete terminal jobs older than *keep_days*, in bounded batches.

    Return deleted IDs for filesystem cleanup. Fresh databases cascade the
    delete to share_links; older ones rely on the caller also invoking
    delete_share_links_for_jobs().

    A retention of zero keeps every job, matching list_expired_job_ids().
    """
    if _validate_keep_days(keep_days) == 0:
        return []
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


def get_settings(
    *,
    include_internal: bool = False,
    include_secrets: bool = False,
) -> dict[str, Any]:
    """Retrieve settings with type coercion.

    ``include_internal`` adds internal keys (admin_password_hash,
    session_version, ...); ``include_secrets`` adds stored credentials. Both
    default off - user-facing APIs must never opt in, to avoid disclosure.
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
    """Update settings; unknown keys are ignored.

    ``allow_internal`` also accepts internal keys such as
    ``admin_password_hash``. Callers handling user input must leave it
    ``False`` so the password hash cannot be overwritten unverified.
    """
    allowed = _ALLOWED_SETTINGS_KEYS | _INTERNAL_SETTINGS_KEYS if allow_internal else _ALLOWED_SETTINGS_KEYS
    filtered = {k: v for k, v in data.items() if k in allowed}

    values = _serialize_settings(filtered)
    with get_db() as con:
        for key, value in values.items():
            con.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                (key, value),
            )
        con.commit()


def _serialize_settings(data: dict[str, Any]) -> dict[str, str]:
    """Parse and encode setting values before opening a write transaction."""
    values: dict[str, str] = {}
    for key, raw_value in data.items():
        parser = _SETTINGS_TYPES.get(key, str)
        parsed_value = parser(raw_value)
        values[key] = ("true" if parsed_value else "false") if isinstance(parsed_value, bool) else str(parsed_value)
    return values


def set_settings_and_bump_session_version(data: dict[str, Any]) -> int:
    """Atomically persist *data* and increment the global session version.

    Credential changes and authentication-mode transitions invalidate every
    stateless session. The version is read and incremented inside the same
    ``BEGIN IMMEDIATE`` transaction as the settings write, so concurrent
    requests cannot both publish the same next version.
    """
    allowed = _ALLOWED_SETTINGS_KEYS | _INTERNAL_SETTINGS_KEYS
    filtered = {key: value for key, value in data.items() if key in allowed and key != "session_version"}
    values = _serialize_settings(filtered)

    with get_db() as con:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute("SELECT value FROM settings WHERE key='session_version'").fetchone()
        try:
            current_version = _parse_nonnegative_int(row["value"]) if row else 0
        except (TypeError, ValueError) as exc:
            logger.warning("Invalid stored session_version=%r; using default: %s", row["value"], exc)
            current_version = 0
        next_version = current_version + 1

        for key, value in values.items():
            con.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                (key, value),
            )
        con.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES ('session_version', ?)",
            (str(next_version),),
        )
        con.commit()
    return next_version
