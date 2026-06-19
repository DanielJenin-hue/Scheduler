from __future__ import annotations

import html
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Dict, List, Mapping, Optional, Sequence, Set, Tuple

from lab_scheduler.compliance.compliance_rules import (
    UNION_MIN_TURNAROUND_HOURS,
    ShiftTransition,
    check_11_hour_rest,
    clinical_floor_stretch_allowed,
)
from lab_scheduler.compliance.engine import ShiftTemplateInfo, _hours_between, _shift_interval
from lab_scheduler.compliance.jurisdictions import JurisdictionRules
from lab_scheduler.engine.constraints import validate_contract_line_eligibility
from lab_scheduler.engine.demand import asymmetric_shift_transition_violation
from lab_scheduler.scheduling.auto_generate import PlannedAssignment, _is_qualified, infer_qual_code
from lab_scheduler.scheduling.clinical_seats import assess_clinical_floor_contract_line
from lab_scheduler.scheduling.date_utils import daterange as _daterange
from lab_scheduler.scheduling.profiles import EmployeeProfile
from lab_scheduler.scheduling.portage_template import (
    FULLTIME_FTE_THRESHOLD,
    parse_vacant_portage_line,
    vacant_master_rotation_fte,
    vacant_master_rotation_permits_shift,
)

PORTAGE_NIGHT_STREAK_TARGETS: Tuple[str, ...] = ("portage-mlt-01", "portage-mlt-03")
PORTAGE_NIGHT_STREAK_PEER_ORDER: Dict[str, Tuple[str, ...]] = {
    "portage-mlt-01": ("portage-mlt-02", "portage-mlt-04"),
    "portage-mlt-03": ("portage-mlt-02", "portage-mlt-04", "portage-mlt-01"),
}
PORTAGE_MAX_CONSECUTIVE_NIGHTS = 4
NIGHT_STREAK_CORRECTION_MIN_LENGTH = PORTAGE_MAX_CONSECUTIVE_NIGHTS + 1
MAX_CONSECUTIVE_NIGHTS_BEFORE_CORRECTION = NIGHT_STREAK_CORRECTION_MIN_LENGTH
NIGHT_STREAK_OBJECTIVE_PENALTY = 1000
NIGHT_SHIFT_CODE = "NIGHT"
DAY_SHIFT_CODE = "MORNING"


def catalog_dn_weekday_night_streak_allowed(
    employee: EmployeeProfile,
    streak: NightStreak,
    period_start: date,
) -> bool:
    """Catalog-stamped N cells on full-time D/N master lines are intentional."""

    if parse_vacant_portage_line(employee.full_name) is None:
        return False
    if (employee.contract_line_type or "") != "D/N":
        return False
    rotation_fte = vacant_master_rotation_fte(employee)
    if rotation_fte is None or rotation_fte < FULLTIME_FTE_THRESHOLD:
        return False
    day = streak.start_date
    while day <= streak.end_date:
        if not vacant_master_rotation_permits_shift(
            employee,
            day,
            period_start,
            NIGHT_SHIFT_CODE,
        ):
            return False
        day += timedelta(days=1)
    return True


def catalog_dn_night_streak_allowed(
    employee: EmployeeProfile,
    streak: NightStreak,
    period_start: date,
) -> bool:
    """Alias for catalog weekday + weekend N streak protection."""

    return catalog_dn_weekday_night_streak_allowed(employee, streak, period_start)


@dataclass(frozen=True, slots=True)
class NightStreak:
    employee_id: str
    start_date: date
    end_date: date
    length: int


@dataclass(frozen=True, slots=True)
class NightStreakSwap:
    assignment_date: date
    target_employee_id: str
    peer_employee_id: str
    target_shift_before: str
    peer_shift_before: str
    rationale: str


@dataclass
class NightStreakCorrectionResult:
    swaps_applied: List[NightStreakSwap] = field(default_factory=list)
    remaining_streaks: List[NightStreak] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class NightStreakViolation:
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


def _night_streak_violation_message(
    *,
    employee_name: str,
    streak: NightStreak,
    max_consecutive_nights: int,
) -> str:
    return (
        f"{employee_name}: {streak.length} consecutive night shifts "
        f"({streak.start_date.isoformat()}..{streak.end_date.isoformat()}) "
        f"exceeds the {max_consecutive_nights}-night Portage limit."
    )


def _streaks_from_night_dates(
    *,
    employee_id: str,
    period_start: date,
    period_end: date,
    night_dates: Set[date],
    min_length: int,
) -> List[NightStreak]:
    streaks: List[NightStreak] = []
    run_start: Optional[date] = None
    run_length = 0

    for day in _daterange(period_start, period_end):
        if day in night_dates:
            if run_length == 0:
                run_start = day
            run_length += 1
            continue
        if run_length >= min_length and run_start is not None:
            streaks.append(
                NightStreak(
                    employee_id=employee_id,
                    start_date=run_start,
                    end_date=run_start + timedelta(days=run_length - 1),
                    length=run_length,
                )
            )
        run_start = None
        run_length = 0

    if run_length >= min_length and run_start is not None:
        streaks.append(
            NightStreak(
                employee_id=employee_id,
                start_date=run_start,
                end_date=run_start + timedelta(days=run_length - 1),
                length=run_length,
            )
        )
    return streaks


def _shift_code(
    shift_template_id: str,
    shift_templates: Mapping[str, ShiftTemplateInfo],
) -> str:
    template = shift_templates.get(shift_template_id)
    return str(template.code if template is not None else "")


def _night_dates_for_employee(
    employee_id: str,
    assignments: Sequence[PlannedAssignment],
    shift_templates: Mapping[str, ShiftTemplateInfo],
) -> Set[date]:
    return {
        assignment.assignment_date
        for assignment in assignments
        if assignment.employee_id == employee_id
        and _shift_code(assignment.shift_template_id, shift_templates) == NIGHT_SHIFT_CODE
    }


def find_consecutive_night_streaks(
    *,
    employee_id: str,
    period_start: date,
    period_end: date,
    assignments: Sequence[PlannedAssignment],
    shift_templates: Mapping[str, ShiftTemplateInfo],
    min_length: int = NIGHT_STREAK_CORRECTION_MIN_LENGTH,
) -> List[NightStreak]:
    """Return calendar runs of consecutive NIGHT shifts at or above ``min_length``."""

    night_dates = _night_dates_for_employee(employee_id, assignments, shift_templates)
    return _streaks_from_night_dates(
        employee_id=employee_id,
        period_start=period_start,
        period_end=period_end,
        night_dates=night_dates,
        min_length=min_length,
    )


def _assignment_index(
    assignments: Sequence[PlannedAssignment],
    *,
    employee_id: str,
    assignment_date: date,
) -> Optional[int]:
    for index, assignment in enumerate(assignments):
        if assignment.employee_id == employee_id and assignment.assignment_date == assignment_date:
            return index
    return None


def _assignment_records_for_employee(
    employee_id: str,
    assignments: Sequence[PlannedAssignment],
) -> List[Tuple[date, str]]:
    return sorted(
        (assignment.assignment_date, assignment.shift_template_id)
        for assignment in assignments
        if assignment.employee_id == employee_id
    )


def _turnaround_violation_for_records(
    records: Sequence[Tuple[date, str]],
    shift_templates: Mapping[str, ShiftTemplateInfo],
) -> Optional[str]:
    transitions: List[ShiftTransition] = []
    for work_date, template_id in records:
        template = shift_templates.get(template_id)
        if template is None:
            continue
        start, end = _shift_interval(work_date, template)
        transitions.append(ShiftTransition(code=template.code, start=start, end=end))
    transitions.sort(key=lambda item: item.start)

    for index in range(1, len(transitions)):
        prior = transitions[index - 1]
        current = transitions[index]
        gap = _hours_between(prior.end, current.start)
        stretch_ok = clinical_floor_stretch_allowed(prior, current)
        if index == len(transitions) - 1 and stretch_ok:
            continue
        if gap < 0:
            continue
        if not check_11_hour_rest(prior, current):
            return "would violate 11h rest before Morning after Evening/Night"
        if index == len(transitions) - 1 and gap < UNION_MIN_TURNAROUND_HOURS - 1e-9 and not stretch_ok:
            return (
                f"would violate {UNION_MIN_TURNAROUND_HOURS:.0f}h turnaround "
                f"({gap:.1f}h gap)"
            )
    return None


def _validate_same_day_band_swap_for_employee(
    *,
    employee: EmployeeProfile,
    records: Sequence[Tuple[date, str]],
    assignment_date: date,
    new_template_id: str,
    shift_templates: Mapping[str, ShiftTemplateInfo],
    shift_required_qualifications: Mapping[str, Set[str]],
    rules: JurisdictionRules,
    qual_codes: Mapping[str, str],
) -> Optional[str]:
    template = shift_templates.get(new_template_id)
    if template is None:
        return "Unknown shift type."

    required = shift_required_qualifications.get(new_template_id, set())
    if not _is_qualified(employee, required):
        return "Employee lacks the required qualification for this shift."

    emp_qual = infer_qual_code(employee, qual_codes=qual_codes)
    line_assessment = assess_clinical_floor_contract_line(
        contract_line_type=employee.contract_line_type,
        shift_code=template.code,
        qual_code=emp_qual,
        role_pool_id=None,
    )
    if line_assessment.hard_rejection:
        return line_assessment.violation_message

    contract_violation = validate_contract_line_eligibility(
        employee.contract_line_type or "",
        template.code,
        qual_code=emp_qual,
    )
    if contract_violation:
        return contract_violation

    trial_records = [
        (work_date, template_id)
        for work_date, template_id in records
        if work_date != assignment_date
    ]
    trial_records.append((assignment_date, new_template_id))
    trial_records.sort(key=lambda item: item[0])

    transition = asymmetric_shift_transition_violation(
        trial_records,
        assignment_date,
        template.code,
        shift_templates,
    )
    if transition:
        return transition

    return _turnaround_violation_for_records(trial_records, shift_templates)


def _build_qual_code_lookup(
    employees: Sequence[EmployeeProfile],
    shift_required_qualifications: Mapping[str, Set[str]],
) -> Dict[str, str]:
    from lab_scheduler.engine.demand import build_qual_code_lookup

    return build_qual_code_lookup(employees, shift_required_qualifications)

def _clone_assignment(
    assignment: PlannedAssignment,
    *,
    employee_id: str,
) -> PlannedAssignment:
    return PlannedAssignment(
        employee_id=employee_id,
        shift_template_id=assignment.shift_template_id,
        assignment_date=assignment.assignment_date,
        forced_clinical_ot=assignment.forced_clinical_ot,
        overtime_compliance_bypassed=assignment.overtime_compliance_bypassed,
        approved_stretch=assignment.approved_stretch,
        clinical_floor_stretch=assignment.clinical_floor_stretch,
        provisional_compliance=assignment.provisional_compliance,
        contract_line_exception=assignment.contract_line_exception,
        contract_line_exception_message=assignment.contract_line_exception_message,
    )


def _same_day_peer_swap_is_valid(
    *,
    assignments: Sequence[PlannedAssignment],
    employees: Sequence[EmployeeProfile],
    shift_templates: Mapping[str, ShiftTemplateInfo],
    shift_required_qualifications: Mapping[str, Set[str]],
    rules: JurisdictionRules,
    period_start: date,
    period_end: date,
    weeks_in_period: int,
    target_employee_id: str,
    peer_employee_id: str,
    assignment_date: date,
    employee_target_hours: Optional[Mapping[str, float]] = None,
    availability_blocked: Optional[Mapping[str, Set[date]]] = None,
    max_peer_night_streak: int = PORTAGE_MAX_CONSECUTIVE_NIGHTS,
) -> Optional[str]:
    target_index = _assignment_index(
        assignments,
        employee_id=target_employee_id,
        assignment_date=assignment_date,
    )
    peer_index = _assignment_index(
        assignments,
        employee_id=peer_employee_id,
        assignment_date=assignment_date,
    )
    if target_index is None or peer_index is None:
        return "Both employees must have assignments on the swap date."

    target_assignment = assignments[target_index]
    peer_assignment = assignments[peer_index]
    if _shift_code(target_assignment.shift_template_id, shift_templates) != NIGHT_SHIFT_CODE:
        return "Target employee must hold the night shift on the swap date."
    if _shift_code(peer_assignment.shift_template_id, shift_templates) != DAY_SHIFT_CODE:
        return "Peer employee must hold a day shift on the swap date."

    trial = list(assignments)
    trial[target_index] = _clone_assignment(peer_assignment, employee_id=target_employee_id)
    trial[peer_index] = _clone_assignment(target_assignment, employee_id=peer_employee_id)

    employee_by_id = {employee.id: employee for employee in employees}
    qual_codes = _build_qual_code_lookup(employees, shift_required_qualifications)
    for employee_id in (target_employee_id, peer_employee_id):
        employee = employee_by_id[employee_id]
        proposed = next(
            item
            for item in trial
            if item.employee_id == employee_id and item.assignment_date == assignment_date
        )
        violation = _validate_same_day_band_swap_for_employee(
            employee=employee,
            records=_assignment_records_for_employee(employee_id, assignments),
            assignment_date=assignment_date,
            new_template_id=proposed.shift_template_id,
            shift_templates=shift_templates,
            shift_required_qualifications=shift_required_qualifications,
            rules=rules,
            qual_codes=qual_codes,
        )
        if violation:
            return violation

    if availability_blocked:
        for employee_id in (target_employee_id, peer_employee_id):
            if assignment_date in availability_blocked.get(employee_id, set()):
                return f"{employee_id} has approved time off on the swap date."

    peer_streaks = find_consecutive_night_streaks(
        employee_id=peer_employee_id,
        period_start=period_start,
        period_end=period_end,
        assignments=trial,
        shift_templates=shift_templates,
        min_length=max_peer_night_streak + 1,
    )
    if peer_streaks:
        return (
            f"Swap would create a {peer_streaks[0].length}-night streak on "
            f"{peer_employee_id}."
        )
    return None


def _apply_same_day_swap(
    assignments: List[PlannedAssignment],
    *,
    target_employee_id: str,
    peer_employee_id: str,
    assignment_date: date,
) -> NightStreakSwap:
    target_index = _assignment_index(
        assignments,
        employee_id=target_employee_id,
        assignment_date=assignment_date,
    )
    peer_index = _assignment_index(
        assignments,
        employee_id=peer_employee_id,
        assignment_date=assignment_date,
    )
    if target_index is None or peer_index is None:
        raise ValueError("Cannot apply swap without both assignments present.")

    target_assignment = assignments[target_index]
    peer_assignment = assignments[peer_index]
    assignments[target_index] = _clone_assignment(peer_assignment, employee_id=target_employee_id)
    assignments[peer_index] = _clone_assignment(target_assignment, employee_id=peer_employee_id)
    return NightStreakSwap(
        assignment_date=assignment_date,
        target_employee_id=target_employee_id,
        peer_employee_id=peer_employee_id,
        target_shift_before=NIGHT_SHIFT_CODE,
        peer_shift_before=DAY_SHIFT_CODE,
        rationale=(
            f"Break {target_employee_id} night streak by exchanging "
            f"{NIGHT_SHIFT_CODE}/{DAY_SHIFT_CODE} with {peer_employee_id} on "
            f"{assignment_date.isoformat()}."
        ),
    )


def _peer_order_for_target(
    target_employee_id: str,
    employees: Sequence[EmployeeProfile],
) -> Tuple[str, ...]:
    configured = PORTAGE_NIGHT_STREAK_PEER_ORDER.get(target_employee_id)
    if configured:
        return configured

    dn_peers = [
        employee.id
        for employee in employees
        if employee.id != target_employee_id and (employee.contract_line_type or "") == "D/N"
    ]
    return tuple(dn_peers)


def _break_dates_for_streak(streak: NightStreak) -> List[date]:
    dates = _daterange(streak.start_date, streak.end_date)
    if len(dates) == 1:
        return dates
    # Tail-first: ending a night run with D preserves legal [N, D] transitions.
    return [dates[-1], *dates[1:-1], dates[0]]


def correct_night_streaks(
    assignments: List[PlannedAssignment],
    *,
    employees: Sequence[EmployeeProfile],
    shift_templates: Mapping[str, ShiftTemplateInfo],
    shift_required_qualifications: Mapping[str, Set[str]],
    rules: JurisdictionRules,
    period_start: date,
    period_end: date,
    weeks_in_period: int,
    target_employee_ids: Sequence[str],
    employee_target_hours: Optional[Mapping[str, float]] = None,
    availability_blocked: Optional[Mapping[str, Set[date]]] = None,
    min_streak_length: int = NIGHT_STREAK_CORRECTION_MIN_LENGTH,
    max_passes: int = 24,
) -> NightStreakCorrectionResult:
    """
    Post-processing corrector: break long night chains via same-day D/N peer swaps.

    Swaps preserve per-employee shift counts (8h for 8h) and leave the main CP-SAT
    pass untouched.
    """

    swaps_applied: List[NightStreakSwap] = []
    employee_ids = [employee_id for employee_id in target_employee_ids if employee_id]

    for _ in range(max_passes):
        progress = False
        for target_id in employee_ids:
            streaks = find_consecutive_night_streaks(
                employee_id=target_id,
                period_start=period_start,
                period_end=period_end,
                assignments=assignments,
                shift_templates=shift_templates,
                min_length=min_streak_length,
            )
            if not streaks:
                continue

            for streak in streaks:
                target_employee = next(
                    (profile for profile in employees if profile.id == target_id),
                    None,
                )
                if target_employee is not None and catalog_dn_weekday_night_streak_allowed(
                    target_employee,
                    streak,
                    period_start,
                ):
                    continue
                for break_date in _break_dates_for_streak(streak):
                    for peer_id in _peer_order_for_target(target_id, employees):
                        violation = _same_day_peer_swap_is_valid(
                            assignments=assignments,
                            employees=employees,
                            shift_templates=shift_templates,
                            shift_required_qualifications=shift_required_qualifications,
                            rules=rules,
                            period_start=period_start,
                            period_end=period_end,
                            weeks_in_period=weeks_in_period,
                            target_employee_id=target_id,
                            peer_employee_id=peer_id,
                            assignment_date=break_date,
                            employee_target_hours=employee_target_hours,
                            availability_blocked=availability_blocked,
                        )
                        if violation:
                            continue
                        swaps_applied.append(
                            _apply_same_day_swap(
                                assignments,
                                target_employee_id=target_id,
                                peer_employee_id=peer_id,
                                assignment_date=break_date,
                            )
                        )
                        progress = True
                        break
                    if progress:
                        break
                if progress:
                    break
            if progress:
                break
        if not progress:
            break

    remaining: List[NightStreak] = []
    for target_id in employee_ids:
        remaining.extend(
            find_consecutive_night_streaks(
                employee_id=target_id,
                period_start=period_start,
                period_end=period_end,
                assignments=assignments,
                shift_templates=shift_templates,
                min_length=min_streak_length,
            )
        )
    return NightStreakCorrectionResult(
        swaps_applied=swaps_applied,
        remaining_streaks=remaining,
    )


def correct_portage_night_streaks(
    assignments: List[PlannedAssignment],
    *,
    employees: Sequence[EmployeeProfile],
    shift_templates: Mapping[str, ShiftTemplateInfo],
    shift_required_qualifications: Mapping[str, Set[str]],
    rules: JurisdictionRules,
    period_start: date,
    period_end: date,
    weeks_in_period: int,
    employee_target_hours: Optional[Mapping[str, float]] = None,
    availability_blocked: Optional[Mapping[str, Set[date]]] = None,
) -> NightStreakCorrectionResult:
    """Break >4-night runs on every D/N line that violates the Portage cap."""

    violations = validate_night_streak_sequences(
        assignments,
        employees=employees,
        shift_templates=shift_templates,
        period_start=period_start,
        period_end=period_end,
    )
    target_ids = sorted({violation.employee_id for violation in violations})
    if not target_ids:
        target_ids = [
            employee_id
            for employee_id in PORTAGE_NIGHT_STREAK_TARGETS
            if any(employee.id == employee_id for employee in employees)
        ]
    if not target_ids:
        return NightStreakCorrectionResult()

    return correct_night_streaks(
        assignments,
        employees=employees,
        shift_templates=shift_templates,
        shift_required_qualifications=shift_required_qualifications,
        rules=rules,
        period_start=period_start,
        period_end=period_end,
        weeks_in_period=weeks_in_period,
        target_employee_ids=target_ids,
        employee_target_hours=employee_target_hours,
        availability_blocked=availability_blocked,
        min_streak_length=PORTAGE_MAX_CONSECUTIVE_NIGHTS + 1,
    )


def validate_night_streak_sequences(
    assignments: Sequence[PlannedAssignment],
    *,
    employees: Sequence[EmployeeProfile],
    shift_templates: Mapping[str, ShiftTemplateInfo],
    period_start: date,
    period_end: date,
    max_consecutive_nights: int = PORTAGE_MAX_CONSECUTIVE_NIGHTS,
    employee_ids: Optional[Sequence[str]] = None,
) -> List[NightStreakViolation]:
    """
    Layer-2 validator: flag any employee with more than ``max_consecutive_nights``
    consecutive calendar night shifts.
    """

    violation_min_length = max_consecutive_nights + 1
    allowed_ids = set(employee_ids) if employee_ids is not None else None
    names = {employee.id: employee.full_name for employee in employees}
    violations: List[NightStreakViolation] = []

    for employee in employees:
        if allowed_ids is not None and employee.id not in allowed_ids:
            continue
        streaks = find_consecutive_night_streaks(
            employee_id=employee.id,
            period_start=period_start,
            period_end=period_end,
            assignments=assignments,
            shift_templates=shift_templates,
            min_length=violation_min_length,
        )
        for streak in streaks:
            employee_profile = next(
                (profile for profile in employees if profile.id == streak.employee_id),
                None,
            )
            if employee_profile is not None and catalog_dn_weekday_night_streak_allowed(
                employee_profile,
                streak,
                period_start,
            ):
                continue
            employee_name = names.get(streak.employee_id, streak.employee_id)
            violations.append(
                NightStreakViolation(
                    employee_id=streak.employee_id,
                    employee_name=employee_name,
                    start_date=streak.start_date,
                    end_date=streak.end_date,
                    length=streak.length,
                    message=_night_streak_violation_message(
                        employee_name=employee_name,
                        streak=streak,
                        max_consecutive_nights=max_consecutive_nights,
                    ),
                )
            )
    return violations


def validate_portage_night_streak_sequences(
    assignments: Sequence[PlannedAssignment],
    *,
    employees: Sequence[EmployeeProfile],
    shift_templates: Mapping[str, ShiftTemplateInfo],
    period_start: date,
    period_end: date,
    max_consecutive_nights: int = PORTAGE_MAX_CONSECUTIVE_NIGHTS,
) -> List[NightStreakViolation]:
    """Validate night streaks for the full roster (export gate)."""

    return validate_night_streak_sequences(
        assignments,
        employees=employees,
        shift_templates=shift_templates,
        period_start=period_start,
        period_end=period_end,
        max_consecutive_nights=max_consecutive_nights,
    )


def trim_consecutive_night_overruns(
    assignments: List[PlannedAssignment],
    *,
    employees: Sequence[EmployeeProfile],
    shift_templates: Mapping[str, ShiftTemplateInfo],
    period_start: date,
    period_end: date,
    max_consecutive_nights: int = PORTAGE_MAX_CONSECUTIVE_NIGHTS,
    post_pass_guard: object | None = None,
    anchor_violations: list[str] | None = None,
) -> int:
    """Remove the trailing night of any run longer than the Portage cap (persist last resort)."""

    night_id = next(
        (template_id for template_id, info in shift_templates.items() if info.code == "NIGHT"),
        None,
    )
    if night_id is None:
        return 0

    trimmed = 0
    for _ in range(48):
        streaks = validate_night_streak_sequences(
            assignments,
            employees=employees,
            shift_templates=shift_templates,
            period_start=period_start,
            period_end=period_end,
            max_consecutive_nights=max_consecutive_nights,
        )
        if not streaks:
            break
        violation = streaks[0]
        drop_date = violation.end_date
        drop_index = next(
            (
                index
                for index, assignment in enumerate(assignments)
                if assignment.employee_id == violation.employee_id
                and assignment.assignment_date == drop_date
                and assignment.shift_template_id == night_id
            ),
            None,
        )
        if drop_index is None:
            break
        if post_pass_guard is not None and getattr(
            post_pass_guard, "blocks_anchor_modification", None
        ) is not None:
            if post_pass_guard.blocks_anchor_modification(
                assignments,
                employee_id=violation.employee_id,
                assignment_date=drop_date,
                shift_templates=shift_templates,
            ):
                if anchor_violations is not None:
                    anchor_violations.append(
                        f"blocked night streak trim on anchor "
                        f"{violation.employee_id} {drop_date.isoformat()}"
                    )
                break
        assignments.pop(drop_index)
        trimmed += 1
    return trimmed


def validate_night_streaks_from_schedule_rows(
    schedule_rows: Sequence[Mapping[str, object]],
    *,
    employees: Sequence[Mapping[str, object]],
    dates: Sequence[date],
    max_consecutive_nights: int = PORTAGE_MAX_CONSECUTIVE_NIGHTS,
) -> List[NightStreakViolation]:
    """Validate consecutive ``N`` tokens on breakroom/export grid rows."""

    from lab_scheduler.scheduling.breakroom_print import normalize_breakroom_cell
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
    violations: List[NightStreakViolation] = []
    violation_min_length = max_consecutive_nights + 1

    for row in schedule_rows:
        if is_daily_tally_row(row):
            continue
        employee_id = str(row.get("employee_id", "")).strip()
        if not employee_id:
            continue
        employee_name = str(row.get("Employee", names.get(employee_id, employee_id)))
        night_dates = {
            day
            for day in dates
            if normalize_breakroom_cell(row.get(day.isoformat(), row.get(day, ""))) == "N"
        }
        streaks = _streaks_from_night_dates(
            employee_id=employee_id,
            period_start=period_start,
            period_end=period_end,
            night_dates=night_dates,
            min_length=violation_min_length,
        )
        for streak in streaks:
            violations.append(
                NightStreakViolation(
                    employee_id=streak.employee_id,
                    employee_name=employee_name,
                    start_date=streak.start_date,
                    end_date=streak.end_date,
                    length=streak.length,
                    message=_night_streak_violation_message(
                        employee_name=employee_name,
                        streak=streak,
                        max_consecutive_nights=max_consecutive_nights,
                    ),
                )
            )
    return violations


def format_night_streak_violations_html(
    violations: Sequence[NightStreakViolation],
) -> str:
    if not violations:
        return ""

    items = "".join(
        f"<li><strong>NIGHT_STREAK</strong> — {html.escape(violation.message)}</li>"
        for violation in violations
    )
    return f"""
  <h3>Night Shift Sequence Violations</h3>
  <p class="aggressive-fill-note">Consecutive night runs longer than {PORTAGE_MAX_CONSECUTIVE_NIGHTS} shifts are blocked for breakroom export. Re-run Auto-Pilot; CP-SAT enforces the night cap at generation time.</p>
  <ul class="aggressive-fill-list night-streak-list">{items}</ul>
"""
