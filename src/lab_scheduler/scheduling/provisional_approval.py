from __future__ import annotations

import sqlite3
from datetime import date, datetime, timezone
from typing import Optional

from lab_scheduler.scheduling.provisional_compliance import (
    ProvisionalAssignment,
    approved_contract_line_exception_system_note,
    approved_stretch_system_note,
    is_approved_contract_line_exception_note,
    is_approved_stretch_note,
    is_provisional_contract_line_exception_note,
    is_provisional_stretch_note,
)
from lab_scheduler.scheduling.provisional_constants import (
    CONTRACT_LINE_EXCEPTION_VIOLATION_CODE,
)
from lab_scheduler.scheduling.provisional_state_cleanup import (
    ClearProvisionalStateResult,
    clear_provisional_session_files,
    clear_provisional_stretch_state,
    provisional_session_artifact_paths,
)

__all__ = [
    "ClearProvisionalStateResult",
    "ProvisionalAssignment",
    "approve_provisional_assignment",
    "attach_assignment_ids",
    "clear_provisional_session_files",
    "clear_provisional_stretch_state",
    "load_pending_provisional_assignments",
    "provisional_session_artifact_paths",
]


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _shift_assignments_has_system_note(conn: sqlite3.Connection) -> bool:
    rows = conn.execute("PRAGMA table_info(shift_assignments)").fetchall()
    return any(str(row[1]) == "system_note" for row in rows)


def find_assignment_id(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    schedule_period_id: str,
    employee_id: str,
    assignment_date: date,
    shift_template_id: str,
) -> Optional[str]:
    row = conn.execute(
        """
        SELECT id
        FROM shift_assignments
        WHERE tenant_id = ?
          AND schedule_period_id = ?
          AND employee_id = ?
          AND assignment_date = ?
          AND shift_template_id = ?
        LIMIT 1
        """,
        (
            tenant_id,
            schedule_period_id,
            employee_id,
            assignment_date.isoformat(),
            shift_template_id,
        ),
    ).fetchone()
    if row is None:
        return None
    return str(row[0])


def approve_provisional_assignment(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    schedule_period_id: str,
    provisional: ProvisionalAssignment,
    actor: str,
) -> bool:
    """One-click manager approval for a suggested stretch/turnaround or contract-line override."""

    if not _shift_assignments_has_system_note(conn):
        return False

    assignment_id = provisional.assignment_id or find_assignment_id(
        conn,
        tenant_id=tenant_id,
        schedule_period_id=schedule_period_id,
        employee_id=provisional.employee_id,
        assignment_date=provisional.assignment_date,
        shift_template_id=provisional.shift_template_id,
    )
    if assignment_id is None:
        return False

    if provisional.violation_code == CONTRACT_LINE_EXCEPTION_VIOLATION_CODE:
        approved_note = approved_contract_line_exception_system_note(actor=actor)
    else:
        approved_note = approved_stretch_system_note(actor=actor)

    now = _utc_now_iso()
    conn.execute(
        """
        UPDATE shift_assignments
        SET system_note = ?, updated_at = ?
        WHERE id = ? AND tenant_id = ? AND schedule_period_id = ?
        """,
        (
            approved_note,
            now,
            assignment_id,
            tenant_id,
            schedule_period_id,
        ),
    )
    conn.commit()
    return True


def attach_assignment_ids(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    schedule_period_id: str,
    provisional_assignments: list[ProvisionalAssignment],
) -> list[ProvisionalAssignment]:
    enriched: list[ProvisionalAssignment] = []
    for item in provisional_assignments:
        assignment_id = find_assignment_id(
            conn,
            tenant_id=tenant_id,
            schedule_period_id=schedule_period_id,
            employee_id=item.employee_id,
            assignment_date=item.assignment_date,
            shift_template_id=item.shift_template_id,
        )
        if assignment_id is None:
            enriched.append(item)
            continue
        enriched.append(
            ProvisionalAssignment(
                employee_id=item.employee_id,
                employee_name=item.employee_name,
                assignment_date=item.assignment_date,
                shift_template_id=item.shift_template_id,
                shift_code=item.shift_code,
                violation_code=item.violation_code,
                violation_label=item.violation_label,
                message=item.message,
                reason=item.reason,
                assignment_id=assignment_id,
            )
        )
    return enriched


def load_pending_provisional_assignments(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    schedule_period_id: str,
    assignments: list[dict],
    templates: dict[str, dict],
) -> list[ProvisionalAssignment]:
    """Rebuild pending provisional rows from persisted system_note markers."""

    if not _shift_assignments_has_system_note(conn):
        return []

    employee_names = {
        str(row["employee_id"]): str(row.get("employee_name") or row["employee_id"])
        for row in assignments
        if row.get("employee_id")
    }
    pending: list[ProvisionalAssignment] = []
    for row in assignments:
        note = str(row.get("system_note") or "")
        if is_approved_stretch_note(note) or is_approved_contract_line_exception_note(note):
            continue
        employee_id = str(row["employee_id"])
        shift_template_id = str(row["shift_template_id"])
        assignment_date = row["assignment_date"]
        if not isinstance(assignment_date, date):
            assignment_date = date.fromisoformat(str(assignment_date))
        template = templates.get(shift_template_id) or {}
        shift_code = str(template.get("code") or shift_template_id)
        if is_provisional_contract_line_exception_note(note):
            pending.append(
                ProvisionalAssignment(
                    employee_id=employee_id,
                    employee_name=employee_names.get(employee_id, employee_id),
                    assignment_date=assignment_date,
                    shift_template_id=shift_template_id,
                    shift_code=shift_code,
                    violation_code=CONTRACT_LINE_EXCEPTION_VIOLATION_CODE,
                    violation_label="Contract Line Exception",
                    message=note.split("|", 1)[-1].strip(),
                    assignment_id=str(row.get("id") or ""),
                )
            )
            continue
        if not is_provisional_stretch_note(note):
            continue
        pending.append(
            ProvisionalAssignment(
                employee_id=employee_id,
                employee_name=employee_names.get(employee_id, employee_id),
                assignment_date=assignment_date,
                shift_template_id=shift_template_id,
                shift_code=shift_code,
                violation_code="PROVISIONAL_STRETCH",
                violation_label="Suggested compliance override",
                message=note.split("|", 1)[-1].strip(),
                assignment_id=str(row.get("id") or ""),
            )
        )
    return pending
