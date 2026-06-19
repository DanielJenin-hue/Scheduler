from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Dict, Mapping, Optional, Sequence, Set, Tuple

from lab_scheduler.compliance.engine import ScheduledShift, ShiftTemplateInfo
from lab_scheduler.compliance.jurisdictions import JurisdictionRules
from lab_scheduler.data.import_manager import (
    FTE_CONTRACT_TIERS,
    fte_from_target_weekly_hours,
    next_employee_code,
)
from lab_scheduler.engine.constraints import validate_contract_line_eligibility
from lab_scheduler.models.employee import CONTRACT_LINE_TYPES, ensure_contract_line_schema

from lab_scheduler.scheduling.auto_generate import EmployeeProfile, validate_assignment_change

STAFF_DEACTIVATION_NOTE = "Vacated due to Staff Deactivation"
CONTRACT_LINE_CHANGE_NOTE = "Vacated due to Contract Line Type change"
REALLOCATION_HORIZON_DAYS = 30
ASSIGNMENT_STATUS_ASSIGNED = "assigned"
ASSIGNMENT_STATUS_UNASSIGNED = "unassigned"


PORTAGE_WEEKLY_HOUR_TIERS: Tuple[float, ...] = (40.0, 28.0, 24.0, 20.0, 16.0, 8.0)


def bulk_target_weekly_hours_options(
    standard_weekly_hours: float = 40.0,
) -> Tuple[float, ...]:
    """
    Portage bulk-provision hour tiers (40h, 28h, 24h, 20h, 16h, 8h).

    Maps to FTE steps 1.0, 0.7, 0.6, 0.5, 0.4, 0.2 at a 40h baseline.
    """

    if abs(standard_weekly_hours - 40.0) < 0.01:
        return PORTAGE_WEEKLY_HOUR_TIERS
    return tuple(round(tier * standard_weekly_hours, 2) for tier in FTE_CONTRACT_TIERS)


class StaffLifecycleError(Exception):
    """Raised when a staffing lifecycle action cannot be completed."""


@dataclass(frozen=True, slots=True)
class VacatedShift:
    shift_id: str
    tenant_id: str
    schedule_period_id: str
    shift_template_id: str
    assignment_date: date
    vacated_from_employee_id: str
    vacated_from_employee_name: str
    system_note: str


@dataclass(frozen=True, slots=True)
class DeactivationResult:
    employee_id: str
    employee_name: str
    shifts_vacated_count: int
    vacated_shifts: tuple[VacatedShift, ...]
    audit_log_id: int


@dataclass(frozen=True, slots=True)
class VacatedShiftFillResult:
    success: bool
    message: str
    shift_id: str
    new_employee_id: str
    assignment_date: date


@dataclass(frozen=True, slots=True)
class VacantLineCreateResult:
    employee_id: str
    display_name: str
    line_number: int


@dataclass(frozen=True, slots=True)
class RosterLineUpdateResult:
    employee_id: str
    employee_name: str
    contract_line_changed: bool
    previous_contract_line: Optional[str]
    new_contract_line: str
    shifts_vacated_count: int
    fte: float
    seniority_hours: float
    target_weekly_hours: float
    audit_log_id: int


def _infer_qual_code_from_name(employee_name: str) -> Optional[str]:
    upper = employee_name.upper()
    if "MLT" in upper:
        return "MLT"
    if "MLA" in upper:
        return "MLA"
    return None


def _vacate_future_contract_line_violations(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    employee_id: str,
    employee_name: str,
    contract_line_type: str,
    as_of_date: date,
) -> list[VacatedShift]:
    rows = conn.execute(
        """
        SELECT
          sa.id,
          sa.schedule_period_id,
          sa.shift_template_id,
          sa.assignment_date,
          st.code
        FROM shift_assignments sa
        JOIN shift_templates st
          ON st.tenant_id = sa.tenant_id AND st.id = sa.shift_template_id
        WHERE sa.tenant_id = ?
          AND sa.employee_id = ?
          AND sa.assignment_date >= ?
          AND COALESCE(sa.assignment_status, ?) = ?
        ORDER BY sa.assignment_date, sa.id
        """,
        (
            tenant_id,
            employee_id,
            as_of_date.isoformat(),
            ASSIGNMENT_STATUS_ASSIGNED,
            ASSIGNMENT_STATUS_ASSIGNED,
        ),
    ).fetchall()

    now = _utc_now_iso()
    vacated: list[VacatedShift] = []
    qual_code = _infer_qual_code_from_name(employee_name)
    for shift_id, period_id, template_id, assignment_date, shift_code in rows:
        violation = validate_contract_line_eligibility(
            contract_line_type,
            str(shift_code),
            qual_code=qual_code,
        )
        if not violation:
            continue
        conn.execute(
            """
            UPDATE shift_assignments
            SET assignment_status = ?,
                system_note = ?,
                vacated_from_employee_id = ?,
                updated_at = ?
            WHERE id = ? AND tenant_id = ?
            """,
            (
                ASSIGNMENT_STATUS_UNASSIGNED,
                CONTRACT_LINE_CHANGE_NOTE,
                employee_id,
                now,
                shift_id,
                tenant_id,
            ),
        )
        vacated.append(
            VacatedShift(
                shift_id=shift_id,
                tenant_id=tenant_id,
                schedule_period_id=period_id,
                shift_template_id=template_id,
                assignment_date=date.fromisoformat(assignment_date),
                vacated_from_employee_id=employee_id,
                vacated_from_employee_name=employee_name,
                system_note=CONTRACT_LINE_CHANGE_NOTE,
            )
        )
    return vacated


def log_roster_line_update(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    manager_id: str,
    employee_id: str,
    shifts_vacated_count: int,
    metadata: Mapping[str, object],
) -> int:
    ensure_staff_lifecycle_schema(conn)
    cur = conn.execute(
        """
        INSERT INTO sys_audit_log (
          recorded_at_utc, tenant_id, manager_id, action_type,
          employee_id, shifts_vacated_count, metadata_json
        ) VALUES (?, ?, ?, 'audit_warning', ?, ?, ?)
        """,
        (
            _utc_now_iso(),
            tenant_id,
            manager_id,
            employee_id,
            shifts_vacated_count,
            json.dumps(dict(metadata)),
        ),
    )
    return int(cur.lastrowid)


def update_employee_roster_line(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    employee_id: str,
    manager_id: str,
    seniority_hours: float,
    contract_line_type: str,
    target_weekly_hours: float,
    standard_weekly_hours: float = 40.0,
    as_of_date: Optional[date] = None,
) -> RosterLineUpdateResult:
    """Validate and persist inline roster spreadsheet edits for one employee."""

    if seniority_hours < 0:
        raise StaffLifecycleError("Seniority hours cannot be negative.")
    if contract_line_type not in CONTRACT_LINE_TYPES:
        raise StaffLifecycleError(
            f"Contract line must be one of {', '.join(CONTRACT_LINE_TYPES)}."
        )
    allowed_hours = bulk_target_weekly_hours_options(standard_weekly_hours)
    if target_weekly_hours not in allowed_hours:
        readable = ", ".join(f"{hours:g}" for hours in allowed_hours)
        raise StaffLifecycleError(f"Target weekly hours must be one of: {readable}.")

    ensure_contract_line_schema(conn)
    ensure_staff_lifecycle_schema(conn)

    row = conn.execute(
        """
        SELECT first_name, last_name, contract_line_type, is_active
        FROM employees
        WHERE tenant_id = ? AND id = ?
        """,
        (tenant_id, employee_id),
    ).fetchone()
    if row is None:
        raise StaffLifecycleError("Employee was not found on this tenant roster.")
    if int(row[3]) != 1:
        raise StaffLifecycleError("Archived employees cannot be edited from the active roster.")

    employee_name = f"{row[0]} {row[1]}"
    previous_contract_line = row[2]
    as_of = as_of_date or date.today()
    fte = fte_from_target_weekly_hours(
        target_weekly_hours,
        standard_weekly_hours=standard_weekly_hours,
    )
    now = _utc_now_iso()

    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("BEGIN IMMEDIATE")
    try:
        vacated: list[VacatedShift] = []
        if previous_contract_line != contract_line_type:
            vacated = _vacate_future_contract_line_violations(
                conn,
                tenant_id=tenant_id,
                employee_id=employee_id,
                employee_name=employee_name,
                contract_line_type=contract_line_type,
                as_of_date=as_of,
            )

        conn.execute(
            """
            UPDATE employees
            SET seniority_hours = ?,
                contract_line_type = ?,
                fte = ?,
                updated_at = ?
            WHERE tenant_id = ? AND id = ?
            """,
            (
                float(seniority_hours),
                contract_line_type,
                fte,
                now,
                tenant_id,
                employee_id,
            ),
        )

        audit_log_id = log_roster_line_update(
            conn,
            tenant_id=tenant_id,
            manager_id=manager_id,
            employee_id=employee_id,
            shifts_vacated_count=len(vacated),
            metadata={
                "event": "roster_line_update",
                "employee_name": employee_name,
                "previous_contract_line": previous_contract_line,
                "new_contract_line": contract_line_type,
                "seniority_hours": float(seniority_hours),
                "target_weekly_hours": float(target_weekly_hours),
                "fte": fte,
                "vacated_shift_ids": [shift.shift_id for shift in vacated],
            },
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    return RosterLineUpdateResult(
        employee_id=employee_id,
        employee_name=employee_name,
        contract_line_changed=previous_contract_line != contract_line_type,
        previous_contract_line=previous_contract_line,
        new_contract_line=contract_line_type,
        shifts_vacated_count=len(vacated),
        fte=fte,
        seniority_hours=float(seniority_hours),
        target_weekly_hours=float(target_weekly_hours),
        audit_log_id=audit_log_id,
    )


def _vacant_line_first_name(role: str, contract_line_type: str) -> str:
    return f"Vacant {role} {contract_line_type} - Line"


def _vacant_line_display_name(role: str, contract_line_type: str, line_number: int) -> str:
    return f"{_vacant_line_first_name(role, contract_line_type)} {line_number:02d}"


def _next_vacant_line_number(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    role: str,
    contract_line_type: str,
) -> int:
    first_name = _vacant_line_first_name(role, contract_line_type)
    rows = conn.execute(
        """
        SELECT last_name
        FROM employees
        WHERE tenant_id = ? AND is_active = 1 AND first_name = ?
        """,
        (tenant_id, first_name),
    ).fetchall()
    max_num = 0
    for (last_name,) in rows:
        try:
            max_num = max(max_num, int(str(last_name).strip()))
        except ValueError:
            continue
    return max_num + 1


def create_vacant_line(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    role: str,
    contract_line_type: str,
    target_weekly_hours: float,
    qualification_id: str,
    hire_date: Optional[date] = None,
    standard_weekly_hours: float = 40.0,
) -> VacantLineCreateResult:
    """Provision a single vacant roster line."""

    role_code = role.strip().upper()
    if role_code not in {"MLT", "MLA"}:
        raise StaffLifecycleError("Role must be MLT or MLA.")
    if contract_line_type not in CONTRACT_LINE_TYPES:
        raise StaffLifecycleError(
            f"Contract line must be one of {', '.join(CONTRACT_LINE_TYPES)}."
        )
    if target_weekly_hours not in bulk_target_weekly_hours_options(standard_weekly_hours):
        allowed = ", ".join(
            f"{hours:g}"
            for hours in bulk_target_weekly_hours_options(standard_weekly_hours)
        )
        raise StaffLifecycleError(
            f"Target weekly hours must be one of: {allowed}."
        )

    ensure_contract_line_schema(conn)

    fte = fte_from_target_weekly_hours(
        target_weekly_hours,
        standard_weekly_hours=standard_weekly_hours,
    )
    hourly_rate = 26.0 if role_code == "MLA" else 40.0
    hire = hire_date or date.today()
    now = _utc_now_iso()
    first_name = _vacant_line_first_name(role_code, contract_line_type)
    line_number = _next_vacant_line_number(
        conn,
        tenant_id=tenant_id,
        role=role_code,
        contract_line_type=contract_line_type,
    )
    last_name = f"{line_number:02d}"
    display_name = _vacant_line_display_name(
        role_code,
        contract_line_type,
        line_number,
    )
    employee_id = f"emp-{uuid.uuid4().hex[:10]}"
    employee_code = next_employee_code(conn, tenant_id)

    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            """
            INSERT INTO employees (
              id, tenant_id, employee_code, first_name, last_name,
              hire_date, fte, base_hourly_rate, seniority_hours, contract_line_type,
              is_active, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0.0, ?, 1, ?, ?)
            """,
            (
                employee_id,
                tenant_id,
                employee_code,
                first_name,
                last_name,
                hire.isoformat(),
                fte,
                hourly_rate,
                contract_line_type,
                now,
                now,
            ),
        )
        conn.execute(
            """
            INSERT INTO employee_qualifications (
              tenant_id, employee_id, qualification_id, awarded_on, expires_on, created_at
            ) VALUES (?, ?, ?, ?, NULL, ?)
            """,
            (tenant_id, employee_id, qualification_id, hire.isoformat(), now),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    return VacantLineCreateResult(
        employee_id=employee_id,
        display_name=display_name,
        line_number=line_number,
    )


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def ensure_staff_lifecycle_schema(conn: sqlite3.Connection) -> None:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(shift_assignments)")}
    if "assignment_status" not in cols:
        conn.execute(
            """
            ALTER TABLE shift_assignments
            ADD COLUMN assignment_status TEXT NOT NULL DEFAULT 'assigned'
            """
        )
    if "system_note" not in cols:
        conn.execute("ALTER TABLE shift_assignments ADD COLUMN system_note TEXT")
    if "vacated_from_employee_id" not in cols:
        conn.execute("ALTER TABLE shift_assignments ADD COLUMN vacated_from_employee_id TEXT")
    if "is_compliance_overridden" not in cols:
        conn.execute(
            """
            ALTER TABLE shift_assignments
            ADD COLUMN is_compliance_overridden INTEGER NOT NULL DEFAULT 0
            """
        )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sys_audit_log (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          recorded_at_utc TEXT NOT NULL,
          tenant_id TEXT NOT NULL,
          manager_id TEXT NOT NULL,
          action_type TEXT NOT NULL,
          employee_id TEXT,
          shifts_vacated_count INTEGER NOT NULL DEFAULT 0,
          metadata_json TEXT,
          CHECK (action_type IN (
            'employee_deactivation',
            'employee_reactivation',
            'audit_warning',
            'snapshot_restore'
          ))
        )
        """
    )
    table_sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'sys_audit_log'"
    ).fetchone()
    ddl = (table_sql[0] or "") if table_sql else ""
    if ddl and "'audit_warning'" not in ddl:
        _rebuild_sys_audit_log_action_type_check(conn)
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_sys_audit_log_tenant_recorded
        ON sys_audit_log (tenant_id, recorded_at_utc DESC)
        """
    )
    conn.commit()


def _rebuild_sys_audit_log_action_type_check(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE sys_audit_log__new (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          recorded_at_utc TEXT NOT NULL,
          tenant_id TEXT NOT NULL,
          manager_id TEXT NOT NULL,
          action_type TEXT NOT NULL,
          employee_id TEXT,
          shifts_vacated_count INTEGER NOT NULL DEFAULT 0,
          metadata_json TEXT,
          CHECK (action_type IN (
            'employee_deactivation',
            'employee_reactivation',
            'audit_warning',
            'snapshot_restore'
          ))
        );

        INSERT INTO sys_audit_log__new (
          id, recorded_at_utc, tenant_id, manager_id, action_type,
          employee_id, shifts_vacated_count, metadata_json
        )
        SELECT
          id, recorded_at_utc, tenant_id, manager_id, action_type,
          employee_id, shifts_vacated_count, metadata_json
        FROM sys_audit_log;

        DROP TABLE sys_audit_log;
        ALTER TABLE sys_audit_log__new RENAME TO sys_audit_log;

        CREATE INDEX IF NOT EXISTS idx_sys_audit_log_tenant_recorded
          ON sys_audit_log (tenant_id, recorded_at_utc DESC);
        """
    )


def log_audit_warning(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    manager_id: str,
    warning_message: str,
    employee_id: Optional[str] = None,
    is_compliance_overridden: bool = False,
    context: Optional[Mapping[str, object]] = None,
) -> int:
    """Persist a break-glass / compliance override warning to sys_audit_log."""

    ensure_staff_lifecycle_schema(conn)
    metadata = {
        "warning_message": warning_message,
        "is_compliance_overridden": is_compliance_overridden,
    }
    if context:
        metadata.update(context)
    cur = conn.execute(
        """
        INSERT INTO sys_audit_log (
          recorded_at_utc, tenant_id, manager_id, action_type,
          employee_id, shifts_vacated_count, metadata_json
        ) VALUES (?, ?, ?, 'audit_warning', ?, 0, ?)
        """,
        (
            _utc_now_iso(),
            tenant_id,
            manager_id,
            employee_id,
            json.dumps(metadata),
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def log_snapshot_restore(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    manager_id: str,
    snapshot_filename: str,
) -> int:
    ensure_staff_lifecycle_schema(conn)
    metadata = json.dumps({"snapshot_filename": snapshot_filename})
    cur = conn.execute(
        """
        INSERT INTO sys_audit_log (
          recorded_at_utc, tenant_id, manager_id, action_type,
          employee_id, shifts_vacated_count, metadata_json
        ) VALUES (?, ?, ?, 'snapshot_restore', NULL, 0, ?)
        """,
        (_utc_now_iso(), tenant_id, manager_id, metadata),
    )
    conn.commit()
    return int(cur.lastrowid)


def fetch_archived_employees(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
) -> list[Dict[str, object]]:
    rows = conn.execute(
        """
        SELECT
          e.id,
          e.employee_code,
          e.first_name,
          e.last_name,
          e.fte,
          e.hire_date,
          e.updated_at,
          GROUP_CONCAT(q.code, ', ') AS qual_codes
        FROM employees e
        LEFT JOIN employee_qualifications eq
          ON eq.tenant_id = e.tenant_id AND eq.employee_id = e.id
        LEFT JOIN qualifications q
          ON q.tenant_id = eq.tenant_id AND q.id = eq.qualification_id
        WHERE e.tenant_id = ? AND e.is_active = 0
        GROUP BY e.id
        ORDER BY e.last_name, e.first_name
        """,
        (tenant_id,),
    ).fetchall()
    return [
        {
            "id": row[0],
            "code": row[1] or "",
            "first_name": row[2],
            "last_name": row[3],
            "full_name": f"{row[2]} {row[3]}",
            "fte": float(row[4]),
            "hire_date": row[5],
            "archived_at": row[6],
            "qualifications": row[7] or "—",
        }
        for row in rows
    ]


def fetch_vacated_shifts(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    from_date: date,
    horizon_days: int = REALLOCATION_HORIZON_DAYS,
) -> list[VacatedShift]:
    to_date = from_date + timedelta(days=horizon_days)
    rows = conn.execute(
        """
        SELECT
          sa.id,
          sa.tenant_id,
          sa.schedule_period_id,
          sa.shift_template_id,
          sa.assignment_date,
          sa.vacated_from_employee_id,
          sa.system_note,
          e.first_name,
          e.last_name
        FROM shift_assignments sa
        LEFT JOIN employees e
          ON e.tenant_id = sa.tenant_id
         AND e.id = COALESCE(sa.vacated_from_employee_id, sa.employee_id)
        WHERE sa.tenant_id = ?
          AND sa.assignment_status = ?
          AND sa.assignment_date >= ?
          AND sa.assignment_date <= ?
        ORDER BY sa.assignment_date, sa.id
        """,
        (
            tenant_id,
            ASSIGNMENT_STATUS_UNASSIGNED,
            from_date.isoformat(),
            to_date.isoformat(),
        ),
    ).fetchall()
    vacated: list[VacatedShift] = []
    for row in rows:
        first = row[7] or "Former"
        last = row[8] or "Staff"
        vacated.append(
            VacatedShift(
                shift_id=row[0],
                tenant_id=row[1],
                schedule_period_id=row[2],
                shift_template_id=row[3],
                assignment_date=date.fromisoformat(row[4]),
                vacated_from_employee_id=row[5] or "",
                vacated_from_employee_name=f"{first} {last}".strip(),
                system_note=row[6] or STAFF_DEACTIVATION_NOTE,
            )
        )
    return vacated


def log_employee_deactivation(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    manager_id: str,
    employee_id: str,
    employee_name: str,
    shifts_vacated_count: int,
) -> int:
    ensure_staff_lifecycle_schema(conn)
    metadata = json.dumps(
        {
            "employee_name": employee_name,
            "note": "Employee archived; future shifts marked unassigned.",
        }
    )
    cur = conn.execute(
        """
        INSERT INTO sys_audit_log (
          recorded_at_utc, tenant_id, manager_id, action_type,
          employee_id, shifts_vacated_count, metadata_json
        ) VALUES (?, ?, ?, 'employee_deactivation', ?, ?, ?)
        """,
        (
            _utc_now_iso(),
            tenant_id,
            manager_id,
            employee_id,
            shifts_vacated_count,
            metadata,
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def deactivate_employee(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    employee_id: str,
    manager_id: str,
    as_of_date: Optional[date] = None,
) -> DeactivationResult:
    ensure_staff_lifecycle_schema(conn)
    as_of = as_of_date or date.today()

    row = conn.execute(
        """
        SELECT first_name, last_name, is_active
        FROM employees
        WHERE tenant_id = ? AND id = ?
        """,
        (tenant_id, employee_id),
    ).fetchone()
    if row is None:
        raise StaffLifecycleError("Employee was not found on this tenant roster.")
    if int(row[2]) != 1:
        raise StaffLifecycleError("Employee is already archived.")

    employee_name = f"{row[0]} {row[1]}"
    now = _utc_now_iso()

    future_rows = conn.execute(
        """
        SELECT
          id, schedule_period_id, shift_template_id, assignment_date
        FROM shift_assignments
        WHERE tenant_id = ?
          AND employee_id = ?
          AND assignment_date >= ?
          AND COALESCE(assignment_status, ?) = ?
        ORDER BY assignment_date, id
        """,
        (
            tenant_id,
            employee_id,
            as_of.isoformat(),
            ASSIGNMENT_STATUS_ASSIGNED,
            ASSIGNMENT_STATUS_ASSIGNED,
        ),
    ).fetchall()

    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute(
        """
        UPDATE employees
        SET is_active = 0, updated_at = ?
        WHERE tenant_id = ? AND id = ?
        """,
        (now, tenant_id, employee_id),
    )

    vacated_shifts: list[VacatedShift] = []
    for shift_row in future_rows:
        shift_id = shift_row[0]
        conn.execute(
            """
            UPDATE shift_assignments
            SET assignment_status = ?,
                system_note = ?,
                vacated_from_employee_id = ?,
                updated_at = ?
            WHERE id = ? AND tenant_id = ?
            """,
            (
                ASSIGNMENT_STATUS_UNASSIGNED,
                STAFF_DEACTIVATION_NOTE,
                employee_id,
                now,
                shift_id,
                tenant_id,
            ),
        )
        vacated_shifts.append(
            VacatedShift(
                shift_id=shift_id,
                tenant_id=tenant_id,
                schedule_period_id=shift_row[1],
                shift_template_id=shift_row[2],
                assignment_date=date.fromisoformat(shift_row[3]),
                vacated_from_employee_id=employee_id,
                vacated_from_employee_name=employee_name,
                system_note=STAFF_DEACTIVATION_NOTE,
            )
        )

    audit_log_id = log_employee_deactivation(
        conn,
        tenant_id=tenant_id,
        manager_id=manager_id,
        employee_id=employee_id,
        employee_name=employee_name,
        shifts_vacated_count=len(vacated_shifts),
    )

    return DeactivationResult(
        employee_id=employee_id,
        employee_name=employee_name,
        shifts_vacated_count=len(vacated_shifts),
        vacated_shifts=tuple(vacated_shifts),
        audit_log_id=audit_log_id,
    )


def fill_vacated_shift(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    shift_id: str,
    new_employee_id: str,
    rules: JurisdictionRules,
    period_start: date,
    period_end: date,
    weeks_in_period: int,
    employees: Sequence[EmployeeProfile],
    all_assignments: Sequence[ScheduledShift],
    shift_templates: Dict[str, ShiftTemplateInfo],
    shift_required_qualifications: Dict[str, Set[str]],
    employee_target_hours: Optional[Mapping[str, float]] = None,
    availability_blocked: Optional[Mapping[str, Set[date]]] = None,
    actor: str = "manager",
) -> VacatedShiftFillResult:
    ensure_staff_lifecycle_schema(conn)
    row = conn.execute(
        """
        SELECT schedule_period_id, shift_template_id, assignment_date,
               vacated_from_employee_id, employee_id
        FROM shift_assignments
        WHERE tenant_id = ? AND id = ? AND assignment_status = ?
        """,
        (tenant_id, shift_id, ASSIGNMENT_STATUS_UNASSIGNED),
    ).fetchone()
    if row is None:
        return VacatedShiftFillResult(
            success=False,
            message="Vacated shift was not found or is already filled.",
            shift_id=shift_id,
            new_employee_id=new_employee_id,
            assignment_date=date.today(),
        )

    assignment_date = date.fromisoformat(row[2])
    shift_template_id = row[1]
    employee_lookup = {employee.id: employee for employee in employees}
    if new_employee_id not in employee_lookup:
        return VacatedShiftFillResult(
            success=False,
            message="Replacement employee was not found on the active roster.",
            shift_id=shift_id,
            new_employee_id=new_employee_id,
            assignment_date=assignment_date,
        )

    existing = conn.execute(
        """
        SELECT 1
        FROM shift_assignments
        WHERE tenant_id = ? AND employee_id = ? AND assignment_date = ?
          AND COALESCE(assignment_status, ?) = ?
        LIMIT 1
        """,
        (
            tenant_id,
            new_employee_id,
            assignment_date.isoformat(),
            ASSIGNMENT_STATUS_ASSIGNED,
            ASSIGNMENT_STATUS_ASSIGNED,
        ),
    ).fetchone()
    if existing:
        return VacatedShiftFillResult(
            success=False,
            message="Replacement employee already has an assignment on this date.",
            shift_id=shift_id,
            new_employee_id=new_employee_id,
            assignment_date=assignment_date,
        )

    violation = validate_assignment_change(
        rules=rules,
        period_start=period_start,
        period_end=period_end,
        weeks_in_period=weeks_in_period,
        employee=employee_lookup[new_employee_id],
        all_assignments=all_assignments,
        shift_templates=shift_templates,
        shift_required_qualifications=shift_required_qualifications,
        assignment_date=assignment_date,
        new_shift_template_id=shift_template_id,
        employee_target_hours=employee_target_hours,
        availability_blocked=availability_blocked,
    )
    if violation:
        return VacatedShiftFillResult(
            success=False,
            message=violation,
            shift_id=shift_id,
            new_employee_id=new_employee_id,
            assignment_date=assignment_date,
        )

    now = _utc_now_iso()
    try:
        conn.execute(
            """
            UPDATE shift_assignments
            SET employee_id = ?,
                assignment_status = ?,
                system_note = NULL,
                vacated_from_employee_id = NULL,
                updated_at = ?
            WHERE id = ? AND tenant_id = ?
            """,
            (
                new_employee_id,
                ASSIGNMENT_STATUS_ASSIGNED,
                now,
                shift_id,
                tenant_id,
            ),
        )
        conn.commit()
    except sqlite3.IntegrityError as exc:
        conn.rollback()
        return VacatedShiftFillResult(
            success=False,
            message=f"Database rejected reassignment: {exc}",
            shift_id=shift_id,
            new_employee_id=new_employee_id,
            assignment_date=assignment_date,
        )

    from lab_scheduler.audit.schedule_log import log_manual_edit

    template = shift_templates.get(shift_template_id)
    shift_code = template.code[:1].upper() if template else "?"
    log_manual_edit(
        conn,
        tenant_id=tenant_id,
        schedule_period_id=row[0],
        employee_id=new_employee_id,
        shift_date=assignment_date,
        previous_shift_code="UNASSIGNED",
        new_shift_code=shift_code,
        actor=f"{actor} [reallocation-wizard]",
    )

    new_name = employee_lookup[new_employee_id].full_name
    return VacatedShiftFillResult(
        success=True,
        message=f"Shift reassigned to {new_name}.",
        shift_id=shift_id,
        new_employee_id=new_employee_id,
        assignment_date=assignment_date,
    )
