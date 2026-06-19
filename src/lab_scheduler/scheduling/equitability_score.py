"""Deterministic equitability scoring for Layer 3 soft-slot fill."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Mapping, Sequence, Set

from lab_scheduler.scheduling.profiles import EmployeeProfile
from lab_scheduler.scheduling.schedule_tallies import shift_band_from_template_code


@dataclass(frozen=True, slots=True)
class FairnessWeights:
    hour_deficit: float = 1.0
    consecutive_work_penalty: float = 0.5
    isolated_workday_penalty: float = 2.0
    weekend_share: float = 0.3

    def fairness_weight_scale(self) -> float:
        return max(0.25, self.hour_deficit + self.weekend_share)


def _work_dates_for_employee(
    employee_id: str,
    work_dates: Set[date],
    *,
    period_start: date,
    period_end: date,
) -> list[date]:
    return sorted(
        day
        for day in work_dates
        if period_start <= day <= period_end
    )


def _max_consecutive_work_streak(work_days: Sequence[date]) -> int:
    if not work_days:
        return 0
    best = 1
    current = 1
    for index in range(1, len(work_days)):
        if work_days[index].toordinal() == work_days[index - 1].toordinal() + 1:
            current += 1
            best = max(best, current)
        else:
            current = 1
    return best


def _isolated_workday_count(work_days: Sequence[date]) -> int:
    day_set = set(work_days)
    isolated = 0
    for day in work_days:
        prev_day = day - timedelta(days=1)
        next_day = day + timedelta(days=1)
        if prev_day not in day_set and next_day not in day_set:
            isolated += 1
    return isolated


def _weekend_work_count(
    employee_id: str,
    assignments: Sequence[object],
    shift_templates: Mapping[str, object],
    *,
    period_start: date,
    period_end: date,
) -> int:
    count = 0
    for assignment in assignments:
        if getattr(assignment, "employee_id", "") != employee_id:
            continue
        assignment_date = getattr(assignment, "assignment_date", None)
        if assignment_date is None or assignment_date < period_start or assignment_date > period_end:
            continue
        if assignment_date.weekday() < 5:
            continue
        template = shift_templates.get(getattr(assignment, "shift_template_id", ""))
        if template is None:
            continue
        if shift_band_from_template_code(template.code) in {"D", "E", "N"}:
            count += 1
    return count


def score_line(
    employee: EmployeeProfile,
    *,
    total_hours: float,
    target_hours: float,
    work_dates: Set[date],
    assignments: Sequence[object],
    shift_templates: Mapping[str, object],
    period_start: date,
    period_end: date,
    weekend_target: int = 8,
    weights: FairnessWeights | None = None,
) -> float:
    """
    Lower score = higher priority to receive the next soft slot.
    """

    weights = weights or FairnessWeights()
    deficit = max(0.0, target_hours - total_hours)
    score = -weights.hour_deficit * deficit

    work_days = _work_dates_for_employee(
        employee.id,
        work_dates,
        period_start=period_start,
        period_end=period_end,
    )
    streak = _max_consecutive_work_streak(work_days)
    if streak > 5:
        score += weights.consecutive_work_penalty * float(streak - 5)

    isolated = _isolated_workday_count(work_days)
    score += weights.isolated_workday_penalty * float(isolated)

    weekend_actual = _weekend_work_count(
        employee.id,
        assignments,
        shift_templates,
        period_start=period_start,
        period_end=period_end,
    )
    if weekend_target > 0:
        weekend_delta = abs(weekend_actual - weekend_target)
        score += weights.weekend_share * float(weekend_delta)

    return score
