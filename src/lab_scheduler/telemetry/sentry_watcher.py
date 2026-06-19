from __future__ import annotations

import difflib
import sqlite3
import traceback
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator, Mapping, Optional, Sequence

PROJECT_FRAME_MARKERS: tuple[str, ...] = ("lab_scheduler", "scripts")
RESOLUTION_STATUSES: tuple[str, ...] = (
    "unresolved",
    "resolved",
    "ignored",
    "awaiting_review",
    "patched",
    "patch_failed",
)


@dataclass(frozen=True, slots=True)
class SentryLogRecord:
    log_id: int
    recorded_at_utc: str
    tenant_id: Optional[str]
    username: Optional[str]
    exception_type: str
    error_message: str
    target_file: Optional[str]
    line_number: Optional[int]
    clean_traceback: str
    resolution_status: str
    proposed_patch_code: Optional[str] = None


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def ensure_sentry_schema(conn: sqlite3.Connection) -> None:
    ddl = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='sys_sentry_logs'"
    ).fetchone()
    if ddl is None:
        _create_sentry_logs_table(conn)
    else:
        sql = ddl[0] or ""
        if "'patched'" not in sql:
            _migrate_sentry_logs_table(conn)
            ddl = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='sys_sentry_logs'"
            ).fetchone()
            sql = ddl[0] or "" if ddl else ""
        if "'awaiting_review'" not in sql or "proposed_patch_code" not in sql:
            _migrate_sentry_logs_hitl(conn)
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_sys_sentry_logs_status_recorded
          ON sys_sentry_logs (resolution_status, recorded_at_utc DESC)
        """
    )


def _create_sentry_logs_table(conn: sqlite3.Connection) -> None:
    statuses = ", ".join(f"'{status}'" for status in RESOLUTION_STATUSES)
    conn.execute(
        f"""
        CREATE TABLE sys_sentry_logs (
          log_id INTEGER PRIMARY KEY AUTOINCREMENT,
          recorded_at_utc TEXT NOT NULL,
          tenant_id TEXT,
          username TEXT,
          exception_type TEXT NOT NULL,
          error_message TEXT NOT NULL,
          target_file TEXT,
          line_number INTEGER,
          clean_traceback TEXT NOT NULL,
          resolution_status TEXT NOT NULL DEFAULT 'unresolved'
            CHECK (resolution_status IN ({statuses})),
          proposed_patch_code TEXT
        )
        """
    )


def _migrate_sentry_logs_table(conn: sqlite3.Connection) -> None:
    statuses = ", ".join(f"'{status}'" for status in RESOLUTION_STATUSES)
    conn.executescript(
        f"""
        CREATE TABLE sys_sentry_logs__new (
          log_id INTEGER PRIMARY KEY AUTOINCREMENT,
          recorded_at_utc TEXT NOT NULL,
          tenant_id TEXT,
          username TEXT,
          exception_type TEXT NOT NULL,
          error_message TEXT NOT NULL,
          target_file TEXT,
          line_number INTEGER,
          clean_traceback TEXT NOT NULL,
          resolution_status TEXT NOT NULL DEFAULT 'unresolved'
            CHECK (resolution_status IN ({statuses})),
          proposed_patch_code TEXT
        );
        INSERT INTO sys_sentry_logs__new (
          log_id, recorded_at_utc, tenant_id, username, exception_type, error_message,
          target_file, line_number, clean_traceback, resolution_status, proposed_patch_code
        )
        SELECT
          log_id, recorded_at_utc, tenant_id, username, exception_type, error_message,
          target_file, line_number, clean_traceback, resolution_status, NULL
        FROM sys_sentry_logs;
        DROP TABLE sys_sentry_logs;
        ALTER TABLE sys_sentry_logs__new RENAME TO sys_sentry_logs;
        """
    )


def _migrate_sentry_logs_hitl(conn: sqlite3.Connection) -> None:
    statuses = ", ".join(f"'{status}'" for status in RESOLUTION_STATUSES)
    conn.executescript(
        f"""
        CREATE TABLE sys_sentry_logs__hitl (
          log_id INTEGER PRIMARY KEY AUTOINCREMENT,
          recorded_at_utc TEXT NOT NULL,
          tenant_id TEXT,
          username TEXT,
          exception_type TEXT NOT NULL,
          error_message TEXT NOT NULL,
          target_file TEXT,
          line_number INTEGER,
          clean_traceback TEXT NOT NULL,
          resolution_status TEXT NOT NULL DEFAULT 'unresolved'
            CHECK (resolution_status IN ({statuses})),
          proposed_patch_code TEXT
        );
        INSERT INTO sys_sentry_logs__hitl (
          log_id, recorded_at_utc, tenant_id, username, exception_type, error_message,
          target_file, line_number, clean_traceback, resolution_status, proposed_patch_code
        )
        SELECT
          log_id, recorded_at_utc, tenant_id, username, exception_type, error_message,
          target_file, line_number, clean_traceback, resolution_status, NULL
        FROM sys_sentry_logs;
        DROP TABLE sys_sentry_logs;
        ALTER TABLE sys_sentry_logs__hitl RENAME TO sys_sentry_logs;
        """
    )


def _normalize_project_path(path: str, *, project_root: Optional[Path] = None) -> str:
    text = str(path).replace("\\", "/")
    if project_root is not None:
        try:
            return Path(path).resolve().relative_to(project_root.resolve()).as_posix()
        except ValueError:
            pass
    for marker in PROJECT_FRAME_MARKERS:
        idx = text.lower().find(f"/{marker}/")
        if idx >= 0:
            return text[idx + 1 :]
        if text.lower().startswith(f"{marker}/"):
            return text
    return text


def extract_exception_origin(
    exc: BaseException,
    *,
    project_root: Optional[Path] = None,
) -> tuple[str, str, Optional[str], Optional[int], str]:
    """Return type name, message, project-relative file, line number, and traceback text."""

    exc_type = type(exc).__name__
    message = str(exc).strip() or exc_type
    frames = traceback.extract_tb(exc.__traceback__)
    target_file: Optional[str] = None
    line_number: Optional[int] = None

    for frame in reversed(frames):
        normalized = _normalize_project_path(frame.filename, project_root=project_root)
        if any(marker in normalized for marker in PROJECT_FRAME_MARKERS):
            target_file = normalized
            line_number = int(frame.lineno)
            break

    if target_file is None and frames:
        last = frames[-1]
        target_file = _normalize_project_path(last.filename, project_root=project_root)
        line_number = int(last.lineno)

    project_frames = [
        frame
        for frame in frames
        if any(marker in _normalize_project_path(frame.filename) for marker in PROJECT_FRAME_MARKERS)
    ]
    if project_frames:
        clean_traceback = "".join(traceback.format_list(project_frames)).strip()
    else:
        clean_traceback = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)).strip()

    return exc_type, message, target_file, line_number, clean_traceback


def log_unhandled_exception(
    conn: sqlite3.Connection,
    exc: BaseException,
    *,
    tenant_id: Optional[str] = None,
    username: Optional[str] = None,
    project_root: Optional[Path] = None,
) -> int:
    ensure_sentry_schema(conn)
    exc_type, message, target_file, line_number, clean_traceback = extract_exception_origin(
        exc, project_root=project_root
    )
    cur = conn.execute(
        """
        INSERT INTO sys_sentry_logs (
          recorded_at_utc, tenant_id, username, exception_type, error_message,
          target_file, line_number, clean_traceback, resolution_status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'unresolved')
        """,
        (
            utc_now_iso(),
            tenant_id,
            username,
            exc_type,
            message,
            target_file,
            line_number,
            clean_traceback,
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def fetch_sentry_logs(
    conn: sqlite3.Connection,
    *,
    resolution_status: str = "unresolved",
    limit: int = 20,
) -> list[SentryLogRecord]:
    ensure_sentry_schema(conn)
    rows = conn.execute(
        """
        SELECT
          log_id, recorded_at_utc, tenant_id, username, exception_type, error_message,
          target_file, line_number, clean_traceback, resolution_status, proposed_patch_code
        FROM sys_sentry_logs
        WHERE resolution_status = ?
        ORDER BY log_id DESC
        LIMIT ?
        """,
        (resolution_status, limit),
    ).fetchall()
    return [_row_to_sentry_log_record(row) for row in rows]


def fetch_oldest_unresolved_sentry_log(conn: sqlite3.Connection) -> Optional[SentryLogRecord]:
    ensure_sentry_schema(conn)
    row = conn.execute(
        """
        SELECT
          log_id, recorded_at_utc, tenant_id, username, exception_type, error_message,
          target_file, line_number, clean_traceback, resolution_status, proposed_patch_code
        FROM sys_sentry_logs
        WHERE resolution_status = 'unresolved'
        ORDER BY log_id ASC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        return None
    return _row_to_sentry_log_record(row)


def fetch_sentry_log_by_id(conn: sqlite3.Connection, log_id: int) -> Optional[SentryLogRecord]:
    ensure_sentry_schema(conn)
    row = conn.execute(
        """
        SELECT
          log_id, recorded_at_utc, tenant_id, username, exception_type, error_message,
          target_file, line_number, clean_traceback, resolution_status, proposed_patch_code
        FROM sys_sentry_logs
        WHERE log_id = ?
        """,
        (log_id,),
    ).fetchone()
    if row is None:
        return None
    return _row_to_sentry_log_record(row)


def update_sentry_log_status(conn: sqlite3.Connection, log_id: int, resolution_status: str) -> None:
    if resolution_status not in RESOLUTION_STATUSES:
        raise ValueError(f"Unsupported resolution_status: {resolution_status}")
    ensure_sentry_schema(conn)
    conn.execute(
        "UPDATE sys_sentry_logs SET resolution_status = ? WHERE log_id = ?",
        (resolution_status, log_id),
    )
    conn.commit()


def update_sentry_log_for_review(
    conn: sqlite3.Connection,
    log_id: int,
    proposed_patch_code: str,
    *,
    resolution_status: str = "awaiting_review",
) -> None:
    if resolution_status not in RESOLUTION_STATUSES:
        raise ValueError(f"Unsupported resolution_status: {resolution_status}")
    ensure_sentry_schema(conn)
    conn.execute(
        """
        UPDATE sys_sentry_logs
        SET resolution_status = ?, proposed_patch_code = ?
        WHERE log_id = ?
        """,
        (resolution_status, proposed_patch_code, log_id),
    )
    conn.commit()


def format_unified_patch_diff(
    *,
    original_content: str,
    patched_content: str,
    target_file: str,
) -> str:
    original_lines = original_content.splitlines(keepends=True)
    patched_lines = patched_content.splitlines(keepends=True)
    return "".join(
        difflib.unified_diff(
            original_lines,
            patched_lines,
            fromfile=f"a/{target_file}",
            tofile=f"b/{target_file}",
            lineterm="\n",
        )
    )


def generate_llm_diagnostic_packet(
    conn: sqlite3.Connection,
    *,
    limit: int = 10,
    resolution_status: str = "unresolved",
    log_id: Optional[int] = None,
) -> str:
    """Format unresolved Sentry logs as Markdown for an autonomous patch agent."""

    if log_id is not None:
        record = fetch_sentry_log_by_id(conn, log_id)
        records = [record] if record is not None else []
    else:
        records = fetch_sentry_logs(
            conn, resolution_status=resolution_status, limit=limit
        )
    lines = [
        "# Lab Staffing Scheduler — Sentry Diagnostic Packet",
        "",
        f"Generated at (UTC): {utc_now_iso()}",
        f"Unresolved log count in packet: {len(records)}",
        "",
        "## Instructions for Patch Agent",
        "- Reproduce using tenant/username context when present.",
        "- Prioritize the `target_file` and `line_number` of the first incident.",
        "- Apply the smallest safe fix; keep compliance/finance tests green.",
        "",
    ]
    if not records:
        lines.extend(["## Status", "", "No unresolved sentry logs were found.", ""])
        return "\n".join(lines)

    for idx, record in enumerate(records, start=1):
        lines.extend(
            [
                f"## Incident {idx} · log_id `{record.log_id}`",
                "",
                "| Field | Value |",
                "| --- | --- |",
                f"| recorded_at_utc | `{record.recorded_at_utc}` |",
                f"| tenant_id | `{record.tenant_id or '—'}` |",
                f"| username | `{record.username or '—'}` |",
                f"| exception_type | `{record.exception_type}` |",
                f"| target_file | `{record.target_file or '—'}` |",
                f"| line_number | `{record.line_number or '—'}` |",
                f"| resolution_status | `{record.resolution_status}` |",
                "",
                "### error_message",
                "",
                "```text",
                record.error_message,
                "```",
                "",
                "### clean_traceback",
                "",
                "```text",
                record.clean_traceback,
                "```",
                "",
            ]
        )
    return "\n".join(lines)


@contextmanager
def sentry_exception_guard(
    conn: sqlite3.Connection,
    *,
    tenant_id: Optional[str] = None,
    username: Optional[str] = None,
    project_root: Optional[Path] = None,
    on_captured: Optional[Callable[[int, BaseException], None]] = None,
) -> Iterator[None]:
    """Capture unhandled exceptions and optionally invoke a UI callback."""

    try:
        yield
    except Exception as exc:  # noqa: BLE001 - deliberate global interception boundary
        log_id = log_unhandled_exception(
            conn,
            exc,
            tenant_id=tenant_id,
            username=username,
            project_root=project_root,
        )
        if on_captured is not None:
            on_captured(log_id, exc)
        return


def session_context_from_mapping(session: Mapping[str, object]) -> tuple[Optional[str], Optional[str]]:
    tenant_id = session.get("tenant_id")
    username = session.get("username")
    tenant_text = str(tenant_id) if tenant_id else None
    username_text = str(username) if username else None
    return tenant_text, username_text


def _row_to_sentry_log_record(row: Sequence[object]) -> SentryLogRecord:
    return SentryLogRecord(
        log_id=int(row[0]),
        recorded_at_utc=str(row[1]),
        tenant_id=row[2] if row[2] is not None else None,
        username=row[3] if row[3] is not None else None,
        exception_type=str(row[4]),
        error_message=str(row[5]),
        target_file=row[6] if row[6] is not None else None,
        line_number=int(row[7]) if row[7] is not None else None,
        clean_traceback=str(row[8]),
        resolution_status=str(row[9]),
        proposed_patch_code=row[10] if len(row) > 10 and row[10] is not None else None,
    )
