from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Dict, List, Mapping, Optional, Sequence, Set

from lab_scheduler.compliance.engine import ScheduledShift, ShiftTemplateInfo
from lab_scheduler.compliance.jurisdictions import JurisdictionRules
from lab_scheduler.engine.constraints import (
    CoverageTierResult,
    CoverageTierTarget,
    build_coverage_targets_from_roster,
    compute_coverage_success_rate_pct,
    evaluate_coverage_tier_results,
)
from lab_scheduler.scheduling.profiles import EmployeeProfile


@dataclass(frozen=True, slots=True)
class ManagerHealthSnapshot:
    compliance_health_pct: float
    coverage_success_pct: float
    gap_alert_count: int

    @property
    def compliance_status(self) -> str:
        return "healthy" if self.compliance_health_pct >= 100.0 else "warn"

    @property
    def coverage_status(self) -> str:
        return "healthy" if self.coverage_success_pct >= 90.0 else "warn"

    @property
    def gap_status(self) -> str:
        return "healthy" if self.gap_alert_count == 0 else "warn"


@dataclass(frozen=True, slots=True)
class UnderTargetEmployee:
    employee_id: str
    full_name: str
    role: str
    seniority_hours: float
    fte_deficit: float
    scheduled_hours: float
    period_target_hours: float
    contractual_fte: float
    actual_fte: float


def _role_from_qualifications(qualification_ids: Set[str], qual_codes: Mapping[str, str]) -> str:
    for qual_id in qualification_ids:
        code = qual_codes.get(qual_id, "")
        if code == "MLA":
            return "MLA"
        if code == "MLT":
            return "MLT"
    return "—"


def build_manager_health_snapshot(
    *,
    compliance_error_count: int,
    coverage_success_pct: float,
    gap_alert_count: int,
) -> ManagerHealthSnapshot:
    compliance_pct = 100.0 if compliance_error_count == 0 else max(
        0.0,
        100.0 - compliance_error_count,
    )
    return ManagerHealthSnapshot(
        compliance_health_pct=round(compliance_pct, 1),
        coverage_success_pct=round(coverage_success_pct, 1),
        gap_alert_count=gap_alert_count,
    )


def compute_live_coverage_success_pct(
    *,
    rules: JurisdictionRules,
    period_start: date,
    period_end: date,
    weeks_in_period: int,
    employees: Sequence[EmployeeProfile],
    assignments: Sequence[ScheduledShift],
    shift_templates: Mapping[str, ShiftTemplateInfo],
    qual_codes: Mapping[str, str],
) -> float:
    targets = build_coverage_targets_from_roster(employees, qual_codes=qual_codes)
    employee_hours: Dict[str, float] = {employee.id: 0.0 for employee in employees}
    for assignment in assignments:
        template = shift_templates.get(assignment.shift_template_id)
        if template is None:
            continue
        employee_hours[assignment.employee_id] = (
            employee_hours.get(assignment.employee_id, 0.0)
            + template.duration_minutes / 60.0
        )
    results = evaluate_coverage_tier_results(
        targets=targets,
        employee_hours=employee_hours,
        rules=rules,
        weeks_in_period=weeks_in_period,
        shift_templates=shift_templates,
        period_start=period_start,
        period_end=period_end,
    )
    return compute_coverage_success_rate_pct(results)


def count_open_shift_gaps(
    *,
    period_start: date,
    period_end: date,
    shift_templates: Mapping[str, ShiftTemplateInfo],
    assignments: Sequence[ScheduledShift],
    schedule_archetype: str = "STANDARD",
) -> int:
    from lab_scheduler.scheduling.auto_generate import list_open_shift_slots

    return len(
        list_open_shift_slots(
            period_start=period_start,
            period_end=period_end,
            shift_templates=dict(shift_templates),
            assignments=list(assignments),
            schedule_archetype=schedule_archetype,
        )
    )


def build_under_target_roster(
    tier_results: Sequence[CoverageTierResult],
    *,
    employees_by_id: Mapping[str, EmployeeProfile],
    qual_codes: Mapping[str, str],
) -> List[UnderTargetEmployee]:
    rows: List[UnderTargetEmployee] = []
    for result in tier_results:
        if result.meets_target and result.gap_fte < 0.05:
            continue
        profile = employees_by_id.get(result.tier_id)
        if profile is None:
            continue
        rows.append(
            UnderTargetEmployee(
                employee_id=result.tier_id,
                full_name=profile.full_name,
                role=_role_from_qualifications(set(profile.qualification_ids), qual_codes),
                seniority_hours=profile.seniority_hours,
                fte_deficit=round(result.actual_fte - result.target_fte, 2),
                scheduled_hours=result.actual_hours,
                period_target_hours=result.period_target_hours,
                contractual_fte=result.target_fte,
                actual_fte=result.actual_fte,
            )
        )
    rows.sort(key=lambda row: (row.fte_deficit, -row.seniority_hours))
    return rows


def evaluate_period_coverage(
    *,
    rules: JurisdictionRules,
    period_start: date,
    period_end: date,
    weeks_in_period: int,
    employees: Sequence[EmployeeProfile],
    assignments: Sequence[ScheduledShift],
    shift_templates: Mapping[str, ShiftTemplateInfo],
    qual_codes: Mapping[str, str],
) -> tuple[float, tuple[CoverageTierResult, ...]]:
    targets = build_coverage_targets_from_roster(employees, qual_codes=qual_codes)
    employee_hours: Dict[str, float] = {employee.id: 0.0 for employee in employees}
    for assignment in assignments:
        template = shift_templates.get(assignment.shift_template_id)
        if template is None:
            continue
        employee_hours[assignment.employee_id] = (
            employee_hours.get(assignment.employee_id, 0.0)
            + template.duration_minutes / 60.0
        )
    results = evaluate_coverage_tier_results(
        targets=targets,
        employee_hours=employee_hours,
        rules=rules,
        weeks_in_period=weeks_in_period,
        shift_templates=shift_templates,
        period_start=period_start,
        period_end=period_end,
    )
    return compute_coverage_success_rate_pct(results), results
