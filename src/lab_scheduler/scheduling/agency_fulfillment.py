from __future__ import annotations

import sqlite3
import uuid
from datetime import date, datetime, timezone
from typing import Dict, List, Mapping, Optional, Sequence

AGENCY_PLACEHOLDER_NOTE_PREFIX = "AGENCY_PLACEHOLDER"
AGENCY_PLACEHOLDER_DISPLAY = "AGY"
DEFAULT_PLACEHOLDER_LABEL = "Agency - TBD"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _shift_assignments_has_system_note(conn: sqlite3.Connection) -> bool:
    rows = conn.execute("PRAGMA table_info(shift_assignments)").fetchall()
    return any(str(row[1]) == "system_note" for row in rows)


def _shift_assignments_has_assignment_status(conn: sqlite3.Connection) -> bool:
    rows = conn.execute("PRAGMA table_info(shift_assignments)").fetchall()
    return any(str(row[1]) == "assignment_status" for row in rows)


def _assigned_status_clause(conn: sqlite3.Connection) -> str:
    if _shift_assignments_has_assignment_status(conn):
        return "AND COALESCE(assignment_status, 'assigned') = 'assigned'"
    return ""


def resolve_employee_id_for_slot(
    slot_label: str,
    employees: Sequence[Mapping[str, object]],
) -> Optional[str]:
    target = str(slot_label or "").strip().casefold()
    if not target:
        return None
    for employee in employees:
        full_name = str(employee.get("full_name", employee.get("Employee", ""))).strip()
        if full_name.casefold() == target:
            return str(employee.get("id", ""))
    return None


def shift_template_id_for_code(
    templates: Mapping[str, Mapping[str, object]],
    shift_code: str,
) -> Optional[str]:
    normalized = str(shift_code or "").strip().upper()
    if not normalized:
        return None
    for template_id, template in templates.items():
        if str(template.get("code", "")).strip().upper() == normalized:
            return str(template_id)
    aliases = {
        "MORNING": ("MORNING", "M", "D"),
        "EVENING": ("EVENING", "E"),
        "NIGHT": ("NIGHT", "N"),
    }
    for alias in aliases.get(normalized, ()):
        for template_id, template in templates.items():
            code = str(template.get("code", "")).strip().upper()
            short = str(template.get("short", "")).strip().upper()
            if code == alias or short == alias:
                return str(template_id)
    return None


def agency_system_note(assignee_label: str = DEFAULT_PLACEHOLDER_LABEL) -> str:
    return f"{AGENCY_PLACEHOLDER_NOTE_PREFIX}|{assignee_label}"


def persist_agency_placeholder_assignment(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    schedule_period_id: str,
    employee_id: str,
    shift_template_id: str,
    assignment_date: date,
    assignee_label: str = DEFAULT_PLACEHOLDER_LABEL,
) -> str:
    """
    Write a vacant-line placeholder assignment for external agency coverage.

    Does not invoke generation or compliance engines.
    """

    conn.execute("PRAGMA foreign_keys = ON;")
    now = _utc_now_iso()
    note = agency_system_note(assignee_label)
    status_clause = _assigned_status_clause(conn)
    existing = conn.execute(
        f"""
        SELECT id
        FROM shift_assignments
        WHERE tenant_id = ? AND employee_id = ? AND assignment_date = ?
          {status_clause}
        """,
        (tenant_id, employee_id, assignment_date.isoformat()),
    ).fetchone()

    if existing:
        assignment_id = str(existing[0])
        if _shift_assignments_has_system_note(conn):
            conn.execute(
                """
                UPDATE shift_assignments
                SET shift_template_id = ?,
                    schedule_period_id = ?,
                    updated_at = ?,
                    system_note = ?
                WHERE id = ? AND tenant_id = ?
                """,
                (
                    shift_template_id,
                    schedule_period_id,
                    now,
                    note,
                    assignment_id,
                    tenant_id,
                ),
            )
        else:
            conn.execute(
                """
                UPDATE shift_assignments
                SET shift_template_id = ?,
                    schedule_period_id = ?,
                    updated_at = ?
                WHERE id = ? AND tenant_id = ?
                """,
                (
                    shift_template_id,
                    schedule_period_id,
                    now,
                    assignment_id,
                    tenant_id,
                ),
            )
    else:
        assignment_id = f"asg-{uuid.uuid4().hex[:12]}"
        if _shift_assignments_has_system_note(conn):
            conn.execute(
                """
                INSERT INTO shift_assignments (
                  id, tenant_id, schedule_period_id, employee_id,
                  shift_template_id, assignment_date, created_at, updated_at, system_note
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    assignment_id,
                    tenant_id,
                    schedule_period_id,
                    employee_id,
                    shift_template_id,
                    assignment_date.isoformat(),
                    now,
                    now,
                    note,
                ),
            )
        else:
            conn.execute(
                """
                INSERT INTO shift_assignments (
                  id, tenant_id, schedule_period_id, employee_id,
                  shift_template_id, assignment_date, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    assignment_id,
                    tenant_id,
                    schedule_period_id,
                    employee_id,
                    shift_template_id,
                    assignment_date.isoformat(),
                    now,
                    now,
                ),
            )
    conn.commit()
    return assignment_id


def apply_agency_placements_to_schedule_rows(
    schedule_rows: List[Dict[str, object]],
    placements: Sequence[Mapping[str, object]],
    dates: Sequence[date],
) -> List[Dict[str, object]]:
    if not placements:
        return schedule_rows

    date_keys = {day.isoformat(): day for day in dates}
    rows_by_employee = {
        str(row.get("employee_id", "")).strip(): row for row in schedule_rows
    }
    for placement in placements:
        employee_id = str(placement.get("mapped_employee_id") or "").strip()
        day_key = str(placement.get("assignment_date") or "").strip()
        if not employee_id or day_key not in date_keys:
            continue
        row = rows_by_employee.get(employee_id)
        if row is None:
            continue
        label = str(placement.get("assignee_label") or DEFAULT_PLACEHOLDER_LABEL)
        row[day_key] = AGENCY_PLACEHOLDER_DISPLAY
        row[f"{day_key}__agency_label"] = label
    return schedule_rows
