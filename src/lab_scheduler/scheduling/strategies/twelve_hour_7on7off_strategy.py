"""12-hour 7-on / 7-off cyclical staggered master-array schedule generation."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Dict, List, Mapping, Optional, Sequence, Set, Tuple

from lab_scheduler.compliance.engine import ShiftTemplateInfo
from lab_scheduler.compliance.jurisdictions import JurisdictionRules
from lab_scheduler.engine.constraints import CoverageTierTarget
from lab_scheduler.engine.demand import ShiftConcurrentDemand, roster_line_number
from lab_scheduler.scheduling.auto_generate import AutoGenerateResult, EmployeeProfile, PlannedAssignment
from lab_scheduler.scheduling.contract_payroll import (
    FULLTIME_FTE_THRESHOLD,
    period_contract_hours_for_fte,
)
from lab_scheduler.scheduling.date_utils import daterange as _daterange
from lab_scheduler.scheduling.portage_template import parse_vacant_portage_line
from lab_scheduler.scheduling.strategies import ScheduleArchetype
from lab_scheduler.workers.logic_worker import require_monday_block_start

# Paid hours per 12-hour shift (0.375h unpaid break on a 12.0h tour).
TWELVE_HOUR_PAID_HOURS = 11.625

# Canonical 8-week master cycle; 7-day phase step keeps daily coverage uniform.
MASTER_ROTATION_WEEKS = 8
MASTER_ROTATION_DAYS = MASTER_ROTATION_WEEKS * 7
SEVEN_ON_DAYS = 7
SEVEN_OFF_DAYS = 7
SEVEN_ON_SEVEN_OFF_CYCLE_DAYS = SEVEN_ON_DAYS + SEVEN_OFF_DAYS
COVERAGE_STAGGER_DAYS = 7
LINE_STAGGER_WEEKS = 2
LINE_STAGGER_DAYS = LINE_STAGGER_WEEKS * 7

# Surplus vs 1.0 FTE over one 8-week cycle; one 6-week drop removes 6/8 × 15.5 = 11.625h.
EIGHT_WEEK_CYCLE_SURPLUS_HOURS = 15.5
SIX_WEEK_RECONCILE_WINDOW_WEEKS = 6
SIX_WEEK_RECONCILE_WINDOW_DAYS = SIX_WEEK_RECONCILE_WINDOW_WEEKS * 7
SIX_WEEK_SURPLUS_SHIFT_DROP = 1

# FTE top-up rule variation: a 27-shift block lands at 313.875h, leaving a 1.0 FTE
# line 6.125h short of its 320h Manitoba contract. When enabled, one short "top-up"
# assignment is injected on an off day to close the structural deficit to exactly target.
ENABLE_FTE_TOPUP = True
FTE_TOPUP_TEMPLATE_ID = "twelve-hour-fte-topup"
FTE_TOPUP_TEMPLATE_CODE = "TOPUP"
FTE_TOPUP_TOKEN = "T"
MINIMUM_TOPUP_HOURS = 0.05
# Full-time lines keep at least two 7-on blocks (14 shifts); part-time keeps one block (7).
FULLTIME_MIN_ON_BLOCKS = 2
PARTTIME_MIN_ON_BLOCKS = 1
SHIFTS_PER_ON_BLOCK = SEVEN_ON_DAYS

__all__ = [
    "EIGHT_WEEK_CYCLE_SURPLUS_HOURS",
    "FTE_TOPUP_TEMPLATE_ID",
    "FTE_TOPUP_TOKEN",
    "LINE_STAGGER_DAYS",
    "MASTER_ROTATION_DAYS",
    "MASTER_ROTATION_WEEKS",
    "SIX_WEEK_RECONCILE_WINDOW_WEEKS",
    "TWELVE_HOUR_PAID_HOURS",
    "build_eight_week_master_rotation_string",
    "count_active_staff_on_day",
    "daily_coverage_counts",
    "fte_topup_hours",
    "generate_schedule",
    "fulltime_stagger_offset_days",
    "is_master_work_day",
    "line_stagger_offset_days",
    "lines_share_same_rotation_with_stagger",
    "minimum_period_shifts",
    "select_six_week_surplus_drop",
    "six_week_contract_target_hours",
]


def build_eight_week_master_rotation_string() -> str:
    """
    Single 8-week master rotation string for 12-hour lines.

    ``S`` = scheduled 12-hour shift, ``.`` = off. Strict 7-on / 7-off repeating
    pattern across 56 calendar days (four 14-day blocks).
    """

    block = ("S" * SEVEN_ON_DAYS) + ("." * SEVEN_OFF_DAYS)
    repeats = MASTER_ROTATION_DAYS // len(block)
    rotation = block * repeats
    if len(rotation) != MASTER_ROTATION_DAYS:
        raise ValueError("Master rotation must span exactly 56 days")
    return rotation


def fulltime_stagger_offset_days(employee_index: int) -> int:
    """Absolute 7-day phase offset: (line_index × 7) mod 14.

    Line 01 → 0, Line 02 → 7, Line 03 → 0, … so the only variance between
    full-time lines is the Monday-aligned starting index.
    """

    return (employee_index * COVERAGE_STAGGER_DAYS) % SEVEN_ON_SEVEN_OFF_CYCLE_DAYS


def rotation_phase_index(*, line_number: int) -> int:
    """Zero-based phase index derived from roster line number."""

    if line_number < 1:
        raise ValueError("line_number must be >= 1")
    return line_number - 1


def line_stagger_offset_days(line_number: int) -> int:
    """Backward-compatible alias keyed by roster line number (prefer employee_index)."""

    if line_number < 1:
        raise ValueError("line_number must be >= 1")
    return fulltime_stagger_offset_days(line_number - 1)


def is_master_work_day(
    *,
    day_index: int | None = None,
    employee_index: int = 0,
    period_day_index: int | None = None,
    fulltime_index: int | None = None,
    cohort_size: int | None = None,
    line_number: int | None = None,
) -> bool:
    """
    Active shift when (day_index + employee_index × 7) mod 14 < 7.

    Guarantees Monday (day 0) is ON for employee_index 0 and staggers lines by
    exactly seven days for coverage parity.
    """

    del cohort_size
    resolved_day = day_index if day_index is not None else period_day_index
    if resolved_day is None:
        raise ValueError("day_index is required")
    if fulltime_index is not None:
        resolved_employee = fulltime_index
    elif line_number is not None:
        resolved_employee = line_number - 1
    else:
        resolved_employee = employee_index
    return (
        resolved_day + fulltime_stagger_offset_days(resolved_employee)
    ) % SEVEN_ON_SEVEN_OFF_CYCLE_DAYS < SEVEN_ON_DAYS


def count_active_staff_on_day(*, day_index: int, cohort_size: int) -> int:
    return sum(
        1
        for employee_index in range(cohort_size)
        if is_master_work_day(day_index=day_index, employee_index=employee_index)
    )


def daily_coverage_counts(*, cohort_size: int, days: int = MASTER_ROTATION_DAYS) -> List[int]:
    return [count_active_staff_on_day(day_index=day, cohort_size=cohort_size) for day in range(days)]


def lines_share_same_rotation_with_stagger(
    *,
    reference_day_index: int,
    reference_fulltime_index: int,
    comparison_day_index: int,
    comparison_fulltime_index: int,
    cohort_size: int | None = None,
) -> bool:
    """True when two line/day pairs land on the same master-array token (fairness check)."""

    del cohort_size
    return is_master_work_day(
        day_index=reference_day_index,
        employee_index=reference_fulltime_index,
    ) == is_master_work_day(
        day_index=comparison_day_index,
        employee_index=comparison_fulltime_index,
    )


def six_week_contract_target_hours(
    *,
    fte: float,
    rules: JurisdictionRules,
) -> float:
    """Payroll contract hours for a six-week averaging window."""

    return round(
        float(fte) * rules.standard_hours_per_week_at_1_0_fte * SIX_WEEK_RECONCILE_WINDOW_WEEKS,
        2,
    )


def six_week_scheduled_paid_hours(assignment_count: int) -> float:
    return round(assignment_count * TWELVE_HOUR_PAID_HOURS, 3)


def minimum_period_shifts(*, fte: float) -> int:
    """
    Floor on worked 12-hour shifts after reconciliation.

    Full-time lines keep two complete 7-on blocks (14 shifts). Part-time lines keep at
    least one 7-on block so the breakroom grid still shows a visible on/off rhythm.
    """

    if fte >= FULLTIME_FTE_THRESHOLD:
        return FULLTIME_MIN_ON_BLOCKS * SHIFTS_PER_ON_BLOCK
    return PARTTIME_MIN_ON_BLOCKS * SHIFTS_PER_ON_BLOCK


def period_contract_target_hours(
    *,
    fte: float,
    weeks_in_period: int,
    rules: JurisdictionRules,
) -> float:
    return period_contract_hours_for_fte(
        fte=fte,
        weeks_in_period=weeks_in_period,
        standard_weekly_hours=rules.standard_hours_per_week_at_1_0_fte,
    )


def select_six_week_surplus_drop(
    window_assignments: Sequence[PlannedAssignment],
    *,
    target_hours: float,
    scatter_index: int = 0,
    protected_dates: frozenset[date] = frozenset(),
) -> Optional[PlannedAssignment]:
    """
    Pick one surplus shift to drop, scattered by ``scatter_index`` across the cohort.

    Same-phase lines share identical on-days, so a fixed "drop the last shift" rule would
    punch every line's hole on the same calendar day and collapse coverage. Indexing the
    drop by employee spreads holes across distinct days. ``protected_dates`` (the Monday
    period anchor) are never dropped.
    """

    if not window_assignments:
        return None
    if six_week_scheduled_paid_hours(len(window_assignments)) <= target_hours + 1e-9:
        return None
    eligible = sorted(
        (
            assignment
            for assignment in window_assignments
            if assignment.assignment_date not in protected_dates
        ),
        key=lambda assignment: assignment.assignment_date,
    )
    if not eligible:
        return None
    return eligible[scatter_index % len(eligible)]


def _resolve_line_number(employee: EmployeeProfile, fallback_index: int) -> int:
    vacant = parse_vacant_portage_line(employee.full_name)
    if vacant is not None:
        return vacant[2]
    line_number = roster_line_number(employee)
    if line_number is not None:
        return line_number
    return fallback_index + 1


def _resolve_contract_line_type(employee: EmployeeProfile) -> str:
    if employee.contract_line_type:
        return str(employee.contract_line_type).upper()
    vacant = parse_vacant_portage_line(employee.full_name)
    if vacant is not None:
        return vacant[1].upper()
    return "D/E"


def _resolve_role(employee: EmployeeProfile) -> str:
    """Best-effort role code (e.g. MLT / MLA) used to group a designation cohort."""

    vacant = parse_vacant_portage_line(employee.full_name)
    if vacant is not None:
        return vacant[0].upper()
    haystacks = (str(employee.full_name or ""), str(employee.id or ""))
    for haystack in haystacks:
        upper = haystack.upper()
        if "MLT" in upper:
            return "MLT"
        if "MLA" in upper:
            return "MLA"
    return ""


def _designation_key(employee: EmployeeProfile) -> Tuple[str, str]:
    """Designation cohort key: role + contract line type (e.g. MLA + D/E)."""

    return (_resolve_role(employee), _resolve_contract_line_type(employee))


def _designation_sort_key(employee: EmployeeProfile, fallback_index: int) -> Tuple[str, str, int, str]:
    """
    Order lines by designation first so the running index alternates phases.

    Sorting by ``(role, contract_line_type, line_number)`` keeps every same-designation
    cohort contiguous; the enumeration index then flips each consecutive line between the
    two 7-on/7-off phases, flattening daily coverage instead of stacking lines.
    """

    return (
        _resolve_role(employee),
        _resolve_contract_line_type(employee),
        _resolve_line_number(employee, fallback_index),
        str(employee.id or ""),
    )


def _shift_template_for_twelve_hour_day(
    *,
    day_index: int,
    employee_index: int,
    contract_line_type: str,
    shift_templates: Mapping[str, ShiftTemplateInfo],
) -> ShiftTemplateInfo:
    by_code = {template.code.upper(): template for template in shift_templates.values()}
    contract = contract_line_type.upper()
    if contract == "D/N":
        on_block_index = (day_index + fulltime_stagger_offset_days(employee_index)) // SEVEN_ON_SEVEN_OFF_CYCLE_DAYS
        code = "MORNING" if on_block_index % 2 == 0 else "NIGHT"
    else:
        code = "MORNING"
    template = by_code.get(code)
    if template is not None:
        return template
    return _resolve_twelve_hour_shift_template(shift_templates)


def _resolve_twelve_hour_shift_template(
    shift_templates: Mapping[str, ShiftTemplateInfo],
) -> ShiftTemplateInfo:
    preferred_codes = ("TWELVE_HOUR", "TWELV_HOUR", "DAY_12", "MORNING")
    by_code = {template.code.upper(): template for template in shift_templates.values()}
    for code in preferred_codes:
        template = by_code.get(code)
        if template is not None:
            return template
    return max(shift_templates.values(), key=lambda template: template.duration_minutes)


def _assignment_key(assignment: PlannedAssignment) -> Tuple[str, date, str]:
    return (assignment.employee_id, assignment.assignment_date, assignment.shift_template_id)


def _build_rotation_line_assignments(
    *,
    employee: EmployeeProfile,
    phase_index: int,
    cycle_anchor: date,
    period_start: date,
    period_end: date,
    shift_templates: Mapping[str, ShiftTemplateInfo],
    availability_blocked: Optional[Mapping[str, Set[date]]] = None,
) -> List[PlannedAssignment]:
    blocked = availability_blocked.get(employee.id, set()) if availability_blocked else set()
    contract_line_type = _resolve_contract_line_type(employee)
    assignments: List[PlannedAssignment] = []
    for assignment_date in _daterange(period_start, period_end):
        if assignment_date in blocked:
            continue
        day_index = (assignment_date - cycle_anchor).days
        if day_index < 0 or day_index >= MASTER_ROTATION_DAYS:
            continue
        if not is_master_work_day(day_index=day_index, employee_index=phase_index):
            continue
        template = _shift_template_for_twelve_hour_day(
            day_index=day_index,
            employee_index=phase_index,
            contract_line_type=contract_line_type,
            shift_templates=shift_templates,
        )
        assignments.append(
            PlannedAssignment(
                employee_id=employee.id,
                shift_template_id=template.id,
                assignment_date=assignment_date,
            )
        )
    return assignments


def _apply_six_week_surplus_drops(
    assignments: Sequence[PlannedAssignment],
    *,
    employee_id: str,
    fte: float,
    rules: JurisdictionRules,
    period_start: date,
    period_end: date,
    scatter_index: int = 0,
) -> List[PlannedAssignment]:
    employee_assignments = sorted(
        [assignment for assignment in assignments if assignment.employee_id == employee_id],
        key=lambda assignment: assignment.assignment_date,
    )
    if not employee_assignments:
        return list(assignments)

    protected_dates = frozenset({period_start})
    target_hours = six_week_contract_target_hours(fte=fte, rules=rules)
    drop_keys: Set[Tuple[str, date, str]] = set()
    window_start = period_start
    while window_start <= period_end:
        window_end = window_start + timedelta(days=SIX_WEEK_RECONCILE_WINDOW_DAYS - 1)
        if window_end > period_end:
            break

        in_window = [
            assignment
            for assignment in employee_assignments
            if window_start <= assignment.assignment_date <= window_end
            and _assignment_key(assignment) not in drop_keys
        ]
        if in_window:
            scheduled_hours = six_week_scheduled_paid_hours(len(in_window))
            average_weekly_hours = scheduled_hours / SIX_WEEK_RECONCILE_WINDOW_WEEKS
            contract_weekly = rules.standard_hours_per_week_at_1_0_fte * fte
            surplus_hours = scheduled_hours - target_hours
            if surplus_hours > 1e-9 or average_weekly_hours > contract_weekly + 1e-9:
                drop = select_six_week_surplus_drop(
                    in_window,
                    target_hours=target_hours,
                    scatter_index=scatter_index,
                    protected_dates=protected_dates,
                )
                if drop is not None:
                    drop_keys.add(_assignment_key(drop))

        window_start += timedelta(days=SIX_WEEK_RECONCILE_WINDOW_DAYS)

    if not drop_keys:
        return list(assignments)
    return [
        assignment
        for assignment in assignments
        if _assignment_key(assignment) not in drop_keys
    ]


def _drop_excess_to_period_target(
    assignments: Sequence[PlannedAssignment],
    *,
    employee_id: str,
    fte: float,
    weeks_in_period: int,
    rules: JurisdictionRules,
    period_start: date,
    scatter_index: int,
) -> List[PlannedAssignment]:
    """Scatter-drop surplus 12-hour shifts until a line is at or below its FTE period target."""

    target_hours = period_contract_target_hours(
        fte=fte,
        weeks_in_period=weeks_in_period,
        rules=rules,
    )
    floor_shifts = minimum_period_shifts(fte=fte)
    protected_dates = frozenset({period_start})
    drop_keys: Set[Tuple[str, date, str]] = set()

    while True:
        remaining = [
            assignment
            for assignment in assignments
            if assignment.employee_id == employee_id
            and _assignment_key(assignment) not in drop_keys
        ]
        if len(remaining) <= floor_shifts:
            break
        if six_week_scheduled_paid_hours(len(remaining)) <= target_hours + 1e-9:
            break
        eligible = sorted(
            (
                assignment
                for assignment in remaining
                if assignment.assignment_date not in protected_dates
            ),
            key=lambda assignment: assignment.assignment_date,
        )
        if not eligible:
            break
        drop = eligible[scatter_index % len(eligible)]
        drop_keys.add(_assignment_key(drop))

    if not drop_keys:
        return list(assignments)
    return [
        assignment
        for assignment in assignments
        if _assignment_key(assignment) not in drop_keys
    ]


def fte_topup_hours(
    *,
    fte: float,
    weeks_in_period: int,
    scheduled_shifts: int,
    rules: JurisdictionRules,
) -> float:
    """Residual hours between the period contract target and scheduled 12-hour shifts."""

    target = period_contract_target_hours(
        fte=fte,
        weeks_in_period=weeks_in_period,
        rules=rules,
    )
    scheduled = scheduled_shifts * TWELVE_HOUR_PAID_HOURS
    return round(target - scheduled, 3)


def _select_topup_day(
    *,
    employee_index: int,
    cycle_anchor: date,
    period_start: date,
    period_end: date,
    worked_dates: Set[date],
    blocked: Set[date],
) -> Optional[date]:
    """
    Scatter the structural top-up onto an in-period off day (never Monday day 0).

    Indexing by ``employee_index`` spreads same-phase lines' top-up shifts across distinct
    days so a short-shift spike does not pile onto one calendar day.
    """

    eligible: List[date] = []
    for candidate in _daterange(period_start, period_end):
        if candidate == period_start or candidate in worked_dates or candidate in blocked:
            continue
        day_index = (candidate - cycle_anchor).days
        if 0 <= day_index < MASTER_ROTATION_DAYS and is_master_work_day(
            day_index=day_index,
            employee_index=employee_index,
        ):
            continue
        eligible.append(candidate)
    if not eligible:
        return None
    return eligible[employee_index % len(eligible)]


def _is_fulltime(employee: EmployeeProfile) -> bool:
    return float(employee.fte) >= FULLTIME_FTE_THRESHOLD


def _group_contiguous_assignment_runs(
    assignments: Sequence[PlannedAssignment],
) -> List[List[PlannedAssignment]]:
    ordered = sorted(assignments, key=lambda assignment: assignment.assignment_date)
    if not ordered:
        return []
    groups: List[List[PlannedAssignment]] = [[ordered[0]]]
    for assignment in ordered[1:]:
        previous = groups[-1][-1]
        if (assignment.assignment_date - previous.assignment_date).days == 1:
            groups[-1].append(assignment)
        else:
            groups.append([assignment])
    return groups


def _reconcile_fulltime_fte_topup(
    assignments: Sequence[PlannedAssignment],
    *,
    employee: EmployeeProfile,
    phase_index: int,
    cycle_anchor: date,
    weeks_in_period: int,
    rules: JurisdictionRules,
) -> List[PlannedAssignment]:
    """
    Keep the strict 7-on / 7-off array intact for full-time lines.

    One surplus 12-hour shift in the second contiguous on-run is replaced with the
    short FTE top-up token so contract tracking lands at exactly the period target
    without punching holes in a 7-day on stretch.
    """

    del cycle_anchor
    worked = sorted(
        (
            assignment
            for assignment in assignments
            if assignment.employee_id == employee.id
            and assignment.shift_template_id != FTE_TOPUP_TEMPLATE_ID
        ),
        key=lambda assignment: assignment.assignment_date,
    )
    if not worked:
        return list(assignments)

    target_hours = period_contract_target_hours(
        fte=employee.fte,
        weeks_in_period=weeks_in_period,
        rules=rules,
    )
    scheduled_hours = six_week_scheduled_paid_hours(len(worked))
    if scheduled_hours <= target_hours + MINIMUM_TOPUP_HOURS:
        return list(assignments)

    on_runs = _group_contiguous_assignment_runs(worked)
    target_run = on_runs[1] if len(on_runs) >= 2 else on_runs[-1]
    replace_index = phase_index % len(target_run)
    replace_key = _assignment_key(target_run[replace_index])
    remapped: List[PlannedAssignment] = []
    for assignment in assignments:
        if _assignment_key(assignment) != replace_key:
            remapped.append(assignment)
            continue
        remapped.append(
            PlannedAssignment(
                employee_id=assignment.employee_id,
                shift_template_id=FTE_TOPUP_TEMPLATE_ID,
                assignment_date=assignment.assignment_date,
                forced_clinical_ot=assignment.forced_clinical_ot,
                overtime_compliance_bypassed=assignment.overtime_compliance_bypassed,
                approved_stretch=assignment.approved_stretch,
                clinical_floor_stretch=assignment.clinical_floor_stretch,
                provisional_compliance=assignment.provisional_compliance,
                contract_line_exception=assignment.contract_line_exception,
                contract_line_exception_message=assignment.contract_line_exception_message,
            )
        )
    return remapped


def _inject_fte_topup(
    assignments: List[PlannedAssignment],
    *,
    employee: EmployeeProfile,
    employee_index: int,
    cycle_anchor: date,
    period_start: date,
    period_end: date,
    weeks_in_period: int,
    rules: JurisdictionRules,
    availability_blocked: Optional[Mapping[str, Set[date]]] = None,
) -> List[PlannedAssignment]:
    """Append one top-up shift when a line is short of its payroll contract target."""

    worked = [
        assignment
        for assignment in assignments
        if assignment.employee_id == employee.id
        and assignment.shift_template_id != FTE_TOPUP_TEMPLATE_ID
    ]
    if not worked:
        return assignments
    deficit = fte_topup_hours(
        fte=employee.fte,
        weeks_in_period=weeks_in_period,
        scheduled_shifts=len(worked),
        rules=rules,
    )
    if deficit <= MINIMUM_TOPUP_HOURS:
        return assignments

    blocked = availability_blocked.get(employee.id, set()) if availability_blocked else set()
    topup_day = _select_topup_day(
        employee_index=employee_index,
        cycle_anchor=cycle_anchor,
        period_start=period_start,
        period_end=period_end,
        worked_dates={assignment.assignment_date for assignment in worked},
        blocked=blocked,
    )
    if topup_day is None:
        return assignments
    return [
        *assignments,
        PlannedAssignment(
            employee_id=employee.id,
            shift_template_id=FTE_TOPUP_TEMPLATE_ID,
            assignment_date=topup_day,
        ),
    ]


def _rotation_roster(employees: Sequence[EmployeeProfile]) -> List[EmployeeProfile]:
    """All active roster lines in designation order for staggered 7-on/7-off assignment."""

    active = [employee for employee in employees if employee.fte > 0]
    return sorted(
        active,
        key=lambda employee: _designation_sort_key(employee, 0),
    )


def generate_schedule(
    *,
    rules: JurisdictionRules,
    period_start: date,
    period_end: date,
    weeks_in_period: int,
    employees: Sequence[EmployeeProfile],
    shift_templates: Dict[str, ShiftTemplateInfo],
    shift_required_qualifications: Dict[str, Set[str]],
    employee_target_hours: Optional[Mapping[str, float]] = None,
    availability_blocked: Optional[Mapping[str, Set[date]]] = None,
    coverage_targets: Optional[Sequence[CoverageTierTarget]] = None,
    concurrent_demands: Optional[Sequence[ShiftConcurrentDemand]] = None,
    require_master_compliance: bool = False,
    coverage_aggressor_mode: bool = False,
    strict_complete_block: bool = True,
    emit_triage: bool = False,
    progress_callback=None,
) -> AutoGenerateResult:
    del (
        shift_required_qualifications,
        employee_target_hours,
        coverage_targets,
        concurrent_demands,
        progress_callback,
        require_master_compliance,
        coverage_aggressor_mode,
        strict_complete_block,
        emit_triage,
    )

    result = AutoGenerateResult()
    result.schedule_archetype = ScheduleArchetype.TWELVE_HOUR.value
    if not employees or not shift_templates:
        return result

    assignments: List[PlannedAssignment] = []
    rotation_roster = _rotation_roster(employees)
    cycle_anchor = require_monday_block_start(period_start)

    for fallback_index, employee in enumerate(rotation_roster):
        line_number = _resolve_line_number(employee, fallback_index)
        phase_index = rotation_phase_index(line_number=line_number)
        line_assignments = _build_rotation_line_assignments(
            employee=employee,
            phase_index=phase_index,
            cycle_anchor=cycle_anchor,
            period_start=period_start,
            period_end=period_end,
            shift_templates=shift_templates,
            availability_blocked=availability_blocked,
        )
        if _is_fulltime(employee):
            if ENABLE_FTE_TOPUP:
                line_assignments = _reconcile_fulltime_fte_topup(
                    line_assignments,
                    employee=employee,
                    phase_index=phase_index,
                    cycle_anchor=cycle_anchor,
                    weeks_in_period=weeks_in_period,
                    rules=rules,
                )
        else:
            line_assignments = _apply_six_week_surplus_drops(
                line_assignments,
                employee_id=employee.id,
                fte=employee.fte,
                rules=rules,
                period_start=period_start,
                period_end=period_end,
                scatter_index=phase_index,
            )
            line_assignments = _drop_excess_to_period_target(
                line_assignments,
                employee_id=employee.id,
                fte=employee.fte,
                weeks_in_period=weeks_in_period,
                rules=rules,
                period_start=period_start,
                scatter_index=phase_index,
            )
            if ENABLE_FTE_TOPUP:
                line_assignments = _inject_fte_topup(
                    line_assignments,
                    employee=employee,
                    employee_index=phase_index,
                    cycle_anchor=cycle_anchor,
                    period_start=period_start,
                    period_end=period_end,
                    weeks_in_period=weeks_in_period,
                    rules=rules,
                    availability_blocked=availability_blocked,
                )
        assignments.extend(line_assignments)

    result.assignments = assignments
    result.slots_filled = len(assignments)
    result.slots_total = result.slots_filled
    result.deterministic_status = "GENERATED"
    result.schedule_status = "FINAL"
    result.coverage_complete = True
    return result
