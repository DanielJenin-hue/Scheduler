from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Optional, Sequence, Set

from lab_scheduler.compliance.engine import ScheduledShift, ShiftTemplateInfo
from lab_scheduler.compliance.jurisdictions import JurisdictionRules
from lab_scheduler.finance.forecast import (
    DEFAULT_HOURLY_RATE_MLT,
    LaborCostForecast,
    compute_labor_forecast,
    compute_prevented_overtime_leakage,
)
from lab_scheduler.scheduling.auto_generate import list_open_shift_slots
from lab_scheduler.scheduling.profiles import EmployeeProfile


@dataclass(frozen=True, slots=True)
class OvertimeSavingsReport:
    """Read-only ROI snapshot: compliant schedule vs conservative worst-case OT premium."""

    current_ot_premium: float
    worst_case_ot_premium: float
    estimated_overtime_prevented: float
    current_overtime_hours: float
    worst_case_overtime_hours: float
    open_shift_count: int
    blocked_assignment_count: int
    methodology: str

    @property
    def savings_pct(self) -> float:
        if self.worst_case_ot_premium <= 0:
            return 0.0
        return round(
            100.0 * self.estimated_overtime_prevented / self.worst_case_ot_premium,
            1,
        )


def _average_hourly_rate(employee_hourly_rates: Mapping[str, float]) -> float:
    if not employee_hourly_rates:
        return DEFAULT_HOURLY_RATE_MLT
    return sum(employee_hourly_rates.values()) / len(employee_hourly_rates)


def _ot_premium_dollars(overtime_cost: float, rules: JurisdictionRules) -> float:
    multiplier = rules.overtime_rate_multiplier
    if multiplier <= 1.0 or overtime_cost <= 0:
        return 0.0
    return round(overtime_cost * (multiplier - 1.0) / multiplier, 2)


def _ot_hours_from_premium(premium: float, avg_rate: float, rules: JurisdictionRules) -> float:
    premium_per_hour = avg_rate * (rules.overtime_rate_multiplier - 1.0)
    if premium_per_hour <= 0:
        return 0.0
    return round(premium / premium_per_hour, 2)


def compute_overtime_savings_report(
    *,
    rules: JurisdictionRules,
    period_start,
    period_end,
    weeks_in_period: int,
    assignments: Sequence[ScheduledShift],
    shift_templates: Mapping[str, ShiftTemplateInfo],
    shift_required_qualifications: Mapping[str, Set[str]],
    employees: Sequence[EmployeeProfile],
    employee_hourly_rates: Mapping[str, float],
    flagged_violations: Optional[Sequence[tuple[str, str, str]]] = None,
) -> OvertimeSavingsReport:
    """
    Compare the current compliant schedule against a conservative worst-case baseline.

    Worst-case assumes:
    - Current statutory overtime premiums remain on the books, plus
    - Premium that would have accrued if compliance blocks had been ignored, plus
    - Each unfilled open slot is back-filled at overtime premium rates (conservative).
    """

    current = compute_labor_forecast(
        rules=rules,
        period_start=period_start,
        period_end=period_end,
        assignments=assignments,
        shift_templates=shift_templates,
        employee_hourly_rates=employee_hourly_rates,
    )
    current_premium = _ot_premium_dollars(current.overtime_cost, rules)

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

    open_slots = list_open_shift_slots(
        period_start=period_start,
        period_end=period_end,
        shift_templates=dict(shift_templates),
        assignments=list(assignments),
    )
    avg_rate = _average_hourly_rate(employee_hourly_rates)
    premium_per_hour = avg_rate * (rules.overtime_rate_multiplier - 1.0)
    open_slot_premium = 0.0
    for slot in open_slots:
        template = shift_templates.get(slot.shift_template_id)
        if template is None:
            continue
        hours = template.duration_minutes / 60.0
        open_slot_premium += hours * premium_per_hour

    open_slot_premium = round(open_slot_premium, 2)
    worst_case_premium = round(current_premium + prevented + open_slot_premium, 2)
    estimated_prevented = round(max(0.0, worst_case_premium - current_premium), 2)

    avg_rate_safe = avg_rate if avg_rate > 0 else DEFAULT_HOURLY_RATE_MLT
    worst_case_hours = _ot_hours_from_premium(worst_case_premium, avg_rate_safe, rules)

    blocked_count = 0
    if prevented > 0:
        blocked_count = max(1, len(open_slots))

    methodology = (
        "Conservative, information-only estimate. Compares actual overtime premiums in the "
        "current schedule against a worst-case where open shifts are back-filled at OT rates "
        "and compliance-engine blocks on FTE/rest/statutory limits are ignored. "
        "Not a billing or payroll invoice."
    )

    return OvertimeSavingsReport(
        current_ot_premium=current_premium,
        worst_case_ot_premium=worst_case_premium,
        estimated_overtime_prevented=estimated_prevented,
        current_overtime_hours=current.overtime_hours,
        worst_case_overtime_hours=worst_case_hours,
        open_shift_count=len(open_slots),
        blocked_assignment_count=blocked_count,
        methodology=methodology,
    )


def overtime_savings_from_forecast(
    forecast: LaborCostForecast,
    *,
    rules: JurisdictionRules,
    period_start,
    period_end,
    weeks_in_period: int,
    assignments: Sequence[ScheduledShift],
    shift_templates: Mapping[str, ShiftTemplateInfo],
    shift_required_qualifications: Mapping[str, Set[str]],
    employees: Sequence[EmployeeProfile],
    employee_hourly_rates: Mapping[str, float],
    flagged_violations: Optional[Sequence[tuple[str, str, str]]] = None,
) -> OvertimeSavingsReport:
    """Convenience wrapper when a labor forecast was already computed for the period."""

    _ = forecast
    return compute_overtime_savings_report(
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
