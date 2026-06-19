from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from typing import Dict, List, Mapping, Optional, Sequence, Set

from lab_scheduler.availability import EMERGENCY_SICK_LEAVE_REASON, create_availability_exception
from lab_scheduler.compliance.engine import ScheduledShift, ShiftTemplateInfo
from lab_scheduler.compliance.jurisdictions import JurisdictionRules
from lab_scheduler.finance.forecast import DEFAULT_HOURLY_RATE_MLT, _ot_hours_for_week
from lab_scheduler.scheduling.auto_generate import (
    EmployeeProfile,
    _build_employee_state,
    _is_qualified,
    _would_violate_labor_rules,
)
from lab_scheduler.time import workweek_for


@dataclass(frozen=True, slots=True)
class EmergencySickCallGap:
    employee_id: str
    employee_name: str
    shift_date: date
    shift_template_id: str
    shift_code: str
    previous_shift_code: str


@dataclass(frozen=True, slots=True)
class EmergencyReplacementCandidate:
    employee_id: str
    employee_name: str
    tier_code: str
    projected_cost: float
    cost_label: str

    @property
    def display_line(self) -> str:
        return (
            f"{self.employee_name} ({self.tier_code}) — "
            f"${self.projected_cost:,.2f} {self.cost_label}"
        )


@dataclass(frozen=True, slots=True)
class EmergencySickCallResult:
    gap: EmergencySickCallGap
    candidates: List[EmergencyReplacementCandidate]


def _tier_code_for_employee(
    profile: EmployeeProfile,
    required_qualification_ids: Set[str],
    qualification_codes: Mapping[str, str],
) -> str:
    required_codes = {
        qualification_codes[qid]
        for qid in required_qualification_ids
        if qid in qualification_codes
    }
    held_codes = {
        qualification_codes[qid]
        for qid in profile.qualification_ids
        if qid in qualification_codes
    }
    for preferred in ("MLT", "MLA"):
        if preferred in required_codes and preferred in held_codes:
            return preferred
    overlap = sorted(required_codes & held_codes)
    if overlap:
        return overlap[0]
    return "—"


def _week_day_hours(
    employee_id: str,
    assignments: Sequence[ScheduledShift],
    shift_templates: Mapping[str, ShiftTemplateInfo],
    week_start: date,
) -> Dict[date, float]:
    day_hours: Dict[date, float] = defaultdict(float)
    for assignment in assignments:
        if assignment.employee_id != employee_id:
            continue
        if workweek_for(assignment.assignment_date).start != week_start:
            continue
        template = shift_templates.get(assignment.shift_template_id)
        if template is None:
            continue
        day_hours[assignment.assignment_date] += template.duration_minutes / 60.0
    return dict(day_hours)


def project_shift_cost(
    *,
    employee_id: str,
    assignment_date: date,
    shift_hours: float,
    assignments: Sequence[ScheduledShift],
    shift_templates: Mapping[str, ShiftTemplateInfo],
    rules: JurisdictionRules,
    hourly_rate: float,
) -> tuple[float, str]:
    week_start = workweek_for(assignment_date).start
    before = _week_day_hours(employee_id, assignments, shift_templates, week_start)
    before_reg, before_ot = _ot_hours_for_week(before, rules)

    after = dict(before)
    after[assignment_date] = after.get(assignment_date, 0.0) + shift_hours
    after_reg, after_ot = _ot_hours_for_week(after, rules)

    delta_reg = max(0.0, after_reg - before_reg)
    delta_ot = max(0.0, after_ot - before_ot)
    cost = delta_reg * hourly_rate + delta_ot * hourly_rate * rules.overtime_rate_multiplier
    label = "Overtime" if delta_ot > 1e-6 else "Regular Time"
    return round(cost, 2), label


def rank_emergency_replacements(
    *,
    rules: JurisdictionRules,
    period_start: date,
    period_end: date,
    weeks_in_period: int,
    employees: Sequence[EmployeeProfile],
    all_assignments: Sequence[ScheduledShift],
    shift_templates: Mapping[str, ShiftTemplateInfo],
    shift_required_qualifications: Mapping[str, Set[str]],
    slot_date: date,
    shift_template_id: str,
    qualification_codes: Mapping[str, str],
    employee_hourly_rates: Mapping[str, float],
    employee_target_hours: Optional[Mapping[str, float]] = None,
    availability_blocked: Optional[Mapping[str, Set[date]]] = None,
    exclude_employee_ids: Optional[Set[str]] = None,
    limit: int = 3,
) -> List[EmergencyReplacementCandidate]:
    """Rank safest emergency fill candidates: tier match, statutory compliance, then cost."""

    template = shift_templates.get(shift_template_id)
    if template is None:
        return []

    required = shift_required_qualifications.get(shift_template_id, set())
    shift_hours = template.duration_minutes / 60.0
    excluded = exclude_employee_ids or set()
    ranked: List[tuple[int, float, EmergencyReplacementCandidate]] = []

    for emp in employees:
        if emp.id in excluded:
            continue
        if not _is_qualified(emp, required):
            continue

        others = [
            a
            for a in all_assignments
            if a.employee_id == emp.id and a.assignment_date != slot_date
        ]
        target_override = (
            float(employee_target_hours[emp.id])
            if employee_target_hours and emp.id in employee_target_hours
            else None
        )
        state = _build_employee_state(
            emp, others, dict(shift_templates), rules, weeks_in_period, target_override
        )
        violation = _would_violate_labor_rules(
            state,
            slot_date,
            template,
            dict(shift_templates),
            rules,
            period_start,
            period_end,
            availability_blocked,
            enforce_fte_target=False,
        )
        if violation:
            continue

        rate = employee_hourly_rates.get(emp.id, DEFAULT_HOURLY_RATE_MLT)
        cost, label = project_shift_cost(
            employee_id=emp.id,
            assignment_date=slot_date,
            shift_hours=shift_hours,
            assignments=others,
            shift_templates=shift_templates,
            rules=rules,
            hourly_rate=rate,
        )
        tier = _tier_code_for_employee(emp, required, qualification_codes)
        candidate = EmergencyReplacementCandidate(
            employee_id=emp.id,
            employee_name=emp.full_name,
            tier_code=tier,
            projected_cost=cost,
            cost_label=label,
        )
        overtime_rank = 0 if label == "Regular Time" else 1
        ranked.append((overtime_rank, cost, candidate))

    ranked.sort(key=lambda item: (item[0], item[1], item[2].employee_name))
    return [item[2] for item in ranked[:limit]]


def find_assignment_on_date(
    assignments: Sequence[ScheduledShift],
    *,
    employee_id: str,
    on_date: date,
) -> Optional[ScheduledShift]:
    for assignment in assignments:
        if assignment.employee_id == employee_id and assignment.assignment_date == on_date:
            return assignment
    return None


def flag_emergency_sick_call(
    conn,
    *,
    tenant_id: str,
    period_start: date,
    period_end: date,
    weeks_in_period: int,
    rules: JurisdictionRules,
    employees: Sequence[EmployeeProfile],
    all_assignments: Sequence[ScheduledShift],
    shift_templates: Mapping[str, ShiftTemplateInfo],
    shift_required_qualifications: Mapping[str, Set[str]],
    qualification_codes: Mapping[str, str],
    employee_hourly_rates: Mapping[str, float],
    sick_employee_id: str,
    sick_date: date,
    employee_target_hours: Optional[Mapping[str, float]] = None,
    availability_blocked: Optional[Mapping[str, Set[date]]] = None,
    template_short_by_id: Optional[Mapping[str, str]] = None,
) -> EmergencySickCallResult:
    """Mark an assigned shift as emergency sick leave and rank replacement candidates."""

    assignment = find_assignment_on_date(
        all_assignments, employee_id=sick_employee_id, on_date=sick_date
    )
    if assignment is None:
        raise ValueError("No scheduled shift found for that employee on the selected date.")

    sick_profile = next((e for e in employees if e.id == sick_employee_id), None)
    if sick_profile is None:
        raise ValueError("Employee not found.")

    template = shift_templates.get(assignment.shift_template_id)
    if template is None:
        raise ValueError("Shift template not found for assignment.")

    create_availability_exception(
        conn,
        tenant_id=tenant_id,
        employee_id=sick_employee_id,
        start_date=sick_date,
        end_date=sick_date,
        reason=EMERGENCY_SICK_LEAVE_REASON,
    )

    conn.execute(
        """
        DELETE FROM shift_assignments
        WHERE tenant_id = ? AND employee_id = ? AND assignment_date = ?
        """,
        (tenant_id, sick_employee_id, sick_date.isoformat()),
    )
    conn.commit()

    remaining = [
        a
        for a in all_assignments
        if not (a.employee_id == sick_employee_id and a.assignment_date == sick_date)
    ]
    blocked = {k: set(v) for k, v in (availability_blocked or {}).items()}
    blocked.setdefault(sick_employee_id, set()).add(sick_date)

    candidates = rank_emergency_replacements(
        rules=rules,
        period_start=period_start,
        period_end=period_end,
        weeks_in_period=weeks_in_period,
        employees=employees,
        all_assignments=remaining,
        shift_templates=shift_templates,
        shift_required_qualifications=shift_required_qualifications,
        slot_date=sick_date,
        shift_template_id=assignment.shift_template_id,
        qualification_codes=qualification_codes,
        employee_hourly_rates=employee_hourly_rates,
        employee_target_hours=employee_target_hours,
        availability_blocked=blocked,
        exclude_employee_ids={sick_employee_id},
    )

    short_code = (template_short_by_id or {}).get(
        assignment.shift_template_id,
        template.code[:1].upper() if template.code else "",
    )
    gap = EmergencySickCallGap(
        employee_id=sick_employee_id,
        employee_name=sick_profile.full_name,
        shift_date=sick_date,
        shift_template_id=assignment.shift_template_id,
        shift_code=short_code,
        previous_shift_code=short_code,
    )
    return EmergencySickCallResult(gap=gap, candidates=candidates)
