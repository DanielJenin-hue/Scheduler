from __future__ import annotations

import html
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Dict, List, Mapping, Optional, Sequence, Set, Tuple

from lab_scheduler.compliance.engine import (
    ShiftTemplateInfo,
    _consecutive_work_day_streaks,
)
from lab_scheduler.engine.demand import PORTAGE_MAX_CONSECUTIVE_WORK_DAYS
from lab_scheduler.scheduling.auto_generate import PlannedAssignment
from lab_scheduler.scheduling.breakroom_print import WORKED_SHIFT_TOKENS, normalize_breakroom_cell
from lab_scheduler.scheduling.date_utils import daterange as _daterange
from lab_scheduler.scheduling.profiles import EmployeeProfile

WORK_SHIFT_CODES: frozenset[str] = frozenset({"MORNING", "EVENING", "NIGHT"})


@dataclass(frozen=True, slots=True)
class WorkStreakViolation:
    employee_id: str
    employee_name: str
    start_date: date
    end_date: date
    length: int
    message: str

    def to_dict(self) -> Dict[str, object]:
        return {
            "employee_id": self.employee_id,
            "employee_name": self.employee_name,
            "start_date": self.start_date.isoformat(),
            "end_date": self.end_date.isoformat(),
            "length": self.length,
            "message": self.message,
        }


def is_worked_schedule_cell(value: object) -> bool:
    """Shift-agnostic: any worked D/E/N token (including M→D normalization) counts."""

    token = normalize_breakroom_cell(value)
    if not token:
        return False
    if token == "[UNFILLED - ESCALATED]" or " | " in token:
        return False
    return token in WORKED_SHIFT_TOKENS


def work_dates_from_schedule_row(
    row: Mapping[str, object],
    dates: Sequence[date],
) -> Set[date]:
    worked: Set[date] = set()
    for day in dates:
        raw = row.get(day.isoformat(), row.get(day, ""))
        if is_worked_schedule_cell(raw):
            worked.add(day)
    return worked


def find_work_streak_violations_for_dates(
    *,
    employee_id: str,
    employee_name: str,
    work_dates: Set[date],
    period_start: date,
    period_end: date,
    max_consecutive_work_days: int = PORTAGE_MAX_CONSECUTIVE_WORK_DAYS,
) -> List[WorkStreakViolation]:
    """Return consecutive calendar work streaks longer than the Portage cap."""

    if not work_dates:
        return []

    violation_min_length = max_consecutive_work_days + 1
    violations: List[WorkStreakViolation] = []
    for start, end, length in _consecutive_work_day_streaks(sorted(work_dates)):
        if length < violation_min_length:
            continue
        violations.append(
            WorkStreakViolation(
                employee_id=employee_id,
                employee_name=employee_name,
                start_date=start,
                end_date=end,
                length=length,
                message=(
                    f"{employee_name}: {length} consecutive work days "
                    f"({start.isoformat()}..{end.isoformat()}) exceeds the "
                    f"{max_consecutive_work_days}-day Portage fatigue cap."
                ),
            )
        )
    return violations


def validate_work_streaks_from_schedule_rows(
    schedule_rows: Sequence[Mapping[str, object]],
    *,
    employees: Sequence[Mapping[str, object]],
    dates: Sequence[date],
    max_consecutive_work_days: int = PORTAGE_MAX_CONSECUTIVE_WORK_DAYS,
) -> List[WorkStreakViolation]:
    """
    Shift-agnostic export validator: count any non-empty D/E/N cell as a work day
    across the full ``dates`` array passed to breakroom render (not a UI week slice).
    """

    from lab_scheduler.scheduling.schedule_tallies import is_daily_tally_row

    if not dates:
        return []

    period_start = dates[0]
    period_end = dates[-1]
    names = {
        str(employee.get("id", "")): str(
            employee.get("full_name", employee.get("Employee", ""))
        )
        for employee in employees
    }
    violations: List[WorkStreakViolation] = []

    for row in schedule_rows:
        if is_daily_tally_row(row):
            continue
        employee_id = str(row.get("employee_id", "")).strip()
        if not employee_id:
            continue
        employee_name = str(row.get("Employee", names.get(employee_id, employee_id)))
        work_dates = work_dates_from_schedule_row(row, dates)
        violations.extend(
            find_work_streak_violations_for_dates(
                employee_id=employee_id,
                employee_name=employee_name,
                work_dates=work_dates,
                period_start=period_start,
                period_end=period_end,
                max_consecutive_work_days=max_consecutive_work_days,
            )
        )
    return violations


def validate_work_streaks_from_assignments(
    assignments: Sequence[PlannedAssignment],
    *,
    employees: Sequence[EmployeeProfile],
    shift_templates: Mapping[str, ShiftTemplateInfo],
    period_start: date,
    period_end: date,
    max_consecutive_work_days: int = PORTAGE_MAX_CONSECUTIVE_WORK_DAYS,
) -> List[WorkStreakViolation]:
    """Assignment-based validator used after generation / before export."""

    names = {employee.id: employee.full_name for employee in employees}
    work_dates_by_employee: Dict[str, Set[date]] = {}

    for assignment in assignments:
        template = shift_templates.get(assignment.shift_template_id)
        if template is None or template.code not in WORK_SHIFT_CODES:
            continue
        work_dates_by_employee.setdefault(assignment.employee_id, set()).add(
            assignment.assignment_date
        )

    violations: List[WorkStreakViolation] = []
    for employee_id, work_dates in work_dates_by_employee.items():
        violations.extend(
            find_work_streak_violations_for_dates(
                employee_id=employee_id,
                employee_name=names.get(employee_id, employee_id),
                work_dates=work_dates,
                period_start=period_start,
                period_end=period_end,
                max_consecutive_work_days=max_consecutive_work_days,
            )
        )
    return violations


def format_work_streak_violations_html(
    violations: Sequence[WorkStreakViolation],
) -> str:
    if not violations:
        return ""

    items = "".join(
        f"<li><strong>WORK_STREAK</strong> — {html.escape(violation.message)}</li>"
        for violation in violations
    )
    return f"""
  <h3>Consecutive Work-Day Violations</h3>
  <p class="aggressive-fill-note">Any worked D/E/N cell counts toward the streak. Runs longer than {PORTAGE_MAX_CONSECUTIVE_WORK_DAYS} calendar days block breakroom export approval. Re-run Auto-Pilot; do not hand-edit cells.</p>
  <ul class="aggressive-fill-list work-streak-list">{items}</ul>
"""


def summarize_worst_work_streaks(
    violations: Sequence[WorkStreakViolation],
    *,
    limit: int = 5,
) -> List[Tuple[str, int, date, date]]:
    ranked = sorted(violations, key=lambda item: item.length, reverse=True)
    return [
        (item.employee_id, item.length, item.start_date, item.end_date)
        for item in ranked[:limit]
    ]
