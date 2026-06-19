from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date
from typing import Dict, Mapping, Optional, Sequence, Set

from lab_scheduler.compliance.engine import ScheduledShift, ShiftTemplateInfo
from lab_scheduler.compliance.jurisdictions import JurisdictionRules
from lab_scheduler.engine.demand import infer_qual_code
from lab_scheduler.models.employee import (
    is_critical_contract_line_violation,
    normalize_shift_band_code,
)
from lab_scheduler.scheduling.auto_generate import (
    EmployeeProfile,
    _is_qualified,
    validate_assignment_change,
)

_SHIFT_BAND_ALIASES = {
    "D": "MORNING",
    "M": "MORNING",
    "E": "EVENING",
    "N": "NIGHT",
    "DAY": "MORNING",
    "MORNING": "MORNING",
    "EVENING": "EVENING",
    "NIGHT": "NIGHT",
}


@dataclass(frozen=True, slots=True)
class ScheduleState:
    """Snapshot of roster + assignments used for smart swap evaluation."""

    rules: JurisdictionRules
    period_start: date
    period_end: date
    weeks_in_period: int
    employees: Sequence[EmployeeProfile]
    assignments: Sequence[ScheduledShift]
    shift_templates: Mapping[str, ShiftTemplateInfo]
    shift_required_qualifications: Mapping[str, Set[str]]
    employee_target_hours: Optional[Mapping[str, float]] = None
    availability_blocked: Optional[Mapping[str, Set[date]]] = None


@dataclass(frozen=True, slots=True)
class SwapAssistCandidate:
    employee_id: str
    employee_name: str
    role_code: str
    scheduled_hours: float
    target_hours: float
    fte: float
    scheduled_fte: float
    hour_deficit: float
    is_eligible: bool = True
    block_reason: Optional[str] = None


def _normalize_shift_band(shift_type: str) -> str:
    token = str(shift_type or "").strip().upper()
    if token in _SHIFT_BAND_ALIASES:
        return _SHIFT_BAND_ALIASES[token]
    return normalize_shift_band_code(token)


def resolve_shift_template_id(
    shift_type: str,
    shift_templates: Mapping[str, ShiftTemplateInfo],
) -> Optional[str]:
    """Map D/E/N, template id, or band code to a shift template id."""

    if shift_type in shift_templates:
        return shift_type

    target_band = _normalize_shift_band(shift_type)
    for template_id, template in shift_templates.items():
        if normalize_shift_band_code(template.code) == target_band:
            return template_id
    return None


def _employee_scheduled_hours(
    employee_id: str,
    assignments: Sequence[ScheduledShift],
    shift_templates: Mapping[str, ShiftTemplateInfo],
) -> float:
    total = 0.0
    for assignment in assignments:
        if assignment.employee_id != employee_id:
            continue
        template = shift_templates.get(assignment.shift_template_id)
        if template is None:
            continue
        total += template.duration_minutes / 60.0
    return total


def _employee_target_hours(
    employee: EmployeeProfile,
    schedule_state: ScheduleState,
) -> float:
    if (
        schedule_state.employee_target_hours
        and employee.id in schedule_state.employee_target_hours
    ):
        return float(schedule_state.employee_target_hours[employee.id])
    baseline = schedule_state.rules.standard_hours_per_week_at_1_0_fte
    return employee.fte * baseline * schedule_state.weeks_in_period


def _scheduled_fte(
    scheduled_hours: float,
    *,
    rules: JurisdictionRules,
    weeks_in_period: int,
) -> float:
    capacity = rules.standard_hours_per_week_at_1_0_fte * weeks_in_period
    if capacity <= 0:
        return 0.0
    return scheduled_hours / capacity


def _infer_role_code(employee: EmployeeProfile) -> str:
    qual = infer_qual_code(employee)
    if qual in {"MLT", "MLA"}:
        return qual
    name = employee.full_name.upper()
    if "MLT" in name:
        return "MLT"
    if "MLA" in name:
        return "MLA"
    return qual or "MLT"


def _has_assignment_on_date(
    employee_id: str,
    assignment_date: date,
    assignments: Sequence[ScheduledShift],
) -> bool:
    return any(
        assignment.employee_id == employee_id
        and assignment.assignment_date == assignment_date
        for assignment in assignments
    )


def format_manual_assignment_warning(
    *,
    employee_name: str,
    contract_line_type: Optional[str],
    assignment_date: date,
    shift_type: str,
    violation: str,
) -> str:
    """Plain-language explanation for a blocked manual grid assignment."""

    band = _normalize_shift_band(shift_type)
    band_label = {"MORNING": "Day", "EVENING": "Evening", "NIGHT": "Night"}.get(
        band, shift_type
    )
    line = contract_line_type or "unspecified"
    short_name = employee_name
    if "Vacant" in employee_name and "Line" in employee_name:
        parts = employee_name.replace("Vacant ", "").split(" - Line ")
        if len(parts) == 2:
            short_name = parts[0].strip()

    if is_critical_contract_line_violation(violation):
        if line == "D/E" and band == "NIGHT":
            return (
                f"Cannot assign: {short_name} has a Day/Evening contract and is "
                f"ineligible for Night shifts on {assignment_date.strftime('%a %b %d, %Y')}."
            )
        if line == "D/N" and band == "EVENING":
            return (
                f"Cannot assign: {short_name} has a Day/Night contract and is "
                f"ineligible for Evening shifts on {assignment_date.strftime('%a %b %d, %Y')}."
            )
        return (
            f"Cannot assign: {short_name} ({line} contract line) cannot work "
            f"{band_label} shifts on {assignment_date.strftime('%a %b %d, %Y')}."
        )
    if "11h rest" in violation.lower() or "11-hour" in violation.lower():
        return (
            f"Cannot assign: {short_name} would violate Manitoba's 11-hour rest window "
            f"before/after the {band_label} shift on {assignment_date.strftime('%a %b %d, %Y')}."
        )
    if "consecutive work days" in violation.lower():
        return (
            f"Cannot assign: {short_name} would exceed the maximum consecutive work-day "
            f"limit on {assignment_date.strftime('%a %b %d, %Y')}."
        )
    if "approved time off" in violation.lower():
        return (
            f"Cannot assign: {short_name} has approved time off on "
            f"{assignment_date.strftime('%a %b %d, %Y')}."
        )
    if "already scheduled" in violation.lower():
        return (
            f"Cannot assign: {short_name} already has a shift on "
            f"{assignment_date.strftime('%a %b %d, %Y')}."
        )
    return (
        f"Cannot assign {band_label} shift to {short_name} on "
        f"{assignment_date.strftime('%a %b %d, %Y')}: {violation}"
    )


def get_eligible_swap_candidates(
    schedule_state: ScheduleState,
    target_employee_id: str,
    target_date: date,
    target_shift_type: str,
    *,
    include_ineligible: bool = False,
    limit: int = 25,
) -> list[SwapAssistCandidate]:
    """
    Return employees who can legally cover ``target_shift_type`` on ``target_date``.

    Filters contract-line masking, Manitoba 11-hour rest, consecutive-day limits,
    and double-booking. Ranks eligible staff by FTE hour deficit (under-target first).
    """

    shift_template_id = resolve_shift_template_id(
        target_shift_type, schedule_state.shift_templates
    )
    if shift_template_id is None:
        return []

    template = schedule_state.shift_templates[shift_template_id]
    required = schedule_state.shift_required_qualifications.get(shift_template_id, set())
    eligible: list[SwapAssistCandidate] = []
    ineligible: list[SwapAssistCandidate] = []

    for employee in schedule_state.employees:
        if employee.id == target_employee_id:
            continue

        scheduled_hours = _employee_scheduled_hours(
            employee.id,
            schedule_state.assignments,
            schedule_state.shift_templates,
        )
        target_hours = _employee_target_hours(employee, schedule_state)
        scheduled_fte = _scheduled_fte(
            scheduled_hours,
            rules=schedule_state.rules,
            weeks_in_period=schedule_state.weeks_in_period,
        )
        hour_deficit = max(0.0, target_hours - scheduled_hours)
        role_code = _infer_role_code(employee)

        base = SwapAssistCandidate(
            employee_id=employee.id,
            employee_name=employee.full_name,
            role_code=role_code,
            scheduled_hours=scheduled_hours,
            target_hours=target_hours,
            fte=employee.fte,
            scheduled_fte=scheduled_fte,
            hour_deficit=hour_deficit,
        )

        if _has_assignment_on_date(employee.id, target_date, schedule_state.assignments):
            blocked = replace(
                base,
                is_eligible=False,
                block_reason="Employee already scheduled that day.",
            )
            if include_ineligible:
                ineligible.append(blocked)
            continue

        if not _is_qualified(employee, required):
            blocked = replace(
                base,
                is_eligible=False,
                block_reason="Missing required qualification (MLT/MLA).",
            )
            if include_ineligible:
                ineligible.append(blocked)
            continue

        violation = validate_assignment_change(
            rules=schedule_state.rules,
            period_start=schedule_state.period_start,
            period_end=schedule_state.period_end,
            weeks_in_period=schedule_state.weeks_in_period,
            employee=employee,
            all_assignments=schedule_state.assignments,
            shift_templates=dict(schedule_state.shift_templates),
            shift_required_qualifications=dict(schedule_state.shift_required_qualifications),
            assignment_date=target_date,
            new_shift_template_id=shift_template_id,
            employee_target_hours=schedule_state.employee_target_hours,
            availability_blocked=schedule_state.availability_blocked,
        )
        if violation:
            blocked = replace(base, is_eligible=False, block_reason=violation)
            if include_ineligible:
                ineligible.append(blocked)
            continue

        eligible.append(base)

    eligible.sort(
        key=lambda candidate: (
            -candidate.hour_deficit,
            -candidate.fte,
            candidate.employee_name,
        )
    )
    ordered = eligible[:limit]
    if include_ineligible:
        ordered.extend(ineligible[: max(0, limit - len(ordered))])
    return ordered
