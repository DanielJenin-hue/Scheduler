"""Shared breakroom export metrics and HTML builder (UI + headless scripts)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Mapping, Optional, Sequence

from lab_scheduler.compliance.engine import ShiftTemplateInfo
from lab_scheduler.compliance.jurisdictions import JurisdictionRules
from lab_scheduler.engine.demand import expand_schedule_slots, portage_concurrent_demands
from lab_scheduler.scheduling.auto_generate import PlannedAssignment, _seat_fill_counts
from lab_scheduler.scheduling.auto_pilot import dedupe_planned_assignments
from lab_scheduler.scheduling.breakroom_print import (
    build_required_coverage_gaps_by_day,
    generate_breakroom_print_html,
)
from lab_scheduler.scheduling.persist_validation import (
    UNION_PERSIST_CODES,
    find_core_persist_violations,
)
from lab_scheduler.scheduling.profiles import EmployeeProfile
from lab_scheduler.scheduling.schedule_tallies import shift_band_from_template_code


@dataclass(frozen=True, slots=True)
class BreakroomExportMetrics:
    coverage_gaps_by_day: dict[date, int]
    total_open_gaps: int
    union_violation_count: int


def planned_assignments_from_mappings(
    assignment_rows: Sequence[Mapping[str, object] | PlannedAssignment],
) -> list[PlannedAssignment]:
    planned: list[PlannedAssignment] = []
    for row in assignment_rows:
        if isinstance(row, PlannedAssignment):
            planned.append(row)
            continue
        assignment_date = row.get("assignment_date")
        if not isinstance(assignment_date, date):
            assignment_date = date.fromisoformat(str(assignment_date))
        assignment = PlannedAssignment(
            str(row["employee_id"]),
            str(row["shift_template_id"]),
            assignment_date,
        )
        if bool(row.get("master_template_frozen", False)):
            assignment.master_template_frozen = True
        planned.append(assignment)
    return planned


def employee_profiles_from_mappings(
    employees: Sequence[Mapping[str, object]],
    *,
    qual_ids_by_employee: Optional[Mapping[str, set[str]]] = None,
) -> list[EmployeeProfile]:
    qual_ids_by_employee = qual_ids_by_employee or {}
    profiles: list[EmployeeProfile] = []
    for employee in employees:
        employee_id = str(employee.get("id", ""))
        profiles.append(
            EmployeeProfile(
                id=employee_id,
                full_name=str(
                    employee.get("full_name", employee.get("Employee", employee_id))
                ),
                fte=float(employee.get("fte", 1.0) or 1.0),
                qualification_ids=set(qual_ids_by_employee.get(employee_id, set())),
                seniority_hours=float(employee.get("seniority_hours", 0.0) or 0.0),
                base_hourly_rate=float(employee.get("base_hourly_rate", 40.0) or 40.0),
                contract_line_type=employee.get("contract_line_type"),
            )
        )
    return profiles


def compute_breakroom_export_metrics(
    *,
    assignments: Sequence[PlannedAssignment],
    employees: Sequence[EmployeeProfile],
    shift_templates: Mapping[str, ShiftTemplateInfo],
    period_start: date,
    period_end: date,
    weeks_in_period: int,
    rules: JurisdictionRules,
    qual_codes: Mapping[str, str],
    compliance_first: bool = True,
) -> BreakroomExportMetrics:
    """Required-seat gap counts and union violation tally for breakroom export."""

    template_id_to_band = {
        template_id: shift_band_from_template_code(info.code)
        for template_id, info in shift_templates.items()
    }
    deduped = dedupe_planned_assignments(
        assignments,
        template_id_to_band=template_id_to_band,
    )
    expanded = expand_schedule_slots(
        period_start=period_start,
        period_end=period_end,
        shift_templates=dict(shift_templates),
        concurrent_demands=portage_concurrent_demands(),
        employees=employees,
        rules=rules,
        weeks_in_period=weeks_in_period,
    )
    fill_counts = _seat_fill_counts(deduped, employees, qual_codes)
    coverage_gaps_by_day = build_required_coverage_gaps_by_day(
        expanded,
        fill_counts,
        shift_templates,
    )
    total_open_gaps = sum(coverage_gaps_by_day.values())
    union_violation_count = sum(
        1
        for violation in find_core_persist_violations(
            assignments=deduped,
            employees=employees,
            shift_templates=shift_templates,
            period_start=period_start,
            period_end=period_end,
            weeks_in_period=weeks_in_period,
            rules=rules,
            qual_codes=qual_codes,
            template_id_to_band=template_id_to_band,
            compliance_first=compliance_first,
        )
        if violation.code in UNION_PERSIST_CODES
    )
    return BreakroomExportMetrics(
        coverage_gaps_by_day=coverage_gaps_by_day,
        total_open_gaps=total_open_gaps,
        union_violation_count=union_violation_count,
    )


def build_hero_breakroom_html(
    *,
    facility_name: str,
    period_name: str,
    period_start: date,
    period_end: date,
    week_count: int,
    employees: Sequence[Mapping[str, object]],
    dates: Sequence[date],
    schedule_rows: Sequence[Mapping[str, object]],
    planned_assignments: Sequence[PlannedAssignment | Mapping[str, object]],
    shift_templates: Mapping[str, ShiftTemplateInfo],
    rules: JurisdictionRules,
    qual_codes: Mapping[str, str],
    qual_ids_by_employee: Optional[Mapping[str, set[str]]] = None,
    compliance_verified_on: Optional[date] = None,
    schedule_archetype: str = "STANDARD",
    paper_size: str = "legal",
    compliance_first: bool = True,
    contract_target_hours_by_employee: Optional[Mapping[str, float]] = None,
) -> str:
    """Build sales-ready breakroom HTML with statutory banner and accurate gap row."""

    profiles = employee_profiles_from_mappings(
        employees,
        qual_ids_by_employee=qual_ids_by_employee,
    )
    planned = planned_assignments_from_mappings(planned_assignments)
    metrics = compute_breakroom_export_metrics(
        assignments=planned,
        employees=profiles,
        shift_templates=shift_templates,
        period_start=period_start,
        period_end=period_end,
        weeks_in_period=week_count,
        rules=rules,
        qual_codes=qual_codes,
        compliance_first=compliance_first,
    )
    return generate_breakroom_print_html(
        facility_name=facility_name,
        period_name=period_name,
        period_start=period_start,
        period_end=period_end,
        week_count=week_count,
        employees=employees,
        dates=dates,
        schedule_rows=schedule_rows,
        compliance_verified_on=compliance_verified_on,
        schedule_archetype=schedule_archetype,
        coverage_gaps_by_day=metrics.coverage_gaps_by_day,
        union_violation_count=metrics.union_violation_count,
        total_open_gaps=metrics.total_open_gaps,
        paper_size=paper_size,
        contract_target_hours_by_employee=contract_target_hours_by_employee,
    )
