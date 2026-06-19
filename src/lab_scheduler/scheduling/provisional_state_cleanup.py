from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from lab_scheduler.scheduling.provisional_constants import (
    APPROVED_CONTRACT_LINE_EXCEPTION_NOTE_PREFIX,
    APPROVED_STRETCH_NOTE_PREFIX,
    PROVISIONAL_SESSION_FILE_GLOBS,
    PROVISIONAL_STRETCH_NOTE_PREFIX,
    CONTRACT_LINE_EXCEPTION_NOTE_PREFIX,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ClearProvisionalStateResult:
    db_notes_cleared: int = 0
    session_files_removed: list[str] = field(default_factory=list)

    @property
    def total_cleared(self) -> int:
        return self.db_notes_cleared + len(self.session_files_removed)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _shift_assignments_has_system_note(conn: sqlite3.Connection) -> bool:
    rows = conn.execute("PRAGMA table_info(shift_assignments)").fetchall()
    return any(str(row[1]) == "system_note" for row in rows)


def _like_prefix(prefix: str) -> str:
    escaped = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"{escaped}%"


def _provisional_note_like_clause() -> str:
    return (
        "(system_note LIKE ? ESCAPE '\\' OR system_note LIKE ? ESCAPE '\\' "
        "OR system_note LIKE ? ESCAPE '\\' OR system_note LIKE ? ESCAPE '\\')"
    )


def _is_provisional_system_note(system_note: Optional[str]) -> bool:
    if not system_note:
        return False
    return (
        system_note.startswith(PROVISIONAL_STRETCH_NOTE_PREFIX)
        or system_note.startswith(APPROVED_STRETCH_NOTE_PREFIX)
        or system_note.startswith(CONTRACT_LINE_EXCEPTION_NOTE_PREFIX)
        or system_note.startswith(APPROVED_CONTRACT_LINE_EXCEPTION_NOTE_PREFIX)
    )


def provisional_session_artifact_paths(
    project_root: Path,
    *,
    schedule_period_id: Optional[str] = None,
) -> list[Path]:
    """Locate on-disk provisional override caches written outside Streamlit memory."""

    roots = [project_root / "exports", project_root / ".streamlit"]
    paths: list[Path] = []
    for root in roots:
        if not root.is_dir():
            continue
        for pattern in PROVISIONAL_SESSION_FILE_GLOBS:
            for path in root.glob(pattern):
                if not path.is_file():
                    continue
                if schedule_period_id and schedule_period_id not in path.name:
                    continue
                paths.append(path)
    return sorted(set(paths))


def clear_provisional_session_files(
    project_root: Path,
    *,
    schedule_period_id: Optional[str] = None,
) -> list[str]:
    """Delete cached provisional-assignment JSON sidecars, if present."""

    removed: list[str] = []
    for path in provisional_session_artifact_paths(
        project_root,
        schedule_period_id=schedule_period_id,
    ):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = None
        if isinstance(payload, list):
            if not any(
                _is_provisional_system_note(str(item.get("message", "")))
                or str(item.get("violation_code", "")).upper().startswith("PROVISIONAL")
                or str(item.get("violation_code", "")) == "PROVISIONAL_STRETCH"
                or str(item.get("violation_code", "")) == "CONTRACT_LINE_EXCEPTION"
                for item in payload
                if isinstance(item, dict)
            ):
                continue
        path.unlink(missing_ok=True)
        if path.exists():
            continue
        removed.append(str(path))
        logger.info("Removed provisional session artifact: %s", path)
    return removed


def clear_provisional_stretch_state(
    conn: sqlite3.Connection,
    *,
    tenant_id: Optional[str] = None,
    schedule_period_id: Optional[str] = None,
    project_root: Optional[Path] = None,
    commit: bool = True,
) -> ClearProvisionalStateResult:
    """
    Remove stale provisional override markers before a fresh solve.

    Clears stretch and contract-line exception notes from ``shift_assignments.system_note``
    only; other notes such as ``FORCED_CLINICAL_OT`` or agency placeholders are preserved.
    """

    db_notes_cleared = 0
    if _shift_assignments_has_system_note(conn):
        params: list[str] = []
        filters = [_provisional_note_like_clause()]
        params.extend(
            [
                _like_prefix(PROVISIONAL_STRETCH_NOTE_PREFIX),
                _like_prefix(APPROVED_STRETCH_NOTE_PREFIX),
                _like_prefix(CONTRACT_LINE_EXCEPTION_NOTE_PREFIX),
                _like_prefix(APPROVED_CONTRACT_LINE_EXCEPTION_NOTE_PREFIX),
            ]
        )
        if tenant_id is not None:
            filters.append("tenant_id = ?")
            params.append(tenant_id)
        if schedule_period_id is not None:
            filters.append("schedule_period_id = ?")
            params.append(schedule_period_id)

        where_clause = " AND ".join(filters)
        count_row = conn.execute(
            f"SELECT COUNT(*) FROM shift_assignments WHERE {where_clause}",
            params,
        ).fetchone()
        db_notes_cleared = int(count_row[0]) if count_row else 0
        if db_notes_cleared:
            now = _utc_now_iso()
            conn.execute(
                f"""
                UPDATE shift_assignments
                SET system_note = NULL, updated_at = ?
                WHERE {where_clause}
                """,
                [now, *params],
            )
            if commit:
                conn.commit()
            logger.info(
                "Cleared %s provisional override system_note row(s) tenant=%s period=%s",
                db_notes_cleared,
                tenant_id,
                schedule_period_id,
            )

    session_files_removed: list[str] = []
    if project_root is not None:
        session_files_removed = clear_provisional_session_files(
            project_root,
            schedule_period_id=schedule_period_id,
        )

    return ClearProvisionalStateResult(
        db_notes_cleared=db_notes_cleared,
        session_files_removed=session_files_removed,
    )
