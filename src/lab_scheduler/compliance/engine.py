from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Dict, Iterable, List, Literal, Mapping, Optional, Sequence, Set, Tuple

from lab_scheduler.compliance.jurisdictions import JurisdictionRules
from lab_scheduler.errors.schedule_error import ScheduleError
from lab_scheduler.time import workweek_for

Severity = Literal["error", "warning"]


@dataclass(frozen=True, slots=True)
class ShiftTemplateInfo:
    id: str
    code: str
    name: str
    start_time: str  # HH:MM
    end_time: str
    duration_minutes: int
    crosses_midnight: bool


@dataclass(frozen=True, slots=True)
class ScheduledShift:
    employee_id: str
    employee_name: str
    assignment_date: date
    shift_template_id: str
    approved_stretch: bool = False
    clinical_floor_stretch: bool = False
    contract_line_exception: bool = False
    contract_line_exception_message: str = ""


@dataclass(frozen=True, slots=True)
class ComplianceViolation:
    code: str
    severity: Severity
    employee_id: str
    employee_name: str
    message: str
    rule_reference: str


@dataclass
class EmployeeLaborSummary:
    employee_id: str
    employee_name: str
    fte: float
    target_hours: float
    scheduled_hours: float
    delta_hours: float
    statutory_overtime_hours: float
    is_over_target_fte: bool
    has_statutory_violations: bool


@dataclass
class ComplianceReport:
    jurisdiction_code: str
    violations: List[ComplianceViolation] = field(default_factory=list)
    labor_summaries: List[EmployeeLaborSummary] = field(default_factory=list)

    @property
    def error_count(self) -> int:
        return sum(1 for v in self.violations if v.severity == "error")

    @property
    def warning_count(self) -> int:
        return sum(1 for v in self.violations if v.severity == "warning")


def _parse_hhmm(value: str) -> time:
    hour, minute = (int(part) for part in value.split(":"))
    return time(hour=hour, minute=minute)


def _shift_interval(assignment_date: date, template: ShiftTemplateInfo) -> Tuple[datetime, datetime]:
    start_t = _parse_hhmm(template.start_time)
    end_t = _parse_hhmm(template.end_time)
    start = datetime.combine(assignment_date, start_t)
    end_day = assignment_date
    if template.crosses_midnight or end_t <= start_t:
        end_day = assignment_date + timedelta(days=1)
    end = datetime.combine(end_day, end_t)
    return start, end


def _hours_between(end_dt: datetime, start_dt: datetime) -> float:
    return (start_dt - end_dt).total_seconds() / 3600.0


def _iter_work_weeks(period_start: date, period_end: date) -> Iterable[Tuple[date, date]]:
    cur = workweek_for(period_start).start
    while cur <= period_end:
        week_end = min(cur + timedelta(days=6), period_end)
        yield cur, week_end
        cur += timedelta(days=7)


def _consecutive_work_day_streaks(work_dates: Sequence[date]) -> List[Tuple[date, date, int]]:
    if not work_dates:
        return []
    sorted_dates = sorted(set(work_dates))
    streaks: List[Tuple[date, date, int]] = []
    streak_start = sorted_dates[0]
    prev = sorted_dates[0]
    streak_len = 1
    for d in sorted_dates[1:]:
        if (d - prev).days == 1:
            streak_len += 1
            prev = d
            continue
        streaks.append((streak_start, prev, streak_len))
        streak_start = d
        prev = d
        streak_len = 1
    streaks.append((streak_start, prev, streak_len))
    return streaks


def evaluate_schedule(
    rules: JurisdictionRules,
    *,
    employees: Sequence[Dict],
    assignments: Sequence[ScheduledShift],
    shift_templates: Dict[str, ShiftTemplateInfo],
    period_start: date,
    period_end: date,
    weeks_in_period: int,
    employee_target_hours: Optional[Mapping[str, float]] = None,
) -> ComplianceReport:
    report = ComplianceReport(jurisdiction_code=rules.code)

    by_employee: Dict[str, List[ScheduledShift]] = {}
    for a in assignments:
        by_employee.setdefault(a.employee_id, []).append(a)

    for emp in employees:
        emp_id = emp["id"]
        emp_name = emp["full_name"]
        fte = float(emp["fte"])
        emp_assignments = by_employee.get(emp_id, [])

        scheduled_hours = sum(
            shift_templates[a.shift_template_id].duration_minutes / 60.0
            for a in emp_assignments
            if a.shift_template_id in shift_templates
        )
        default_target = rules.standard_hours_per_week_at_1_0_fte * fte * weeks_in_period
        if employee_target_hours is not None and emp_id in employee_target_hours:
            target_hours = float(employee_target_hours[emp_id])
        else:
            target_hours = default_target
        delta = scheduled_hours - target_hours
        statutory_ot = _statutory_overtime_hours(rules, emp_assignments, shift_templates, period_start, period_end)

        report.labor_summaries.append(
            EmployeeLaborSummary(
                employee_id=emp_id,
                employee_name=emp_name,
                fte=fte,
                target_hours=target_hours,
                scheduled_hours=scheduled_hours,
                delta_hours=delta,
                statutory_overtime_hours=statutory_ot,
                is_over_target_fte=delta > 1e-9,
                has_statutory_violations=False,
            )
        )

        if not emp_assignments:
            continue

        work_dates = [a.assignment_date for a in emp_assignments]
        _check_consecutive_days(report, rules, emp_id, emp_name, work_dates)
        _check_weekly_rest(report, rules, emp_id, emp_name, work_dates, period_start, period_end)
        _check_daily_and_weekly_overtime(
            report, rules, emp_id, emp_name, emp_assignments, shift_templates, period_start, period_end
        )
        _check_rest_periods(report, rules, emp_id, emp_name, emp_assignments, shift_templates)
        _check_shift_breaks(report, rules, emp_id, emp_name, emp_assignments, shift_templates)

    for summary in report.labor_summaries:
        if any(v.employee_id == summary.employee_id and v.severity == "error" for v in report.violations):
            summary.has_statutory_violations = True

    return report


def _statutory_overtime_hours(
    rules: JurisdictionRules,
    assignments: Sequence[ScheduledShift],
    templates: Dict[str, ShiftTemplateInfo],
    period_start: date,
    period_end: date,
) -> float:
    # Weekly overtime hours (daily OT is surfaced via violation alerts).
    total_ot = 0.0
    by_week: Dict[date, float] = {}

    for a in assignments:
        if a.shift_template_id not in templates:
            continue
        hours = templates[a.shift_template_id].duration_minutes / 60.0
        week_start = workweek_for(a.assignment_date).start
        by_week[week_start] = by_week.get(week_start, 0.0) + hours

    for week_start, hours in by_week.items():
        if week_start + timedelta(days=6) < period_start or week_start > period_end:
            continue
        if hours > rules.weekly_overtime_threshold_hours:
            total_ot += hours - rules.weekly_overtime_threshold_hours

    return round(total_ot, 2)


def _check_consecutive_days(
    report: ComplianceReport,
    rules: JurisdictionRules,
    employee_id: str,
    employee_name: str,
    work_dates: Sequence[date],
) -> None:
    for start, end, length in _consecutive_work_day_streaks(work_dates):
        if length > rules.max_consecutive_work_days:
            report.violations.append(
                ComplianceViolation(
                    code=ScheduleError.CONSECUTIVE_DAYS.value,
                    severity="error",
                    employee_id=employee_id,
                    employee_name=employee_name,
                    message=(
                        f"{length} consecutive work days ({start.isoformat()} to {end.isoformat()}) "
                        f"exceeds the {rules.max_consecutive_work_days}-day limit."
                    ),
                    rule_reference=rules.citation_label,
                )
            )


def _check_weekly_rest(
    report: ComplianceReport,
    rules: JurisdictionRules,
    employee_id: str,
    employee_name: str,
    work_dates: Sequence[date],
    period_start: date,
    period_end: date,
) -> None:
    work_set = set(work_dates)
    for week_start, week_end in _iter_work_weeks(period_start, period_end):
        days_in_week = [week_start + timedelta(days=i) for i in range((week_end - week_start).days + 1)]
        worked = sum(1 for d in days_in_week if d in work_set)
        if worked > rules.max_work_days_per_work_week:
            report.violations.append(
                ComplianceViolation(
                    code=ScheduleError.WEEKLY_REST.value,
                    severity="error",
                    employee_id=employee_id,
                    employee_name=employee_name,
                    message=(
                        f"{worked} scheduled days in work week {week_start.isoformat()}–{week_end.isoformat()} "
                        f"leaves insufficient time for {rules.min_weekly_rest_hours:.0f}h weekly rest."
                    ),
                    rule_reference=rules.citation_label,
                )
            )


def _check_daily_and_weekly_overtime(
    report: ComplianceReport,
    rules: JurisdictionRules,
    employee_id: str,
    employee_name: str,
    assignments: Sequence[ScheduledShift],
    templates: Dict[str, ShiftTemplateInfo],
    period_start: date,
    period_end: date,
) -> None:
    by_week: Dict[date, float] = {}
    by_day: Dict[date, float] = {}

    for a in assignments:
        tmpl = templates[a.shift_template_id]
        hours = tmpl.duration_minutes / 60.0
        week_start = workweek_for(a.assignment_date).start
        by_week[week_start] = by_week.get(week_start, 0.0) + hours
        by_day[a.assignment_date] = by_day.get(a.assignment_date, 0.0) + hours

        if rules.max_scheduled_hours_per_day is not None and hours > rules.max_scheduled_hours_per_day:
            report.violations.append(
                ComplianceViolation(
                    code=ScheduleError.MAX_DAILY_HOURS.value,
                    severity="warning",
                    employee_id=employee_id,
                    employee_name=employee_name,
                    message=(
                        f"{hours:.1f}h on {a.assignment_date.isoformat()} ({tmpl.code}) exceeds the "
                        f"{rules.max_scheduled_hours_per_day:.0f}h general daily limit."
                    ),
                    rule_reference=rules.citation_label,
                )
            )

    for week_start, hours in by_week.items():
        if hours > rules.weekly_overtime_threshold_hours:
            ot = hours - rules.weekly_overtime_threshold_hours
            report.violations.append(
                ComplianceViolation(
                    code=ScheduleError.WEEKLY_OVERTIME.value,
                    severity="warning",
                    employee_id=employee_id,
                    employee_name=employee_name,
                    message=(
                        f"{hours:.1f}h in work week starting {week_start.isoformat()} "
                        f"→ {ot:.1f}h statutory overtime (threshold {rules.weekly_overtime_threshold_hours:.0f}h)."
                    ),
                    rule_reference=rules.citation_label,
                )
            )

    if rules.daily_overtime_threshold_hours is not None:
        for day, hours in by_day.items():
            if hours > rules.daily_overtime_threshold_hours:
                ot = hours - rules.daily_overtime_threshold_hours
                report.violations.append(
                    ComplianceViolation(
                        code=ScheduleError.DAILY_OVERTIME.value,
                        severity="warning",
                        employee_id=employee_id,
                        employee_name=employee_name,
                        message=(
                            f"{hours:.1f}h on {day.isoformat()} "
                            f"→ {ot:.1f}h daily overtime (threshold {rules.daily_overtime_threshold_hours:.0f}h)."
                        ),
                        rule_reference=rules.citation_label,
                    )
                )


def _check_rest_periods(
    report: ComplianceReport,
    rules: JurisdictionRules,
    employee_id: str,
    employee_name: str,
    assignments: Sequence[ScheduledShift],
    templates: Dict[str, ShiftTemplateInfo],
) -> None:
    intervals: List[Tuple[datetime, datetime, ScheduledShift]] = []
    for a in assignments:
        tmpl = templates[a.shift_template_id]
        start, end = _shift_interval(a.assignment_date, tmpl)
        intervals.append((start, end, a))
    intervals.sort(key=lambda x: x[0])

    for i in range(1, len(intervals)):
        prev_start, prev_end, prev_a = intervals[i - 1]
        cur_start, cur_end, cur_a = intervals[i]
        gap = _hours_between(prev_end, cur_start)
        if gap < 0:
            report.violations.append(
                ComplianceViolation(
                    code=ScheduleError.OVERLAPPING_SHIFTS.value,
                    severity="error",
                    employee_id=employee_id,
                    employee_name=employee_name,
                    message=(
                        f"Overlapping shifts: {prev_a.assignment_date.isoformat()} and "
                        f"{cur_a.assignment_date.isoformat()}."
                    ),
                    rule_reference=rules.citation_label,
                )
            )
            continue

        min_rest = rules.min_daily_rest_hours or rules.min_rest_between_shifts_hours
        if min_rest is None:
            continue

        if gap < min_rest:
            code = (
                ScheduleError.DAILY_REST.value
                if rules.min_daily_rest_hours
                else ScheduleError.BETWEEN_SHIFTS.value
            )
            label = (
                f"{rules.min_daily_rest_hours:.0f}h daily rest"
                if rules.min_daily_rest_hours
                else f"{rules.min_rest_between_shifts_hours:.0f}h between shifts"
            )
            report.violations.append(
                ComplianceViolation(
                    code=code,
                    severity="error",
                    employee_id=employee_id,
                    employee_name=employee_name,
                    message=(
                        f"Only {gap:.1f}h off between {prev_a.assignment_date.isoformat()} and "
                        f"{cur_a.assignment_date.isoformat()} ({label} required)."
                    ),
                    rule_reference=rules.citation_label,
                )
            )


def _check_shift_breaks(
    report: ComplianceReport,
    rules: JurisdictionRules,
    employee_id: str,
    employee_name: str,
    assignments: Sequence[ScheduledShift],
    templates: Dict[str, ShiftTemplateInfo],
) -> None:
    if rules.break_after_consecutive_hours is None:
        return

    for a in assignments:
        tmpl = templates[a.shift_template_id]
        hours = tmpl.duration_minutes / 60.0
        if hours > rules.break_after_consecutive_hours:
            report.violations.append(
                ComplianceViolation(
                    code=ScheduleError.UNPAID_BREAK.value,
                    severity="warning",
                    employee_id=employee_id,
                    employee_name=employee_name,
                    message=(
                        f"{hours:.1f}h {tmpl.code} shift on {a.assignment_date.isoformat()} requires a "
                        f"{rules.break_minutes}-minute unpaid break after "
                        f"{rules.break_after_consecutive_hours:.0f} consecutive hours."
                    ),
                    rule_reference=rules.citation_label,
                )
            )
