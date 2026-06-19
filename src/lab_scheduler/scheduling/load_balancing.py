from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Dict, Mapping, MutableMapping, Optional, Sequence

from lab_scheduler.compliance.engine import ShiftTemplateInfo
from lab_scheduler.engine.demand import (
    WEEKDAY_DAY_SHIFT_CAPACITY_LIMIT,
    WEEKDAY_LOAD_BALANCE_TOLERANCE,
    WEEKEND_CLINICAL_MAX_PER_QUAL,
    WEEKEND_CLINICAL_MIN_PER_QUAL,
    ExpandedScheduleSlot,
    infer_qual_code,
    is_clinical_floor_pool,
)
from lab_scheduler.scheduling.date_utils import daterange as _daterange
from lab_scheduler.scheduling.profiles import EmployeeProfile
from lab_scheduler.scheduling.schedule_tallies import (
    WEEKDAY_SHIFT_TARGETS,
    WEEKEND_MORNING_TOTAL_CAP,
)
from lab_scheduler.time import workweek_for


@dataclass(frozen=True, slots=True)
class WeekdayDailyStaffingPlan:
    """Target weekday headcount derived from roster FTE over working days."""

    target_per_day: float
    weekday_count: int
    tolerance: float = WEEKDAY_LOAD_BALANCE_TOLERANCE

    @property
    def upper_bound(self) -> float:
        return self.target_per_day * (1.0 + self.tolerance)

    @property
    def lower_bound(self) -> float:
        return max(0.0, self.target_per_day * (1.0 - self.tolerance))


def compute_weekday_daily_staffing_plan(
    employees: Sequence[EmployeeProfile],
    *,
    period_start: date,
    period_end: date,
    standard_weekly_hours: float,
    weeks_in_period: int,
    shift_hours: float,
    tolerance: float = WEEKDAY_LOAD_BALANCE_TOLERANCE,
) -> WeekdayDailyStaffingPlan:
    """
    Average weekday staffing = total roster contract hours / (shift hours × weekdays).
    """

    weekday_count = sum(
        1 for day in _daterange(period_start, period_end) if day.weekday() < 5
    )
    if weekday_count <= 0:
        return WeekdayDailyStaffingPlan(
            target_per_day=0.0,
            weekday_count=weekday_count,
            tolerance=tolerance,
        )

    return WeekdayDailyStaffingPlan(
        target_per_day=float(WEEKDAY_SHIFT_TARGETS["D"]),
        weekday_count=weekday_count,
        tolerance=tolerance,
    )


def daily_staffing_count_from_states(
    states: Mapping[str, object],
    assignment_date: date,
) -> int:
    count = 0
    for state in states.values():
        for work_date, _shift_id in getattr(state, "assignment_records", ()):
            if work_date == assignment_date:
                count += 1
    return count


def build_daily_staffing_counts(
    states: Mapping[str, object],
    *,
    period_start: date,
    period_end: date,
) -> Dict[date, int]:
    counts: Dict[date, int] = {}
    for assignment_date in _daterange(period_start, period_end):
        counts[assignment_date] = daily_staffing_count_from_states(states, assignment_date)
    return counts


def _morning_shift_template_ids(
    shift_templates: Mapping[str, ShiftTemplateInfo],
) -> set[str]:
    return {
        shift_id
        for shift_id, template in shift_templates.items()
        if template.code == "MORNING"
    }


def weekday_morning_shift_count_from_states(
    states: Mapping[str, object],
    assignment_date: date,
    *,
    shift_templates: Mapping[str, ShiftTemplateInfo],
) -> int:
    morning_ids = _morning_shift_template_ids(shift_templates)
    count = 0
    for state in states.values():
        for work_date, shift_id in getattr(state, "assignment_records", ()):
            if work_date == assignment_date and shift_id in morning_ids:
                count += 1
    return count


def weekday_day_shift_capacity_block(
    assignment_date: date,
    current_morning_count: int,
    *,
    shift_code: str,
    limit: int = WEEKDAY_DAY_SHIFT_CAPACITY_LIMIT,
) -> bool:
    """Hard gate: weekday day-shift (MORNING) headcount must stay at or below limit."""

    if assignment_date.weekday() >= 5 or shift_code != "MORNING":
        return False
    return current_morning_count >= limit


def weekday_morning_staffing_rank_penalty(
    assignment_date: date,
    current_morning_count: int,
    plan: Optional[WeekdayDailyStaffingPlan],
) -> float:
    """Prefer weekday day shifts on under-target dates; penalize exceeding the 13-seat target."""

    if plan is None or plan.target_per_day <= 0 or assignment_date.weekday() >= 5:
        return 0.0

    projected = float(current_morning_count + 1)
    target = plan.target_per_day
    if projected < target:
        return -60.0 * (target - projected + 1.0) / max(target, 1.0)
    if projected <= target + 1e-9:
        return 0.0
    return 120.0 + (projected - target) * 30.0


def weekday_morning_staffing_hard_block(
    assignment_date: date,
    current_morning_count: int,
    plan: Optional[WeekdayDailyStaffingPlan],
) -> bool:
    """Hard guard: weekday day-shift headcount must not exceed the operational target (13)."""

    if plan is None or plan.target_per_day <= 0 or assignment_date.weekday() >= 5:
        return False
    projected = float(current_morning_count + 1)
    return projected > plan.target_per_day + 1e-9


def weekday_daily_staffing_rank_penalty(
    assignment_date: date,
    current_count: int,
    plan: Optional[WeekdayDailyStaffingPlan],
) -> float:
    return weekday_morning_staffing_rank_penalty(
        assignment_date,
        current_count,
        plan,
    )


def weekday_daily_staffing_hard_block(
    assignment_date: date,
    current_count: int,
    plan: Optional[WeekdayDailyStaffingPlan],
) -> bool:
    return weekday_morning_staffing_hard_block(
        assignment_date,
        current_count,
        plan,
    )


def weekend_qual_counts_from_states(
    states: Mapping[str, object],
    *,
    employees: Sequence[EmployeeProfile],
    qual_codes: Mapping[str, str],
    assignment_date: date,
    shift_templates: Optional[Mapping[str, ShiftTemplateInfo]] = None,
    morning_only: bool = False,
) -> Dict[str, int]:
    counts = {qual: 0 for qual in WEEKEND_CLINICAL_MAX_PER_QUAL}
    morning_ids = (
        _morning_shift_template_ids(shift_templates)
        if morning_only and shift_templates is not None
        else None
    )
    employees_by_id = {employee.id: employee for employee in employees}
    for employee_id, state in states.items():
        employee = employees_by_id.get(employee_id)
        if employee is None:
            continue
        for work_date, shift_id in getattr(state, "assignment_records", ()):
            if work_date != assignment_date:
                continue
            if morning_ids is not None and shift_id not in morning_ids:
                continue
            qual_code = infer_qual_code(employee, qual_codes=qual_codes)
            if qual_code in counts:
                counts[qual_code] += 1
    return counts


def weekend_qual_cap_reached(
    counts: Mapping[str, int],
    qual_code: str,
) -> bool:
    cap = WEEKEND_CLINICAL_MAX_PER_QUAL.get(qual_code)
    if cap is None:
        return False
    return int(counts.get(qual_code, 0)) >= cap


def weekend_morning_clinical_floor_satisfied(counts: Mapping[str, int]) -> bool:
    """
    True when a Sat/Sun morning meets the clinical floor: 1 MLT + 1 MLA, or 2 MLT.
    """

    mlt = int(counts.get("MLT", 0))
    mla = int(counts.get("MLA", 0))
    if (
        mlt >= WEEKEND_CLINICAL_MIN_PER_QUAL.get("MLT", 1)
        and mla >= WEEKEND_CLINICAL_MIN_PER_QUAL.get("MLA", 1)
    ):
        return True
    return mlt >= WEEKEND_MORNING_TOTAL_CAP


def weekend_morning_fill_blocked(
    counts: Mapping[str, int],
    qual_code: str,
) -> bool:
    """Block another weekend morning assignment when caps or the floor are already met."""

    if weekend_qual_cap_reached(counts, qual_code):
        return True
    total = sum(int(counts.get(code, 0)) for code in WEEKEND_CLINICAL_MIN_PER_QUAL)
    if total >= WEEKEND_MORNING_TOTAL_CAP:
        return True
    if qual_code == "MLA" and int(counts.get("MLT", 0)) >= WEEKEND_MORNING_TOTAL_CAP:
        return True
    return False


def weekend_morning_slot_credited_as_filled(
    slot: ExpandedScheduleSlot,
    fill_counts: Mapping[tuple[date, str, Optional[str]], int],
    *,
    shift_templates: Optional[Mapping[str, ShiftTemplateInfo]] = None,
) -> bool:
    """
    Credit unfilled weekend morning MLA clinical seats when two MLT are already scheduled.
    """

    if slot.assignment_date.weekday() < 5 or slot.required_qual_code != "MLA":
        return False
    if shift_templates is None:
        return False
    template = shift_templates.get(slot.shift_id)
    if template is None or template.code != "MORNING":
        return False
    if not is_clinical_floor_pool(slot.role_pool_id):
        return False
    mlt_key = (slot.assignment_date, slot.shift_id, "MLT")
    return int(fill_counts.get(mlt_key, 0)) >= WEEKEND_MORNING_TOTAL_CAP


def prune_weekend_assignments_to_cap(
    assignments: list[object],
    *,
    states: Mapping[str, object],
    employees: Sequence[EmployeeProfile],
    qual_codes: Mapping[str, str],
    shift_templates: Mapping[str, ShiftTemplateInfo],
    period_start: date,
    period_end: date,
    all_shifts: bool = False,
) -> int:
    """
    Remove excess weekend assignments so each Sat/Sun respects ``WEEKEND_CLINICAL_MAX_PER_QUAL``.
    When ``all_shifts`` is False (default), only morning clinical-floor shifts are trimmed.
    Returns the number of assignments removed.
    """

    morning_ids = _morning_shift_template_ids(shift_templates)

    employees_by_id = {employee.id: employee for employee in employees}
    removed = 0
    keep_keys: set[tuple[date, str, str]] = set()

    for assignment_date in _daterange(period_start, period_end):
        if assignment_date.weekday() < 5:
            continue
        kept_by_qual: Dict[str, int] = {qual: 0 for qual in WEEKEND_CLINICAL_MAX_PER_QUAL}
        day_assignments = [
            assignment
            for assignment in assignments
            if getattr(assignment, "assignment_date", None) == assignment_date
        ]
        for assignment in day_assignments:
            employee = employees_by_id.get(getattr(assignment, "employee_id", ""))
            if employee is None:
                continue
            shift_id = str(getattr(assignment, "shift_template_id", ""))
            if not all_shifts and shift_id not in morning_ids:
                continue
            qual_code = infer_qual_code(employee, qual_codes=qual_codes)
            cap = WEEKEND_CLINICAL_MAX_PER_QUAL.get(qual_code)
            if cap is None:
                continue
            if kept_by_qual.get(qual_code, 0) >= cap:
                continue
            kept_by_qual[qual_code] = kept_by_qual.get(qual_code, 0) + 1
            keep_keys.add(
                (
                    assignment_date,
                    str(getattr(assignment, "employee_id", "")),
                    shift_id,
                )
            )

    pruned: list[object] = []
    for assignment in assignments:
        assignment_date = getattr(assignment, "assignment_date", None)
        shift_id = str(getattr(assignment, "shift_template_id", ""))
        if (
            isinstance(assignment_date, date)
            and assignment_date.weekday() >= 5
            and (all_shifts or shift_id in morning_ids)
            and (
                assignment_date,
                str(getattr(assignment, "employee_id", "")),
                shift_id,
            )
            not in keep_keys
        ):
            employee_id = str(getattr(assignment, "employee_id", ""))
            state = states.get(employee_id)
            template = shift_templates.get(str(getattr(assignment, "shift_template_id", "")))
            if state is not None and template is not None:
                shift_hours = template.duration_minutes / 60.0
                week_start = workweek_for(assignment_date).start
                state.work_dates.discard(assignment_date)
                state.assignment_records[:] = [
                    record
                    for record in state.assignment_records
                    if not (record[0] == assignment_date and record[1] == template.id)
                ]
                state.total_hours = max(0.0, state.total_hours - shift_hours)
                if week_start in state.week_hours:
                    state.week_hours[week_start] = max(
                        0.0,
                        state.week_hours[week_start] - shift_hours,
                    )
            removed += 1
            continue
        pruned.append(assignment)

    assignments[:] = pruned
    return removed


def weekend_qual_counts_from_assignments(
    assignments: Sequence[object],
    *,
    employees: Sequence[EmployeeProfile],
    qual_codes: Mapping[str, str],
    assignment_date: date,
    shift_templates: Optional[Mapping[str, ShiftTemplateInfo]] = None,
    morning_only: bool = False,
) -> Dict[str, int]:
    counts = {qual: 0 for qual in WEEKEND_CLINICAL_MAX_PER_QUAL}
    morning_ids = (
        _morning_shift_template_ids(shift_templates)
        if morning_only and shift_templates is not None
        else None
    )
    employees_by_id = {employee.id: employee for employee in employees}
    for assignment in assignments:
        if getattr(assignment, "assignment_date", None) != assignment_date:
            continue
        shift_id = str(getattr(assignment, "shift_template_id", ""))
        if morning_ids is not None and shift_id not in morning_ids:
            continue
        employee = employees_by_id.get(getattr(assignment, "employee_id", ""))
        if employee is None:
            continue
        qual_code = infer_qual_code(employee, qual_codes=qual_codes)
        if qual_code in counts:
            counts[qual_code] += 1
    return counts


def trim_weekend_daily_qual_over_cap(
    assignments: list[object],
    *,
    states: Mapping[str, object],
    employees: Sequence[EmployeeProfile],
    qual_codes: Mapping[str, str],
    shift_templates: Mapping[str, ShiftTemplateInfo],
    period_start: date,
    period_end: date,
) -> int:
    """
    Drop excess Sat/Sun assignments when a qual exceeds ``WEEKEND_CLINICAL_MAX_PER_QUAL``.

    When choosing which rows to keep at the cap, prefer lines with fewer weekend shifts overall.
    """

    employees_by_id = {employee.id: employee for employee in employees}
    morning_ids = _morning_shift_template_ids(shift_templates)
    weekend_shift_totals: Dict[str, int] = {employee.id: 0 for employee in employees}
    for assignment in assignments:
        assignment_date = getattr(assignment, "assignment_date", None)
        if not isinstance(assignment_date, date):
            continue
        if assignment_date < period_start or assignment_date > period_end:
            continue
        if assignment_date.weekday() < 5:
            continue
        employee_id = str(getattr(assignment, "employee_id", ""))
        weekend_shift_totals[employee_id] = weekend_shift_totals.get(employee_id, 0) + 1

    keep_keys: set[tuple[date, str, str]] = set()
    for assignment_date in _daterange(period_start, period_end):
        if assignment_date.weekday() < 5:
            continue
        for qual_code, maximum in WEEKEND_CLINICAL_MAX_PER_QUAL.items():
            day_assignments = [
                assignment
                for assignment in assignments
                if getattr(assignment, "assignment_date", None) == assignment_date
                and str(getattr(assignment, "shift_template_id", "")) in morning_ids
                and qual_code
                == infer_qual_code(
                    employees_by_id.get(str(getattr(assignment, "employee_id", "")), employees[0]),
                    qual_codes=qual_codes,
                )
            ]
            day_assignments.sort(
                key=lambda item: weekend_shift_totals.get(
                    str(getattr(item, "employee_id", "")), 0
                )
            )
            kept = 0
            for assignment in day_assignments:
                if kept >= maximum:
                    break
                keep_keys.add(
                    (
                        assignment_date,
                        str(getattr(assignment, "employee_id", "")),
                        str(getattr(assignment, "shift_template_id", "")),
                    )
                )
                kept += 1

    removed = 0
    pruned: list[object] = []
    for assignment in assignments:
        assignment_date = getattr(assignment, "assignment_date", None)
        shift_id = str(getattr(assignment, "shift_template_id", ""))
        employee_id = str(getattr(assignment, "employee_id", ""))
        if (
            isinstance(assignment_date, date)
            and assignment_date.weekday() >= 5
            and shift_id in morning_ids
            and (assignment_date, employee_id, shift_id) not in keep_keys
        ):
            state = states.get(employee_id)
            template = shift_templates.get(shift_id)
            if state is not None and template is not None:
                shift_hours = template.duration_minutes / 60.0
                week_start = workweek_for(assignment_date).start
                state.work_dates.discard(assignment_date)  # type: ignore[union-attr]
                state.assignment_records[:] = [  # type: ignore[union-attr]
                    record
                    for record in state.assignment_records  # type: ignore[union-attr]
                    if not (record[0] == assignment_date and record[1] == template.id)
                ]
                state.total_hours = max(0.0, state.total_hours - shift_hours)  # type: ignore[union-attr]
                state.week_hours[week_start] = max(  # type: ignore[union-attr]
                    0.0,
                    state.week_hours.get(week_start, 0.0) - shift_hours,  # type: ignore[union-attr]
                )
            weekend_shift_totals[employee_id] = max(
                0, weekend_shift_totals.get(employee_id, 0) - 1
            )
            removed += 1
            continue
        pruned.append(assignment)
    assignments[:] = pruned
    return removed


def morning_shift_hours(
    shift_templates: Mapping[str, ShiftTemplateInfo],
    *,
    default: float = 8.0,
) -> float:
    for shift_id, template in shift_templates.items():
        if template.code == "MORNING":
            return template.duration_minutes / 60.0
        if "morning" in shift_id.lower():
            return template.duration_minutes / 60.0
    return default
