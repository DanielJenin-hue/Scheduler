from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence, Set, Tuple

from lab_scheduler.compliance.compliance_rules import (
    PORTAGE_WARNING_CONSECUTIVE_DAYS,
    UNION_MIN_TURNAROUND_HOURS,
    ShiftTransition,
    check_11_hour_rest,
    clinical_floor_stretch_allowed,
    turnaround_gap_hours,
)
from lab_scheduler.errors.schedule_error import (
    CONSECUTIVE_DAYS_WARNING_CODE,
    OVERTIME_COMPLIANCE_BYPASS_LABEL,
    ScheduleError,
    manager_label_for_code,
)
from lab_scheduler.compliance.engine import (
    ComplianceViolation,
    ScheduledShift,
    ShiftTemplateInfo,
    _consecutive_work_day_streaks,
    evaluate_schedule,
)
from lab_scheduler.compliance.jurisdictions import JurisdictionRules
from lab_scheduler.engine.demand import (
    CLINICAL_FLOOR,
    PORTAGE_MAX_CONSECUTIVE_WORK_DAYS,
    WEEKEND_CLINICAL_MAX_PER_QUAL,
    WEEKEND_CLINICAL_MIN_PER_QUAL,
    WEEKDAY_DAY_SHIFT_CAPACITY_LIMIT,
    build_qual_code_lookup,
    clinical_floor_filled_for_day,
    count_band_shifts_by_day,
    is_evening_night_clinical_floor_satisfied,
)
from lab_scheduler.scheduling.load_balancing import weekend_qual_counts_from_assignments
from lab_scheduler.scheduling.contract_payroll import (
    FULLTIME_FTE_THRESHOLD,
    contract_fte_manager_label,
    fulltime_period_contract_hours,
    is_fulltime_contract_deficit,
)
from lab_scheduler.scheduling.profiles import EmployeeProfile
from lab_scheduler.scheduling.assignment_rejection_log import log_assignment_rejection
from lab_scheduler.time import workweek_for

from lab_scheduler.scheduling.provisional_compliance import (
    ProvisionalAssignment,
    approved_stretch_from_system_note,
    build_contract_line_provisional_assignment,
    partition_provisional_conflicts,
)
from lab_scheduler.scheduling.provisional_constants import (
    CONTRACT_LINE_EXCEPTION_VIOLATION_CODE,
)

CONFLICT_REPORT_PREFIX = "Conflict_Report"


@dataclass(frozen=True, slots=True)
class ComplianceConflict:
    """Single rule violation surfaced to managers and conflict reports."""

    category: str
    code: str
    manager_label: str
    message: str
    employee_id: str = ""
    employee_name: str = ""
    assignment_date: Optional[date] = None

    def to_dict(self) -> dict:
        payload = asdict(self)
        if self.assignment_date is not None:
            payload["assignment_date"] = self.assignment_date.isoformat()
        else:
            payload.pop("assignment_date")
        return payload


@dataclass
class ComplianceValidationResult:
    passed: bool
    pass_rate_pct: float
    conflicts: List[ComplianceConflict] = field(default_factory=list)
    warnings: List[ComplianceConflict] = field(default_factory=list)
    provisional_assignments: List[ProvisionalAssignment] = field(default_factory=list)
    report_path: Optional[Path] = None

    @property
    def requires_provisional_approval(self) -> bool:
        return bool(self.provisional_assignments)

    @property
    def conflict_count(self) -> int:
        return len(self.conflicts)

    @property
    def manager_summary(self) -> List[str]:
        seen: Set[str] = set()
        labels: List[str] = []
        for conflict in self.conflicts:
            if conflict.manager_label in seen:
                continue
            seen.add(conflict.manager_label)
            labels.append(conflict.manager_label)
        return labels


def _default_project_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


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


def _daterange(start: date, end_inclusive: date) -> List[date]:
    days: List[date] = []
    cur = start
    while cur <= end_inclusive:
        days.append(cur)
        cur += timedelta(days=1)
    return days


def _manager_label_for_violation(violation: ComplianceViolation) -> str:
    return manager_label_for_code(violation.code, fallback_message=violation.message)


class ComplianceValidator:
    """
    Master Schedule pre-flight audit.

    Validates union rest/turnaround rules, immutable Evening/Night clinical floors,
    and 1.0 FTE contract targets over the full master rotation block.
    """

    def __init__(self, project_root: Optional[Path] = None) -> None:
        self.project_root = project_root or _default_project_root()

    def validate(
        self,
        *,
        rules: JurisdictionRules,
        employees: Sequence[EmployeeProfile],
        assignments: Sequence[ScheduledShift],
        shift_templates: Mapping[str, ShiftTemplateInfo],
        period_start: date,
        period_end: date,
        weeks_in_period: int,
        employee_target_hours: Optional[Mapping[str, float]] = None,
        fill_counts: Optional[Mapping[Tuple[date, str, Optional[str]], int]] = None,
        expanded_slots: Optional[Sequence[object]] = None,
        enforce_clinical_floors: bool = True,
        enforce_weekend_limits: bool = True,
        require_contract_fte: bool = True,
        log_rejections: bool = False,
    ) -> ComplianceValidationResult:
        conflicts: List[ComplianceConflict] = []
        warnings: List[ComplianceConflict] = []
        if enforce_clinical_floors:
            conflicts.extend(
                self._check_clinical_evening_night_floors(
                    assignments=assignments,
                    fill_counts=fill_counts,
                    shift_templates=shift_templates,
                    period_start=period_start,
                    period_end=period_end,
                    expanded_slots=expanded_slots,
                )
            )
        if enforce_weekend_limits:
            conflicts.extend(
                self._check_weekend_qual_limits(
                    employees=employees,
                    assignments=assignments,
                    shift_templates=shift_templates,
                    period_start=period_start,
                    period_end=period_end,
                )
            )
        conflicts.extend(
            self._check_weekday_day_shift_capacity(
                assignments=assignments,
                shift_templates=shift_templates,
                period_start=period_start,
                period_end=period_end,
            )
        )
        conflicts.extend(
            self._check_manitoba_union_laws(
                rules=rules,
                employees=employees,
                assignments=assignments,
                shift_templates=shift_templates,
                period_start=period_start,
                period_end=period_end,
                weeks_in_period=weeks_in_period,
                employee_target_hours=employee_target_hours,
            )
        )
        portage_conflicts, portage_warnings = self._check_portage_consecutive_streaks(
            employees=employees,
            assignments=assignments,
            rules=rules,
        )
        conflicts.extend(portage_conflicts)
        warnings.extend(portage_warnings)
        if require_contract_fte:
            conflicts.extend(
                self._check_contract_fte_targets(
                    rules=rules,
                    employees=employees,
                    assignments=assignments,
                    shift_templates=shift_templates,
                    weeks_in_period=weeks_in_period,
                )
            )

        if log_rejections:
            for conflict in conflicts:
                if not conflict.employee_id:
                    continue
                log_assignment_rejection(
                    conflict.employee_id,
                    conflict.assignment_date,
                    conflict.manager_label or conflict.message,
                )

        approved_keys = {
            (
                assignment.employee_id,
                assignment.assignment_date,
                assignment.shift_template_id,
            )
            for assignment in assignments
            if assignment.approved_stretch or approved_stretch_from_system_note(
                getattr(assignment, "system_note", None)
            )
        }
        hard_conflicts, provisional_assignments = partition_provisional_conflicts(
            conflicts,
            assignments=assignments,
            shift_templates=shift_templates,
            approved_keys=approved_keys,
        )
        contract_warnings, contract_provisionals = self._collect_clinical_contract_line_provisionals(
            assignments=assignments,
            shift_templates=shift_templates,
        )
        warnings.extend(contract_warnings)
        provisional_by_key = {
            item.assignment_key(): item for item in contract_provisionals
        }
        for item in provisional_assignments:
            provisional_by_key[item.assignment_key()] = item
        provisional_assignments = list(provisional_by_key.values())
        passed = len(hard_conflicts) == 0
        return ComplianceValidationResult(
            passed=passed,
            pass_rate_pct=100.0 if passed else 0.0,
            conflicts=hard_conflicts,
            warnings=warnings,
            provisional_assignments=provisional_assignments,
        )

    def validate_or_abort(
        self,
        *,
        rules: JurisdictionRules,
        employees: Sequence[EmployeeProfile],
        assignments: Sequence[ScheduledShift],
        shift_templates: Mapping[str, ShiftTemplateInfo],
        period_start: date,
        period_end: date,
        weeks_in_period: int,
        employee_target_hours: Optional[Mapping[str, float]] = None,
        fill_counts: Optional[Mapping[Tuple[date, str, Optional[str]], int]] = None,
        expanded_slots: Optional[Sequence[object]] = None,
        require_contract_fte: bool = True,
        enforce_clinical_floors: bool = True,
        enforce_weekend_limits: bool = True,
        log_rejections: bool = False,
        report_date: Optional[date] = None,
        overtime_compliance_bypassed: Optional[Sequence[ComplianceConflict]] = None,
    ) -> ComplianceValidationResult:
        result = self.validate(
            rules=rules,
            employees=employees,
            assignments=assignments,
            shift_templates=shift_templates,
            period_start=period_start,
            period_end=period_end,
            weeks_in_period=weeks_in_period,
            employee_target_hours=employee_target_hours,
            fill_counts=fill_counts,
            expanded_slots=expanded_slots,
            require_contract_fte=require_contract_fte,
            enforce_clinical_floors=enforce_clinical_floors,
            enforce_weekend_limits=enforce_weekend_limits,
            log_rejections=log_rejections,
        )
        if result.passed:
            return result

        result.report_path = write_conflict_report(
            self.project_root,
            result,
            period_start=period_start,
            period_end=period_end,
            week_count=weeks_in_period,
            report_date=report_date,
            overtime_compliance_bypassed=overtime_compliance_bypassed,
        )
        return result

    def _check_clinical_evening_night_floors(
        self,
        *,
        assignments: Sequence[ScheduledShift],
        fill_counts: Optional[Mapping[Tuple[date, str, Optional[str]], int]],
        shift_templates: Mapping[str, ShiftTemplateInfo],
        period_start: date,
        period_end: date,
        expanded_slots: Optional[Sequence[object]],
    ) -> List[ComplianceConflict]:
        if fill_counts is not None and expanded_slots is not None:
            if is_evening_night_clinical_floor_satisfied(
                fill_counts=fill_counts,
                shift_templates=shift_templates,
                period_start=period_start,
                period_end=period_end,
                expanded_slots=expanded_slots,
            ):
                return []

            conflicts: List[ComplianceConflict] = []
            for assignment_date in _daterange(period_start, period_end):
                if assignment_date.weekday() >= 5:
                    continue
                for shift_code in ("EVENING", "NIGHT"):
                    filled = clinical_floor_filled_for_day(
                        assignment_date,
                        shift_code,
                        fill_counts=fill_counts,
                        expanded_slots=expanded_slots,
                        shift_templates=shift_templates,
                    )
                    required = CLINICAL_FLOOR[shift_code]
                    if filled == required:
                        continue
                    label = (
                        "Clinical floor violation (Evening)"
                        if shift_code == "EVENING"
                        else "Clinical floor violation (Night)"
                    )
                    conflicts.append(
                        ComplianceConflict(
                            category="clinical_floor",
                            code=ScheduleError.clinical_floor(shift_code).value,
                            manager_label=label,
                            message=(
                                f"{assignment_date.isoformat()} {shift_code}: "
                                f"{filled}/{required} seats filled (requires exactly {required})."
                            ),
                            assignment_date=assignment_date,
                        )
                    )
            return conflicts

        evening_counts = count_band_shifts_by_day(
            assignments,
            shift_templates=shift_templates,
            shift_code="EVENING",
        )
        night_counts = count_band_shifts_by_day(
            assignments,
            shift_templates=shift_templates,
            shift_code="NIGHT",
        )
        conflicts = []
        for assignment_date in _daterange(period_start, period_end):
            if assignment_date.weekday() >= 5:
                continue
            for shift_code, counts in (("EVENING", evening_counts), ("NIGHT", night_counts)):
                filled = counts.get(assignment_date, 0)
                required = CLINICAL_FLOOR[shift_code]
                if filled == required:
                    continue
                label = (
                    "Clinical floor violation (Evening)"
                    if shift_code == "EVENING"
                    else "Clinical floor violation (Night)"
                )
                conflicts.append(
                    ComplianceConflict(
                        category="clinical_floor",
                        code=ScheduleError.clinical_floor(shift_code).value,
                        manager_label=label,
                        message=(
                            f"{assignment_date.isoformat()} {shift_code}: "
                            f"{filled}/{required} seats filled (requires exactly {required})."
                        ),
                        assignment_date=assignment_date,
                    )
                )
        return conflicts

    def _check_weekend_qual_limits(
        self,
        *,
        employees: Sequence[EmployeeProfile],
        assignments: Sequence[ScheduledShift],
        shift_templates: Mapping[str, ShiftTemplateInfo],
        period_start: date,
        period_end: date,
    ) -> List[ComplianceConflict]:
        qual_codes = build_qual_code_lookup(employees)
        conflicts: List[ComplianceConflict] = []
        for assignment_date in _daterange(period_start, period_end):
            if assignment_date.weekday() < 5:
                continue
            counts = weekend_qual_counts_from_assignments(
                assignments,
                employees=employees,
                qual_codes=qual_codes,
                assignment_date=assignment_date,
                shift_templates=shift_templates,
                morning_only=True,
            )
            for qual_code, minimum in WEEKEND_CLINICAL_MIN_PER_QUAL.items():
                if counts.get(qual_code, 0) >= minimum:
                    continue
                conflicts.append(
                    ComplianceConflict(
                        category="weekend_staffing",
                        code=ScheduleError.WEEKEND_CLINICAL_FLOOR.value,
                        manager_label="Weekend staffing floor (1 MLT + 1 MLA)",
                        message=(
                            f"{assignment_date.isoformat()}: missing {qual_code} "
                            f"(counts MLT={counts.get('MLT', 0)} "
                            f"MLA={counts.get('MLA', 0)})"
                        ),
                        assignment_date=assignment_date,
                    )
                )
            for qual_code, maximum in WEEKEND_CLINICAL_MAX_PER_QUAL.items():
                if counts.get(qual_code, 0) <= maximum:
                    continue
                conflicts.append(
                    ComplianceConflict(
                        category="weekend_staffing",
                        code=ScheduleError.WEEKEND_STAFFING_CAP.value,
                        manager_label="Weekend staffing cap (1 MLT + 1 MLA)",
                        message=(
                            f"{assignment_date.isoformat()}: {qual_code} cap exceeded "
                            f"(max {maximum}, count {counts.get(qual_code, 0)})"
                        ),
                        assignment_date=assignment_date,
                    )
                )
        return conflicts

    def _check_weekday_day_shift_capacity(
        self,
        *,
        assignments: Sequence[ScheduledShift],
        shift_templates: Mapping[str, ShiftTemplateInfo],
        period_start: date,
        period_end: date,
    ) -> List[ComplianceConflict]:
        morning_counts = count_band_shifts_by_day(
            assignments,
            shift_templates=shift_templates,
            shift_code="MORNING",
        )
        conflicts: List[ComplianceConflict] = []
        for assignment_date in _daterange(period_start, period_end):
            if assignment_date.weekday() >= 5:
                continue
            count = morning_counts.get(assignment_date, 0)
            if count <= WEEKDAY_DAY_SHIFT_CAPACITY_LIMIT:
                continue
            conflicts.append(
                ComplianceConflict(
                    category="weekday_capacity",
                    code=ScheduleError.WEEKDAY_DAY_SHIFT_CAPACITY.value,
                    manager_label=(
                        f"Weekday day-shift capacity limit "
                        f"({WEEKDAY_DAY_SHIFT_CAPACITY_LIMIT} staff)"
                    ),
                    message=(
                        f"{assignment_date.isoformat()}: day shift staffed "
                        f"{count}/{WEEKDAY_DAY_SHIFT_CAPACITY_LIMIT} "
                        f"(hard limit {WEEKDAY_DAY_SHIFT_CAPACITY_LIMIT})."
                    ),
                    assignment_date=assignment_date,
                )
            )
        return conflicts

    def _check_manitoba_union_laws(
        self,
        *,
        rules: JurisdictionRules,
        employees: Sequence[EmployeeProfile],
        assignments: Sequence[ScheduledShift],
        shift_templates: Mapping[str, ShiftTemplateInfo],
        period_start: date,
        period_end: date,
        weeks_in_period: int,
        employee_target_hours: Optional[Mapping[str, float]],
    ) -> List[ComplianceConflict]:
        employee_dicts = [
            {"id": employee.id, "full_name": employee.full_name, "fte": employee.fte}
            for employee in employees
        ]
        report = evaluate_schedule(
            rules,
            employees=employee_dicts,
            assignments=assignments,
            shift_templates=dict(shift_templates),
            period_start=period_start,
            period_end=period_end,
            weeks_in_period=weeks_in_period,
            employee_target_hours=employee_target_hours,
        )

        conflicts: List[ComplianceConflict] = []
        for violation in report.violations:
            if violation.severity != "error":
                continue
            conflicts.append(
                ComplianceConflict(
                    category="manitoba_union",
                    code=violation.code,
                    manager_label=_manager_label_for_violation(violation),
                    message=violation.message,
                    employee_id=violation.employee_id,
                    employee_name=violation.employee_name,
                )
            )

        by_employee: Dict[str, List[ScheduledShift]] = {}
        for assignment in assignments:
            by_employee.setdefault(assignment.employee_id, []).append(assignment)

        emp_names = {employee.id: employee.full_name for employee in employees}
        employees_by_id = {employee.id: employee for employee in employees}
        for employee_id, emp_assignments in by_employee.items():
            transitions: List[ShiftTransition] = []
            transition_assignments: List[Optional[ScheduledShift]] = []
            for assignment in emp_assignments:
                template = shift_templates[assignment.shift_template_id]
                start, end = _shift_interval(assignment.assignment_date, template)
                transitions.append(ShiftTransition(code=template.code, start=start, end=end))
                transition_assignments.append(assignment)
            paired = sorted(
                zip(transitions, transition_assignments),
                key=lambda item: item[0].start,
            )
            transitions = [item[0] for item in paired]
            transition_assignments = [item[1] for item in paired]

            for index in range(1, len(transitions)):
                prior = transitions[index - 1]
                current = transitions[index]
                gap = turnaround_gap_hours(prior, current)
                if gap < 0:
                    continue
                current_assignment = transition_assignments[index]
                approved = bool(
                    current_assignment is not None and current_assignment.approved_stretch
                )
                clinical_stretch = bool(
                    current_assignment is not None
                    and current_assignment.clinical_floor_stretch
                )
                stretch_allowed = approved or (
                    clinical_stretch
                    and clinical_floor_stretch_allowed(prior, current)
                )
                if stretch_allowed:
                    continue
                if gap < UNION_MIN_TURNAROUND_HOURS - 1e-9:
                    conflicts.append(
                        ComplianceConflict(
                            category="manitoba_union",
                            code=ScheduleError.UNION_TURNAROUND_15H.value,
                            manager_label="15h turnaround violation",
                            message=(
                                f"{emp_names.get(employee_id, employee_id)}: "
                                f"{gap:.1f}h between {prior.code} ending {prior.end.isoformat()} "
                                f"and {current.code} starting {current.start.isoformat()} "
                                f"(requires {UNION_MIN_TURNAROUND_HOURS:.0f}h)."
                            ),
                            employee_id=employee_id,
                            employee_name=emp_names.get(employee_id, employee_id),
                            assignment_date=current.start.date(),
                        )
                    )
                if not check_11_hour_rest(prior, current):
                    conflicts.append(
                        ComplianceConflict(
                            category="manitoba_union",
                            code=ScheduleError.UNION_MORNING_REST_11H.value,
                            manager_label="11h rest violation",
                            message=(
                                f"{emp_names.get(employee_id, employee_id)}: "
                                f"{gap:.1f}h rest before Morning after Evening/Night "
                                f"(requires 11h)."
                            ),
                            employee_id=employee_id,
                            employee_name=emp_names.get(employee_id, employee_id),
                            assignment_date=current.start.date(),
                        )
                    )

            week_hours: Dict[date, float] = {}
            for assignment in emp_assignments:
                template = shift_templates[assignment.shift_template_id]
                hours = template.duration_minutes / 60.0
                week_start = workweek_for(assignment.assignment_date).start
                week_hours[week_start] = week_hours.get(week_start, 0.0) + hours
            for week_start, hours in week_hours.items():
                if hours <= rules.weekly_overtime_threshold_hours + 1e-9:
                    continue
                conflicts.append(
                    ComplianceConflict(
                        category="manitoba_union",
                        code=ScheduleError.MAX_WEEKLY_HOURS.value,
                        manager_label="Max weekly hours violation",
                        message=(
                            f"{emp_names.get(employee_id, employee_id)}: "
                            f"{hours:.1f}h in work week starting {week_start.isoformat()} "
                            f"(limit {rules.weekly_overtime_threshold_hours:.0f}h)."
                        ),
                        employee_id=employee_id,
                        employee_name=emp_names.get(employee_id, employee_id),
                        assignment_date=week_start,
                    )
                )

        return conflicts

    def _check_portage_consecutive_streaks(
        self,
        *,
        employees: Sequence[EmployeeProfile],
        assignments: Sequence[ScheduledShift],
        rules: JurisdictionRules,
    ) -> Tuple[List[ComplianceConflict], List[ComplianceConflict]]:
        """
        Portage 6-day humane cap: warning at 6+, hard-stop at 7+ unless modified schedule.
        """

        conflicts: List[ComplianceConflict] = []
        warnings: List[ComplianceConflict] = []
        employees_by_id = {employee.id: employee for employee in employees}
        work_dates_by_employee: Dict[str, Set[date]] = {}
        for assignment in assignments:
            work_dates_by_employee.setdefault(assignment.employee_id, set()).add(
                assignment.assignment_date
            )

        for employee_id, work_dates in work_dates_by_employee.items():
            employee = employees_by_id.get(employee_id)
            modified = bool(employee and employee.modified_work_schedule)
            employee_name = employee.full_name if employee else employee_id
            for start, end, length in _consecutive_work_day_streaks(sorted(work_dates)):
                if length >= PORTAGE_WARNING_CONSECUTIVE_DAYS:
                    warnings.append(
                        ComplianceConflict(
                            category="warning",
                            code=CONSECUTIVE_DAYS_WARNING_CODE,
                            manager_label="6-day consecutive work streak (warning)",
                            message=(
                                f"{employee_name}: {length} consecutive work days "
                                f"({start.isoformat()} to {end.isoformat()})."
                                + (
                                    " Modified work schedule on file."
                                    if modified
                                    else " Consider scheduling a rest block."
                                )
                            ),
                            employee_id=employee_id,
                            employee_name=employee_name,
                            assignment_date=end,
                        )
                    )
                if length > PORTAGE_MAX_CONSECUTIVE_WORK_DAYS and not modified:
                    conflicts.append(
                        ComplianceConflict(
                            category="portage_fatigue",
                            code=ScheduleError.PORTAGE_CONSECUTIVE_DAYS.value,
                            manager_label="Consecutive work-day violation",
                            message=(
                                f"{employee_name}: {length} consecutive work days "
                                f"({start.isoformat()} to {end.isoformat()}) exceeds the "
                                f"{PORTAGE_MAX_CONSECUTIVE_WORK_DAYS}-day Portage cap."
                            ),
                            employee_id=employee_id,
                            employee_name=employee_name,
                            assignment_date=end,
                        )
                    )
                elif modified and length > rules.max_consecutive_work_days:
                    conflicts.append(
                        ComplianceConflict(
                            category="portage_fatigue",
                            code=ScheduleError.CONSECUTIVE_DAYS.value,
                            manager_label="Consecutive work-day violation",
                            message=(
                                f"{employee_name}: {length} consecutive work days exceeds the "
                                f"{rules.max_consecutive_work_days}-day statutory limit."
                            ),
                            employee_id=employee_id,
                            employee_name=employee_name,
                            assignment_date=end,
                        )
                    )
        return conflicts, warnings

    def _collect_clinical_contract_line_provisionals(
        self,
        *,
        assignments: Sequence[ScheduledShift],
        shift_templates: Mapping[str, ShiftTemplateInfo],
    ) -> tuple[List[ComplianceConflict], List[ProvisionalAssignment]]:
        """
        Surface clinical-floor contract-line borrows as soft warnings, not hard conflicts.
        """

        warnings: List[ComplianceConflict] = []
        provisionals: List[ProvisionalAssignment] = []
        seen: Set[Tuple[str, date, str]] = set()
        for assignment in assignments:
            if not assignment.contract_line_exception:
                continue
            key = (
                assignment.employee_id,
                assignment.assignment_date,
                assignment.shift_template_id,
            )
            if key in seen:
                continue
            seen.add(key)
            template = shift_templates.get(assignment.shift_template_id)
            shift_code = (
                template.code if template is not None else assignment.shift_template_id
            )
            message = assignment.contract_line_exception_message or (
                "Contract line borrow pending manager approval"
            )
            warnings.append(
                ComplianceConflict(
                    category="clinical_contract_line",
                    code=CONTRACT_LINE_EXCEPTION_VIOLATION_CODE,
                    manager_label="Contract Line Exception",
                    message=message,
                    employee_id=assignment.employee_id,
                    employee_name=assignment.employee_name,
                    assignment_date=assignment.assignment_date,
                )
            )
            provisionals.append(
                build_contract_line_provisional_assignment(
                    employee_id=assignment.employee_id,
                    employee_name=assignment.employee_name,
                    assignment_date=assignment.assignment_date,
                    shift_template_id=assignment.shift_template_id,
                    shift_code=shift_code,
                    violation_message=message,
                )
            )
        return warnings, provisionals

    def _check_contract_fte_targets(
        self,
        *,
        rules: JurisdictionRules,
        employees: Sequence[EmployeeProfile],
        assignments: Sequence[ScheduledShift],
        shift_templates: Mapping[str, ShiftTemplateInfo],
        weeks_in_period: int,
    ) -> List[ComplianceConflict]:
        hours_by_employee: Dict[str, float] = {}
        for assignment in assignments:
            template = shift_templates.get(assignment.shift_template_id)
            if template is None:
                continue
            hours_by_employee[assignment.employee_id] = (
                hours_by_employee.get(assignment.employee_id, 0.0)
                + template.duration_minutes / 60.0
            )

        fulltime_target = fulltime_period_contract_hours(
            rules=rules,
            weeks_in_period=weeks_in_period,
        )
        fte_label = contract_fte_manager_label(
            rules=rules,
            weeks_in_period=weeks_in_period,
        )
        conflicts: List[ComplianceConflict] = []
        for employee in employees:
            if employee.fte < FULLTIME_FTE_THRESHOLD:
                continue
            scheduled = hours_by_employee.get(employee.id, 0.0)
            if not is_fulltime_contract_deficit(
                employee,
                scheduled,
                fulltime_target=fulltime_target,
            ):
                continue
            deficit = fulltime_target - scheduled
            conflicts.append(
                ComplianceConflict(
                    category="contract_fte",
                    code=ScheduleError.CONTRACT_FTE_160.value,
                    manager_label=fte_label,
                    message=(
                        f"{employee.full_name} scheduled {scheduled:.1f}h "
                        f"vs {fulltime_target:.0f}h contract target "
                        f"({deficit:.1f}h short)."
                    ),
                    employee_id=employee.id,
                    employee_name=employee.full_name,
                )
            )
        return conflicts


def build_overtime_compliance_bypass_conflicts(
    assignments: Sequence[object],
    *,
    employees: Sequence[EmployeeProfile],
    shift_templates: Mapping[str, ShiftTemplateInfo],
) -> List[ComplianceConflict]:
    """Surface mandatory overtime/clinical bypass assignments in conflict reports."""

    employees_by_id = {employee.id: employee for employee in employees}
    conflicts: List[ComplianceConflict] = []
    for assignment in assignments:
        bypassed = bool(getattr(assignment, "overtime_compliance_bypassed", False))
        forced = bool(getattr(assignment, "forced_clinical_ot", False))
        if not bypassed and not forced:
            continue
        employee_id = str(getattr(assignment, "employee_id", ""))
        employee = employees_by_id.get(employee_id)
        template_id = str(getattr(assignment, "shift_template_id", ""))
        template = shift_templates.get(template_id)
        shift_code = template.code if template is not None else template_id
        assignment_date = getattr(assignment, "assignment_date", None)
        conflicts.append(
            ComplianceConflict(
                category="overtime_compliance_bypass",
                code=ScheduleError.OVERTIME_REQUIRED_COMPLIANCE_BYPASSED.value,
                manager_label=OVERTIME_COMPLIANCE_BYPASS_LABEL,
                message=(
                    f"{employee.full_name if employee else employee_id} assigned "
                    f"{shift_code} on "
                    f"{assignment_date.isoformat() if assignment_date else 'unknown'} "
                    f"with weekly-hour compliance bypassed to preserve clinical coverage."
                ),
                employee_id=employee_id,
                employee_name=employee.full_name if employee else employee_id,
                assignment_date=assignment_date if isinstance(assignment_date, date) else None,
            )
        )
    return conflicts


def conflict_report_path(project_root: Path, report_date: Optional[date] = None) -> Path:
    stamp = (report_date or date.today()).isoformat()
    return project_root / "exports" / f"{CONFLICT_REPORT_PREFIX}_{stamp}.json"


def write_conflict_report(
    project_root: Path,
    result: ComplianceValidationResult,
    *,
    period_start: date,
    period_end: date,
    week_count: int,
    report_date: Optional[date] = None,
    overtime_compliance_bypassed: Optional[Sequence[ComplianceConflict]] = None,
) -> Path:
    path = conflict_report_path(project_root, report_date=report_date)
    path.parent.mkdir(parents=True, exist_ok=True)
    bypass_conflicts = list(overtime_compliance_bypassed or ())
    bypass_labels = list(
        dict.fromkeys(
            [OVERTIME_COMPLIANCE_BYPASS_LABEL]
            if bypass_conflicts
            else []
        )
    )
    payload = {
        "generated_at_utc": _utc_now_iso(),
        "period_start": period_start.isoformat(),
        "period_end": period_end.isoformat(),
        "week_count": week_count,
        "passed": result.passed,
        "pass_rate_pct": result.pass_rate_pct,
        "conflict_count": result.conflict_count,
        "manager_summary": result.manager_summary + bypass_labels,
        "conflicts": [conflict.to_dict() for conflict in result.conflicts],
        "overtime_compliance_bypassed": [
            conflict.to_dict() for conflict in bypass_conflicts
        ],
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    result.report_path = path
    return path


def read_latest_conflict_report(project_root: Path) -> Optional[dict]:
    exports_dir = project_root / "exports"
    if not exports_dir.is_dir():
        return None
    candidates = sorted(
        exports_dir.glob(f"{CONFLICT_REPORT_PREFIX}_*.json"),
        key=lambda item: item.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        return None
    return json.loads(candidates[0].read_text(encoding="utf-8"))
