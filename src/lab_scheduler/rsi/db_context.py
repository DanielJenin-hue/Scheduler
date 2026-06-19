from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date
from typing import Dict, List, Mapping, Optional, Set

from lab_scheduler.compliance.engine import ScheduledShift, ShiftTemplateInfo
from lab_scheduler.scheduling.profiles import EmployeeProfile


@dataclass(frozen=True, slots=True)
class ScheduleAuditContext:
    tenant_id: str
    period_id: str
    period_start: date
    period_end: date
    weeks_in_period: int
    employees: List[EmployeeProfile]
    assignments: List[Dict]
    shift_templates: Dict[str, ShiftTemplateInfo]
    shift_required_qualifications: Dict[str, Set[str]]
    employee_target_hours: Dict[str, float]
    qual_codes: Dict[str, str]


def count_active_tenants(conn: sqlite3.Connection) -> int:
    try:
        row = conn.execute(
            """
            SELECT COUNT(*)
            FROM tenants
            WHERE COALESCE(subscription_status, 'trial') = 'active'
            """
        ).fetchone()
    except sqlite3.Error:
        return 0
    return int(row[0] if row else 0)


def _shift_templates_for_compliance(templates: Mapping[str, Dict]) -> Dict[str, ShiftTemplateInfo]:
    converted: Dict[str, ShiftTemplateInfo] = {}
    for template_id, template in templates.items():
        converted[template_id] = ShiftTemplateInfo(
            id=template_id,
            code=str(template.get("code", "")),
            name=str(template.get("name", template.get("code", ""))),
            start_time=str(template.get("start_time", "07:00")),
            end_time=str(template.get("end_time", "15:00")),
            duration_minutes=int(template.get("duration_minutes", 480)),
            crosses_midnight=bool(template.get("crosses_midnight", False)),
        )
    return converted


def load_schedule_audit_context(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    period_id: str,
    employees: List[Dict],
    templates: Dict[str, Dict],
    assignments: List[Dict],
    emp_quals: Dict[str, Set[str]],
    target_hours: Dict[str, float],
    qual_code_map: Dict[str, str],
) -> ScheduleAuditContext:
    period_row = conn.execute(
        """
        SELECT period_start, period_end_inclusive, week_count
        FROM schedule_periods
        WHERE tenant_id = ? AND id = ?
        """,
        (tenant_id, period_id),
    ).fetchone()
    if period_row is None:
        raise ValueError(f"Unknown schedule period {period_id!r}")

    period_start = date.fromisoformat(str(period_row[0]))
    period_end = date.fromisoformat(str(period_row[1]))
    weeks = int(period_row[2])

    shift_quals: Dict[str, Set[str]] = {}
    for row in conn.execute(
        """
        SELECT shift_template_id, qualification_id
        FROM shift_template_qualifications
        WHERE tenant_id = ?
        """,
        (tenant_id,),
    ):
        shift_quals.setdefault(str(row[0]), set()).add(str(row[1]))

    profiles = [
        EmployeeProfile(
            id=str(employee["id"]),
            full_name=str(employee.get("full_name", employee.get("Employee", ""))),
            fte=float(employee.get("fte", 1.0) or 1.0),
            qualification_ids=emp_quals.get(str(employee["id"]), set()),
            seniority_hours=float(employee.get("seniority_hours", 0.0) or 0.0),
            base_hourly_rate=float(employee.get("base_hourly_rate", 40.0) or 40.0),
            contract_line_type=employee.get("contract_line_type"),
        )
        for employee in employees
    ]

    return ScheduleAuditContext(
        tenant_id=tenant_id,
        period_id=period_id,
        period_start=period_start,
        period_end=period_end,
        weeks_in_period=weeks,
        employees=profiles,
        assignments=assignments,
        shift_templates=_shift_templates_for_compliance(templates),
        shift_required_qualifications=shift_quals,
        employee_target_hours=target_hours,
        qual_codes=qual_code_map,
    )


def assignments_to_scheduled(
    assignments: List[Dict],
    employees: List[Dict],
) -> List[ScheduledShift]:
    names = {str(employee["id"]): str(employee.get("full_name", "")) for employee in employees}
    scheduled: List[ScheduledShift] = []
    for assignment in assignments:
        assignment_date = assignment["assignment_date"]
        if not isinstance(assignment_date, date):
            assignment_date = date.fromisoformat(str(assignment_date))
        employee_id = str(assignment["employee_id"])
        scheduled.append(
            ScheduledShift(
                employee_id=employee_id,
                employee_name=names.get(employee_id, employee_id),
                assignment_date=assignment_date,
                shift_template_id=str(assignment["shift_template_id"]),
            )
        )
    return scheduled
