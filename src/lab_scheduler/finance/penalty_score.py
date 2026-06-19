"""Financial objective function for the ROUTER-8H scheduling agent.

This module is the SINGLE SOURCE OF TRUTH for the penalty score. The ROUTER-8H
system prompt instructs the LLM to minimize exactly this score; the same formula
is implemented here so any produced schedule (LLM, legacy engine, or human) can
be graded identically and compared for gainshare savings.

Penalty model (weights configurable via PenaltyWeights):
  1. FTE OVERAGE   - 85 / hour scheduled above an employee's target_hours.
  2. UNFILLED GAP  - 150 / hour of demand left unassigned (8h shift = 1,200).
  3. WEEKEND ASYMMETRY - 25 / weekend shift held above the pool floor average.

The points are denominated in dollars so the total maps directly onto the
gainshare revenue model (baseline schedule cost - agent schedule cost = $ saved).
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date
from typing import Any, Dict, List, Mapping, Optional, Sequence

from lab_scheduler.compliance.engine import ShiftTemplateInfo
from lab_scheduler.scheduling.profiles import EmployeeProfile


@dataclass(frozen=True, slots=True)
class PenaltyWeights:
    """Dollar weights for each soft-objective violation.

    Defaults match the ROUTER-8H financial objective function. Swap to the
    codebase-grounded rates ($60 full-OT / $20 premium for an MLT) by passing a
    different instance; no formula changes required.
    """

    fte_overage_per_hour: float = 85.0
    unfilled_gap_per_hour: float = 150.0
    weekend_variance_per_unit: float = 25.0


DEFAULT_WEIGHTS = PenaltyWeights()


@dataclass(frozen=True, slots=True)
class PenaltyBreakdown:
    """Itemized penalty score for one schedule configuration."""

    fte_overage_penalty: float
    unfilled_gap_penalty: float
    weekend_asymmetry_penalty: float
    total_penalty: float
    # Diagnostics
    overage_hours: float
    unfilled_gap_hours: float
    unfilled_gap_count: int
    weekend_floor_average: int
    weekend_excess_shifts: int

    def to_dict(self) -> Dict[str, object]:
        return {
            "fte_overage_penalty": self.fte_overage_penalty,
            "unfilled_gap_penalty": self.unfilled_gap_penalty,
            "weekend_asymmetry_penalty": self.weekend_asymmetry_penalty,
            "total_penalty": self.total_penalty,
            "overage_hours": self.overage_hours,
            "unfilled_gap_hours": self.unfilled_gap_hours,
            "unfilled_gap_count": self.unfilled_gap_count,
            "weekend_floor_average": self.weekend_floor_average,
            "weekend_excess_shifts": self.weekend_excess_shifts,
        }


def _attr(obj: Any, key: str) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(key)
    return getattr(obj, key, None)


def _normalize_assignments(
    assignments: Any,
    shift_templates: Mapping[str, ShiftTemplateInfo],
) -> List[tuple[str, date, str, float]]:
    """Return a flat list of (employee_id, day, shift_code, hours).

    Accepts either a routed ``{employee_id: {date_iso: shift_code}}`` map or a
    sequence of ScheduledShift-like records (objects or mappings) carrying
    ``employee_id`` / ``assignment_date`` / ``shift_template_id``.
    """
    code_hours = {t.code: t.duration_minutes / 60.0 for t in shift_templates.values()}
    template_by_id = {tid: t for tid, t in shift_templates.items()}
    rows: List[tuple[str, date, str, float]] = []

    if isinstance(assignments, Mapping):
        for employee_id, day_map in assignments.items():
            for day_raw, shift_code in (day_map or {}).items():
                day = day_raw if isinstance(day_raw, date) else date.fromisoformat(str(day_raw))
                hours = code_hours.get(shift_code, 8.0)
                rows.append((str(employee_id), day, shift_code, hours))
        return rows

    for record in assignments:
        employee_id = _attr(record, "employee_id")
        day = _attr(record, "assignment_date")
        template_id = _attr(record, "shift_template_id")
        if employee_id is None or not isinstance(day, date):
            continue
        template = template_by_id.get(template_id)
        shift_code = template.code if template is not None else str(template_id)
        hours = template.duration_minutes / 60.0 if template is not None else 8.0
        rows.append((str(employee_id), day, shift_code, hours))
    return rows


def _is_weekend(day: date) -> bool:
    return day.weekday() >= 5


def score_schedule(
    *,
    employees: Sequence[EmployeeProfile],
    target_hours: Mapping[str, float],
    assignments: Any,
    shift_templates: Mapping[str, ShiftTemplateInfo],
    daily_demand: Mapping[date, Mapping[str, int]],
    weights: PenaltyWeights = DEFAULT_WEIGHTS,
) -> PenaltyBreakdown:
    """Compute the total financial penalty for a schedule configuration."""
    rows = _normalize_assignments(assignments, shift_templates)

    # --- 1. FTE overage penalty ------------------------------------------- #
    hours_by_employee: Dict[str, float] = defaultdict(float)
    for employee_id, _day, _code, hours in rows:
        hours_by_employee[employee_id] += hours

    overage_hours = 0.0
    for employee in employees:
        scheduled = hours_by_employee.get(employee.id, 0.0)
        target = target_hours.get(employee.id)
        if target is None:
            continue
        overage_hours += max(0.0, scheduled - target)
    fte_overage_penalty = overage_hours * weights.fte_overage_per_hour

    # --- 2. Unfilled demand gap penalty ----------------------------------- #
    assigned_counts: Counter = Counter()
    code_hours = {t.code: t.duration_minutes / 60.0 for t in shift_templates.values()}
    for employee_id, day, code, _hours in rows:
        assigned_counts[(day, code)] += 1

    unfilled_gap_hours = 0.0
    unfilled_gap_count = 0
    for day, demand in daily_demand.items():
        for shift_code, required in demand.items():
            if required <= 0:
                continue
            have = assigned_counts.get((day, shift_code), 0)
            short = max(0, required - have)
            if short:
                unfilled_gap_count += short
                unfilled_gap_hours += short * code_hours.get(shift_code, 8.0)
    unfilled_gap_penalty = unfilled_gap_hours * weights.unfilled_gap_per_hour

    # --- 3. Weekend asymmetry penalty ------------------------------------- #
    weekend_by_employee: Dict[str, int] = defaultdict(int)
    for employee_id, day, _code, _hours in rows:
        if _is_weekend(day):
            weekend_by_employee[employee_id] += 1

    eligible_ids = [e.id for e in employees]
    weekend_counts = [weekend_by_employee.get(emp_id, 0) for emp_id in eligible_ids]
    if weekend_counts:
        floor_average = math.floor(sum(weekend_counts) / len(weekend_counts))
    else:
        floor_average = 0
    weekend_excess = sum(max(0, count - floor_average) for count in weekend_counts)
    weekend_asymmetry_penalty = weekend_excess * weights.weekend_variance_per_unit

    total = fte_overage_penalty + unfilled_gap_penalty + weekend_asymmetry_penalty

    return PenaltyBreakdown(
        fte_overage_penalty=round(fte_overage_penalty, 2),
        unfilled_gap_penalty=round(unfilled_gap_penalty, 2),
        weekend_asymmetry_penalty=round(weekend_asymmetry_penalty, 2),
        total_penalty=round(total, 2),
        overage_hours=round(overage_hours, 2),
        unfilled_gap_hours=round(unfilled_gap_hours, 2),
        unfilled_gap_count=unfilled_gap_count,
        weekend_floor_average=floor_average,
        weekend_excess_shifts=weekend_excess,
    )


def gainshare_delta(
    baseline: PenaltyBreakdown,
    candidate: PenaltyBreakdown,
) -> Dict[str, float]:
    """Savings of ``candidate`` vs ``baseline`` (the gainshare billing number).

    ``saved`` is positive when the candidate schedule is cheaper than baseline.
    """
    saved = round(baseline.total_penalty - candidate.total_penalty, 2)
    if baseline.total_penalty > 0:
        saved_pct = round(100.0 * saved / baseline.total_penalty, 1)
    else:
        saved_pct = 0.0
    return {
        "baseline_total": baseline.total_penalty,
        "agent_total": candidate.total_penalty,
        "saved": saved,
        "saved_pct": saved_pct,
    }
