from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Dict, Mapping, Optional, Sequence, Set

from lab_scheduler.compliance.engine import ScheduledShift, ShiftTemplateInfo
from lab_scheduler.compliance.jurisdictions import JurisdictionRules
from lab_scheduler.scheduling.auto_generate import (
    EmployeeProfile,
    list_open_shift_slots,
    suggest_employees_for_slot,
    validate_assignment_change,
)
from lab_scheduler.time import workweek_for

DEFAULT_HOURLY_RATE_MLT = 40.0
DEFAULT_HOURLY_RATE_MLA = 26.0

DEFAULT_RATE_BY_QUAL_CODE = {
    "MLT": DEFAULT_HOURLY_RATE_MLT,
    "MLA": DEFAULT_HOURLY_RATE_MLA,
}


@dataclass(frozen=True, slots=True)
class LaborCostForecast:
    total_cost: float
    regular_hours: float
    overtime_hours: float
    regular_cost: float
    overtime_cost: float
    prevented_leakage: float

    @property
    def total_hours(self) -> float:
        return self.regular_hours + self.overtime_hours


def _hours_for_assignment(
    assignment: ScheduledShift, templates: Mapping[str, ShiftTemplateInfo]
) -> float:
    tmpl = templates.get(assignment.shift_template_id)
    if tmpl is None:
        return 0.0
    return tmpl.duration_minutes / 60.0


def _ot_hours_for_week(
    day_hours: Mapping[date, float], rules: JurisdictionRules
) -> tuple[float, float]:
    week_total = sum(day_hours.values())
    weekly_ot = max(0.0, week_total - rules.weekly_overtime_threshold_hours)

    daily_ot = 0.0
    if rules.daily_overtime_threshold_hours is not None:
        for hours in day_hours.values():
            daily_ot += max(0.0, hours - rules.daily_overtime_threshold_hours)

    ot_hours = max(weekly_ot, daily_ot)
    regular_hours = max(0.0, week_total - ot_hours)
    return regular_hours, ot_hours


def compute_labor_forecast(
    *,
    rules: JurisdictionRules,
    period_start: date,
    period_end: date,
    assignments: Sequence[ScheduledShift],
    shift_templates: Mapping[str, ShiftTemplateInfo],
    employee_hourly_rates: Mapping[str, float],
) -> LaborCostForecast:
    """Project regular vs overtime labor cost for a schedule period."""

    by_employee_week: Dict[str, Dict[date, Dict[date, float]]] = defaultdict(
        lambda: defaultdict(lambda: defaultdict(float))
    )

    for assignment in assignments:
        hours = _hours_for_assignment(assignment, shift_templates)
        if hours <= 0:
            continue
        week_start = workweek_for(assignment.assignment_date).start
        by_employee_week[assignment.employee_id][week_start][assignment.assignment_date] += hours

    regular_hours = 0.0
    overtime_hours = 0.0
    regular_cost = 0.0
    overtime_cost = 0.0
    ot_multiplier = rules.overtime_rate_multiplier

    for emp_id, weeks in by_employee_week.items():
        rate = employee_hourly_rates.get(emp_id, DEFAULT_HOURLY_RATE_MLT)
        for week_start, day_hours in weeks.items():
            if week_start + timedelta(days=6) < period_start or week_start > period_end:
                continue
            reg, ot = _ot_hours_for_week(day_hours, rules)
            regular_hours += reg
            overtime_hours += ot
            regular_cost += reg * rate
            overtime_cost += ot * rate * ot_multiplier

    total_cost = regular_cost + overtime_cost
    return LaborCostForecast(
        total_cost=round(total_cost, 2),
        regular_hours=round(regular_hours, 2),
        overtime_hours=round(overtime_hours, 2),
        regular_cost=round(regular_cost, 2),
        overtime_cost=round(overtime_cost, 2),
        prevented_leakage=0.0,
    )


def _parse_ot_hours_from_violation(message: str) -> Optional[float]:
    match = re.search(r"→\s*([\d.]+)h", message)
    if match:
        return float(match.group(1))
    match = re.search(r"([\d.]+)h\s+(?:daily|statutory)\s+overtime", message, re.I)
    if match:
        return float(match.group(1))
    return None


def _ot_premium_per_hour(base_rate: float, rules: JurisdictionRules) -> float:
    return base_rate * (rules.overtime_rate_multiplier - 1.0)


def _is_overtime_related_violation(message: str) -> bool:
    lower = message.lower()
    return any(
        token in lower
        for token in ("overtime", "statutory limit", "fte target", "exceeds", "weekly limit")
    )


def compute_prevented_overtime_leakage(
    *,
    rules: JurisdictionRules,
    period_start: date,
    period_end: date,
    weeks_in_period: int,
    assignments: Sequence[ScheduledShift],
    shift_templates: Mapping[str, ShiftTemplateInfo],
    shift_required_qualifications: Mapping[str, Set[str]],
    employees: Sequence[EmployeeProfile],
    employee_hourly_rates: Mapping[str, float],
    flagged_violations: Optional[Sequence[tuple[str, str, str]]] = None,
) -> float:
    """
    Estimate overtime premium ($) avoided by safe scheduling and compliance blocks.

    ``flagged_violations`` is an optional sequence of (employee_id, code, message)
    tuples for non-compliant shifts surfaced by the compliance engine.
    """

    prevented = 0.0
    scheduled = list(assignments)

    open_slots = list_open_shift_slots(
        period_start=period_start,
        period_end=period_end,
        shift_templates=dict(shift_templates),
        assignments=scheduled,
    )

    for slot in open_slots:
        safe = suggest_employees_for_slot(
            rules=rules,
            period_start=period_start,
            period_end=period_end,
            weeks_in_period=weeks_in_period,
            employees=employees,
            all_assignments=scheduled,
            shift_templates=dict(shift_templates),
            shift_required_qualifications=dict(shift_required_qualifications),
            slot_date=slot.assignment_date,
            shift_template_id=slot.shift_template_id,
            limit=1,
        )
        if safe:
            continue

        template = shift_templates.get(slot.shift_template_id)
        if template is None:
            continue
        shift_hours = template.duration_minutes / 60.0
        required = shift_required_qualifications.get(slot.shift_template_id, set())

        for emp in employees:
            if not required.issubset(emp.qualification_ids):
                continue
            rate = employee_hourly_rates.get(emp.id, DEFAULT_HOURLY_RATE_MLT)
            violation = validate_assignment_change(
                rules=rules,
                period_start=period_start,
                period_end=period_end,
                weeks_in_period=weeks_in_period,
                employee=emp,
                all_assignments=scheduled,
                shift_templates=dict(shift_templates),
                shift_required_qualifications=dict(shift_required_qualifications),
                assignment_date=slot.assignment_date,
                new_shift_template_id=slot.shift_template_id,
            )
            if not violation or not _is_overtime_related_violation(violation):
                continue
            prevented += shift_hours * _ot_premium_per_hour(rate, rules)
            break

    if flagged_violations:
        for employee_id, code, message in flagged_violations:
            if code not in ("WEEKLY_OVERTIME", "DAILY_OVERTIME"):
                continue
            ot_hours = _parse_ot_hours_from_violation(message)
            if ot_hours is None:
                continue
            rate = employee_hourly_rates.get(employee_id, DEFAULT_HOURLY_RATE_MLT)
            prevented += ot_hours * _ot_premium_per_hour(rate, rules)

    return round(prevented, 2)


def build_full_forecast(
    *,
    rules: JurisdictionRules,
    period_start: date,
    period_end: date,
    weeks_in_period: int,
    assignments: Sequence[ScheduledShift],
    shift_templates: Mapping[str, ShiftTemplateInfo],
    shift_required_qualifications: Mapping[str, Set[str]],
    employees: Sequence[EmployeeProfile],
    employee_hourly_rates: Mapping[str, float],
    flagged_violations: Optional[Sequence[tuple[str, str, str]]] = None,
) -> LaborCostForecast:
    base = compute_labor_forecast(
        rules=rules,
        period_start=period_start,
        period_end=period_end,
        assignments=assignments,
        shift_templates=shift_templates,
        employee_hourly_rates=employee_hourly_rates,
    )
    prevented = compute_prevented_overtime_leakage(
        rules=rules,
        period_start=period_start,
        period_end=period_end,
        weeks_in_period=weeks_in_period,
        assignments=assignments,
        shift_templates=shift_templates,
        shift_required_qualifications=shift_required_qualifications,
        employees=employees,
        employee_hourly_rates=employee_hourly_rates,
        flagged_violations=flagged_violations,
    )
    return LaborCostForecast(
        total_cost=base.total_cost,
        regular_hours=base.regular_hours,
        overtime_hours=base.overtime_hours,
        regular_cost=base.regular_cost,
        overtime_cost=base.overtime_cost,
        prevented_leakage=prevented,
    )
