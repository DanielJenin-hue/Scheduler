from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Dict, Mapping, Optional, Sequence, Set

from lab_scheduler.compliance.engine import ScheduledShift, ShiftTemplateInfo
from lab_scheduler.compliance.jurisdictions import JurisdictionRules
from lab_scheduler.models.employee import is_critical_contract_line_violation
from lab_scheduler.telemetry.sentry_watcher import ensure_sentry_schema, utc_now_iso

from .profiles import EmployeeProfile
from .auto_generate import (
    _is_qualified,
    validate_assignment_change,
)
from .seniority_ranking import evaluate_seniority_bypass, rank_profiles_cba


class ShiftSwapError(Exception):
    """Raised when a reactive shift swap cannot be executed safely."""


@dataclass(frozen=True, slots=True)
class ShiftAssignmentRecord:
    shift_id: str
    tenant_id: str
    schedule_period_id: str
    employee_id: str
    shift_template_id: str
    assignment_date: date


@dataclass(frozen=True, slots=True)
class SwapCandidate:
    employee_id: str
    employee_name: str
    score: float
    is_eligible: bool
    block_reason: Optional[str] = None
    seniority_bypass: bool = False
    seniority_bypass_justification: Optional[str] = None
    requires_seniority_justification: bool = False


@dataclass(frozen=True, slots=True)
class ShiftSwapResult:
    success: bool
    message: str
    requires_hitl: bool
    shift_id: str
    old_employee_id: str
    new_employee_id: str
    assignment_date: date
    shift_template_id: str
    previous_shift_code: str
    new_shift_code: str
    is_compliance_overridden: bool = False


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def fetch_shift_assignment(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    shift_id: str,
) -> Optional[ShiftAssignmentRecord]:
    row = conn.execute(
        """
        SELECT id, tenant_id, schedule_period_id, employee_id, shift_template_id, assignment_date
        FROM shift_assignments
        WHERE tenant_id = ? AND id = ?
          AND COALESCE(assignment_status, 'assigned') = 'assigned'
        """,
        (tenant_id, shift_id),
    ).fetchone()
    if row is None:
        return None
    return ShiftAssignmentRecord(
        shift_id=row[0],
        tenant_id=row[1],
        schedule_period_id=row[2],
        employee_id=row[3],
        shift_template_id=row[4],
        assignment_date=date.fromisoformat(row[5]),
    )


def fetch_shift_assignment_for_cell(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    schedule_period_id: str,
    employee_id: str,
    assignment_date: date,
) -> Optional[ShiftAssignmentRecord]:
    row = conn.execute(
        """
        SELECT id, tenant_id, schedule_period_id, employee_id, shift_template_id, assignment_date
        FROM shift_assignments
        WHERE tenant_id = ? AND schedule_period_id = ? AND employee_id = ? AND assignment_date = ?
          AND COALESCE(assignment_status, 'assigned') = 'assigned'
        """,
        (tenant_id, schedule_period_id, employee_id, assignment_date.isoformat()),
    ).fetchone()
    if row is None:
        return None
    return ShiftAssignmentRecord(
        shift_id=row[0],
        tenant_id=row[1],
        schedule_period_id=row[2],
        employee_id=row[3],
        shift_template_id=row[4],
        assignment_date=date.fromisoformat(row[5]),
    )


def _employee_has_assignment_on_date(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    employee_id: str,
    assignment_date: date,
    exclude_shift_id: Optional[str] = None,
) -> bool:
    if exclude_shift_id:
        row = conn.execute(
            """
            SELECT 1
            FROM shift_assignments
            WHERE tenant_id = ? AND employee_id = ? AND assignment_date = ? AND id != ?
              AND COALESCE(assignment_status, 'assigned') = 'assigned'
            LIMIT 1
            """,
            (tenant_id, employee_id, assignment_date.isoformat(), exclude_shift_id),
        ).fetchone()
    else:
        row = conn.execute(
            """
            SELECT 1
            FROM shift_assignments
            WHERE tenant_id = ? AND employee_id = ? AND assignment_date = ?
              AND COALESCE(assignment_status, 'assigned') = 'assigned'
            LIMIT 1
            """,
            (tenant_id, employee_id, assignment_date.isoformat()),
        ).fetchone()
    return row is not None


def _shift_short_code(shift_templates: Mapping[str, ShiftTemplateInfo], shift_template_id: str) -> str:
    template = shift_templates.get(shift_template_id)
    if template is None:
        return shift_template_id
    code = template.code.upper()
    if code.startswith("MOR"):
        return "M"
    if code.startswith("EVE"):
        return "E"
    if code.startswith("NIG"):
        return "N"
    return code[:1]


def list_swap_candidates(
    *,
    shift_assignment: ShiftAssignmentRecord,
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
    include_ineligible: bool = True,
    limit: int = 12,
) -> list[SwapCandidate]:
    template = shift_templates.get(shift_assignment.shift_template_id)
    if template is None:
        return []

    required = shift_required_qualifications.get(shift_assignment.shift_template_id, set())
    qualified_profiles = [
        employee
        for employee in employees
        if employee.id != shift_assignment.employee_id and _is_qualified(employee, required)
    ]
    ineligible: list[SwapCandidate] = []
    eligible_profiles: list[EmployeeProfile] = []
    ineligible_reasons: Dict[str, str] = {}

    for employee in qualified_profiles:
        violation = validate_assignment_change(
            rules=rules,
            period_start=period_start,
            period_end=period_end,
            weeks_in_period=weeks_in_period,
            employee=employee,
            all_assignments=all_assignments,
            shift_templates=shift_templates,
            shift_required_qualifications=shift_required_qualifications,
            assignment_date=shift_assignment.assignment_date,
            new_shift_template_id=shift_assignment.shift_template_id,
            employee_target_hours=employee_target_hours,
            availability_blocked=availability_blocked,
        )
        if violation:
            ineligible_reasons[employee.id] = violation
            if include_ineligible:
                ineligible.append(
                    SwapCandidate(
                        employee_id=employee.id,
                        employee_name=employee.full_name,
                        score=-1.0,
                        is_eligible=False,
                        block_reason=violation,
                    )
                )
            continue
        eligible_profiles.append(employee)

    for employee in employees:
        if employee.id == shift_assignment.employee_id:
            continue
        if not _is_qualified(employee, required) and include_ineligible:
            ineligible.append(
                SwapCandidate(
                    employee_id=employee.id,
                    employee_name=employee.full_name,
                    score=-1.0,
                    is_eligible=False,
                    block_reason="Missing required qualification (MLT/MLA).",
                )
            )

    eligible_ids = {employee.id for employee in eligible_profiles}
    eligible_ranked: list[SwapCandidate] = []
    for employee in rank_profiles_cba(eligible_profiles):
        bypass = evaluate_seniority_bypass(
            qualified_profiles=qualified_profiles,
            eligible_ids=eligible_ids,
            selected=employee,
            ineligible_reasons=ineligible_reasons,
        )
        eligible_ranked.append(
            SwapCandidate(
                employee_id=employee.id,
                employee_name=employee.full_name,
                score=employee.seniority_hours,
                is_eligible=True,
                seniority_bypass=bypass is not None,
                seniority_bypass_justification=bypass.justification if bypass else None,
                requires_seniority_justification=(
                    bypass.requires_manual_justification if bypass else False
                ),
            )
        )

    ordered = eligible_ranked[:limit]
    if include_ineligible:
        ordered.extend(ineligible[: max(0, limit - len(ordered))])
    return ordered


def flag_reactive_swap_hitl(
    conn: sqlite3.Connection,
    *,
    tenant_id: Optional[str],
    username: Optional[str],
    summary: str,
) -> int:
    ensure_sentry_schema(conn)
    cur = conn.execute(
        """
        INSERT INTO sys_sentry_logs (
          recorded_at_utc, tenant_id, username, exception_type, error_message,
          target_file, line_number, clean_traceback, resolution_status, proposed_patch_code
        ) VALUES (?, ?, ?, 'ReactiveSwap', ?, 'scripts/app.py', NULL, ?, 'awaiting_review', ?)
        """,
        (
            utc_now_iso(),
            tenant_id,
            username,
            "Manual reactive shift swap",
            summary,
            summary,
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def execute_shift_swap(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    shift_id: str,
    old_employee_id: str,
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
    username: Optional[str] = None,
    manual: bool = True,
    override_rules: bool = False,
    bypass_compliance_rules: bool = False,
    seniority_bypass_flag: bool = False,
    seniority_bypass_justification: Optional[str] = None,
) -> ShiftSwapResult:
    compliance_override = override_rules or bypass_compliance_rules
    assignment = fetch_shift_assignment(conn, tenant_id=tenant_id, shift_id=shift_id)
    if assignment is None:
        raise ShiftSwapError(f"Shift assignment `{shift_id}` was not found.")
    if assignment.employee_id != old_employee_id:
        raise ShiftSwapError("Shift assignment owner does not match the provided old employee.")

    previous_code = _shift_short_code(shift_templates, assignment.shift_template_id)
    employee_lookup = {employee.id: employee for employee in employees}
    if new_employee_id not in employee_lookup:
        raise ShiftSwapError("Replacement employee was not found on the active roster.")

    if _employee_has_assignment_on_date(
        conn,
        tenant_id=tenant_id,
        employee_id=new_employee_id,
        assignment_date=assignment.assignment_date,
        exclude_shift_id=shift_id,
    ):
        return ShiftSwapResult(
            success=False,
            message="Replacement employee already has an assignment on this date.",
            requires_hitl=False,
            shift_id=shift_id,
            old_employee_id=old_employee_id,
            new_employee_id=new_employee_id,
            assignment_date=assignment.assignment_date,
            shift_template_id=assignment.shift_template_id,
            previous_shift_code=previous_code,
            new_shift_code=previous_code,
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
        assignment_date=assignment.assignment_date,
        new_shift_template_id=assignment.shift_template_id,
        employee_target_hours=employee_target_hours,
        availability_blocked=availability_blocked,
    )
    if violation and is_critical_contract_line_violation(violation):
        return ShiftSwapResult(
            success=False,
            message=violation,
            requires_hitl=True,
            shift_id=shift_id,
            old_employee_id=old_employee_id,
            new_employee_id=new_employee_id,
            assignment_date=assignment.assignment_date,
            shift_template_id=assignment.shift_template_id,
            previous_shift_code=previous_code,
            new_shift_code=previous_code,
            is_compliance_overridden=False,
        )
    if violation and not compliance_override:
        requires_hitl = "11h rest before Morning after Evening/Night" in violation
        return ShiftSwapResult(
            success=False,
            message=violation,
            requires_hitl=requires_hitl,
            shift_id=shift_id,
            old_employee_id=old_employee_id,
            new_employee_id=new_employee_id,
            assignment_date=assignment.assignment_date,
            shift_template_id=assignment.shift_template_id,
            previous_shift_code=previous_code,
            new_shift_code=previous_code,
            is_compliance_overridden=False,
        )

    from lab_scheduler.staff.lifecycle import ensure_staff_lifecycle_schema

    ensure_staff_lifecycle_schema(conn)
    conn.execute("PRAGMA foreign_keys = ON;")
    try:
        conn.execute(
            """
            UPDATE shift_assignments
            SET employee_id = ?, updated_at = ?, is_compliance_overridden = ?
            WHERE id = ? AND tenant_id = ? AND employee_id = ?
            """,
            (
                new_employee_id,
                _now_iso(),
                1 if compliance_override and violation else 0,
                shift_id,
                tenant_id,
                old_employee_id,
            ),
        )
        conn.commit()
    except sqlite3.IntegrityError as exc:
        conn.rollback()
        return ShiftSwapResult(
            success=False,
            message=f"Database rejected swap: {exc}",
            requires_hitl=True,
            shift_id=shift_id,
            old_employee_id=old_employee_id,
            new_employee_id=new_employee_id,
            assignment_date=assignment.assignment_date,
            shift_template_id=assignment.shift_template_id,
            previous_shift_code=previous_code,
            new_shift_code=previous_code,
            is_compliance_overridden=False,
        )

    new_name = employee_lookup[new_employee_id].full_name
    old_name = employee_lookup[old_employee_id].full_name
    summary = (
        f"Reactive swap on {assignment.assignment_date.isoformat()}: "
        f"{old_name} ({old_employee_id}) -> {new_name} ({new_employee_id}) "
        f"for shift {previous_code} [{shift_id}]"
    )
    if compliance_override and violation:
        summary += f" | BREAK-GLASS OVERRIDE: {violation}"
        from lab_scheduler.staff.lifecycle import log_audit_warning

        log_audit_warning(
            conn,
            tenant_id=tenant_id,
            manager_id=actor,
            warning_message=(
                "I am overriding compliance rules for this assignment. "
                "This will be captured in the audit trail."
            ),
            employee_id=new_employee_id,
            is_compliance_overridden=True,
            context={
                "shift_id": shift_id,
                "assignment_date": assignment.assignment_date.isoformat(),
                "violation": violation,
                "override_type": "reactive_swap",
            },
        )
    elif override_rules and violation and not bypass_compliance_rules:
        summary += f" | OVERRIDE: {violation}"

    from lab_scheduler.audit.schedule_log import log_reactive_swap

    log_reactive_swap(
        conn,
        tenant_id=tenant_id,
        schedule_period_id=assignment.schedule_period_id,
        shift_id=shift_id,
        old_employee_id=old_employee_id,
        new_employee_id=new_employee_id,
        shift_date=assignment.assignment_date,
        previous_shift_code=previous_code,
        actor=actor,
        summary=summary,
        seniority_bypass_flag=seniority_bypass_flag,
        seniority_bypass_justification=seniority_bypass_justification,
    )

    requires_hitl = manual or (compliance_override and bool(violation))
    if requires_hitl:
        flag_reactive_swap_hitl(
            conn,
            tenant_id=tenant_id,
            username=username or actor,
            summary=summary,
        )

    return ShiftSwapResult(
        success=True,
        message=f"Shift reassigned to {new_name}.",
        requires_hitl=requires_hitl,
        shift_id=shift_id,
        old_employee_id=old_employee_id,
        new_employee_id=new_employee_id,
        assignment_date=assignment.assignment_date,
        shift_template_id=assignment.shift_template_id,
        previous_shift_code=previous_code,
        new_shift_code=previous_code,
        is_compliance_overridden=compliance_override and bool(violation),
    )
