"""Core schedule quality gates that block DB persist and breakroom export."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Mapping, Optional, Sequence, Tuple

from lab_scheduler.compliance.engine import ShiftTemplateInfo
from lab_scheduler.compliance.jurisdictions import JurisdictionRules
from lab_scheduler.engine.demand import (
    CLINICAL_FLOOR,
    ExpandedScheduleSlot,
    WEEKEND_CLINICAL_MAX_PER_QUAL,
    clinical_floor_filled_for_day,
    clinical_floor_slots_for_day,
    count_band_shifts_by_day,
)
from lab_scheduler.scheduling.auto_generate import PlannedAssignment
from lab_scheduler.scheduling.contract_payroll import (
    apply_catalog_targets_for_vacant_master_lines,
    build_solver_target_hours_map,
)
from lab_scheduler.engine.demand import infer_qual_code
from lab_scheduler.scheduling.load_balancing import weekend_qual_counts_from_assignments
from lab_scheduler.scheduling.night_streak_corrector import validate_night_streak_sequences
from lab_scheduler.scheduling.profiles import EmployeeProfile
from lab_scheduler.scheduling.schedule_tallies import (
    find_portage_operational_tally_violations,
    format_portage_tally_violation_summary,
)
from lab_scheduler.scheduling.portage_equity_targets import (
    CATALOG_PERSIST_WEEKEND_DEFICIT_FT,
    CATALOG_PERSIST_WEEKEND_SURPLUS_FT,
    CATALOG_PERSIST_WEEKEND_TOLERANCE_PT,
    PORTAGE_DN_FT_NIGHT_SHIFT_TARGET,
    PORTAGE_DN_FT_PERIOD_WORK_SHIFTS,
    build_vacant_line_weekend_target_map,
    portage_is_dn_fulltime_employee,
    portage_is_fulltime_catalog_hours,
)
from lab_scheduler.scheduling.portage_template import (
    parse_vacant_portage_line,
    vacant_master_scheduled_shift_code,
)
from lab_scheduler.engine.demand import find_day_night_transition_violations
from lab_scheduler.scheduling.streak_validator import validate_work_streaks_from_assignments

logger = logging.getLogger(__name__)

_CATALOG_PERSIST_EPSILON = 0.25

# Union-risk band on breakroom exports: contract hours must land within ±8h of target.
FULLTIME_CONTRACT_HOUR_TOLERANCE = 8.0
# Persist gate for vacant master lines: catalog hours must match target (±15 min).
CATALOG_PERSIST_HOUR_TOLERANCE = 0.25

UNION_PERSIST_CODES: frozenset[str] = frozenset(
    {
        "WORK_STREAK",
        "CONSECUTIVE_NIGHT",
        "WEEKEND_STAFFING_CAP",
        "WEEKEND_SHIFT_DRIFT",
        "CONTRACT_HOURS",
        "PORTAGE_EN_TALLY",
        "CONTRACT_TOP_UP",
        "DAY_NIGHT_TRANSITION",
        "DN_CATALOG_QUOTA",
    }
)


@dataclass(frozen=True, slots=True)
class CorePersistViolation:
    code: str
    message: str
    employee_id: str = ""
    employee_name: str = ""
    assignment_date: Optional[date] = None

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "code": self.code,
            "message": self.message,
        }
        if self.employee_id:
            payload["employee_id"] = self.employee_id
        if self.employee_name:
            payload["employee_name"] = self.employee_name
        if self.assignment_date is not None:
            payload["assignment_date"] = self.assignment_date.isoformat()
        return payload


def _is_topup_assignment(
    assignment: PlannedAssignment,
    shift_templates: Mapping[str, ShiftTemplateInfo],
) -> bool:
    template_id = str(assignment.shift_template_id).lower()
    if "topup" in template_id or "top-up" in template_id or "top_up" in template_id:
        return True
    template = shift_templates.get(assignment.shift_template_id)
    if template is None:
        return False
    code = str(template.code or "").strip().upper()
    return code.startswith("TOPUP")


def _scheduled_hours_by_employee(
    assignments: Sequence[PlannedAssignment],
    shift_templates: Mapping[str, ShiftTemplateInfo],
) -> dict[str, float]:
    hours: dict[str, float] = {}
    for assignment in assignments:
        if _is_topup_assignment(assignment, shift_templates):
            continue
        template = shift_templates.get(assignment.shift_template_id)
        if template is None:
            continue
        hours[assignment.employee_id] = (
            hours.get(assignment.employee_id, 0.0) + template.duration_minutes / 60.0
        )
    return hours


def _scheduled_weekend_shifts_by_employee(
    assignments: Sequence[PlannedAssignment],
    shift_templates: Mapping[str, ShiftTemplateInfo],
    *,
    period_start: date,
    period_end: date,
) -> dict[str, int]:
    from lab_scheduler.scheduling.schedule_tallies import shift_band_from_template_code

    counts: dict[str, int] = {}
    for assignment in assignments:
        if assignment.assignment_date < period_start or assignment.assignment_date > period_end:
            continue
        if assignment.assignment_date.weekday() < 5:
            continue
        if _is_topup_assignment(assignment, shift_templates):
            continue
        template = shift_templates.get(assignment.shift_template_id)
        if template is None:
            continue
        band = shift_band_from_template_code(template.code)
        if band not in ("D", "E", "N"):
            continue
        counts[assignment.employee_id] = counts.get(assignment.employee_id, 0) + 1
    return counts


def _assignment_seat_fill_counts(
    assignments: Sequence[PlannedAssignment],
    employees: Sequence[EmployeeProfile],
    qual_codes: Mapping[str, str],
) -> dict[Tuple[date, str, Optional[str]], int]:
    from collections import defaultdict

    emp_by_id = {employee.id: employee for employee in employees}
    counts: dict[Tuple[date, str, Optional[str]], int] = defaultdict(int)
    for assignment in assignments:
        employee = emp_by_id.get(assignment.employee_id)
        if employee is None:
            continue
        qual = infer_qual_code(employee, qual_codes=qual_codes)
        counts[(assignment.assignment_date, assignment.shift_template_id, qual)] += 1
    return counts


def collect_live_clinical_gap_messages(
    *,
    assignments: Sequence[PlannedAssignment],
    employees: Sequence[EmployeeProfile],
    shift_templates: Mapping[str, ShiftTemplateInfo],
    qual_codes: Mapping[str, str],
    period_start: date,
    period_end: date,
    expanded_slots: Optional[Sequence[ExpandedScheduleSlot]] = None,
) -> Tuple[str, ...]:
    """Recompute clinical floor gaps from final assignments (not stale pipeline snapshots)."""

    messages: list[str] = []
    if expanded_slots:
        fill_counts = _assignment_seat_fill_counts(assignments, employees, qual_codes)
        current = period_start
        while current <= period_end:
            for shift_code in ("EVENING", "NIGHT", "MORNING"):
                floor_slots = clinical_floor_slots_for_day(
                    current,
                    shift_code,
                    expanded_slots,
                    shift_templates=shift_templates,
                )
                if not floor_slots:
                    continue
                filled = clinical_floor_filled_for_day(
                    current,
                    shift_code,
                    fill_counts=fill_counts,
                    expanded_slots=expanded_slots,
                    shift_templates=shift_templates,
                )
                required = len(floor_slots)
                if filled < required:
                    messages.append(
                        f"{current.isoformat()} {shift_code}: Clinical floor shortfall: "
                        f"{filled}/{required} {shift_code} seats on {current.isoformat()}"
                    )
            current += timedelta(days=1)
        return tuple(messages)

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
    current = period_start
    while current <= period_end:
        evening_required = CLINICAL_FLOOR.get("EVENING", 2)
        night_required = CLINICAL_FLOOR.get("NIGHT", 2)
        evening_actual = evening_counts.get(current, 0)
        night_actual = night_counts.get(current, 0)
        if evening_actual < evening_required:
            messages.append(
                f"{current.isoformat()} EVENING: pool band shortfall "
                f"{evening_actual}/{evening_required}"
            )
        if night_actual < night_required:
            messages.append(
                f"{current.isoformat()} NIGHT: pool band shortfall "
                f"{night_actual}/{night_required}"
            )
        current += timedelta(days=1)
    return tuple(messages)


def count_subfloor_evening_night_days(
    *,
    assignments: Sequence[PlannedAssignment],
    shift_templates: Mapping[str, ShiftTemplateInfo],
    period_start: date,
    period_end: date,
) -> Tuple[int, int]:
    """Return (days below evening floor, days below night floor)."""

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
    evening_floor = CLINICAL_FLOOR.get("EVENING", 2)
    night_floor = CLINICAL_FLOOR.get("NIGHT", 2)
    below_e = 0
    below_n = 0
    current = period_start
    while current <= period_end:
        if evening_counts.get(current, 0) < evening_floor:
            below_e += 1
        if night_counts.get(current, 0) < night_floor:
            below_n += 1
        current += timedelta(days=1)
    return below_e, below_n


def find_dn_ft_master_catalog_quota_violations(
    *,
    assignments: Sequence[PlannedAssignment],
    employees: Sequence[EmployeeProfile],
    shift_templates: Mapping[str, ShiftTemplateInfo],
    period_start: date,
    period_end: date,
) -> list[CorePersistViolation]:
    """Full-time D/N vacant lines must land exactly 40 shifts with 14 catalog nights."""

    violations: list[CorePersistViolation] = []
    assignment_index: dict[tuple[str, date], str] = {}
    for assignment in assignments:
        if period_start <= assignment.assignment_date <= period_end:
            assignment_index[(assignment.employee_id, assignment.assignment_date)] = (
                assignment.shift_template_id
            )

    for employee in employees:
        if not portage_is_dn_fulltime_employee(employee):
            continue
        if parse_vacant_portage_line(employee.full_name) is None:
            continue

        total_shifts = 0
        night_shifts = 0
        catalog_work_days = 0
        missing_catalog_days: list[date] = []
        off_catalog_days: list[date] = []

        day = period_start
        while day <= period_end:
            expected_code = vacant_master_scheduled_shift_code(
                employee,
                day,
                period_start,
                assignments=assignments,
                shift_templates=shift_templates,
            )
            if expected_code is not None:
                catalog_work_days += 1
                shift_template_id = assignment_index.get((employee.id, day))
                if shift_template_id is None:
                    missing_catalog_days.append(day)
                else:
                    actual_code = shift_templates[shift_template_id].code
                    if actual_code != expected_code:
                        off_catalog_days.append(day)

            shift_template_id = assignment_index.get((employee.id, day))
            if shift_template_id is not None:
                total_shifts += 1
                if shift_templates[shift_template_id].code == "NIGHT":
                    night_shifts += 1
            day += timedelta(days=1)

        if (
            total_shifts != PORTAGE_DN_FT_PERIOD_WORK_SHIFTS
            or night_shifts != PORTAGE_DN_FT_NIGHT_SHIFT_TARGET
            or catalog_work_days != PORTAGE_DN_FT_PERIOD_WORK_SHIFTS
        ):
            violations.append(
                CorePersistViolation(
                    code="DN_CATALOG_QUOTA",
                    message=(
                        f"{employee.full_name} must have "
                        f"{PORTAGE_DN_FT_PERIOD_WORK_SHIFTS} shifts "
                        f"({PORTAGE_DN_FT_NIGHT_SHIFT_TARGET} night); "
                        f"scheduled {total_shifts} total with {night_shifts} night."
                    ),
                    employee_id=employee.id,
                    employee_name=employee.full_name,
                )
            )
            continue

        if missing_catalog_days:
            violations.append(
                CorePersistViolation(
                    code="DN_CATALOG_QUOTA",
                    message=(
                        f"{employee.full_name} missing catalog shift on "
                        f"{missing_catalog_days[0].isoformat()} "
                        f"({len(missing_catalog_days)} catalog day(s) open)."
                    ),
                    employee_id=employee.id,
                    employee_name=employee.full_name,
                    assignment_date=missing_catalog_days[0],
                )
            )
        elif off_catalog_days:
            violations.append(
                CorePersistViolation(
                    code="DN_CATALOG_QUOTA",
                    message=(
                        f"{employee.full_name} off-catalog shift on "
                        f"{off_catalog_days[0].isoformat()} "
                        f"({len(off_catalog_days)} mismatch(es))."
                    ),
                    employee_id=employee.id,
                    employee_name=employee.full_name,
                    assignment_date=off_catalog_days[0],
                )
            )

    return violations


def find_dn_pool_catalog_violations() -> list[CorePersistViolation]:
    """Block persist when the loaded D/N reference pool is not exactly one night per day."""

    from lab_scheduler.scheduling.portage_dn_reference import (
        validate_pool_exactly_one_night_per_day,
    )

    violations: list[CorePersistViolation] = []
    for role in ("MLT", "MLA"):
        try:
            validate_pool_exactly_one_night_per_day(role=role)
        except ValueError as exc:
            violations.append(
                CorePersistViolation(code="DN_POOL_CATALOG", message=str(exc))
            )
    return violations


def find_core_persist_violations(
    *,
    assignments: Sequence[PlannedAssignment],
    employees: Sequence[EmployeeProfile],
    shift_templates: Mapping[str, ShiftTemplateInfo],
    period_start: date,
    period_end: date,
    weeks_in_period: int,
    rules: JurisdictionRules,
    qual_codes: Mapping[str, str],
    template_id_to_band: Mapping[str, str],
    coverage_complete: bool = True,
    coverage_gap_count: int = 0,
    clinical_gap_messages: Sequence[str] = (),
    compliance_first: bool = False,
    expanded_slots: Optional[Sequence[ExpandedScheduleSlot]] = None,
    recompute_clinical_gaps: bool = True,
) -> list[CorePersistViolation]:
    """Return ordered core violations that must block persist/export for Portage schedules."""

    violations: list[CorePersistViolation] = []

    if employees and any(portage_is_dn_fulltime_employee(employee) for employee in employees):
        violations.extend(find_dn_pool_catalog_violations())

    if not compliance_first:
        if not coverage_complete or coverage_gap_count > 0:
            detail = (
                f"{coverage_gap_count} required demand seat(s) remain unfilled."
                if coverage_gap_count
                else "Coverage tier targets are incomplete."
            )
            violations.append(
                CorePersistViolation(code="COVERAGE_GAP", message=detail)
            )

        gap_messages = clinical_gap_messages
        if recompute_clinical_gaps and employees and shift_templates and qual_codes:
            gap_messages = collect_live_clinical_gap_messages(
                assignments=assignments,
                employees=employees,
                shift_templates=shift_templates,
                qual_codes=qual_codes,
                period_start=period_start,
                period_end=period_end,
                expanded_slots=expanded_slots,
            )
        for message in gap_messages:
            violations.append(
                CorePersistViolation(code="CLINICAL_GAP", message=message)
            )

    for streak in validate_work_streaks_from_assignments(
        assignments,
        employees=employees,
        shift_templates=shift_templates,
        period_start=period_start,
        period_end=period_end,
    ):
        violations.append(
            CorePersistViolation(
                code="WORK_STREAK",
                message=streak.message,
                employee_id=streak.employee_id,
                employee_name=streak.employee_name,
                assignment_date=streak.start_date,
            )
        )

    for night in validate_night_streak_sequences(
        assignments,
        employees=employees,
        shift_templates=shift_templates,
        period_start=period_start,
        period_end=period_end,
    ):
        violations.append(
            CorePersistViolation(
                code="CONSECUTIVE_NIGHT",
                message=night.message,
                employee_id=night.employee_id,
                employee_name=night.employee_name,
                assignment_date=night.start_date,
            )
        )


    scan_rows = [
        (assignment.employee_id, assignment.assignment_date, assignment.shift_template_id)
        for assignment in assignments
    ]
    for employee_id, day_d, day_n in find_day_night_transition_violations(scan_rows, shift_templates):
        employee_name = next(
            (employee.full_name for employee in employees if employee.id == employee_id),
            employee_id,
        )
        violations.append(
            CorePersistViolation(
                code="DAY_NIGHT_TRANSITION",
                message=(
                    f"Day shift on {day_d.isoformat()} cannot be followed by a Night shift "
                    f"on {day_n.isoformat()}."
                ),
                employee_id=employee_id,
                employee_name=employee_name,
                assignment_date=day_d,
            )
        )

    if compliance_first:
        return violations

    payroll_targets = build_solver_target_hours_map(
        employees,
        rules=rules,
        weeks_in_period=weeks_in_period,
    )
    catalog_targets = apply_catalog_targets_for_vacant_master_lines(
        employees,
        payroll_targets,
        rules=rules,
        weeks_in_period=weeks_in_period,
        period_start=period_start,
        period_end=period_end,
    )
    hours_by_employee = _scheduled_hours_by_employee(assignments, shift_templates)
    for employee in employees:
        scheduled = hours_by_employee.get(employee.id, 0.0)
        vacant_line = parse_vacant_portage_line(employee.full_name) is not None
        if vacant_line:
            payroll_target = float(payroll_targets.get(employee.id, 0.0))
            if payroll_target <= 0.0:
                continue
            if portage_is_fulltime_catalog_hours(payroll_target):
                hour_delta = scheduled - payroll_target
                if abs(hour_delta) > CATALOG_PERSIST_HOUR_TOLERANCE + _CATALOG_PERSIST_EPSILON:
                    direction = "over" if hour_delta > 0 else "under"
                    violations.append(
                        CorePersistViolation(
                            code="CONTRACT_HOURS",
                            message=(
                                f"{employee.full_name} scheduled {scheduled:.1f}h "
                                f"vs {payroll_target:.0f}h contract target "
                                f"({abs(hour_delta):.1f}h {direction} persist band)."
                            ),
                            employee_id=employee.id,
                            employee_name=employee.full_name,
                        )
                    )
            else:
                target = float(catalog_targets.get(employee.id, 0.0))
                if target <= 0.0:
                    continue
                surplus = scheduled - target
                if abs(surplus) > CATALOG_PERSIST_HOUR_TOLERANCE + _CATALOG_PERSIST_EPSILON:
                    direction = "over" if surplus > 0 else "under"
                    violations.append(
                        CorePersistViolation(
                            code="CONTRACT_HOURS",
                            message=(
                                f"{employee.full_name} scheduled {scheduled:.1f}h "
                                f"vs {target:.0f}h catalog target "
                                f"({abs(surplus):.1f}h {direction} persist band)."
                            ),
                            employee_id=employee.id,
                            employee_name=employee.full_name,
                        )
                    )
            continue
        target = float(payroll_targets.get(employee.id, 0.0))
        if target <= 0.0:
            continue
        surplus = scheduled - target
        if surplus > FULLTIME_CONTRACT_HOUR_TOLERANCE + _CATALOG_PERSIST_EPSILON:
            violations.append(
                CorePersistViolation(
                    code="CONTRACT_HOURS",
                    message=(
                        f"{employee.full_name} scheduled {scheduled:.1f}h "
                        f"vs {target:.0f}h contract target "
                        f"({surplus:.1f}h over union-risk band)."
                    ),
                    employee_id=employee.id,
                    employee_name=employee.full_name,
                )
            )

    weekend_targets = build_vacant_line_weekend_target_map(
        employees,
        catalog_targets,
        qual_codes,
        period_start=period_start,
        period_end=period_end,
    )
    weekend_scheduled = _scheduled_weekend_shifts_by_employee(
        assignments,
        shift_templates,
        period_start=period_start,
        period_end=period_end,
    )
    for employee in employees:
        if parse_vacant_portage_line(employee.full_name) is None:
            continue
        target_weekends = int(weekend_targets.get(employee.id, 0))
        if target_weekends <= 0:
            continue
        scheduled_weekends = int(weekend_scheduled.get(employee.id, 0))
        delta = scheduled_weekends - target_weekends
        catalog_hours = float(catalog_targets.get(employee.id, 0.0))
        if portage_is_fulltime_catalog_hours(catalog_hours):
            if delta > CATALOG_PERSIST_WEEKEND_SURPLUS_FT:
                violations.append(
                    CorePersistViolation(
                        code="WEEKEND_SHIFT_DRIFT",
                        message=(
                            f"{employee.full_name} scheduled {scheduled_weekends} weekend "
                            f"shift(s) vs {target_weekends} pool-scaled catalog target "
                            f"({delta:+d} over persist band)."
                        ),
                        employee_id=employee.id,
                        employee_name=employee.full_name,
                    )
                )
            elif delta < -CATALOG_PERSIST_WEEKEND_DEFICIT_FT:
                violations.append(
                    CorePersistViolation(
                        code="WEEKEND_SHIFT_DRIFT",
                        message=(
                            f"{employee.full_name} scheduled {scheduled_weekends} weekend "
                            f"shift(s) vs {target_weekends} pool-scaled catalog target "
                            f"({-delta} under persist band)."
                        ),
                        employee_id=employee.id,
                        employee_name=employee.full_name,
                    )
                )
        elif abs(delta) > CATALOG_PERSIST_WEEKEND_TOLERANCE_PT:
            direction = "over" if delta > 0 else "under"
            violations.append(
                CorePersistViolation(
                    code="WEEKEND_SHIFT_DRIFT",
                    message=(
                        f"{employee.full_name} scheduled {scheduled_weekends} weekend "
                        f"shift(s) vs {target_weekends} pool-scaled catalog target "
                        f"({abs(delta)} {direction} persist band)."
                    ),
                    employee_id=employee.id,
                    employee_name=employee.full_name,
                )
            )

    current = period_start
    while current <= period_end:
        if current.weekday() >= 5:
            counts = weekend_qual_counts_from_assignments(
                assignments,
                employees=employees,
                qual_codes=qual_codes,
                assignment_date=current,
                shift_templates=shift_templates,
                morning_only=True,
            )
            for qual_code, maximum in WEEKEND_CLINICAL_MAX_PER_QUAL.items():
                count = counts.get(qual_code, 0)
                if count > maximum:
                    violations.append(
                        CorePersistViolation(
                            code="WEEKEND_STAFFING_CAP",
                            message=(
                                f"{current.isoformat()}: {qual_code} weekend cap exceeded "
                                f"(max {maximum}, count {count})."
                            ),
                            assignment_date=current,
                        )
                    )
        current += timedelta(days=1)

    violations.extend(
        find_dn_ft_master_catalog_quota_violations(
            assignments=assignments,
            employees=employees,
            shift_templates=shift_templates,
            period_start=period_start,
            period_end=period_end,
        )
    )

    tally_violations = find_portage_operational_tally_violations(
        assignments,
        period_start=period_start,
        period_end=period_end,
        template_id_to_band=template_id_to_band,
    )
    if compliance_first:
        tally_violations = [item for item in tally_violations if item.actual > item.target]
    if tally_violations:
        summary = format_portage_tally_violation_summary(tally_violations)
        violations.append(
            CorePersistViolation(
                code="PORTAGE_EN_TALLY",
                message=summary,
            )
        )

    for assignment in assignments:
        if not _is_topup_assignment(assignment, shift_templates):
            continue
        employee = next((e for e in employees if e.id == assignment.employee_id), None)
        name = employee.full_name if employee is not None else assignment.employee_id
        violations.append(
            CorePersistViolation(
                code="CONTRACT_TOP_UP",
                message=(
                    f"{name}: FTE top-up token on {assignment.assignment_date.isoformat()} "
                    "is not allowed on Portage master-rotation schedules."
                ),
                employee_id=assignment.employee_id,
                employee_name=name,
                assignment_date=assignment.assignment_date,
            )
        )

    return violations


def log_core_persist_violations(violations: Sequence[CorePersistViolation]) -> None:
    if not violations:
        return
    logger.warning(
        "Persist blocked: %d core schedule violation(s) detected.",
        len(violations),
    )
    for violation in violations[:12]:
        logger.warning("[%s] %s", violation.code, violation.message)
    if len(violations) > 12:
        logger.warning("... and %d more violation(s).", len(violations) - 12)


def format_core_persist_blocked_message(
    violations: Sequence[CorePersistViolation],
    *,
    limit: int = 4,
) -> str:
    if not violations:
        return ""
    by_code: dict[str, list[CorePersistViolation]] = {}
    for violation in violations:
        by_code.setdefault(violation.code, []).append(violation)
    summary_parts = [
        f"{len(items)} {code}"
        for code, items in sorted(by_code.items(), key=lambda item: -len(item[1]))
    ]
    summary = ", ".join(summary_parts)
    samples = "; ".join(v.message for v in violations[:limit])
    clinical_count = len([v for v in violations if v.code == "CLINICAL_GAP"])
    preview_note = (
        " The grid shows a **preview only**; the database was **not** updated."
    )
    subfloor_note = (
        f" ({clinical_count} clinical gap(s) in final schedule.)"
        if clinical_count
        else ""
    )
    return (
        "**Auto-Pilot blocked:** Schedule quality gates failed."
        f"{preview_note} "
        "Your saved schedule was **not** replaced. "
        f"Violations by type: {summary}."
        f"{subfloor_note} "
        f"{samples} "
        "Adjust roster, availability, or templates and re-run Auto-Pilot."
    )
