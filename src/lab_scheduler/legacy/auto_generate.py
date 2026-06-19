from __future__ import annotations

import logging
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Dict, List, Mapping, Optional, Sequence, Set, Tuple

if TYPE_CHECKING:
    from lab_scheduler.audit.compliance import ComplianceValidationResult

from lab_scheduler.scheduling.assignment_rejection_log import (
    emit_scheduling_trace,
    log_assignment_rejection,
)
from lab_scheduler.scheduling.date_utils import daterange as _daterange
from lab_scheduler.scheduling.profiles import EmployeeProfile
from lab_scheduler.scheduling.provisional_compliance import (
    ProvisionalAssignment,
    build_contract_line_provisional_assignment,
    is_provisional_labor_violation,
    provisional_stretch_system_note,
)

from lab_scheduler.compliance.engine import (
    ScheduledShift,
    ShiftTemplateInfo,
    _consecutive_work_day_streaks,
    _hours_between,
    _shift_interval,
    evaluate_schedule,
)
from lab_scheduler.compliance.compliance_rules import (
    UNION_MIN_TURNAROUND_HOURS,
    ShiftTransition,
    check_11_hour_rest,
    clinical_floor_stretch_allowed,
)
from lab_scheduler.engine.constraints import (
    CoverageTierResult,
    CoverageTierTarget,
    assess_impossible_coverage_slots,
    coverage_priority_key,
    coverage_deficit_rank,
    compute_period_target_hours_map,
    evaluate_coverage_tier_results,
    is_schedule_coverage_complete,
    validate_contract_line_eligibility,
)
from lab_scheduler.engine.demand import (
    WEEKDAY_DAY_SHIFT_CAPACITY_LIMIT,
    ExpandedScheduleSlot,
    ShiftConcurrentDemand,
    PORTAGE_MAX_CONSECUTIVE_WORK_DAYS,
    AutonomousDemandBalancer,
    assess_concurrent_capacity_shortfall,
    asymmetric_shift_transition_violation,
    autonomous_balance_slot_sort_key,
    build_assignment_rank_key,
    build_qual_code_lookup,
    clinical_band_filled_for_day,
    clinical_demand_slot_sort_key,
    clinical_floor_filled_for_day,
    clinical_floor_slots_for_day,
    CLINICAL_FLOOR,
    employee_matches_seat_qual,
    expand_schedule_slots,
    fatigue_guardrail_violation,
    find_day_night_transition_violations,
    find_night_day_transition_violations,
    HARD_NIGHT_SHIFTS_PER_DAY,
    infer_qual_code,
    weekend_paired_day_rank_penalty,
    WEEKEND_CLINICAL_MIN_PER_QUAL,
    WEEKEND_CLINICAL_MAX_PER_QUAL,
    is_clinical_floor_pool,
    is_clinical_floor_satisfied,
    is_demand_satisfied,
    is_evening_night_clinical_floor_satisfied,
    is_night_demand_satisfied,
    is_optional_supplemental_coverage_slot,
    is_smooth_day_balance_pool,
    night_shifts_filled_for_day,
    portage_concurrent_demands,
)
from lab_scheduler.scheduling.portage_template import (
    TOKEN_TO_SHIFT_CODE,
    PortageMasterLineSpec,
    parse_vacant_portage_line,
    portage_master_line_spec,
    shift_token_for_day,
    vacant_master_rotation_fte,
    vacant_master_rotation_permits_shift,
    vacant_master_scheduled_shift_code,
)
from lab_scheduler.scheduling.balanced_load import (
    CapacityShortfallAlert,
    assess_elastic_capacity_shortfall,
    balanced_deficit_hours,
)
from lab_scheduler.scheduling.coverage_aggressor import (
    AggressiveFillFlag,
    collect_aggressive_fill_flags,
    format_aggressive_fill_flags_html,
)
from lab_scheduler.scheduling.clinical_seats import (
    ClinicalContractLineProvisional,
    CriticalClinicalGap,
    assess_clinical_floor_contract_line,
    clinical_seat_label,
    clinical_seat_number,
    collect_critical_clinical_gaps,
    evening_night_clinical_band_satisfied,
    evening_night_clinical_seat_slots,
    evening_night_clinical_seats_satisfied,
    mandatory_clinical_candidates_with_audit,
    non_clinical_fill_blocked_until_clinical_floor,
    select_mandatory_clinical_candidate,
    slot_is_filled,
)
from lab_scheduler.scheduling.schedule_tallies import (
    WEEKDAY_SHIFT_TARGETS,
    WEEKEND_MORNING_TOTAL_CAP,
)
from lab_scheduler.scheduling.load_balancing import (
    WeekdayDailyStaffingPlan,
    compute_weekday_daily_staffing_plan,
    morning_shift_hours,
    prune_weekend_assignments_to_cap,
    trim_weekend_daily_qual_over_cap,
    weekday_morning_staffing_hard_block,
    weekday_morning_staffing_rank_penalty,
    weekday_day_shift_capacity_block,
    weekday_morning_shift_count_from_states,
    weekend_morning_clinical_floor_satisfied,
    weekend_morning_fill_blocked,
    weekend_morning_slot_credited_as_filled,
    weekend_qual_cap_reached,
    weekend_qual_counts_from_states,
)
from lab_scheduler.scheduling.pool_manager import ElasticPoolManager
from lab_scheduler.scheduling.contract_payroll import (
    FULLTIME_FTE_THRESHOLD,
    apply_catalog_targets_for_vacant_master_lines,
    build_elastic_target_hours_map,
    build_solver_target_hours_map,
    fulltime_period_contract_hours,
    is_fulltime_contract_deficit,
    vacant_assignment_ceiling_message,
    would_exceed_vacant_assignment_ceiling,
)
from lab_scheduler.scheduling.post_pass_guard import PostPassGuard, should_bypass_post_cpsat_healing
from lab_scheduler.scheduling.schedule_tallies import shift_band_from_template_code
from lab_scheduler.compliance.jurisdictions import JurisdictionRules
from lab_scheduler.scheduling.models import SlotSuggestion, UnfilledSlot
from lab_scheduler.scheduling.seniority_ranking import (
    SeniorityBypassInfo,
    cba_rank_key,
    evaluate_seniority_bypass,
    rank_profiles_cba,
)
from lab_scheduler.time import workweek_for
from lab_scheduler.workers.logic_worker import (
    GenerationTriageSink,
    TriageEntry,
    handle_unfillable_slot,
    raise_unfillable_slot_failure,
)


@dataclass(frozen=True, slots=True)
class SeniorityBypassEvent:
    assignment_date: date
    shift_template_id: str
    shift_code: str
    selected_employee_id: str
    selected_employee_name: str
    bypass: SeniorityBypassInfo


class ClinicalShortageError(Exception):
    """Raised when immutable Evening/Night clinical floor seats cannot be filled."""

    def __init__(self, assignment_date: date, shift_code: str, reason: str) -> None:
        self.assignment_date = assignment_date
        self.shift_code = shift_code
        self.reason = reason
        super().__init__(str(self))

    def __str__(self) -> str:
        return (
            f"Clinical Shortage on {self.assignment_date.isoformat()}: "
            f"{self.shift_code} floor unfilled — {self.reason}"
        )


class ImmediateClinicalFailure(Exception):
    """Raised when mandatory weekend or clinical coverage cannot be satisfied."""

    def __init__(
        self,
        assignment_date: date,
        *,
        shift_code: str = "WEEKEND",
        reason: str,
    ) -> None:
        self.assignment_date = assignment_date
        self.shift_code = shift_code
        self.reason = reason
        super().__init__(
            f"IMMEDIATE CLINICAL FAILURE on {assignment_date.isoformat()}: {reason}"
        )


@dataclass(frozen=True, slots=True)
class PlannedAssignment:
    employee_id: str
    shift_template_id: str
    assignment_date: date
    forced_clinical_ot: bool = False
    overtime_compliance_bypassed: bool = False
    approved_stretch: bool = False
    clinical_floor_stretch: bool = False
    provisional_compliance: bool = False
    contract_line_exception: bool = False
    contract_line_exception_message: str = ""
    master_template_frozen: bool = False


@dataclass(frozen=True, slots=True)
class ClinicalGapReport:
    """Per-day clinical or demand gap emitted by Deterministic-First mode."""

    assignment_date: date
    shift_code: str
    required_seats: int
    filled_seats: int
    reason: str

    def to_dict(self) -> dict:
        return {
            "assignment_date": self.assignment_date.isoformat(),
            "shift_code": self.shift_code,
            "required_seats": self.required_seats,
            "filled_seats": self.filled_seats,
            "reason": self.reason,
        }


class DeterministicScheduleFailure(Exception):
    """Raised when Mandatory Injection cannot reach 100% compliance in one pass."""

    def __init__(self, result: AutoGenerateResult, message: str) -> None:
        self.result = result
        self.message = message
        super().__init__(message)


@dataclass
class AutoGenerateResult:
    assignments: List[PlannedAssignment] = field(default_factory=list)
    unfilled: List[UnfilledSlot] = field(default_factory=list)
    slots_total: int = 0
    slots_filled: int = 0
    required_slots_total: int = 0
    required_slots_filled: int = 0
    seniority_bypasses: List[SeniorityBypassEvent] = field(default_factory=list)
    coverage_tier_results: List[CoverageTierResult] = field(default_factory=list)
    coverage_complete: bool = True
    deterministic_status: str = ""
    clinical_gap_reports: List[ClinicalGapReport] = field(default_factory=list)
    compliance_validation: Optional["ComplianceValidationResult"] = None
    conflict_report_path: str = ""
    gap_closure_assignments_count: int = 0
    night_streak_swaps_applied: int = 0
    night_streak_violations: List[Dict[str, object]] = field(default_factory=list)
    work_streak_violations: List[Dict[str, object]] = field(default_factory=list)
    shift_equity_metrics: Dict[str, object] = field(default_factory=dict)
    fairness_rerun_count: int = 0
    staff_fairness_report: Dict[str, object] = field(default_factory=dict)
    breakroom_export_path: str = ""
    overtime_compliance_bypass_count: int = 0
    capacity_shortfall: Optional[CapacityShortfallAlert] = None
    elastic_pool_staff_count: int = 0
    elastic_average_load_hours: Dict[str, float] = field(default_factory=dict)
    coverage_aggressor_mode: bool = False
    aggressive_fill_flags: List[AggressiveFillFlag] = field(default_factory=list)
    critical_clinical_gaps: List[CriticalClinicalGap] = field(default_factory=list)
    triage_list: List[TriageEntry] = field(default_factory=list)
    provisional_assignments: List[ProvisionalAssignment] = field(default_factory=list)
    schedule_status: str = "FINAL"
    schedule_archetype: str = "STANDARD"
    phase_timing_ms: Dict[str, float] = field(default_factory=dict)
    core_persist_violations: List[Dict[str, object]] = field(default_factory=list)
    frozen_master_cells: Set[Tuple[str, date]] = field(default_factory=set)
    manager_locked_cells: Set[Tuple[str, date]] = field(default_factory=set)
    post_cpsat_healing_skipped: bool = False
    compliance_first: bool = False
    portage_scheduling_policy_id: str = ""
    anchor_violations: List[str] = field(default_factory=list)
    fairness_weights: Dict[str, float] = field(default_factory=dict)

    @property
    def requires_provisional_approval(self) -> bool:
        return bool(self.provisional_assignments)

    @property
    def fill_rate_pct(self) -> float:
        if self.slots_total == 0:
            return 100.0
        return 100.0 * self.slots_filled / self.slots_total

    @property
    def coverage_gap_count(self) -> int:
        if self.required_slots_total > 0:
            return max(0, self.required_slots_total - self.required_slots_filled)
        if self.slots_total <= 0:
            return 0
        return max(0, self.slots_total - self.slots_filled)

    @property
    def optional_coverage_gap_count(self) -> int:
        return max(0, self.slots_total - self.slots_filled - self.coverage_gap_count)


@dataclass
class _EmployeeState:
    profile: EmployeeProfile
    target_hours: float
    total_hours: float = 0.0
    work_dates: Set[date] = field(default_factory=set)
    week_hours: Dict[date, float] = field(default_factory=dict)
    assignment_records: List[Tuple[date, str]] = field(default_factory=list)
    contract_completion_ot_used: bool = False


def _resolve_master_line_spec(
    employee: EmployeeProfile,
    pool_manager: Optional[ElasticPoolManager],
) -> Optional[PortageMasterLineSpec]:
    if pool_manager is not None:
        return pool_manager.master_line_spec_for(employee.id)
    return portage_master_line_spec(employee)


def _vacant_master_fulltime_line(employee: EmployeeProfile) -> bool:
    """True when a vacant Portage line follows full-time master rotation rules."""

    if parse_vacant_portage_line(employee.full_name) is None:
        return False
    rotation_fte = vacant_master_rotation_fte(employee)
    if rotation_fte is not None:
        return rotation_fte >= FULLTIME_FTE_THRESHOLD
    return employee.fte >= FULLTIME_FTE_THRESHOLD


def _master_catalog_dn_authoritative_stamp(
    profile: EmployeeProfile,
    assignment_date: date,
    period_start: date,
    shift_code: str,
    *,
    master_catalog_stamp: bool,
) -> bool:
    """Full-time vacant catalog cells override generic fatigue and D→N heuristics."""

    if not master_catalog_stamp:
        return False
    if parse_vacant_portage_line(profile.full_name) is None:
        return False
    if not _vacant_master_fulltime_line(profile):
        return False
    return vacant_master_rotation_permits_shift(
        profile,
        assignment_date,
        period_start,
        shift_code,
    )


def _catalog_master_stamp_protected(
    employee: EmployeeProfile,
    assignment: PlannedAssignment,
    period_start: date,
    shift_templates: Mapping[str, ShiftTemplateInfo],
) -> bool:
    """Do not trim shifts that match the line's 8-week master catalog for that day."""

    template = shift_templates.get(assignment.shift_template_id)
    if template is None:
        return False
    return vacant_master_rotation_permits_shift(
        employee,
        assignment.assignment_date,
        period_start,
        template.code,
    )


def _is_dn_fulltime_vacant_line(employee: EmployeeProfile) -> bool:
    if parse_vacant_portage_line(employee.full_name) is None:
        return False
    if (employee.contract_line_type or "").upper() != "D/N":
        return False
    return _vacant_master_fulltime_line(employee)


def _pool_interleave_dn_weekend_catalog_stamps(
    employees: Sequence[EmployeeProfile],
    period_start: date,
    period_end: date,
) -> List[Tuple[EmployeeProfile, date]]:
    """
    Round-robin weekend N pairs across D/N full-time vacant lines before weekday fill.

    Matches manager intent: spread weekend nights evenly, then build weekday blocks.
    """

    dn_ft_employees = sorted(
        [employee for employee in employees if _is_dn_fulltime_vacant_line(employee)],
        key=lambda employee: parse_vacant_portage_line(employee.full_name) or ("", "", 0),
    )
    if not dn_ft_employees:
        return []

    schedule: List[Tuple[EmployeeProfile, date, int]] = []
    for employee in dn_ft_employees:
        parsed = parse_vacant_portage_line(employee.full_name)
        line_number = parsed[2] if parsed is not None else 0
        for saturday in _daterange(period_start, period_end):
            if saturday.weekday() != 5:
                continue
            sunday = saturday + timedelta(days=1)
            if sunday > period_end:
                continue
            pair: List[date] = []
            for day in (saturday, sunday):
                if (
                    vacant_master_scheduled_shift_code(
                        employee,
                        day,
                        period_start,
                    )
                    == "NIGHT"
                ):
                    pair.append(day)
            if not pair:
                continue
            for day in pair:
                schedule.append((employee, day, line_number))

    schedule.sort(key=lambda item: (item[1], item[2], item[0].id))
    return [(employee, day) for employee, day, _line_number in schedule]


def _dn_ft_catalog_stamp_dates_ordered(
    employee: EmployeeProfile,
    weekend_dates: Sequence[date],
    *,
    period_start: date,
    period_end: date,
) -> List[date]:
    """Weekend N pairs first, weekday night strings next, weekday days last."""

    weekday_nights: List[date] = []
    weekday_days: List[date] = []
    for assignment_date in _daterange(period_start, period_end):
        if assignment_date.weekday() >= 5:
            continue
        expected_code = vacant_master_scheduled_shift_code(
            employee,
            assignment_date,
            period_start,
        )
        if expected_code is None:
            continue
        if expected_code == "NIGHT":
            weekday_nights.append(assignment_date)
        else:
            weekday_days.append(assignment_date)
    ordered = list(weekend_dates) + weekday_nights + weekday_days
    return ordered


def _catalog_stamp_dates_for_employee(
    employee: EmployeeProfile,
    *,
    period_start: date,
    period_end: date,
    weekend_dates_by_employee: Optional[Mapping[str, Sequence[date]]] = None,
) -> List[date]:
    if _is_dn_fulltime_vacant_line(employee):
        weekend_dates = (
            list(weekend_dates_by_employee.get(employee.id, ()))
            if weekend_dates_by_employee is not None
            else []
        )
        return _dn_ft_catalog_stamp_dates_ordered(
            employee,
            weekend_dates,
            period_start=period_start,
            period_end=period_end,
        )
    return list(_daterange(period_start, period_end))


def _dn_night_streak_continuation_bonus(
    state: _EmployeeState,
    assignment_date: date,
    shift_templates: Mapping[str, ShiftTemplateInfo],
) -> float:
    """Prefer extending an in-progress catalog night string on D/N lines."""

    if (state.profile.contract_line_type or "").upper() != "D/N":
        return 0.0
    prior_date = assignment_date - timedelta(days=1)
    if prior_date not in state.work_dates:
        return 0.0
    for work_date, shift_template_id in state.assignment_records:
        if work_date != prior_date:
            continue
        template = shift_templates.get(shift_template_id)
        if template is not None and template.code == "NIGHT":
            return -450.0
    return 0.0


def _restore_missing_catalog_master_assignments(
    assignments: List[PlannedAssignment],
    states: Dict[str, _EmployeeState],
    *,
    employees: Sequence[EmployeeProfile],
    shift_templates: Dict[str, ShiftTemplateInfo],
    rules: JurisdictionRules,
    period_start: date,
    period_end: date,
    availability_blocked: Optional[Mapping[str, Set[date]]],
    qual_codes: Mapping[str, str],
    weekends_only: bool = False,
    fulltime_only: bool = True,
    payroll_targets: Mapping[str, float] | None = None,
    catalog_targets: Mapping[str, float] | None = None,
) -> int:
    """
    Re-stamp vacant master lines on days the catalog calls for work but post-passes cleared.

    Only adds missing frozen assignments; never converts an existing shift band.
    """

    dn_ft_employees = [
        employee
        for employee in employees
        if _is_dn_fulltime_vacant_line(employee)
    ]
    weekend_dates_by_employee: Dict[str, List[date]] = {
        employee.id: []
        for employee in dn_ft_employees
    }
    for employee, assignment_date in _pool_interleave_dn_weekend_catalog_stamps(
        employees,
        period_start,
        period_end,
    ):
        weekend_dates_by_employee.setdefault(employee.id, []).append(assignment_date)

    added = 0
    for emp in employees:
        if parse_vacant_portage_line(emp.full_name) is None:
            continue
        if fulltime_only and not _vacant_master_fulltime_line(emp):
            continue
        is_fulltime_vacant = _vacant_master_fulltime_line(emp)
        state = states[emp.id]
        emp_qual = infer_qual_code(emp, qual_codes=qual_codes)
        contract_line = emp.contract_line_type or ""

        stamp_dates = _catalog_stamp_dates_for_employee(
            emp,
            period_start=period_start,
            period_end=period_end,
            weekend_dates_by_employee=weekend_dates_by_employee,
        )
        for assignment_date in stamp_dates:
            if weekends_only and assignment_date.weekday() < 5:
                continue
            if availability_blocked and assignment_date in availability_blocked.get(emp.id, set()):
                continue
            expected_code = vacant_master_scheduled_shift_code(
                emp,
                assignment_date,
                period_start,
                assignments=assignments,
                shift_templates=shift_templates,
            )
            if expected_code is None:
                continue
            if _employee_assignment_on_date(
                assignments,
                employee_id=emp.id,
                assignment_date=assignment_date,
            ) is not None:
                continue

            line_violation = validate_contract_line_eligibility(
                contract_line,
                expected_code,
                qual_code=emp_qual,
            )
            if line_violation:
                continue

            if (
                is_fulltime_vacant
                and expected_code == "MORNING"
                and (contract_line or "").upper() != "D/E"
            ):
                weekend_counts = weekend_qual_counts_from_states(
                    states,
                    employees=employees,
                    qual_codes=qual_codes,
                    assignment_date=assignment_date,
                    shift_templates=shift_templates,
                    morning_only=True,
                )
                if weekend_qual_cap_reached(weekend_counts, emp_qual):
                    continue

            shift_id = _shift_id_for_code(expected_code, shift_templates)
            if shift_id is None:
                continue
            template = shift_templates[shift_id]
            violation = _would_violate_labor_rules(
                state,
                assignment_date,
                template,
                shift_templates,
                rules,
                period_start,
                period_end,
                availability_blocked,
                enforce_fte_target=not is_fulltime_vacant,
                payroll_targets=payroll_targets,
                catalog_targets=catalog_targets,
                master_catalog_stamp=True,
            )
            if violation:
                continue

            shift_hours = template.duration_minutes / 60.0
            week_start = workweek_for(assignment_date).start
            state.work_dates.add(assignment_date)
            state.assignment_records.append((assignment_date, shift_id))
            state.total_hours += shift_hours
            state.week_hours[week_start] = state.week_hours.get(week_start, 0.0) + shift_hours
            assignments.append(
                PlannedAssignment(
                    employee_id=emp.id,
                    shift_template_id=shift_id,
                    assignment_date=assignment_date,
                    master_template_frozen=True,
                )
            )
            added += 1

    return added


def _enforce_dn_fulltime_master_catalog(
    assignments: List[PlannedAssignment],
    states: Dict[str, _EmployeeState],
    *,
    employees: Sequence[EmployeeProfile],
    shift_templates: Dict[str, ShiftTemplateInfo],
    rules: JurisdictionRules,
    period_start: date,
    period_end: date,
    availability_blocked: Optional[Mapping[str, Set[date]]],
    qual_codes: Mapping[str, str],
    post_pass_guard: Optional[PostPassGuard] = None,
    payroll_targets: Mapping[str, float] | None = None,
    catalog_targets: Mapping[str, float] | None = None,
) -> int:
    """
    Restore screenshot-derived D/N master catalog fidelity after post-passes.

    Strips weekend D, off-catalog cells, and wrong-band drift; re-stamps every
    catalog N and weekday D. Never removes catalog night placements.
    """

    removed = 0
    for index in range(len(assignments) - 1, -1, -1):
        assignment = assignments[index]
        employee = next((emp for emp in employees if emp.id == assignment.employee_id), None)
        if employee is None:
            continue
        if (employee.contract_line_type or "") != "D/N":
            continue
        if not _vacant_master_fulltime_line(employee):
            continue
        if parse_vacant_portage_line(employee.full_name) is None:
            continue
        assignment_date = assignment.assignment_date
        if post_pass_guard is not None and post_pass_guard.blocks_worked_cell_modification(
            assignments,
            employee_id=employee.id,
            assignment_date=assignment_date,
            shift_templates=shift_templates,
        ):
            continue
        template = shift_templates.get(assignment.shift_template_id)
        actual_code = template.code if template is not None else None
        if actual_code == "EVENING":
            shift_hours = (template.duration_minutes / 60.0) if template is not None else 0.0
            _remove_assignment_from_state(
                states[employee.id],
                assignment_date,
                assignment.shift_template_id,
                shift_hours,
            )
            assignments.pop(index)
            removed += 1
            continue
        if assignment_date.weekday() >= 5 and actual_code == "MORNING":
            shift_hours = (template.duration_minutes / 60.0) if template is not None else 0.0
            _remove_assignment_from_state(
                states[employee.id],
                assignment_date,
                assignment.shift_template_id,
                shift_hours,
            )
            assignments.pop(index)
            removed += 1
            continue
        expected_code = vacant_master_scheduled_shift_code(
            employee,
            assignment_date,
            period_start,
            assignments=assignments,
            shift_templates=shift_templates,
        )
        if expected_code is None or actual_code != expected_code:
            shift_hours = (template.duration_minutes / 60.0) if template is not None else 0.0
            _remove_assignment_from_state(
                states[employee.id],
                assignment_date,
                assignment.shift_template_id,
                shift_hours,
            )
            assignments.pop(index)
            removed += 1

    restored = _restore_missing_catalog_master_assignments(
        assignments,
        states,
        employees=employees,
        shift_templates=shift_templates,
        rules=rules,
        period_start=period_start,
        period_end=period_end,
        availability_blocked=availability_blocked,
        qual_codes=qual_codes,
        fulltime_only=True,
        payroll_targets=payroll_targets,
        catalog_targets=catalog_targets,
    )
    return removed + restored


def _enforce_de_fulltime_master_catalog(
    assignments: List[PlannedAssignment],
    states: Dict[str, _EmployeeState],
    *,
    employees: Sequence[EmployeeProfile],
    shift_templates: Dict[str, ShiftTemplateInfo],
    rules: JurisdictionRules,
    period_start: date,
    period_end: date,
    availability_blocked: Optional[Mapping[str, Set[date]]],
    qual_codes: Mapping[str, str],
    post_pass_guard: Optional[PostPassGuard] = None,
    payroll_targets: Mapping[str, float] | None = None,
    catalog_targets: Mapping[str, float] | None = None,
) -> int:
    """
    Restore D/E full-time master catalog fidelity after post-passes.

    Each line alternates day blocks then evening blocks (real rotation). Strips
    off-catalog cells and wrong-band drift; re-stamps every catalog D and E.
    """

    removed = 0
    for index in range(len(assignments) - 1, -1, -1):
        assignment = assignments[index]
        employee = next((emp for emp in employees if emp.id == assignment.employee_id), None)
        if employee is None:
            continue
        if (employee.contract_line_type or "") != "D/E":
            continue
        if not _vacant_master_fulltime_line(employee):
            continue
        if parse_vacant_portage_line(employee.full_name) is None:
            continue
        assignment_date = assignment.assignment_date
        if assignment_date < period_start or assignment_date > period_end:
            continue
        if post_pass_guard is not None and post_pass_guard.blocks_worked_cell_modification(
            assignments,
            employee_id=employee.id,
            assignment_date=assignment_date,
            shift_templates=shift_templates,
        ):
            continue
        template = shift_templates.get(assignment.shift_template_id)
        actual_code = template.code if template is not None else None
        expected_code = vacant_master_scheduled_shift_code(
            employee,
            assignment_date,
            period_start,
            assignments=assignments,
            shift_templates=shift_templates,
        )
        if expected_code is None or actual_code != expected_code:
            shift_hours = (template.duration_minutes / 60.0) if template is not None else 0.0
            _remove_assignment_from_state(
                states[employee.id],
                assignment_date,
                assignment.shift_template_id,
                shift_hours,
            )
            assignments.pop(index)
            removed += 1

    restored = _restore_missing_catalog_master_assignments(
        assignments,
        states,
        employees=employees,
        shift_templates=shift_templates,
        rules=rules,
        period_start=period_start,
        period_end=period_end,
        availability_blocked=availability_blocked,
        qual_codes=qual_codes,
        fulltime_only=True,
        payroll_targets=payroll_targets,
        catalog_targets=catalog_targets,
    )
    return removed + restored


def _elastic_load_deficit(
    employee: EmployeeProfile,
    state: _EmployeeState,
    *,
    load_reference_hours: Optional[Mapping[str, float]],
    fulltime_target: float,
) -> float:
    use_elastic = load_reference_hours is not None
    return balanced_deficit_hours(
        employee_id=employee.id,
        total_hours=state.total_hours,
        load_reference_hours=load_reference_hours or {},
        fulltime_target=fulltime_target,
        use_elastic=use_elastic,
    )


def _is_qualified(profile: EmployeeProfile, required_qualification_ids: Set[str]) -> bool:
    if not required_qualification_ids:
        return True
    return bool(profile.qualification_ids & required_qualification_ids)


def _clinical_floor_stretch_for_assignment(
    state: _EmployeeState,
    assignment_date: date,
    template: ShiftTemplateInfo,
    shift_templates: Dict[str, ShiftTemplateInfo],
) -> bool:
    """True when the proposed shift closes a sub-15h gap within the 24h clinical stretch ceiling."""

    transitions: List[ShiftTransition] = []
    for day, shift_template_id in state.assignment_records:
        prior_template = shift_templates[shift_template_id]
        start, end = _shift_interval(day, prior_template)
        transitions.append(ShiftTransition(code=prior_template.code, start=start, end=end))
    start, end = _shift_interval(assignment_date, template)
    transitions.append(ShiftTransition(code=template.code, start=start, end=end))
    transitions.sort(key=lambda item: item.start)
    if len(transitions) < 2:
        return False
    prior = transitions[-2]
    current = transitions[-1]
    return clinical_floor_stretch_allowed(prior, current)


def _annotate_clinical_floor_stretches(
    assignments: Sequence[PlannedAssignment],
    shift_templates: Mapping[str, ShiftTemplateInfo],
) -> List[PlannedAssignment]:
    """
    Tag Morning (or closing) shifts that complete a Joanne-style E/N→next-band stretch.

    Lockdown only assigns Evening/Night seats; template and force-fill paths add the
    closing shift without ``clinical_floor_stretch``. This pass retroactively marks
    qualifying assignments before compliance audit.
    """

    if not assignments:
        return []

    stretch_keys: Set[Tuple[str, date, str]] = set()
    by_employee: Dict[str, List[PlannedAssignment]] = {}
    for assignment in assignments:
        by_employee.setdefault(assignment.employee_id, []).append(assignment)

    for emp_assignments in by_employee.values():
        transitions: List[Tuple[ShiftTransition, PlannedAssignment]] = []
        for assignment in emp_assignments:
            template = shift_templates.get(assignment.shift_template_id)
            if template is None:
                continue
            start, end = _shift_interval(assignment.assignment_date, template)
            transitions.append(
                (
                    ShiftTransition(code=template.code, start=start, end=end),
                    assignment,
                )
            )
        transitions.sort(key=lambda item: item[0].start)
        for index in range(1, len(transitions)):
            prior, current_assignment = transitions[index - 1][0], transitions[index][1]
            current = transitions[index][0]
            if clinical_floor_stretch_allowed(prior, current):
                stretch_keys.add(
                    (
                        current_assignment.employee_id,
                        current_assignment.assignment_date,
                        current_assignment.shift_template_id,
                    )
                )

    annotated: List[PlannedAssignment] = []
    for assignment in assignments:
        key = (assignment.employee_id, assignment.assignment_date, assignment.shift_template_id)
        if assignment.clinical_floor_stretch or key not in stretch_keys:
            annotated.append(assignment)
            continue
        annotated.append(
            PlannedAssignment(
                employee_id=assignment.employee_id,
                shift_template_id=assignment.shift_template_id,
                assignment_date=assignment.assignment_date,
                forced_clinical_ot=assignment.forced_clinical_ot,
                overtime_compliance_bypassed=assignment.overtime_compliance_bypassed,
                approved_stretch=assignment.approved_stretch,
                clinical_floor_stretch=True,
            )
        )
    return annotated


def _would_violate_labor_rules(
    state: _EmployeeState,
    assignment_date: date,
    template: ShiftTemplateInfo,
    shift_templates: Dict[str, ShiftTemplateInfo],
    rules: JurisdictionRules,
    period_start: date,
    period_end: date,
    availability_blocked: Optional[Mapping[str, Set[date]]] = None,
    *,
    enforce_fte_target: bool = True,
    relax_dn_contract_completion: bool = False,
    forced_clinical_ot: bool = False,
    mandatory_assignment: bool = False,
    approved_stretch: bool = False,
    clinical_floor_stretch: bool = False,
    allow_provisional: bool = False,
    log_rejection: bool = False,
    peer_equity_swap: bool = False,
    payroll_targets: Optional[Mapping[str, float]] = None,
    catalog_targets: Optional[Mapping[str, float]] = None,
    master_catalog_stamp: bool = False,
) -> Optional[str]:
    shift_hours = template.duration_minutes / 60.0

    def _reject(reason: str) -> str:
        if log_rejection:
            log_assignment_rejection(state.profile.id, assignment_date, reason)
        return reason

    def _block(reason: str) -> Optional[str]:
        if allow_provisional and is_provisional_labor_violation(reason):
            return None
        return _reject(reason)

    if availability_blocked and assignment_date in availability_blocked.get(state.profile.id, set()):
        return _reject("employee has approved time off on this date")

    if assignment_date in state.work_dates:
        return _reject("employee already scheduled that day")

    skip_payroll_caps = forced_clinical_ot or mandatory_assignment

    if not skip_payroll_caps and payroll_targets is not None and would_exceed_vacant_assignment_ceiling(
        state.total_hours,
        shift_hours,
        state.profile,
        payroll_targets,
        catalog_targets,
    ):
        return _reject(
            vacant_assignment_ceiling_message(
                state.profile,
                payroll_targets,
                catalog_targets,
            )
        )

    if template.code == "NIGHT":
        from lab_scheduler.scheduling.night_streak_corrector import (
            PORTAGE_MAX_CONSECUTIVE_NIGHTS,
            find_consecutive_night_streaks,
        )

        skip_night_streak_cap = master_catalog_stamp and vacant_master_rotation_permits_shift(
            state.profile,
            assignment_date,
            period_start,
            "NIGHT",
        )
        if not skip_night_streak_cap:
            night_id = next(
                (template_id for template_id, info in shift_templates.items() if info.code == "NIGHT"),
                None,
            )
            if night_id is not None:
                simulated = [
                    PlannedAssignment(state.profile.id, template_id, work_date)
                    for work_date, template_id in state.assignment_records
                ] + [
                    PlannedAssignment(
                        state.profile.id,
                        night_id,
                        assignment_date,
                    )
                ]
                streaks = find_consecutive_night_streaks(
                    employee_id=state.profile.id,
                    period_start=period_start,
                    period_end=period_end,
                    assignments=simulated,
                    shift_templates=shift_templates,
                    min_length=PORTAGE_MAX_CONSECUTIVE_NIGHTS + 1,
                )
                if streaks:
                    return _reject(
                        f"would exceed {PORTAGE_MAX_CONSECUTIVE_NIGHTS} consecutive night shifts"
                    )

    catalog_authoritative = _master_catalog_dn_authoritative_stamp(
        state.profile,
        assignment_date,
        period_start,
        template.code,
        master_catalog_stamp=master_catalog_stamp,
    )
    transition = asymmetric_shift_transition_violation(
        state.assignment_records,
        assignment_date,
        template.code,
        shift_templates,
    )
    if transition and not catalog_authoritative:
        return _reject(transition)

    simulated_dates = sorted(state.work_dates | {assignment_date})
    if not catalog_authoritative:
        streak_limit = (
            rules.max_consecutive_work_days
            if state.profile.modified_work_schedule
            else PORTAGE_MAX_CONSECUTIVE_WORK_DAYS
        )
        for _start, _end, length in _consecutive_work_day_streaks(simulated_dates):
            if length > streak_limit:
                blocked = _block(
                    f"would exceed {streak_limit} consecutive work days "
                    "(fatigue guardrail)"
                )
                if blocked:
                    return blocked
            if not state.profile.modified_work_schedule and length > rules.max_consecutive_work_days:
                blocked = _block(
                    f"would exceed {rules.max_consecutive_work_days} consecutive work days"
                )
                if blocked:
                    return blocked

        work_set = simulated_dates
        for ws, we in _iter_week_bounds(period_start, period_end):
            days_in_week = [ws + timedelta(days=i) for i in range((we - ws).days + 1)]
            worked = sum(1 for d in days_in_week if d in work_set)
            if worked > rules.max_work_days_per_work_week:
                return _reject(
                    f"would leave insufficient weekly rest in week starting {ws.isoformat()}"
                )

    remaining_contract = state.target_hours - state.total_hours
    dn_contract_completion = (
        relax_dn_contract_completion
        and (state.profile.contract_line_type or "") == "D/N"
        and remaining_contract > 0.25
    )

    fatigue = fatigue_guardrail_violation(
        state.work_dates,
        assignment_date,
        modified_work_schedule=state.profile.modified_work_schedule,
        max_consecutive_work_days=rules.max_consecutive_work_days,
    )
    if (
        fatigue
        and not catalog_authoritative
        and not (dn_contract_completion and remaining_contract <= 8.01)
    ):
        blocked = _block(fatigue)
        if blocked:
            return blocked

    week_start = workweek_for(assignment_date).start
    week_total = state.week_hours.get(week_start, 0.0) + shift_hours
    max_week_hours = rules.weekly_overtime_threshold_hours
    if (
        enforce_fte_target
        and not state.contract_completion_ot_used
        and (
            remaining_contract <= shift_hours + 0.25
            or (dn_contract_completion and remaining_contract <= 16.0 + 0.25)
        )
        and week_total <= rules.weekly_overtime_threshold_hours + shift_hours + 1e-9
    ):
        max_week_hours = rules.weekly_overtime_threshold_hours + shift_hours
    elif dn_contract_completion and week_total <= 48.0 + 1e-9:
        max_week_hours = 48.0
    if week_total > max_week_hours + 1e-9:
        catalog_weekly_cap = 56.0 if catalog_authoritative else 48.0
        if catalog_authoritative and week_total <= catalog_weekly_cap + 1e-9:
            pass
        else:
            return _reject(
                f"would exceed {rules.weekly_overtime_threshold_hours:.0f}h/week statutory limit "
                f"({week_total:.1f}h)"
            )

    if rules.daily_overtime_threshold_hours is not None and shift_hours > rules.daily_overtime_threshold_hours + 1e-9:
        return _reject(
            f"shift is {shift_hours:.1f}h (exceeds {rules.daily_overtime_threshold_hours:.0f}h daily standard)"
        )

    if (
        enforce_fte_target
        and not skip_payroll_caps
        and state.total_hours + shift_hours > state.target_hours + 1e-9
    ):
        return _reject(f"would exceed FTE target ({state.target_hours:.1f}h)")

    intervals = []
    transitions: List[ShiftTransition] = []
    for d, tid in state.assignment_records:
        tmpl = shift_templates[tid]
        start, end = _shift_interval(d, tmpl)
        intervals.append((start, end))
        transitions.append(ShiftTransition(code=tmpl.code, start=start, end=end))
    start, end = _shift_interval(assignment_date, template)
    intervals.append((start, end))
    transitions.append(ShiftTransition(code=template.code, start=start, end=end))
    intervals.sort(key=lambda x: x[0])
    transitions.sort(key=lambda shift: shift.start)

    if not catalog_authoritative:
        for index in range(1, len(transitions)):
            prior = transitions[index - 1]
            current = transitions[index]
            gap = _hours_between(prior.end, current.start)
            stretch_ok = approved_stretch or (
                clinical_floor_stretch and clinical_floor_stretch_allowed(prior, current)
            )
            if index == len(transitions) - 1 and stretch_ok:
                continue
            if not check_11_hour_rest(prior, current):
                if dn_contract_completion:
                    continue
                blocked = _block(
                    "would violate 11h rest before Morning after Evening/Night "
                    f"({gap:.1f}h gap)"
                )
                if blocked:
                    return blocked
            if (
                index == len(transitions) - 1
                and gap < UNION_MIN_TURNAROUND_HOURS - 1e-9
                and not stretch_ok
            ):
                blocked = _block(
                    f"would violate 15h turnaround ({gap:.1f}h gap; "
                    f"requires {UNION_MIN_TURNAROUND_HOURS:.0f}h)"
                )
                if blocked:
                    return blocked

        min_rest = rules.min_daily_rest_hours or rules.min_rest_between_shifts_hours
        if min_rest is not None:
            for i in range(1, len(intervals)):
                gap = _hours_between(intervals[i - 1][1], intervals[i][0])
                if gap < 0:
                    return _reject("would overlap another shift")
                if gap < min_rest - 1e-9:
                    if dn_contract_completion and remaining_contract <= 8.01:
                        continue
                    label = (
                        f"{rules.min_daily_rest_hours:.0f}h daily rest"
                        if rules.min_daily_rest_hours
                        else f"{rules.min_rest_between_shifts_hours:.0f}h between shifts"
                    )
                    return _reject(f"would violate {label} ({gap:.1f}h gap)")

    return None


def _iter_week_bounds(period_start: date, period_end: date):
    cur = workweek_for(period_start).start
    while cur <= period_end:
        yield cur, min(cur + timedelta(days=6), period_end)
        cur += timedelta(days=7)



def _rank_slot_candidates(
    *,
    employees: Sequence[EmployeeProfile],
    required: Set[str],
    states: Mapping[str, _EmployeeState],
    assignment_date: date,
    shift_id: str,
    template: ShiftTemplateInfo,
    shift_templates: Dict[str, ShiftTemplateInfo],
    rules: JurisdictionRules,
    period_start: date,
    period_end: date,
    availability_blocked: Optional[Mapping[str, Set[date]]],
    prioritize_coverage: bool = False,
    period_target_hours: Optional[Mapping[str, float]] = None,
    required_qual_code: Optional[str] = None,
    qual_codes: Optional[Mapping[str, str]] = None,
    employee_target_hours: Optional[Mapping[str, float]] = None,
    role_pool_id: Optional[str] = None,
    fill_counts: Optional[Mapping[Tuple[date, str, Optional[str]], int]] = None,
    expanded_slots: Optional[Sequence[ExpandedScheduleSlot]] = None,
    forced_clinical_ot: bool = False,
    weekday_staffing_plan: Optional[WeekdayDailyStaffingPlan] = None,
    enforce_weekday_load_balance: bool = True,
    fairness_weights: Optional["FairnessWeights"] = None,
    assignments: Optional[Sequence[PlannedAssignment]] = None,
) -> Tuple[list[EmployeeProfile], Dict[str, str], Set[str]]:
    qualified = [
        emp
        for emp in employees
        if _is_qualified(emp, required)
        and employee_matches_seat_qual(
            emp,
            required_qual_code,
            qual_codes=qual_codes,
            shift_required_qualification_ids=required or None,
        )
    ]
    eligible: list[EmployeeProfile] = []
    ineligible_reasons: Dict[str, str] = {}

    shift_hours = template.duration_minutes / 60.0
    target_hours_map = employee_target_hours or {
        emp_id: state.target_hours for emp_id, state in states.items()
    }
    total_hours_map = {emp_id: state.total_hours for emp_id, state in states.items()}
    band_filled = (
        clinical_band_filled_for_day(
            assignment_date,
            template.code,
            fill_counts=fill_counts or {},
            shift_templates=shift_templates,
            expanded_slots=expanded_slots,
        )
        if fill_counts is not None
        else None
    )
    allow_provisional = bool(
        role_pool_id is not None
        and is_clinical_floor_pool(role_pool_id)
        and template.code in {"EVENING", "NIGHT"}
    )
    weekend_counts: Optional[Dict[str, int]] = None
    if assignment_date.weekday() >= 5 and qual_codes is not None and template.code == "MORNING":
        weekend_counts = weekend_qual_counts_from_states(
            states,
            employees=employees,
            qual_codes=qual_codes,
            assignment_date=assignment_date,
            shift_templates=shift_templates,
            morning_only=True,
        )
    weekday_morning_count = weekday_morning_shift_count_from_states(
        states,
        assignment_date,
        shift_templates=shift_templates,
    )

    for emp in qualified:
        emp_qual = infer_qual_code(emp, qual_codes=qual_codes)
        if (
            weekday_day_shift_capacity_block(
                assignment_date,
                weekday_morning_count,
                shift_code=template.code,
            )
        ):
            log_assignment_rejection(
                emp.id,
                assignment_date,
                (
                    f"weekday day-shift capacity reached "
                    f"(max {WEEKDAY_DAY_SHIFT_CAPACITY_LIMIT})"
                ),
            )
            ineligible_reasons[emp.id] = (
                f"weekday day-shift capacity reached "
                f"(max {WEEKDAY_DAY_SHIFT_CAPACITY_LIMIT})"
            )
            continue
        if weekend_counts is not None and weekend_qual_cap_reached(weekend_counts, emp_qual):
            log_assignment_rejection(
                emp.id,
                assignment_date,
                f"weekend {emp_qual} cap reached (max {WEEKEND_CLINICAL_MAX_PER_QUAL.get(emp_qual, 1)})",
            )
            ineligible_reasons[emp.id] = (
                f"weekend {emp_qual} cap reached (max {WEEKEND_CLINICAL_MAX_PER_QUAL.get(emp_qual, 1)})"
            )
            continue
        if (
            enforce_weekday_load_balance
            and weekday_staffing_plan is not None
            and template.code == "MORNING"
            and weekday_morning_staffing_hard_block(
                assignment_date,
                weekday_morning_count,
                weekday_staffing_plan,
            )
        ):
            log_assignment_rejection(
                emp.id,
                assignment_date,
                "weekday day-shift target reached (13 seats)",
            )
            ineligible_reasons[emp.id] = "weekday day-shift target reached (13 seats)"
            continue
        line_assessment = assess_clinical_floor_contract_line(
            contract_line_type=emp.contract_line_type,
            shift_code=template.code,
            qual_code=emp_qual,
            role_pool_id=role_pool_id,
        )
        if line_assessment.hard_rejection:
            log_assignment_rejection(
                emp.id,
                assignment_date,
                line_assessment.violation_message or "contract line conflict",
            )
            ineligible_reasons[emp.id] = line_assessment.violation_message or "contract line conflict"
            continue

        state = states[emp.id]
        violation = _would_violate_labor_rules(
            state,
            assignment_date,
            template,
            shift_templates,
            rules,
            period_start,
            period_end,
            availability_blocked,
            forced_clinical_ot=forced_clinical_ot,
            allow_provisional=allow_provisional,
            log_rejection=True,
        )
        if violation:
            ineligible_reasons[emp.id] = violation
            continue
        if assignment_date.weekday() >= 5 and not _can_assign_with_weekend_pairing(
            state,
            emp,
            assignment_date,
            template,
            shift_templates=shift_templates,
            rules=rules,
            period_start=period_start,
            period_end=period_end,
            availability_blocked=availability_blocked,
            qual_codes=qual_codes,
        ):
            ineligible_reasons[emp.id] = (
                "weekend pairing: paired day cannot take the same shift"
            )
            continue
        eligible.append(emp)

    from lab_scheduler.scheduling.equitability_score import FairnessWeights, score_line

    weights = fairness_weights or FairnessWeights()
    assignment_rows = assignments or []

    def optimization_key(emp: EmployeeProfile) -> Tuple[float, ...]:
        state = states[emp.id]
        load_penalty = weekday_morning_staffing_rank_penalty(
            assignment_date,
            weekday_morning_count,
            weekday_staffing_plan if enforce_weekday_load_balance else None,
        )
        equity_score = score_line(
            emp,
            total_hours=state.total_hours,
            target_hours=target_hours_map.get(emp.id, state.target_hours),
            work_dates=state.work_dates,
            assignments=assignment_rows,
            shift_templates=shift_templates,
            period_start=period_start,
            period_end=period_end,
            weights=weights,
        )
        base_key = build_assignment_rank_key(
            profile=emp,
            work_dates=state.work_dates,
            assignment_records=state.assignment_records,
            week_hours=state.week_hours,
            total_hours=state.total_hours,
            assignment_date=assignment_date,
            shift_id=shift_id,
            shift_hours=shift_hours,
            shift_template_code=template.code,
            rules=rules,
            period_start=period_start,
            period_end=period_end,
            employees=employees,
            employee_total_hours=total_hours_map,
            employee_target_hours=target_hours_map,
            qual_codes=qual_codes,
            prioritize_coverage=prioritize_coverage,
            period_target_hours=period_target_hours,
            coverage_priority_key_fn=coverage_priority_key,
            cba_rank_key_fn=cba_rank_key,
            role_pool_id=role_pool_id,
            night_shifts_filled_for_day_count=band_filled,
            weekday_daily_staffing_penalty=load_penalty,
        )
        streak_bonus = _dn_night_streak_continuation_bonus(
            state,
            assignment_date,
            shift_templates,
        )
        return (equity_score, streak_bonus, *base_key)

    eligible.sort(key=optimization_key)
    return eligible, ineligible_reasons, {emp.id for emp in eligible}


def _summarize_constraint_blocks(ineligible_reasons: Mapping[str, str]) -> str:
    if not ineligible_reasons:
        return "qualified staff blocked by labor rules"
    samples = list(ineligible_reasons.values())[:2]
    summary = "; ".join(samples)
    remaining = len(ineligible_reasons) - len(samples)
    if remaining > 0:
        summary = f"{summary} (+{remaining} more blocked)"
    return summary


def _shift_id_for_code(
    shift_code: str,
    shift_templates: Dict[str, ShiftTemplateInfo],
) -> Optional[str]:
    for shift_id, template in shift_templates.items():
        if template.code == shift_code:
            return shift_id
    return None


def _rebalance_weekday_morning_assignments(
    assignments: List[PlannedAssignment],
    *,
    states: Dict[str, _EmployeeState],
    shift_templates: Dict[str, ShiftTemplateInfo],
    rules: JurisdictionRules,
    period_start: date,
    period_end: date,
    availability_blocked: Optional[Mapping[str, Set[date]]] = None,
    weekday_staffing_plan: Optional[WeekdayDailyStaffingPlan] = None,
) -> int:
    """Move weekday day shifts from heavy dates to light dates toward the 13-seat target."""

    if weekday_staffing_plan is None or weekday_staffing_plan.target_per_day <= 0:
        return 0

    target = int(weekday_staffing_plan.target_per_day)
    moved = 0
    morning_ids = {
        shift_id
        for shift_id, template in shift_templates.items()
        if template.code == "MORNING"
    }
    weekday_dates = [
        assignment_date
        for assignment_date in _daterange(period_start, period_end)
        if assignment_date.weekday() < 5
    ]

    for _attempt in range(len(weekday_dates) * 4):
        counts = {
            assignment_date: weekday_morning_shift_count_from_states(
                states,
                assignment_date,
                shift_templates=shift_templates,
            )
            for assignment_date in weekday_dates
        }
        heavy_dates = sorted(
            [day for day in weekday_dates if counts[day] > target],
            key=lambda day: counts[day],
            reverse=True,
        )
        light_dates = sorted(
            [day for day in weekday_dates if counts[day] < target],
            key=lambda day: counts[day],
        )
        if not heavy_dates or not light_dates:
            break

        heavy_date = heavy_dates[0]
        light_date = light_dates[0]
        moved_one = False
        for index, assignment in enumerate(list(assignments)):
            if assignment.assignment_date != heavy_date:
                continue
            if getattr(assignment, "master_template_frozen", False):
                continue
            if assignment.shift_template_id not in morning_ids:
                continue
            state = states.get(assignment.employee_id)
            if state is None or light_date in state.work_dates:
                continue
            template = shift_templates[assignment.shift_template_id]
            violation = _would_violate_labor_rules(
                state,
                light_date,
                template,
                shift_templates,
                rules,
                period_start,
                period_end,
                availability_blocked,
            )
            if violation:
                continue

            shift_hours = template.duration_minutes / 60.0
            week_start_old = workweek_for(heavy_date).start
            week_start_new = workweek_for(light_date).start
            state.work_dates.discard(heavy_date)
            state.assignment_records[:] = [
                record
                for record in state.assignment_records
                if not (record[0] == heavy_date and record[1] == assignment.shift_template_id)
            ]
            if week_start_old in state.week_hours:
                state.week_hours[week_start_old] = max(
                    0.0,
                    state.week_hours[week_start_old] - shift_hours,
                )
            state.work_dates.add(light_date)
            state.assignment_records.append((light_date, assignment.shift_template_id))
            state.week_hours[week_start_new] = state.week_hours.get(week_start_new, 0.0) + shift_hours
            assignments[index] = PlannedAssignment(
                employee_id=assignment.employee_id,
                shift_template_id=assignment.shift_template_id,
                assignment_date=light_date,
            )
            moved += 1
            moved_one = True
            break
        if not moved_one:
            break

    return moved


def _frozen_master_cell_keys(
    assignments: Sequence[PlannedAssignment],
) -> Set[Tuple[str, date]]:
    return {
        (assignment.employee_id, assignment.assignment_date)
        for assignment in assignments
        if assignment.master_template_frozen
    }


def _is_frozen_master_cell(
    employee_id: str,
    assignment_date: date,
    frozen_cells: Optional[Set[Tuple[str, date]]],
) -> bool:
    return frozen_cells is not None and (employee_id, assignment_date) in frozen_cells


def _register_frozen_master_cells(result: AutoGenerateResult) -> None:
    result.frozen_master_cells = _frozen_master_cell_keys(result.assignments)


def _post_pass_guard_allows(
    post_pass_guard: Optional[PostPassGuard],
    *,
    assignments: Sequence[PlannedAssignment],
    employee_id: str,
    assignment_date: date,
    shift_template_id: str,
    shift_templates: Mapping[str, ShiftTemplateInfo],
    employees: Sequence[EmployeeProfile],
    qual_codes: Mapping[str, str],
    replace_existing: bool = False,
) -> bool:
    if post_pass_guard is None:
        return True
    return post_pass_guard.allows_assignment(
        assignments=assignments,
        employee_id=employee_id,
        assignment_date=assignment_date,
        shift_template_id=shift_template_id,
        shift_templates=shift_templates,
        employees=employees,
        qual_codes=qual_codes,
        replace_existing=replace_existing,
    )


def _post_pass_guard_for_result(
    result: AutoGenerateResult,
    *,
    post_pass_guard: Optional[PostPassGuard] = None,
    employees: Optional[Sequence[EmployeeProfile]] = None,
    period_start: Optional[date] = None,
) -> PostPassGuard:
    if post_pass_guard is not None:
        return post_pass_guard
    return PostPassGuard(
        frozen_master_cells=result.frozen_master_cells,
        manager_locked_cells=set(result.manager_locked_cells),
        employees=tuple(employees or ()),
        period_start=period_start,
    )


def _manager_lock_denies_cell_edit(
    post_pass_guard: Optional[PostPassGuard],
    assignments: Sequence[PlannedAssignment],
    *,
    employee_id: str,
    assignment_date: date,
    shift_templates: Mapping[str, ShiftTemplateInfo],
) -> bool:
    if post_pass_guard is None:
        return False
    return post_pass_guard.blocks_worked_cell_modification(
        assignments,
        employee_id=employee_id,
        assignment_date=assignment_date,
        shift_templates=shift_templates,
    )


def _deduped_coverage_gate_snapshot(
    assignments: Sequence[PlannedAssignment],
    *,
    employees: Sequence[EmployeeProfile],
    expanded_slots: Sequence[ExpandedScheduleSlot],
    shift_templates: Dict[str, ShiftTemplateInfo],
    qual_codes: Mapping[str, str],
    period_start: date,
    period_end: date,
) -> Tuple[int, bool]:
    """Coverage gaps and E/N clinical lock on deduped rows (matches persist gate)."""

    from lab_scheduler.scheduling.auto_pilot import dedupe_planned_assignments

    template_bands = {
        template_id: shift_band_from_template_code(info.code)
        for template_id, info in shift_templates.items()
    }
    deduped = dedupe_planned_assignments(
        assignments,
        template_id_to_band=template_bands,
    )
    fill_counts = _seat_fill_counts(deduped, employees, qual_codes)
    coverage_gap_count = sum(
        1
        for slot in expanded_slots
        if _slot_required_for_coverage_gate(slot, shift_templates)
        and not _slot_already_filled(slot, fill_counts)
    )
    clinical_seats_locked = evening_night_clinical_seats_satisfied(
        fill_counts=fill_counts,
        expanded_slots=expanded_slots,
        shift_templates=shift_templates,
        period_start=period_start,
        period_end=period_end,
    )
    return coverage_gap_count, clinical_seats_locked


def _propagate_portage_template(
    *,
    rules: JurisdictionRules,
    period_start: date,
    period_end: date,
    weeks_in_period: int,
    employees: Sequence[EmployeeProfile],
    shift_templates: Dict[str, ShiftTemplateInfo],
    employee_target_hours: Optional[Mapping[str, float]] = None,
    availability_blocked: Optional[Mapping[str, Set[date]]] = None,
    initial_states: Optional[Dict[str, _EmployeeState]] = None,
    locked_clinical_fill_counts: Optional[Mapping[Tuple[date, str, Optional[str]], int]] = None,
    skip_contract_top_up: bool = False,
    pool_manager: Optional[ElasticPoolManager] = None,
    weekday_staffing_plan: Optional[WeekdayDailyStaffingPlan] = None,
) -> Tuple[List[PlannedAssignment], Dict[str, _EmployeeState]]:
    """Lay down the 8-week Portage master rotation before deviation healing."""

    if initial_states is not None:
        states = {emp_id: _clone_employee_state(state) for emp_id, state in initial_states.items()}
    else:
        states = {}
        for emp in employees:
            default_target = rules.standard_hours_per_week_at_1_0_fte * emp.fte * weeks_in_period
            if employee_target_hours is not None and emp.id in employee_target_hours:
                target = float(employee_target_hours[emp.id])
            else:
                target = default_target
            states[emp.id] = _EmployeeState(profile=emp, target_hours=target)

    assignments: List[PlannedAssignment] = []
    period_days = _daterange(period_start, period_end)
    qual_codes_by_employee = {employee.id: infer_qual_code(employee) for employee in employees}
    # region agent log
    _stamp_stats = {
        "weekday_stamped": 0,
        "weekend_stamped": 0,
        "weekend_skip_no_token": 0,
        "weekend_skip_cap": 0,
        "weekend_skip_labor": 0,
        "weekend_skip_contract": 0,
    }
    # endregion

    weekend_dates_by_employee: Dict[str, List[date]] = {}
    for employee, assignment_date in _pool_interleave_dn_weekend_catalog_stamps(
        employees,
        period_start,
        period_end,
    ):
        weekend_dates_by_employee.setdefault(employee.id, []).append(assignment_date)

    for emp in employees:
        spec = _resolve_master_line_spec(emp, pool_manager)
        if spec is None:
            continue

        contract_line = emp.contract_line_type or spec.contract_line_type
        emp_qual = infer_qual_code(emp, qual_codes=None)
        state = states[emp.id]
        is_fulltime_vacant = _vacant_master_fulltime_line(emp)

        stamp_dates = _catalog_stamp_dates_for_employee(
            emp,
            period_start=period_start,
            period_end=period_end,
            weekend_dates_by_employee=weekend_dates_by_employee,
        )
        for assignment_date in stamp_dates:
            day_index = (assignment_date - period_start).days
            is_weekend = assignment_date.weekday() >= 5
            if is_weekend and not is_fulltime_vacant:
                continue
            if (
                availability_blocked
                and assignment_date in availability_blocked.get(emp.id, set())
            ):
                continue
            token = shift_token_for_day(
                spec,
                week_index=day_index // 7,
                day_of_week=assignment_date.weekday(),
            )
            if not token:
                # region agent log
                if is_weekend and is_fulltime_vacant:
                    _stamp_stats["weekend_skip_no_token"] += 1
                # endregion
                continue

            shift_code = TOKEN_TO_SHIFT_CODE.get(token)
            if shift_code is None:
                continue
            shift_id = _shift_id_for_code(shift_code, shift_templates)
            if shift_id is None:
                continue

            template = shift_templates[shift_id]
            line_violation = validate_contract_line_eligibility(
                contract_line,
                template.code,
                qual_code=emp_qual,
            )
            if line_violation:
                continue

            if is_weekend and template.code == "MORNING":
                weekend_counts = weekend_qual_counts_from_states(
                    states,
                    employees=employees,
                    qual_codes=qual_codes_by_employee,
                    assignment_date=assignment_date,
                    shift_templates=shift_templates,
                    morning_only=True,
                )
                if weekend_qual_cap_reached(weekend_counts, emp_qual):
                    # region agent log
                    if is_fulltime_vacant:
                        _stamp_stats["weekend_skip_cap"] += 1
                    # endregion
                    continue

            if not is_fulltime_vacant and template.code == "MORNING" and assignment_date.weekday() < 5:
                morning_count = weekday_morning_shift_count_from_states(
                    states,
                    assignment_date,
                    shift_templates=shift_templates,
                )
                if weekday_day_shift_capacity_block(
                    assignment_date,
                    morning_count,
                    shift_code=template.code,
                ):
                    continue
                if weekday_morning_staffing_hard_block(
                    assignment_date,
                    morning_count,
                    weekday_staffing_plan,
                ):
                    continue

            if template.code in {"NIGHT", "EVENING"} and locked_clinical_fill_counts is not None:
                # Named staff: clinical E/N seats are owned by lockdown.
                # Vacant Portage lines: master template owns their rotation including E/N.
                if parse_vacant_portage_line(emp.full_name) is None:
                    continue

            shift_hours = template.duration_minutes / 60.0
            if (
                not is_fulltime_vacant
                and state.total_hours + shift_hours > state.target_hours + 1e-9
            ):
                continue

            if assignment_date in state.work_dates:
                continue

            violation = _would_violate_labor_rules(
                state,
                assignment_date,
                template,
                shift_templates,
                rules,
                period_start,
                period_end,
                availability_blocked,
                enforce_fte_target=not is_fulltime_vacant,
                master_catalog_stamp=True,
            )
            if violation:
                # region agent log
                if is_weekend and is_fulltime_vacant:
                    _stamp_stats["weekend_skip_labor"] += 1
                # endregion
                continue

            week_start = workweek_for(assignment_date).start
            state.work_dates.add(assignment_date)
            state.assignment_records.append((assignment_date, shift_id))
            state.total_hours += shift_hours
            state.week_hours[week_start] = state.week_hours.get(week_start, 0.0) + shift_hours
            assignments.append(
                PlannedAssignment(
                    employee_id=emp.id,
                    shift_template_id=shift_id,
                    assignment_date=assignment_date,
                    master_template_frozen=parse_vacant_portage_line(emp.full_name) is not None,
                )
            )
            # region agent log
            if is_weekend:
                _stamp_stats["weekend_stamped"] += 1
            else:
                _stamp_stats["weekday_stamped"] += 1
            # endregion

        if (
            not skip_contract_top_up
            and _vacant_master_fulltime_line(emp)
            and state.total_hours < state.target_hours - 0.25
        ):
            for _round in range(len(period_days)):
                if state.total_hours >= state.target_hours - 0.25:
                    break
                round_progress = False
                weekday_dates = sorted(
                    [
                        assignment_date
                        for assignment_date in period_days
                        if assignment_date.weekday() < 5
                    ],
                    key=lambda assignment_date: weekday_morning_shift_count_from_states(
                        states,
                        assignment_date,
                        shift_templates=shift_templates,
                    ),
                )
                for assignment_date in weekday_dates:
                    for template in shift_templates.values():
                        if state.total_hours >= state.target_hours - 0.25:
                            break
                        if (
                            (contract_line or "") == "D/N"
                            and not vacant_master_rotation_permits_shift(
                                emp,
                                assignment_date,
                                period_start,
                                template.code,
                            )
                        ):
                            continue
                        line_violation = validate_contract_line_eligibility(
                            contract_line,
                            template.code,
                            qual_code=emp_qual,
                        )
                        if line_violation:
                            continue
                        if template.code == "MORNING" and assignment_date.weekday() < 5:
                            morning_count = weekday_morning_shift_count_from_states(
                                states,
                                assignment_date,
                                shift_templates=shift_templates,
                            )
                            if weekday_day_shift_capacity_block(
                                assignment_date,
                                morning_count,
                                shift_code=template.code,
                            ):
                                continue
                            if weekday_morning_staffing_hard_block(
                                assignment_date,
                                morning_count,
                                weekday_staffing_plan,
                            ):
                                continue
                        if template.code in {"NIGHT", "EVENING"} and locked_clinical_fill_counts is not None:
                            continue
                        shift_hours = template.duration_minutes / 60.0
                        if state.total_hours + shift_hours > state.target_hours + 1e-9:
                            continue
                        violation = _would_violate_labor_rules(
                            state,
                            assignment_date,
                            template,
                            shift_templates,
                            rules,
                            period_start,
                            period_end,
                            availability_blocked,
                        )
                        if violation:
                            continue
                        week_start = workweek_for(assignment_date).start
                        state.work_dates.add(assignment_date)
                        state.assignment_records.append((assignment_date, template.id))
                        state.total_hours += shift_hours
                        state.week_hours[week_start] = (
                            state.week_hours.get(week_start, 0.0) + shift_hours
                        )
                        assignments.append(
                            PlannedAssignment(
                                employee_id=emp.id,
                                shift_template_id=template.id,
                                assignment_date=assignment_date,
                            )
                        )
                        round_progress = True
                if not round_progress:
                    break

    return assignments, states


def _clone_employee_state(state: _EmployeeState) -> _EmployeeState:
    return _EmployeeState(
        profile=state.profile,
        target_hours=state.target_hours,
        total_hours=state.total_hours,
        work_dates=set(state.work_dates),
        week_hours=dict(state.week_hours),
        assignment_records=list(state.assignment_records),
        contract_completion_ot_used=state.contract_completion_ot_used,
    )


def _apply_assignment_to_state(
    state: _EmployeeState,
    assignment_date: date,
    shift_id: str,
    shift_hours: float,
    *,
    rules: Optional[JurisdictionRules] = None,
) -> None:
    week_start = workweek_for(assignment_date).start
    prior_week_total = state.week_hours.get(week_start, 0.0)
    state.work_dates.add(assignment_date)
    state.assignment_records.append((assignment_date, shift_id))
    state.total_hours += shift_hours
    state.week_hours[week_start] = prior_week_total + shift_hours
    if (
        rules is not None
        and not state.contract_completion_ot_used
        and state.target_hours - state.total_hours <= 0.25
        and prior_week_total + shift_hours > rules.weekly_overtime_threshold_hours + 1e-9
    ):
        state.contract_completion_ot_used = True


def _all_fulltime_at_contract_target(
    employees: Sequence[EmployeeProfile],
    states: Mapping[str, _EmployeeState],
    *,
    fulltime_target: float,
) -> bool:
    return all(
        not is_fulltime_contract_deficit(
            employee,
            states[employee.id].total_hours,
            fulltime_target=fulltime_target,
        )
        for employee in employees
        if employee.fte >= FULLTIME_FTE_THRESHOLD
    )


def _pick_mandatory_fulltime_candidate(
    *,
    employees: Sequence[EmployeeProfile],
    states: Mapping[str, _EmployeeState],
    slot: ExpandedScheduleSlot,
    shift_templates: Dict[str, ShiftTemplateInfo],
    shift_required_qualifications: Dict[str, Set[str]],
    rules: JurisdictionRules,
    period_start: date,
    period_end: date,
    availability_blocked: Optional[Mapping[str, Set[date]]],
    qual_codes: Mapping[str, str],
    fulltime_target: float,
    relax_dn_contract_completion: bool = False,
    load_reference_hours: Optional[Mapping[str, float]] = None,
) -> Optional[EmployeeProfile]:
    """Greedy pick: largest contract deficit (or pool-average deficit) that can take this seat."""

    shift_id = slot.shift_id
    template = shift_templates[shift_id]
    required = shift_required_qualifications.get(shift_id, set())
    chosen: Optional[EmployeeProfile] = None
    best_deficit = -1.0

    for employee in employees:
        if employee.fte < FULLTIME_FTE_THRESHOLD:
            continue
        state = states[employee.id]
        deficit = _elastic_load_deficit(
            employee,
            state,
            load_reference_hours=load_reference_hours,
            fulltime_target=fulltime_target,
        )
        if deficit <= 0.25:
            continue
        if not _is_qualified(employee, required):
            continue
        if not employee_matches_seat_qual(
            employee,
            slot.required_qual_code,
            qual_codes=qual_codes,
            shift_required_qualification_ids=required or None,
        ):
            continue
        emp_qual = infer_qual_code(employee, qual_codes=qual_codes)
        line_violation = validate_contract_line_eligibility(
            employee.contract_line_type,
            template.code,
            qual_code=emp_qual,
        )
        if line_violation:
            continue
        violation = _would_violate_labor_rules(
            state,
            slot.assignment_date,
            template,
            shift_templates,
            rules,
            period_start,
            period_end,
            availability_blocked,
            relax_dn_contract_completion=relax_dn_contract_completion,
        )
        if violation:
            continue
        if deficit > best_deficit:
            best_deficit = deficit
            chosen = employee
    return chosen


def _clinical_floor_lock_pass(
    *,
    employees: Sequence[EmployeeProfile],
    states: Dict[str, _EmployeeState],
    expanded_slots: Sequence[ExpandedScheduleSlot],
    shift_templates: Dict[str, ShiftTemplateInfo],
    shift_required_qualifications: Dict[str, Set[str]],
    rules: JurisdictionRules,
    period_start: date,
    period_end: date,
    availability_blocked: Optional[Mapping[str, Set[date]]],
    qual_codes: Mapping[str, str],
    target_hours_map: Mapping[str, float],
    period_target_hours: Mapping[str, float],
    fill_counts: Dict[Tuple[date, str, Optional[str]], int],
    filled_smooth_seats: Set[Tuple[date, str, Optional[str], int]],
    prioritize_coverage: bool,
    shift_codes: Tuple[str, ...] = ("EVENING", "NIGHT"),
    allow_forced_clinical_ot: bool = True,
    require_template_alignment: bool = False,
    single_pass: bool = False,
    clinical_mandatory: bool = False,
    pool_manager: Optional[ElasticPoolManager] = None,
    weekday_staffing_plan: Optional[WeekdayDailyStaffingPlan] = None,
    post_pass_guard: Optional[PostPassGuard] = None,
    guard_assignments: Optional[Sequence[PlannedAssignment]] = None,
) -> List[PlannedAssignment]:
    """
    Lock immutable clinical floor seats for Evening and/or Night before roster routing.

    When ``clinical_mandatory`` is True, the pass forbids leaving a clinical seat empty
    if any qualified worker is available — weekly-hour limits are overridden.
    """

    floor_slots = sorted(
        (
            slot
            for slot in expanded_slots
            if shift_templates[slot.shift_id].code in shift_codes
            and (
                is_clinical_floor_pool(slot.role_pool_id)
                or shift_templates[slot.shift_id].code not in CLINICAL_FLOOR
            )
        ),
        key=lambda slot: (
            slot.assignment_date,
            shift_templates[slot.shift_id].code,
            slot.required_qual_code or "",
            slot.seat_index,
        ),
    )
    planned: List[PlannedAssignment] = []

    for _round in range(1 if single_pass else 12):
        if all(_slot_already_filled(slot, fill_counts) for slot in floor_slots):
            break
        progress = False

        for slot in floor_slots:
            if prioritize_coverage and _slot_already_filled(slot, fill_counts):
                continue
            if _slot_blocked_by_weekend_cap(
                slot,
                states=states,
                employees=employees,
                qual_codes=qual_codes,
                shift_templates=shift_templates,
            ):
                continue

            shift_id = slot.shift_id
            template = shift_templates[shift_id]
            required = shift_required_qualifications.get(shift_id, set())

            if (
                template.code in {"EVENING", "NIGHT"}
                and is_clinical_floor_pool(slot.role_pool_id)
            ):
                chosen, provisional, _rejections = _resolve_mandatory_clinical_pick(
                    employees=employees,
                    required=required,
                    states=states,
                    assignment_date=slot.assignment_date,
                    template=template,
                    qual_codes=qual_codes,
                    required_qual_code=slot.required_qual_code,
                    availability_blocked=availability_blocked,
                    role_pool_id=slot.role_pool_id,
                    shift_templates=shift_templates,
                    clinical_mandatory=True,
                    period_start=period_start,
                )
                if chosen is None:
                    continue
                if not _post_pass_guard_allows(
                    post_pass_guard,
                    assignments=list(guard_assignments or ()) + planned,
                    employee_id=chosen.id,
                    assignment_date=slot.assignment_date,
                    shift_template_id=shift_id,
                    shift_templates=shift_templates,
                    employees=employees,
                    qual_codes=qual_codes,
                ):
                    continue
                shift_hours = template.duration_minutes / 60.0
                _apply_assignment_to_state(
                    states[chosen.id],
                    slot.assignment_date,
                    shift_id,
                    shift_hours,
                    rules=rules,
                )
                states[chosen.id].contract_completion_ot_used = True
                planned.append(
                    _planned_assignment_from_mandatory_clinical_pick(
                        chosen=chosen,
                        provisional=provisional,
                        shift_template_id=shift_id,
                        assignment_date=slot.assignment_date,
                    )
                )
                seat_key_count = (slot.assignment_date, shift_id, slot.required_qual_code)
                fill_counts[seat_key_count] = fill_counts.get(seat_key_count, 0) + 1
                progress = True
                continue

            band_filled = clinical_band_filled_for_day(
                slot.assignment_date,
                template.code,
                fill_counts=fill_counts,
                shift_templates=shift_templates,
                expanded_slots=expanded_slots,
            )
            chosen: Optional[EmployeeProfile] = None
            provisional: Optional[ClinicalContractLineProvisional] = None
            forced_ot = False
            bypassed_overtime = False

            attempt_modes = (True,) if clinical_mandatory else (False, True)
            for use_forced_ot in attempt_modes:
                if use_forced_ot and not allow_forced_clinical_ot:
                    break
                alignment_required = require_template_alignment and not use_forced_ot
                ranked, _, _ = _rank_slot_candidates(
                    employees=employees,
                    required=required,
                    states=states,
                    assignment_date=slot.assignment_date,
                    shift_id=shift_id,
                    template=template,
                    shift_templates=shift_templates,
                    rules=rules,
                    period_start=period_start,
                    period_end=period_end,
                    availability_blocked=availability_blocked,
                    prioritize_coverage=prioritize_coverage,
                    period_target_hours=period_target_hours,
                    required_qual_code=slot.required_qual_code,
                    qual_codes=qual_codes,
                    employee_target_hours=target_hours_map,
                    role_pool_id=slot.role_pool_id,
                    fill_counts=fill_counts,
                    expanded_slots=expanded_slots,
                    forced_clinical_ot=use_forced_ot,
                    weekday_staffing_plan=weekday_staffing_plan,
                )
                if not ranked and use_forced_ot:
                    if clinical_mandatory:
                        chosen_pick, provisional_pick, _rejections = _resolve_mandatory_clinical_pick(
                            employees=employees,
                            required=required,
                            states=states,
                            assignment_date=slot.assignment_date,
                            template=template,
                            qual_codes=qual_codes,
                            required_qual_code=slot.required_qual_code,
                            availability_blocked=availability_blocked,
                            role_pool_id=slot.role_pool_id,
                            shift_templates=shift_templates,
                            clinical_mandatory=clinical_mandatory,
                            period_start=period_start,
                        )
                        if chosen_pick is not None:
                            ranked = [chosen_pick]
                            provisional = provisional_pick
                    else:
                        ranked = _forced_clinical_ot_candidates(
                            employees=employees,
                            required=required,
                            states=states,
                            assignment_date=slot.assignment_date,
                            template=template,
                            shift_templates=shift_templates,
                            rules=rules,
                            period_start=period_start,
                            period_end=period_end,
                            qual_codes=qual_codes,
                            required_qual_code=slot.required_qual_code,
                            availability_blocked=availability_blocked,
                            role_pool_id=slot.role_pool_id,
                        )
                    if ranked:
                        bypassed_overtime = True
                if not ranked:
                    continue

                if require_template_alignment and not use_forced_ot:
                    ranked = [
                        employee
                        for employee in ranked
                        if _is_template_aligned_for_clinical_lock(
                            employee,
                            assignment_date=slot.assignment_date,
                            period_start=period_start,
                            shift_code=template.code,
                            pool_manager=pool_manager,
                        )
                    ]
                    if not ranked:
                        continue

                ranked.sort(
                    key=lambda emp: (
                        _template_clinical_alignment_penalty(
                            emp,
                            assignment_date=slot.assignment_date,
                            period_start=period_start,
                            shift_code=template.code,
                            pool_manager=pool_manager,
                        ),
                        build_assignment_rank_key(
                            profile=emp,
                            work_dates=states[emp.id].work_dates,
                            assignment_records=states[emp.id].assignment_records,
                            week_hours=states[emp.id].week_hours,
                            total_hours=states[emp.id].total_hours,
                            assignment_date=slot.assignment_date,
                            shift_id=shift_id,
                            shift_hours=template.duration_minutes / 60.0,
                            shift_template_code=template.code,
                            rules=rules,
                            period_start=period_start,
                            period_end=period_end,
                            employees=employees,
                            employee_total_hours={
                                emp_id: state.total_hours for emp_id, state in states.items()
                            },
                            employee_target_hours=target_hours_map,
                            qual_codes=qual_codes,
                            prioritize_coverage=prioritize_coverage,
                            period_target_hours=period_target_hours,
                            coverage_priority_key_fn=lambda _p, _h, _t: (0, 0, 0, 0, 0),
                            cba_rank_key_fn=lambda _p: (0,),
                            role_pool_id=slot.role_pool_id,
                            night_shifts_filled_for_day_count=band_filled,
                        ),
                    )
                )
                chosen = ranked[0]
                forced_ot = use_forced_ot or clinical_mandatory
                if forced_ot and not clinical_mandatory:
                    bypassed_overtime = True
                elif clinical_mandatory and use_forced_ot:
                    bypassed_overtime = True
                break

            if chosen is None and clinical_mandatory:
                chosen, provisional, _rejections = _resolve_mandatory_clinical_pick(
                    employees=employees,
                    required=required,
                    states=states,
                    assignment_date=slot.assignment_date,
                    template=template,
                    qual_codes=qual_codes,
                    required_qual_code=slot.required_qual_code,
                    availability_blocked=availability_blocked,
                    role_pool_id=slot.role_pool_id,
                    shift_templates=shift_templates,
                    clinical_mandatory=True,
                    period_start=period_start,
                )
                if chosen is not None:
                    forced_ot = True
                    bypassed_overtime = True

            if chosen is None:
                continue

            if not _post_pass_guard_allows(
                post_pass_guard,
                assignments=list(guard_assignments or ()) + planned,
                employee_id=chosen.id,
                assignment_date=slot.assignment_date,
                shift_template_id=shift_id,
                shift_templates=shift_templates,
                employees=employees,
                qual_codes=qual_codes,
            ):
                continue

            shift_hours = template.duration_minutes / 60.0
            _apply_assignment_to_state(
                states[chosen.id],
                slot.assignment_date,
                shift_id,
                shift_hours,
                rules=rules,
            )
            if forced_ot:
                state = states[chosen.id]
                state.contract_completion_ot_used = True
            planned.append(
                _planned_assignment_from_mandatory_clinical_pick(
                    chosen=chosen,
                    provisional=provisional if clinical_mandatory else None,
                    shift_template_id=shift_id,
                    assignment_date=slot.assignment_date,
                    forced_clinical_ot=forced_ot,
                    overtime_compliance_bypassed=bypassed_overtime,
                )
            )
            seat_key_count = (slot.assignment_date, shift_id, slot.required_qual_code)
            fill_counts[seat_key_count] = fill_counts.get(seat_key_count, 0) + 1
            progress = True

        if not progress:
            break

    return planned


def _forced_clinical_ot_candidates(
    *,
    employees: Sequence[EmployeeProfile],
    required: Set[str],
    states: Mapping[str, _EmployeeState],
    assignment_date: date,
    template: ShiftTemplateInfo,
    shift_templates: Dict[str, ShiftTemplateInfo],
    rules: JurisdictionRules,
    period_start: date,
    period_end: date,
    qual_codes: Mapping[str, str],
    required_qual_code: Optional[str],
    availability_blocked: Optional[Mapping[str, Set[date]]],
    role_pool_id: Optional[str] = None,
) -> List[EmployeeProfile]:
    eligible: List[EmployeeProfile] = []
    for emp in employees:
        if not _is_qualified(emp, required):
            continue
        if not employee_matches_seat_qual(
            emp,
            required_qual_code,
            qual_codes=qual_codes,
            shift_required_qualification_ids=required or None,
        ):
            continue
        emp_qual = infer_qual_code(emp, qual_codes=qual_codes)
        line_assessment = assess_clinical_floor_contract_line(
            contract_line_type=emp.contract_line_type,
            shift_code=template.code,
            qual_code=emp_qual,
            role_pool_id=role_pool_id,
        )
        if line_assessment.hard_rejection:
            continue
        state = states[emp.id]
        violation = _would_violate_labor_rules(
            state,
            assignment_date,
            template,
            shift_templates,
            rules,
            period_start,
            period_end,
            availability_blocked,
            forced_clinical_ot=True,
        )
        if violation:
            continue
        eligible.append(emp)
    return eligible


def _mandatory_clinical_candidates(
    *,
    employees: Sequence[EmployeeProfile],
    required: Set[str],
    states: Mapping[str, _EmployeeState],
    assignment_date: date,
    template: ShiftTemplateInfo,
    qual_codes: Mapping[str, str],
    required_qual_code: Optional[str],
    availability_blocked: Optional[Mapping[str, Set[date]]],
    role_pool_id: Optional[str] = None,
    shift_templates: Optional[Dict[str, ShiftTemplateInfo]] = None,
) -> List[EmployeeProfile]:
    """Qualified staff eligible for mandatory clinical fill (ignores hour/OT caps)."""

    audit = mandatory_clinical_candidates_with_audit(
        employees=employees,
        required=required,
        states=states,
        assignment_date=assignment_date,
        template=template,
        qual_codes=qual_codes,
        required_qual_code=required_qual_code,
        availability_blocked=availability_blocked,
        role_pool_id=role_pool_id,
        shift_templates=shift_templates,
    )
    combined = list(audit.eligible) + [
        item.employee for item in audit.provisional_contract_line
    ]
    def _mandatory_weekend_sort(profile: EmployeeProfile) -> Tuple[float, float]:
        state = states[profile.id]
        return (
            weekend_paired_day_rank_penalty(
                work_dates=state.work_dates,
                assignment_date=assignment_date,
            ),
            state.total_hours,
        )

    combined.sort(key=_mandatory_weekend_sort)
    return combined


def _resolve_mandatory_clinical_pick(
    *,
    employees: Sequence[EmployeeProfile],
    required: Set[str],
    states: Mapping[str, _EmployeeState],
    assignment_date: date,
    template: ShiftTemplateInfo,
    qual_codes: Mapping[str, str],
    required_qual_code: Optional[str],
    availability_blocked: Optional[Mapping[str, Set[date]]],
    role_pool_id: Optional[str],
    shift_templates: Optional[Dict[str, ShiftTemplateInfo]] = None,
    clinical_mandatory: bool = False,
    period_start: Optional[date] = None,
) -> Tuple[
    Optional[EmployeeProfile],
    Optional[ClinicalContractLineProvisional],
    Tuple[str, ...],
]:
    audit = mandatory_clinical_candidates_with_audit(
        employees=employees,
        required=required,
        states=states,
        assignment_date=assignment_date,
        template=template,
        qual_codes=qual_codes,
        required_qual_code=required_qual_code,
        availability_blocked=availability_blocked,
        role_pool_id=role_pool_id,
        shift_templates=shift_templates,
        clinical_mandatory=clinical_mandatory,
        period_start=period_start,
    )
    chosen, provisional = select_mandatory_clinical_candidate(audit)
    return chosen, provisional, audit.rejections


def _resolve_mandatory_clinical_placement(
    *,
    employees: Sequence[EmployeeProfile],
    required: Set[str],
    states: Dict[str, _EmployeeState],
    assignment_date: date,
    template: ShiftTemplateInfo,
    shift_templates: Dict[str, ShiftTemplateInfo],
    rules: JurisdictionRules,
    period_start: date,
    period_end: date,
    qual_codes: Mapping[str, str],
    required_qual_code: Optional[str],
    availability_blocked: Optional[Mapping[str, Set[date]]],
    role_pool_id: Optional[str],
    post_pass_guard: Optional[PostPassGuard] = None,
    guard_assignments: Optional[Sequence[PlannedAssignment]] = None,
    planned: Optional[Sequence[PlannedAssignment]] = None,
    frozen_master_cells: Optional[Set[Tuple[str, date]]] = None,
    allow_frozen_clinical_supersede: bool = False,
    employees_for_guard: Optional[Sequence[EmployeeProfile]] = None,
    shift_templates_for_guard: Optional[Dict[str, ShiftTemplateInfo]] = None,
    qual_codes_for_guard: Optional[Mapping[str, str]] = None,
    payroll_targets: Optional[Mapping[str, float]] = None,
    catalog_targets: Optional[Mapping[str, float]] = None,
) -> Tuple[
    Optional[EmployeeProfile],
    Optional[ClinicalContractLineProvisional],
    Tuple[str, ...],
]:
    """Pick the first mandatory clinical candidate that passes labor rules and guards."""

    from lab_scheduler.scheduling.clinical_seats import vacant_may_supersede_for_clinical_band

    audit = mandatory_clinical_candidates_with_audit(
        employees=employees,
        required=required,
        states=states,
        assignment_date=assignment_date,
        template=template,
        qual_codes=qual_codes,
        required_qual_code=required_qual_code,
        availability_blocked=availability_blocked,
        role_pool_id=role_pool_id,
        shift_templates=shift_templates,
        clinical_mandatory=True,
        period_start=period_start,
    )
    provisional_map = {
        item.employee.id: item for item in audit.provisional_contract_line
    }
    guard_employees = employees_for_guard or employees
    guard_templates = shift_templates_for_guard or shift_templates
    guard_qual_codes = qual_codes_for_guard or qual_codes
    assignment_pool = list(guard_assignments or ()) + list(planned or ())

    for employee in audit.eligible:
        violation = _would_violate_labor_rules(
            states[employee.id],
            assignment_date,
            template,
            shift_templates,
            rules,
            period_start,
            period_end,
            availability_blocked,
            enforce_fte_target=False,
            forced_clinical_ot=True,
            log_rejection=True,
            payroll_targets=payroll_targets,
            catalog_targets=catalog_targets,
        )
        if violation:
            continue

        supersede_day = vacant_may_supersede_for_clinical_band(
            profile=employee,
            assignment_date=assignment_date,
            target_shift_code=template.code,
            state=states[employee.id],
            shift_templates=shift_templates,
            period_start=period_start,
        )
        if _is_frozen_master_cell(employee.id, assignment_date, frozen_master_cells):
            if not (allow_frozen_clinical_supersede and supersede_day):
                continue

        if _vacant_line_weekend_deferred_to_cpsat(
            employee,
            assignment_date,
            post_pass_guard=post_pass_guard,
        ):
            continue

        if not _post_pass_guard_allows(
            post_pass_guard,
            assignments=assignment_pool,
            employee_id=employee.id,
            assignment_date=assignment_date,
            shift_template_id=template.id,
            shift_templates=guard_templates,
            employees=guard_employees,
            qual_codes=guard_qual_codes,
            replace_existing=bool(allow_frozen_clinical_supersede and supersede_day),
        ):
            continue

        return employee, provisional_map.get(employee.id), audit.rejections

    return None, None, audit.rejections


def _contract_line_provisionals_from_planned(
    assignments: Sequence[PlannedAssignment],
    *,
    employees: Sequence[EmployeeProfile],
    shift_templates: Mapping[str, ShiftTemplateInfo],
) -> List[ProvisionalAssignment]:
    name_lookup = {employee.id: employee.full_name for employee in employees}
    rows: List[ProvisionalAssignment] = []
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
        shift_code = template.code if template is not None else assignment.shift_template_id
        rows.append(
            build_contract_line_provisional_assignment(
                employee_id=assignment.employee_id,
                employee_name=name_lookup.get(
                    assignment.employee_id,
                    assignment.employee_id,
                ),
                assignment_date=assignment.assignment_date,
                shift_template_id=assignment.shift_template_id,
                shift_code=shift_code,
                violation_message=assignment.contract_line_exception_message,
            )
        )
    return rows


def _planned_assignment_from_mandatory_clinical_pick(
    *,
    chosen: EmployeeProfile,
    provisional: Optional[ClinicalContractLineProvisional],
    shift_template_id: str,
    assignment_date: date,
    clinical_stretch: bool = False,
    forced_clinical_ot: bool = True,
    overtime_compliance_bypassed: bool = True,
) -> PlannedAssignment:
    contract_line_exception = provisional is not None
    return PlannedAssignment(
        employee_id=chosen.id,
        shift_template_id=shift_template_id,
        assignment_date=assignment_date,
        forced_clinical_ot=forced_clinical_ot,
        overtime_compliance_bypassed=overtime_compliance_bypassed,
        clinical_floor_stretch=clinical_stretch,
        contract_line_exception=contract_line_exception,
        contract_line_exception_message=(
            provisional.violation_message if provisional is not None else ""
        ),
        provisional_compliance=contract_line_exception,
    )


_LOGGER = logging.getLogger(__name__)


def _log_critical_clinical_gap(gap: CriticalClinicalGap) -> None:
    from lab_scheduler.scheduling.assignment_rejection_log import scheduling_trace_enabled

    if scheduling_trace_enabled():
        _LOGGER.warning(gap.log_line())
    else:
        _LOGGER.debug(gap.log_line())


def _record_critical_clinical_gaps(
    result: AutoGenerateResult,
    gaps: Sequence[CriticalClinicalGap],
    *,
    emit_summary: bool = False,
) -> None:
    seen = {(g.assignment_date, g.shift_code, g.seat_label) for g in result.critical_clinical_gaps}
    added = 0
    for gap in gaps:
        key = (gap.assignment_date, gap.shift_code, gap.seat_label)
        if key in seen:
            continue
        seen.add(key)
        result.critical_clinical_gaps.append(gap)
        _log_critical_clinical_gap(gap)
        added += 1
    if emit_summary and added:
        _LOGGER.warning(
            "Clinical gaps: %d seat(s) unfilled after lockdown (see result.critical_clinical_gaps)",
            len(result.critical_clinical_gaps),
        )


def _run_clinical_seat_lockdown_pass(
    *,
    employees: Sequence[EmployeeProfile],
    states: Dict[str, _EmployeeState],
    expanded_slots: Sequence[ExpandedScheduleSlot],
    shift_templates: Dict[str, ShiftTemplateInfo],
    shift_required_qualifications: Dict[str, Set[str]],
    rules: JurisdictionRules,
    fill_counts: Dict[Tuple[date, str, Optional[str]], int],
    availability_blocked: Optional[Mapping[str, Set[date]]],
    qual_codes: Mapping[str, str],
    log_critical_gaps: bool = True,
    assignments: Optional[List[PlannedAssignment]] = None,
    frozen_master_cells: Optional[Set[Tuple[str, date]]] = None,
    post_pass_guard: Optional[PostPassGuard] = None,
    employees_for_guard: Optional[Sequence[EmployeeProfile]] = None,
    shift_templates_for_guard: Optional[Dict[str, ShiftTemplateInfo]] = None,
    qual_codes_for_guard: Optional[Mapping[str, str]] = None,
    allow_frozen_clinical_supersede: bool = False,
    weekend_first: bool = False,
    period_start: Optional[date] = None,
    period_end: Optional[date] = None,
    payroll_targets: Optional[Mapping[str, float]] = None,
    catalog_targets: Optional[Mapping[str, float]] = None,
) -> List[PlannedAssignment]:
    """
    Clinical Floor Pass: calendar-ordered Seat_01 then Seat_02 for every Evening/Night day.

    Only MLT/MLA clinical-floor seats are filled, ranked by lowest current hours first.
    """

    seats = evening_night_clinical_seat_slots(
        expanded_slots,
        shift_templates=shift_templates,
        weekend_first=weekend_first,
    )
    planned: List[PlannedAssignment] = []
    logged_seat_gaps: Set[Tuple[date, str, str]] = set()

    for _round in range(12):
        progress = False
        for slot in seats:
            shift_id = slot.shift_id
            template = shift_templates[shift_id]
            seat_num = clinical_seat_number(slot.role_pool_id)

            if slot_is_filled(slot, fill_counts):
                continue

            required = shift_required_qualifications.get(shift_id, set())
            if period_start is not None and period_end is not None:
                chosen, provisional, rejection_reasons = _resolve_mandatory_clinical_placement(
                    employees=employees,
                    required=required,
                    states=states,
                    assignment_date=slot.assignment_date,
                    template=template,
                    shift_templates=shift_templates,
                    rules=rules,
                    period_start=period_start,
                    period_end=period_end,
                    qual_codes=qual_codes,
                    required_qual_code=slot.required_qual_code,
                    availability_blocked=availability_blocked,
                    role_pool_id=slot.role_pool_id,
                    post_pass_guard=post_pass_guard,
                    guard_assignments=assignments,
                    planned=planned,
                    frozen_master_cells=frozen_master_cells,
                    allow_frozen_clinical_supersede=allow_frozen_clinical_supersede,
                    employees_for_guard=employees_for_guard,
                    shift_templates_for_guard=shift_templates_for_guard,
                    qual_codes_for_guard=qual_codes_for_guard,
                    payroll_targets=payroll_targets,
                    catalog_targets=catalog_targets,
                )
            else:
                chosen, provisional, rejection_reasons = _resolve_mandatory_clinical_pick(
                    employees=employees,
                    required=required,
                    states=states,
                    assignment_date=slot.assignment_date,
                    template=template,
                    qual_codes=qual_codes,
                    required_qual_code=slot.required_qual_code,
                    availability_blocked=availability_blocked,
                    role_pool_id=slot.role_pool_id,
                    shift_templates=shift_templates,
                    clinical_mandatory=True,
                    period_start=period_start,
                )
            if seat_num == 2:
                if chosen is None:
                    reason = (
                        "no available staff"
                        if not rejection_reasons
                        else "constraint conflict: " + "; ".join(rejection_reasons)
                    )
                else:
                    suffix = " (contract line exception)" if provisional is not None else ""
                    reason = f"assigning {chosen.id} ({len(rejection_reasons)} rejection(s)){suffix}"
                emit_scheduling_trace(
                    f"CLINICAL_LOCKDOWN_DEBUG round={_round + 1} "
                    f"date={slot.assignment_date.isoformat()} shift={template.code} "
                    f"seat=Seat_02 qualified_count={1 if chosen else 0} reason={reason}"
                )
            if chosen is None:
                if log_critical_gaps and clinical_seat_number(slot.role_pool_id) == 2:
                    gap_key = (
                        slot.assignment_date,
                        template.code,
                        clinical_seat_label(slot.role_pool_id),
                    )
                    if gap_key not in logged_seat_gaps:
                        logged_seat_gaps.add(gap_key)
                        _log_critical_clinical_gap(
                            CriticalClinicalGap(
                                assignment_date=slot.assignment_date,
                                shift_code=template.code,
                                seat_label=clinical_seat_label(slot.role_pool_id),
                                reason=(
                                    f"no qualified MLT/MLA available for "
                                    f"{clinical_seat_label(slot.role_pool_id)}"
                                ),
                            )
                        )
                continue

            shift_hours = template.duration_minutes / 60.0
            if assignments is not None:
                _supersede_vacant_clinical_day_assignment(
                    employee=chosen,
                    assignment_date=slot.assignment_date,
                    new_shift_id=shift_id,
                    assignments=assignments,
                    states=states,
                    shift_templates=shift_templates,
                    fill_counts=fill_counts,
                    qual_codes=qual_codes,
                    allow_frozen_supersede=allow_frozen_clinical_supersede,
                    period_start=period_start,
                )
            clinical_stretch = _clinical_floor_stretch_for_assignment(
                states[chosen.id],
                slot.assignment_date,
                template,
                shift_templates,
            )
            _apply_assignment_to_state(
                states[chosen.id],
                slot.assignment_date,
                shift_id,
                shift_hours,
                rules=rules,
            )
            states[chosen.id].contract_completion_ot_used = True
            planned.append(
                _planned_assignment_from_mandatory_clinical_pick(
                    chosen=chosen,
                    provisional=provisional,
                    shift_template_id=shift_id,
                    assignment_date=slot.assignment_date,
                    clinical_stretch=clinical_stretch,
                )
            )
            seat_key = (slot.assignment_date, shift_id, slot.required_qual_code)
            fill_counts[seat_key] = fill_counts.get(seat_key, 0) + 1
            progress = True

        if not progress:
            break

    if log_critical_gaps:
        for gap in collect_critical_clinical_gaps(
            fill_counts=fill_counts,
            expanded_slots=expanded_slots,
            shift_templates=shift_templates,
        ):
            if gap.seat_label != "Seat_02":
                continue
            gap_key = (gap.assignment_date, gap.shift_code, gap.seat_label)
            if gap_key not in logged_seat_gaps:
                logged_seat_gaps.add(gap_key)
                _log_critical_clinical_gap(gap)

    return planned


def _extend_evening_night_clinical_lockdown(
    result: AutoGenerateResult,
    *,
    employees: Sequence[EmployeeProfile],
    states: Dict[str, _EmployeeState],
    expanded_slots: Sequence[ExpandedScheduleSlot],
    shift_templates: Dict[str, ShiftTemplateInfo],
    shift_required_qualifications: Dict[str, Set[str]],
    rules: JurisdictionRules,
    fill_counts: Dict[Tuple[date, str, Optional[str]], int],
    availability_blocked: Optional[Mapping[str, Set[date]]],
    qual_codes: Mapping[str, str],
    period_start: date,
    period_end: date,
    max_rounds: int = 12,
    log_critical_gaps: bool = True,
    post_pass_guard: Optional[PostPassGuard] = None,
    allow_frozen_clinical_supersede: bool = False,
    weekend_first: bool = False,
    payroll_targets: Optional[Mapping[str, float]] = None,
    catalog_targets: Optional[Mapping[str, float]] = None,
) -> bool:
    """
    Repeat clinical seat lockdown until every Evening/Night date/band has Seat_01 + Seat_02.

    Uses lowest-hours mandatory ranking only — no fairness or even-distribution scoring.
    """

    for _ in range(max_rounds):
        if evening_night_clinical_seats_satisfied(
            fill_counts=fill_counts,
            expanded_slots=expanded_slots,
            shift_templates=shift_templates,
            period_start=period_start,
            period_end=period_end,
        ):
            return True
        batch = _run_clinical_seat_lockdown_pass(
            employees=employees,
            states=states,
            expanded_slots=expanded_slots,
            shift_templates=shift_templates,
            shift_required_qualifications=shift_required_qualifications,
            rules=rules,
            fill_counts=fill_counts,
            availability_blocked=availability_blocked,
            qual_codes=qual_codes,
            log_critical_gaps=log_critical_gaps,
            assignments=result.assignments,
            frozen_master_cells=result.frozen_master_cells,
            post_pass_guard=post_pass_guard,
            employees_for_guard=employees,
            shift_templates_for_guard=shift_templates,
            qual_codes_for_guard=qual_codes,
            allow_frozen_clinical_supersede=allow_frozen_clinical_supersede,
            weekend_first=weekend_first,
            period_start=period_start,
            period_end=period_end,
            payroll_targets=payroll_targets,
            catalog_targets=catalog_targets,
        )
        if not batch:
            break
        result.assignments.extend(batch)
        _rebuild_states_from_assignments(states, result.assignments, shift_templates)
        fill_counts = _seat_fill_counts(result.assignments, employees, qual_codes)
    return evening_night_clinical_seats_satisfied(
        fill_counts=fill_counts,
        expanded_slots=expanded_slots,
        shift_templates=shift_templates,
        period_start=period_start,
        period_end=period_end,
    )


def _aggressor_candidates(
    *,
    employees: Sequence[EmployeeProfile],
    required: Set[str],
    states: Mapping[str, _EmployeeState],
    assignment_date: date,
    template: ShiftTemplateInfo,
    qual_codes: Mapping[str, str],
    required_qual_code: Optional[str],
    availability_blocked: Optional[Mapping[str, Set[date]]],
    fulltime_target: float = 0.0,
    contract_deficit_only: bool = False,
) -> List[EmployeeProfile]:
    """Coverage Aggressor: ignore weekly-hour / union-risk caps; keep qual + line rules."""

    eligible: List[EmployeeProfile] = []
    for emp in employees:
        if not _is_qualified(emp, required):
            continue
        if not employee_matches_seat_qual(
            emp,
            required_qual_code,
            qual_codes=qual_codes,
            shift_required_qualification_ids=required or None,
        ):
            continue
        emp_qual = infer_qual_code(emp, qual_codes=qual_codes)
        line_violation = validate_contract_line_eligibility(
            emp.contract_line_type,
            template.code,
            qual_code=emp_qual,
        )
        if line_violation:
            continue
        state = states[emp.id]
        if availability_blocked and assignment_date in availability_blocked.get(emp.id, set()):
            continue
        if assignment_date in state.work_dates:
            continue
        if contract_deficit_only:
            if emp.fte < FULLTIME_FTE_THRESHOLD:
                continue
            if state.total_hours >= fulltime_target - 0.25:
                continue
        eligible.append(emp)

    if contract_deficit_only and fulltime_target > 0.0:
        eligible.sort(
            key=lambda profile: (
                -(fulltime_target - states[profile.id].total_hours),
                states[profile.id].total_hours,
            )
        )
    else:
        eligible.sort(key=lambda profile: states[profile.id].total_hours)
    return eligible


def _aggressor_slot_sort_key(
    slot: ExpandedScheduleSlot,
    *,
    shift_templates: Mapping[str, ShiftTemplateInfo],
    weekend_only: bool,
) -> Tuple[int, ...]:
    code = shift_templates[slot.shift_id].code
    band_order = {"MORNING": 0, "EVENING": 1, "NIGHT": 2}.get(code, 3)
    is_weekend = slot.assignment_date.weekday() >= 5
    if weekend_only:
        return (
            0 if is_weekend else 1,
            band_order,
            slot.assignment_date.toordinal(),
            slot.seat_index,
        )
    return (slot.assignment_date.toordinal(), band_order, slot.seat_index)


def _run_coverage_aggressor_protocol(
    result: AutoGenerateResult,
    *,
    employees: Sequence[EmployeeProfile],
    states: Dict[str, _EmployeeState],
    expanded_slots: Sequence[ExpandedScheduleSlot],
    shift_templates: Dict[str, ShiftTemplateInfo],
    shift_required_qualifications: Dict[str, Set[str]],
    rules: JurisdictionRules,
    availability_blocked: Optional[Mapping[str, Set[date]]],
    qual_codes: Mapping[str, str],
    fill_counts: Dict[Tuple[date, str, Optional[str]], int],
    filled_smooth_seats: Set[Tuple[date, str, Optional[str], int]],
    weeks_in_period: int,
    period_start: date,
    period_end: date,
    allow_contract_and_even_phases: bool = True,
    post_pass_guard: Optional[PostPassGuard] = None,
) -> int:
    """
    Coverage Aggressor fill protocol:
      1. Weekend Day/Evening/Night open slots
      2. Open slots while fulltime lines remain below the period contract target
      3. All remaining open slots (40h/week caps ignored)
    """

    fulltime_target = fulltime_period_contract_hours(
        rules=rules,
        weeks_in_period=weeks_in_period,
    )
    added = 0

    def _assign_slot(
        slot: ExpandedScheduleSlot,
        *,
        contract_deficit_only: bool,
        weekend_only: bool,
    ) -> bool:
        nonlocal added
        if weekend_only and slot.assignment_date.weekday() < 5:
            return False
        if _slot_already_filled(slot, fill_counts):
            return False

        shift_id = slot.shift_id
        template = shift_templates[shift_id]
        required = shift_required_qualifications.get(shift_id, set())
        ranked = _aggressor_candidates(
            employees=employees,
            required=required,
            states=states,
            assignment_date=slot.assignment_date,
            template=template,
            qual_codes=qual_codes,
            required_qual_code=slot.required_qual_code,
            availability_blocked=availability_blocked,
            fulltime_target=fulltime_target,
            contract_deficit_only=contract_deficit_only,
        )
        if not ranked:
            return False

        chosen = ranked[0]
        if not _post_pass_guard_allows(
            post_pass_guard,
            assignments=result.assignments,
            employee_id=chosen.id,
            assignment_date=slot.assignment_date,
            shift_template_id=shift_id,
            shift_templates=shift_templates,
            employees=employees,
            qual_codes=qual_codes,
        ):
            return False
        shift_hours = template.duration_minutes / 60.0
        is_clinical = (
            is_clinical_floor_pool(slot.role_pool_id)
            or template.code in CLINICAL_FLOOR
        )
        _apply_assignment_to_state(
            states[chosen.id],
            slot.assignment_date,
            shift_id,
            shift_hours,
            rules=rules,
        )
        states[chosen.id].contract_completion_ot_used = True
        result.assignments.append(
            PlannedAssignment(
                employee_id=chosen.id,
                shift_template_id=shift_id,
                assignment_date=slot.assignment_date,
                forced_clinical_ot=is_clinical,
                overtime_compliance_bypassed=True,
            )
        )
        seat_key = (slot.assignment_date, shift_id, slot.required_qual_code)
        fill_counts[seat_key] = fill_counts.get(seat_key, 0) + 1
        if is_smooth_day_balance_pool(slot.role_pool_id):
            filled_smooth_seats.add(
                (slot.assignment_date, shift_id, slot.required_qual_code, slot.seat_index)
            )
        added += 1
        return True

    open_slots = _collect_unassigned_pool_slots(
        expanded_slots,
        fill_counts=fill_counts,
        shift_templates=shift_templates,
        filled_smooth_seats=filled_smooth_seats,
    )

    for slot in sorted(
        open_slots,
        key=lambda item: _aggressor_slot_sort_key(
            item,
            shift_templates=shift_templates,
            weekend_only=True,
        ),
    ):
        _assign_slot(slot, contract_deficit_only=False, weekend_only=True)

    if not allow_contract_and_even_phases:
        return added

    for slot in sorted(
        _collect_unassigned_pool_slots(
            expanded_slots,
            fill_counts=fill_counts,
            shift_templates=shift_templates,
            filled_smooth_seats=filled_smooth_seats,
        ),
        key=lambda item: _aggressor_slot_sort_key(
            item,
            shift_templates=shift_templates,
            weekend_only=False,
        ),
    ):
        _assign_slot(slot, contract_deficit_only=True, weekend_only=False)

    for slot in sorted(
        _collect_unassigned_pool_slots(
            expanded_slots,
            fill_counts=fill_counts,
            shift_templates=shift_templates,
            filled_smooth_seats=filled_smooth_seats,
        ),
        key=lambda item: _aggressor_slot_sort_key(
            item,
            shift_templates=shift_templates,
            weekend_only=False,
        ),
    ):
        _assign_slot(slot, contract_deficit_only=False, weekend_only=False)

    return added


def _skip_pre_template_clinical_lockdown(employees: Sequence[EmployeeProfile]) -> bool:
    """Vacant-only Portage rosters satisfy E/N clinical floor via template + post-stamp lockdown."""

    if not employees:
        return True
    return all(parse_vacant_portage_line(emp.full_name) is not None for emp in employees)


def _vacant_line_weekend_deferred_to_cpsat(
    employee: EmployeeProfile,
    assignment_date: date,
    *,
    post_pass_guard: Optional[PostPassGuard],
) -> bool:
    """Weekend cells on vacant Portage lines are CP-SAT-owned before post-CP-SAT passes."""

    return (
        post_pass_guard is None
        and assignment_date.weekday() >= 5
        and parse_vacant_portage_line(employee.full_name) is not None
    )


def _execute_clinical_safety_first_pass(
    *,
    employees: Sequence[EmployeeProfile],
    states: Dict[str, _EmployeeState],
    expanded_slots: Sequence[ExpandedScheduleSlot],
    shift_templates: Dict[str, ShiftTemplateInfo],
    shift_required_qualifications: Dict[str, Set[str]],
    rules: JurisdictionRules,
    period_start: date,
    period_end: date,
    availability_blocked: Optional[Mapping[str, Set[date]]],
    qual_codes: Mapping[str, str],
    target_hours_map: Mapping[str, float],
    period_target_hours: Mapping[str, float],
    prioritize_coverage: bool,
    clinical_mandatory: bool = False,
) -> List[PlannedAssignment]:
    """
    Mandatory pre-pass: Clinical Seat Lockdown.
    Enforces 2-seat clinical floor requirement on every pass.
    """

    fill_counts: Dict[Tuple[date, str, Optional[str]], int] = {}
    locked = _run_clinical_seat_lockdown_pass(
        employees=employees,
        states=states,
        expanded_slots=expanded_slots,
        shift_templates=shift_templates,
        shift_required_qualifications=shift_required_qualifications,
        rules=rules,
        fill_counts=fill_counts,
        availability_blocked=availability_blocked,
        qual_codes=qual_codes,
        log_critical_gaps=False,
        period_start=period_start,
        period_end=period_end,
    )

    # Hard Enforcement: Check E/N clinical seats on every calendar day.
    for assignment_date in _daterange(period_start, period_end):
        for shift_code in ("EVENING", "NIGHT"):
            filled = clinical_floor_filled_for_day(
                assignment_date,
                shift_code,
                fill_counts=fill_counts,
                expanded_slots=expanded_slots,
                shift_templates=shift_templates,
            )

            if filled != CLINICAL_FLOOR[shift_code]:
                raise ClinicalShortageError(
                    assignment_date=assignment_date,
                    shift_code=shift_code,
                    reason=(
                        f"CRITICAL: Seat lockdown failed. "
                        f"Only {filled} of {CLINICAL_FLOOR[shift_code]} required seats filled."
                    ),
                )
    return locked


def _portage_clinical_safety_first_enabled(
    employees: Sequence[EmployeeProfile],
    active_demands: Optional[Sequence[ShiftConcurrentDemand]],
) -> bool:
    if active_demands is None or tuple(active_demands) != portage_concurrent_demands():
        return False
    contract_lines = {(emp.contract_line_type or "") for emp in employees}
    return "D/E" in contract_lines and "D/N" in contract_lines and len(employees) >= 10


def _can_assign_clinical_floor_slot(
    slot: ExpandedScheduleSlot,
    fill_counts: Mapping[Tuple[date, str, Optional[str]], int],
    *,
    shift_templates: Mapping[str, ShiftTemplateInfo],
    expanded_slots: Sequence[ExpandedScheduleSlot],
) -> bool:
    shift_code = shift_templates[slot.shift_id].code
    if shift_code not in CLINICAL_FLOOR:
        return True
    if shift_code == "MORNING" and not is_clinical_floor_pool(slot.role_pool_id):
        return True
    if shift_code in {"EVENING", "NIGHT"}:
        band_ids = {
            shift_id
            for shift_id, template in shift_templates.items()
            if template.code == shift_code
        }
        total_band = sum(
            fill_counts.get((slot.assignment_date, shift_id, qual_code), 0)
            for shift_id in band_ids
            for qual_code in ("MLT", "MLA")
        )
        if total_band >= CLINICAL_FLOOR[shift_code]:
            return False
    if _slot_already_filled(slot, fill_counts):
        return False
    filled = clinical_floor_filled_for_day(
        slot.assignment_date,
        shift_code,
        fill_counts=fill_counts,
        expanded_slots=expanded_slots,
        shift_templates=shift_templates,
    )
    return filled < CLINICAL_FLOOR[shift_code]


def _is_template_aligned_for_clinical_lock(
    employee: EmployeeProfile,
    *,
    assignment_date: date,
    period_start: date,
    shift_code: str,
    pool_manager: Optional[ElasticPoolManager] = None,
) -> bool:
    return (
        _template_clinical_alignment_penalty(
            employee,
            assignment_date=assignment_date,
            period_start=period_start,
            shift_code=shift_code,
            pool_manager=pool_manager,
        )
        <= -400.0
    )


def _night_shift_lock_pass(
    *,
    employees: Sequence[EmployeeProfile],
    states: Dict[str, _EmployeeState],
    expanded_slots: Sequence[ExpandedScheduleSlot],
    shift_templates: Dict[str, ShiftTemplateInfo],
    shift_required_qualifications: Dict[str, Set[str]],
    rules: JurisdictionRules,
    period_start: date,
    period_end: date,
    availability_blocked: Optional[Mapping[str, Set[date]]],
    qual_codes: Mapping[str, str],
    target_hours_map: Mapping[str, float],
    period_target_hours: Mapping[str, float],
    fill_counts: Dict[Tuple[date, str, Optional[str]], int],
    filled_smooth_seats: Set[Tuple[date, str, Optional[str], int]],
    prioritize_coverage: bool,
) -> List[PlannedAssignment]:
    """Backward-compatible night-only lock wrapper."""

    return _clinical_floor_lock_pass(
        employees=employees,
        states=states,
        expanded_slots=expanded_slots,
        shift_templates=shift_templates,
        shift_required_qualifications=shift_required_qualifications,
        rules=rules,
        period_start=period_start,
        period_end=period_end,
        availability_blocked=availability_blocked,
        qual_codes=qual_codes,
        target_hours_map=target_hours_map,
        period_target_hours=period_target_hours,
        fill_counts=fill_counts,
        filled_smooth_seats=filled_smooth_seats,
        prioritize_coverage=prioritize_coverage,
        shift_codes=("NIGHT",),
        allow_forced_clinical_ot=True,
    )


def _template_clinical_alignment_penalty(
    employee: EmployeeProfile,
    *,
    assignment_date: date,
    period_start: date,
    shift_code: str,
    pool_manager: Optional[ElasticPoolManager] = None,
) -> float:
    if shift_code == "NIGHT":
        return _template_night_alignment_penalty(
            employee,
            assignment_date=assignment_date,
            period_start=period_start,
            pool_manager=pool_manager,
        )
    if shift_code != "EVENING":
        return 0.0
    if (employee.contract_line_type or "") != "D/E":
        return 0.0
    spec = _resolve_master_line_spec(employee, pool_manager)
    if spec is None:
        return 0.0
    week_index = (assignment_date - period_start).days // 7
    token = shift_token_for_day(
        spec,
        week_index=week_index,
        day_of_week=assignment_date.weekday(),
    )
    if token == "E":
        return -500.0
    if token == "D":
        return 300.0
    return 100.0


def _template_night_alignment_penalty(
    employee: EmployeeProfile,
    *,
    assignment_date: date,
    period_start: date,
    pool_manager: Optional[ElasticPoolManager] = None,
) -> float:
    """
    Prefer assigning nights on a D/N worker's template N-block weeks so day weeks
    remain available for contract-hour fulfillment.
    """

    if (employee.contract_line_type or "") != "D/N":
        return 0.0
    spec = _resolve_master_line_spec(employee, pool_manager)
    if spec is None:
        return 0.0
    week_index = (assignment_date - period_start).days // 7
    token = shift_token_for_day(
        spec,
        week_index=week_index,
        day_of_week=assignment_date.weekday(),
    )
    if token == "N":
        return -500.0
    if token == "D":
        return 300.0
    return 100.0


def _backfill_dn_contract_pass(
    *,
    employees: Sequence[EmployeeProfile],
    states: Dict[str, _EmployeeState],
    expanded_slots: Sequence[ExpandedScheduleSlot],
    shift_templates: Dict[str, ShiftTemplateInfo],
    shift_required_qualifications: Dict[str, Set[str]],
    rules: JurisdictionRules,
    period_start: date,
    period_end: date,
    availability_blocked: Optional[Mapping[str, Set[date]]],
    qual_codes: Mapping[str, str],
    fill_counts: Dict[Tuple[date, str, Optional[str]], int],
    fulltime_target: float,
    relax_labor_rules: bool = False,
    single_pass: bool = False,
    shift_codes: Tuple[str, ...] = ("MORNING", "EVENING"),
    load_reference_hours: Optional[Mapping[str, float]] = None,
) -> List[PlannedAssignment]:
    """
    After night lock, reserve day/evening seats for 1.0 FTE D/N lines so template
    routing does not consume the only slots that can complete their payroll hours.
    """

    dn_employees = sorted(
        [
            employee
            for employee in employees
            if employee.fte >= FULLTIME_FTE_THRESHOLD
            and (employee.contract_line_type or "") == "D/N"
        ],
        key=lambda employee: _elastic_load_deficit(
            employee,
            states[employee.id],
            load_reference_hours=load_reference_hours,
            fulltime_target=fulltime_target,
        ),
        reverse=True,
    )
    if not dn_employees:
        return []

    day_slots = sorted(
        (
            slot
            for slot in expanded_slots
            if shift_templates[slot.shift_id].code in shift_codes
        ),
        key=lambda slot: (
            0 if shift_templates[slot.shift_id].code == "EVENING" else 1,
            slot.assignment_date,
            slot.shift_id,
            slot.seat_index,
        ),
    )
    planned: List[PlannedAssignment] = []

    for employee in dn_employees:
        state = states[employee.id]
        emp_qual = infer_qual_code(employee, qual_codes=qual_codes)

        attempt_limit = 1 if single_pass else len(day_slots)
        for _attempt in range(attempt_limit):
            reference_target = (
                load_reference_hours.get(employee.id, fulltime_target)
                if load_reference_hours is not None
                else fulltime_target
            )
            if state.total_hours >= reference_target - 0.25:
                break

            assigned = False
            for slot in day_slots:
                if _slot_already_filled(slot, fill_counts):
                    continue
                if slot.required_qual_code != emp_qual:
                    continue

                shift_id = slot.shift_id
                template = shift_templates[shift_id]
                required = shift_required_qualifications.get(shift_id, set())
                if not employee_matches_seat_qual(
                    employee,
                    slot.required_qual_code,
                    qual_codes=qual_codes,
                    shift_required_qualification_ids=required or None,
                ):
                    continue
                line_violation = validate_contract_line_eligibility(
                    employee.contract_line_type,
                    template.code,
                    qual_code=emp_qual,
                )
                if line_violation:
                    continue
                violation = _would_violate_labor_rules(
                    state,
                    slot.assignment_date,
                    template,
                    shift_templates,
                    rules,
                    period_start,
                    period_end,
                    availability_blocked,
                    relax_dn_contract_completion=relax_labor_rules,
                )
                if violation:
                    continue

                shift_hours = template.duration_minutes / 60.0
                _apply_assignment_to_state(
                    state,
                    slot.assignment_date,
                    shift_id,
                    shift_hours,
                    rules=rules,
                )
                planned.append(
                    PlannedAssignment(
                        employee_id=employee.id,
                        shift_template_id=shift_id,
                        assignment_date=slot.assignment_date,
                    )
                )
                seat_key_count = (slot.assignment_date, shift_id, slot.required_qual_code)
                fill_counts[seat_key_count] = fill_counts.get(seat_key_count, 0) + 1
                assigned = True
                break

            if not assigned:
                break

    return planned


def _mandatory_fulltime_contract_pass(
    *,
    employees: Sequence[EmployeeProfile],
    states: Dict[str, _EmployeeState],
    expanded_slots: Sequence[ExpandedScheduleSlot],
    shift_templates: Dict[str, ShiftTemplateInfo],
    shift_required_qualifications: Dict[str, Set[str]],
    rules: JurisdictionRules,
    period_start: date,
    period_end: date,
    weeks_in_period: int,
    availability_blocked: Optional[Mapping[str, Set[date]]],
    qual_codes: Mapping[str, str],
    fill_counts: Dict[Tuple[date, str, Optional[str]], int],
    filled_smooth_seats: Set[Tuple[date, str, Optional[str], int]],
    prioritize_coverage: bool,
    relax_dn_contract_completion: bool = False,
) -> List[PlannedAssignment]:
    """
    Contract-first routing: fill open seats with 1.0 FTE lines below payroll target
    before any equity or work-life balance distribution.
    """

    fulltime_target = fulltime_period_contract_hours(
        rules=rules,
        weeks_in_period=weeks_in_period,
    )
    if _all_fulltime_at_contract_target(employees, states, fulltime_target=fulltime_target):
        return []

    contract_slots = sorted(
        expanded_slots,
        key=lambda slot: (
            slot.assignment_date,
            autonomous_balance_slot_sort_key(slot),
            slot.seat_index,
        ),
    )
    planned: List[PlannedAssignment] = []

    for _round in range(12):
        if _all_fulltime_at_contract_target(employees, states, fulltime_target=fulltime_target):
            break
        progress = False

        for slot in contract_slots:
            if shift_templates[slot.shift_id].code in {"NIGHT", "EVENING"}:
                continue
            if prioritize_coverage and _slot_already_filled(slot, fill_counts):
                continue
            if is_smooth_day_balance_pool(slot.role_pool_id):
                smooth_key = (
                    slot.assignment_date,
                    slot.shift_id,
                    slot.required_qual_code,
                    slot.seat_index,
                )
                if smooth_key in filled_smooth_seats:
                    continue

            chosen = _pick_mandatory_fulltime_candidate(
                employees=employees,
                states=states,
                slot=slot,
                shift_templates=shift_templates,
                shift_required_qualifications=shift_required_qualifications,
                rules=rules,
                period_start=period_start,
                period_end=period_end,
                availability_blocked=availability_blocked,
                qual_codes=qual_codes,
                fulltime_target=fulltime_target,
                relax_dn_contract_completion=relax_dn_contract_completion,
            )
            if chosen is None:
                continue

            shift_id = slot.shift_id
            template = shift_templates[shift_id]
            shift_hours = template.duration_minutes / 60.0
            state = states[chosen.id]
            _apply_assignment_to_state(state, slot.assignment_date, shift_id, shift_hours)
            planned.append(
                PlannedAssignment(
                    employee_id=chosen.id,
                    shift_template_id=shift_id,
                    assignment_date=slot.assignment_date,
                )
            )
            if is_smooth_day_balance_pool(slot.role_pool_id):
                filled_smooth_seats.add(
                    (slot.assignment_date, shift_id, slot.required_qual_code, slot.seat_index)
                )
            seat_key_count = (slot.assignment_date, shift_id, slot.required_qual_code)
            fill_counts[seat_key_count] = fill_counts.get(seat_key_count, 0) + 1
            progress = True

        if not progress:
            break

    return planned


def _remove_assignment_from_state(
    state: _EmployeeState,
    assignment_date: date,
    shift_id: str,
    shift_hours: float,
) -> None:
    state.work_dates.discard(assignment_date)
    state.assignment_records = [
        (day, template_id)
        for day, template_id in state.assignment_records
        if not (day == assignment_date and template_id == shift_id)
    ]
    state.total_hours -= shift_hours
    week_start = workweek_for(assignment_date).start
    if week_start in state.week_hours:
        state.week_hours[week_start] -= shift_hours
        if state.week_hours[week_start] < 1e-9:
            del state.week_hours[week_start]


def _supersede_vacant_clinical_day_assignment(
    *,
    employee: EmployeeProfile,
    assignment_date: date,
    new_shift_id: str,
    assignments: List[PlannedAssignment],
    states: Dict[str, _EmployeeState],
    shift_templates: Dict[str, ShiftTemplateInfo],
    fill_counts: Dict[Tuple[date, str, Optional[str]], int],
    qual_codes: Mapping[str, str],
    allow_frozen_supersede: bool = False,
    period_start: Optional[date] = None,
) -> bool:
    """Replace a vacant line's same-day non-E/N assignment when filling clinical E/N seats."""

    from lab_scheduler.scheduling.clinical_seats import vacant_may_supersede_for_clinical_band

    state = states[employee.id]
    target_code = shift_templates[new_shift_id].code
    if not vacant_may_supersede_for_clinical_band(
        profile=employee,
        assignment_date=assignment_date,
        target_shift_code=target_code,
        state=state,
        shift_templates=shift_templates,
        period_start=period_start,
    ):
        return False

    emp_qual = infer_qual_code(employee, qual_codes=qual_codes)
    for index in range(len(assignments) - 1, -1, -1):
        assignment = assignments[index]
        if assignment.employee_id != employee.id or assignment.assignment_date != assignment_date:
            continue
        if getattr(assignment, "master_template_frozen", False) and not allow_frozen_supersede:
            return False
        old_shift_id = assignment.shift_template_id
        if old_shift_id == new_shift_id:
            return False
        old_template = shift_templates.get(old_shift_id)
        if old_template is None:
            return False
        old_hours = old_template.duration_minutes / 60.0
        old_key = (assignment_date, old_shift_id, emp_qual)
        if fill_counts.get(old_key, 0) > 0:
            fill_counts[old_key] -= 1
        _remove_assignment_from_state(state, assignment_date, old_shift_id, old_hours)
        del assignments[index]
        return True
    return False


def _reassign_parttime_shifts_to_fulltime_contract(
    *,
    employees: Sequence[EmployeeProfile],
    states: Dict[str, _EmployeeState],
    assignments: List[PlannedAssignment],
    shift_templates: Dict[str, ShiftTemplateInfo],
    shift_required_qualifications: Dict[str, Set[str]],
    rules: JurisdictionRules,
    period_start: date,
    period_end: date,
    availability_blocked: Optional[Mapping[str, Set[date]]],
    qual_codes: Mapping[str, str],
    fulltime_target: float,
    load_reference_hours: Optional[Mapping[str, float]] = None,
    payroll_targets: Mapping[str, float] | None = None,
    catalog_targets: Mapping[str, float] | None = None,
) -> int:
    """Move compatible part-time shifts onto FT lines still below contract target."""

    part_time_ids = {
        employee.id
        for employee in employees
        if employee.fte < FULLTIME_FTE_THRESHOLD
        and not _vacant_master_fulltime_line(employee)
    }
    employee_by_id = {employee.id: employee for employee in employees}
    moved = 0

    def _fulltime_needs_hours(employee: EmployeeProfile) -> bool:
        total_hours = states[employee.id].total_hours
        if (
            payroll_targets is not None
            and catalog_targets is not None
            and _is_fulltime_vacant_master_line(employee, payroll_targets)
        ):
            return _has_contract_finalize_deficit(
                employee,
                total_hours,
                payroll_targets=payroll_targets,
                catalog_targets=catalog_targets,
            )
        return is_fulltime_contract_deficit(
            employee,
            total_hours,
            fulltime_target=fulltime_target,
        )

    under_fulltime = sorted(
        (
            employee
            for employee in employees
            if _fulltime_needs_hours(employee)
        ),
        key=lambda employee: _elastic_load_deficit(
            employee,
            states[employee.id],
            load_reference_hours=load_reference_hours,
            fulltime_target=fulltime_target,
        ),
        reverse=True,
    )

    for ft_employee in under_fulltime:
        ft_state = states[ft_employee.id]
        while _fulltime_needs_hours(ft_employee):
            claimed = False
            for index, planned in enumerate(assignments):
                if planned.employee_id not in part_time_ids:
                    continue
                if getattr(planned, "master_template_frozen", False):
                    continue

                template = shift_templates[planned.shift_template_id]
                required = shift_required_qualifications.get(planned.shift_template_id, set())
                if not _is_qualified(ft_employee, required):
                    continue

                emp_qual = infer_qual_code(ft_employee, qual_codes=qual_codes)
                line_violation = validate_contract_line_eligibility(
                    ft_employee.contract_line_type,
                    template.code,
                    qual_code=emp_qual,
                )
                if line_violation:
                    continue

                violation = _would_violate_labor_rules(
                    ft_state,
                    planned.assignment_date,
                    template,
                    shift_templates,
                    rules,
                    period_start,
                    period_end,
                    availability_blocked,
                )
                if violation:
                    continue

                pt_state = states[planned.employee_id]
                shift_hours = template.duration_minutes / 60.0
                _remove_assignment_from_state(
                    pt_state,
                    planned.assignment_date,
                    planned.shift_template_id,
                    shift_hours,
                )
                _apply_assignment_to_state(
                    ft_state,
                    planned.assignment_date,
                    planned.shift_template_id,
                    shift_hours,
                )
                assignments[index] = PlannedAssignment(
                    employee_id=ft_employee.id,
                    shift_template_id=planned.shift_template_id,
                    assignment_date=planned.assignment_date,
                )
                moved += 1
                claimed = True
                break

            if not claimed:
                break

    return moved


def _reassign_loaded_fulltime_shifts_to_contract_deficit(
    *,
    employees: Sequence[EmployeeProfile],
    states: Dict[str, _EmployeeState],
    assignments: List[PlannedAssignment],
    shift_templates: Dict[str, ShiftTemplateInfo],
    shift_required_qualifications: Dict[str, Set[str]],
    rules: JurisdictionRules,
    period_start: date,
    period_end: date,
    availability_blocked: Optional[Mapping[str, Set[date]]],
    qual_codes: Mapping[str, str],
    fulltime_target: float,
    load_reference_hours: Optional[Mapping[str, float]] = None,
) -> int:
    """Move one shift from a loaded 1.0 FTE line onto a deficit line, then backfill the donor."""

    employee_by_id = {employee.id: employee for employee in employees}
    moved = 0

    under_fulltime = sorted(
        (
            employee
            for employee in employees
            if is_fulltime_contract_deficit(
                employee,
                states[employee.id].total_hours,
                fulltime_target=fulltime_target,
            )
        ),
        key=lambda employee: _elastic_load_deficit(
            employee,
            states[employee.id],
            load_reference_hours=load_reference_hours,
            fulltime_target=fulltime_target,
        ),
        reverse=True,
    )

    for ft_employee in under_fulltime:
        ft_state = states[ft_employee.id]
        while is_fulltime_contract_deficit(
            ft_employee,
            ft_state.total_hours,
            fulltime_target=fulltime_target,
        ):
            claimed = False
            donor_candidates: List[Tuple[int, PlannedAssignment]] = []
            for index, planned in enumerate(assignments):
                donor = employee_by_id.get(planned.employee_id)
                if donor is None or donor.id == ft_employee.id:
                    continue
                if infer_qual_code(donor, qual_codes=qual_codes) != infer_qual_code(
                    ft_employee,
                    qual_codes=qual_codes,
                ):
                    continue
                if getattr(planned, "master_template_frozen", False):
                    continue

                donor_state = states[donor.id]
                template = shift_templates[planned.shift_template_id]
                shift_hours = template.duration_minutes / 60.0
                if donor_state.total_hours <= ft_state.total_hours + 0.25:
                    continue

                required = shift_required_qualifications.get(planned.shift_template_id, set())
                if not _is_qualified(ft_employee, required):
                    continue

                emp_qual = infer_qual_code(ft_employee, qual_codes=qual_codes)
                line_violation = validate_contract_line_eligibility(
                    ft_employee.contract_line_type,
                    template.code,
                    qual_code=emp_qual,
                )
                if line_violation:
                    continue

                violation = _would_violate_labor_rules(
                    ft_state,
                    planned.assignment_date,
                    template,
                    shift_templates,
                    rules,
                    period_start,
                    period_end,
                    availability_blocked,
                )
                if violation:
                    continue
                donor_candidates.append((index, planned))

            donor_candidates.sort(
                key=lambda item: states[item[1].employee_id].total_hours,
                reverse=True,
            )
            for index, planned in donor_candidates:
                donor = employee_by_id[planned.employee_id]
                donor_state = states[donor.id]
                template = shift_templates[planned.shift_template_id]
                shift_hours = template.duration_minutes / 60.0
                _remove_assignment_from_state(
                    donor_state,
                    planned.assignment_date,
                    planned.shift_template_id,
                    shift_hours,
                )
                _apply_assignment_to_state(
                    ft_state,
                    planned.assignment_date,
                    planned.shift_template_id,
                    shift_hours,
                )
                assignments[index] = PlannedAssignment(
                    employee_id=ft_employee.id,
                    shift_template_id=planned.shift_template_id,
                    assignment_date=planned.assignment_date,
                )
                moved += 1
                claimed = True
                break

            if not claimed:
                break

    return moved


def _tail_repair_fulltime_contract(
    *,
    employees: Sequence[EmployeeProfile],
    states: Dict[str, _EmployeeState],
    assignments: List[PlannedAssignment],
    expanded_slots: Sequence[ExpandedScheduleSlot],
    shift_templates: Dict[str, ShiftTemplateInfo],
    shift_required_qualifications: Dict[str, Set[str]],
    rules: JurisdictionRules,
    period_start: date,
    period_end: date,
    availability_blocked: Optional[Mapping[str, Set[date]]],
    qual_codes: Mapping[str, str],
    fill_counts: Dict[Tuple[date, str, Optional[str]], int],
    filled_smooth_seats: Set[Tuple[date, str, Optional[str], int]],
    prioritize_coverage: bool,
    fulltime_target: float,
    load_reference_hours: Optional[Mapping[str, float]] = None,
) -> int:
    """
    Drop one blocking shift and reclaim legal open seats to close the last contract gap.
    """

    repairs = 0
    under_fulltime = sorted(
        (
            employee
            for employee in employees
            if is_fulltime_contract_deficit(
                employee,
                states[employee.id].total_hours,
                fulltime_target=fulltime_target,
            )
        ),
        key=lambda employee: _elastic_load_deficit(
            employee,
            states[employee.id],
            load_reference_hours=load_reference_hours,
            fulltime_target=fulltime_target,
        ),
        reverse=True,
    )

    for ft_employee in under_fulltime:
        ft_state = states[ft_employee.id]
        if not is_fulltime_contract_deficit(
            ft_employee,
            ft_state.total_hours,
            fulltime_target=fulltime_target,
        ):
            continue

        repaired = False
        for remove_index, remove_plan in list(enumerate(assignments)):
            if remove_plan.employee_id != ft_employee.id:
                continue

            remove_template = shift_templates[remove_plan.shift_template_id]
            if remove_template.code in {"EVENING", "NIGHT"}:
                continue
            remove_hours = remove_template.duration_minutes / 60.0
            hours_before = ft_state.total_hours
            _remove_assignment_from_state(
                ft_state,
                remove_plan.assignment_date,
                remove_plan.shift_template_id,
                remove_hours,
            )
            del assignments[remove_index]
            fill_counts.clear()
            fill_counts.update(_seat_fill_counts(assignments, employees, qual_codes))

            added: List[PlannedAssignment] = []
            while is_fulltime_contract_deficit(
                ft_employee,
                ft_state.total_hours,
                fulltime_target=fulltime_target,
            ):
                claimed_slot = False
                for slot in expanded_slots:
                    if prioritize_coverage and _slot_already_filled(slot, fill_counts):
                        continue

                    candidate = _pick_mandatory_fulltime_candidate(
                        employees=[ft_employee],
                        states=states,
                        slot=slot,
                        shift_templates=shift_templates,
                        shift_required_qualifications=shift_required_qualifications,
                        rules=rules,
                        period_start=period_start,
                        period_end=period_end,
                        availability_blocked=availability_blocked,
                        qual_codes=qual_codes,
                        fulltime_target=fulltime_target,
                        relax_dn_contract_completion=(
                            (ft_employee.contract_line_type or "") == "D/N"
                        ),
                    )
                    if candidate is None:
                        continue

                    shift_id = slot.shift_id
                    template = shift_templates[shift_id]
                    shift_hours = template.duration_minutes / 60.0
                    _apply_assignment_to_state(
                        ft_state,
                        slot.assignment_date,
                        shift_id,
                        shift_hours,
                    )
                    planned = PlannedAssignment(
                        employee_id=ft_employee.id,
                        shift_template_id=shift_id,
                        assignment_date=slot.assignment_date,
                    )
                    assignments.append(planned)
                    added.append(planned)
                    if is_smooth_day_balance_pool(slot.role_pool_id):
                        filled_smooth_seats.add(
                            (
                                slot.assignment_date,
                                shift_id,
                                slot.required_qual_code,
                                slot.seat_index,
                            )
                        )
                    seat_key_count = (slot.assignment_date, shift_id, slot.required_qual_code)
                    fill_counts[seat_key_count] = fill_counts.get(seat_key_count, 0) + 1
                    claimed_slot = True
                    break

                if not claimed_slot:
                    break

            if (
                not is_fulltime_contract_deficit(
                    ft_employee,
                    ft_state.total_hours,
                    fulltime_target=fulltime_target,
                )
                and ft_state.total_hours + 1e-9 >= hours_before
            ):
                repairs += 1 + len(added)
                repaired = True
                break

            for planned in reversed(added):
                template = shift_templates[planned.shift_template_id]
                shift_hours = template.duration_minutes / 60.0
                _remove_assignment_from_state(
                    ft_state,
                    planned.assignment_date,
                    planned.shift_template_id,
                    shift_hours,
                )
                assignments.remove(planned)

            assignments.insert(remove_index, remove_plan)
            _apply_assignment_to_state(
                ft_state,
                remove_plan.assignment_date,
                remove_plan.shift_template_id,
                remove_hours,
            )
            fill_counts.clear()
            fill_counts.update(_seat_fill_counts(assignments, employees, qual_codes))

        if repaired and is_fulltime_contract_deficit(
            ft_employee,
            ft_state.total_hours,
            fulltime_target=fulltime_target,
        ):
            repairs += _tail_repair_fulltime_contract(
                employees=employees,
                states=states,
                assignments=assignments,
                expanded_slots=expanded_slots,
                shift_templates=shift_templates,
                shift_required_qualifications=shift_required_qualifications,
                rules=rules,
                period_start=period_start,
                period_end=period_end,
                availability_blocked=availability_blocked,
                qual_codes=qual_codes,
                fill_counts=fill_counts,
                filled_smooth_seats=filled_smooth_seats,
                prioritize_coverage=prioritize_coverage,
                fulltime_target=fulltime_target,
            )

    return repairs


def _mandatory_fulltime_stalled(
    *,
    employees: Sequence[EmployeeProfile],
    states: Mapping[str, _EmployeeState],
    expanded_slots: Sequence[ExpandedScheduleSlot],
    shift_templates: Dict[str, ShiftTemplateInfo],
    shift_required_qualifications: Dict[str, Set[str]],
    rules: JurisdictionRules,
    period_start: date,
    period_end: date,
    weeks_in_period: int,
    availability_blocked: Optional[Mapping[str, Set[date]]],
    qual_codes: Mapping[str, str],
    fill_counts: Dict[Tuple[date, str, Optional[str]], int],
    filled_smooth_seats: Set[Tuple[date, str, Optional[str], int]],
    prioritize_coverage: bool,
    fulltime_target: float,
) -> bool:
    if _all_fulltime_at_contract_target(employees, states, fulltime_target=fulltime_target):
        return True

    for slot in expanded_slots:
        if prioritize_coverage and _slot_already_filled(slot, fill_counts):
            continue
        if is_smooth_day_balance_pool(slot.role_pool_id):
            smooth_key = (
                slot.assignment_date,
                slot.shift_id,
                slot.required_qual_code,
                slot.seat_index,
            )
            if smooth_key in filled_smooth_seats:
                continue
        if (
            _pick_mandatory_fulltime_candidate(
                employees=employees,
                states=states,
                slot=slot,
                shift_templates=shift_templates,
                shift_required_qualifications=shift_required_qualifications,
                rules=rules,
                period_start=period_start,
                period_end=period_end,
                availability_blocked=availability_blocked,
                qual_codes=qual_codes,
                fulltime_target=fulltime_target,
            )
            is not None
        ):
            return False
    return True


def _preassign_smooth_balance_slots(
    *,
    employees: Sequence[EmployeeProfile],
    states: Dict[str, _EmployeeState],
    expanded_slots: Sequence[ExpandedScheduleSlot],
    shift_templates: Dict[str, ShiftTemplateInfo],
    shift_required_qualifications: Dict[str, Set[str]],
    rules: JurisdictionRules,
    period_start: date,
    period_end: date,
    availability_blocked: Optional[Mapping[str, Set[date]]],
    qual_codes: Mapping[str, str],
    target_hours_map: Mapping[str, float],
    period_target_hours: Mapping[str, float],
    filled_smooth_seats: Optional[Set[Tuple[date, str, Optional[str], int]]] = None,
    weekday_staffing_plan: Optional[WeekdayDailyStaffingPlan] = None,
    frozen_master_cells: Optional[Set[Tuple[str, date]]] = None,
) -> List[PlannedAssignment]:
    """Reserve smooth day-balance seats for under-target 1.0 FTE lines before template laydown."""

    smooth_slots = sorted(
        (slot for slot in expanded_slots if is_smooth_day_balance_pool(slot.role_pool_id)),
        key=autonomous_balance_slot_sort_key,
    )
    if not smooth_slots:
        return []

    planned: List[PlannedAssignment] = []
    for slot in smooth_slots:
        if _slot_blocked_by_weekend_cap(
            slot,
            states=states,
            employees=employees,
            qual_codes=qual_codes,
            shift_templates=shift_templates,
        ):
            continue
        shift_id = slot.shift_id
        template = shift_templates[shift_id]
        required = shift_required_qualifications.get(shift_id, set())
        ranked, _, _ = _rank_slot_candidates(
            employees=employees,
            required=required,
            states=states,
            assignment_date=slot.assignment_date,
            shift_id=shift_id,
            template=template,
            shift_templates=shift_templates,
            rules=rules,
            period_start=period_start,
            period_end=period_end,
            availability_blocked=availability_blocked,
            prioritize_coverage=True,
            period_target_hours=period_target_hours,
            required_qual_code=slot.required_qual_code,
            qual_codes=qual_codes,
                    employee_target_hours=target_hours_map,
                    role_pool_id=slot.role_pool_id,
                    weekday_staffing_plan=weekday_staffing_plan,
                )
        if not ranked:
            continue

        ranked = [
            candidate
            for candidate in ranked
            if parse_vacant_portage_line(candidate.full_name) is None
        ]
        if not ranked:
            continue

        chosen = ranked[0]
        if _is_frozen_master_cell(chosen.id, slot.assignment_date, frozen_master_cells):
            continue
        state = states[chosen.id]
        week_start = workweek_for(slot.assignment_date).start
        shift_hours = template.duration_minutes / 60.0
        state.work_dates.add(slot.assignment_date)
        state.assignment_records.append((slot.assignment_date, shift_id))
        state.total_hours += shift_hours
        state.week_hours[week_start] = state.week_hours.get(week_start, 0.0) + shift_hours
        planned.append(
            PlannedAssignment(
                employee_id=chosen.id,
                shift_template_id=shift_id,
                assignment_date=slot.assignment_date,
            )
        )
        if filled_smooth_seats is not None:
            filled_smooth_seats.add(
                (slot.assignment_date, shift_id, slot.required_qual_code, slot.seat_index)
            )
    return planned


def _seat_fill_counts(
    assignments: Sequence[PlannedAssignment],
    employees: Sequence[EmployeeProfile],
    qual_codes: Mapping[str, str],
) -> Dict[Tuple[date, str, Optional[str]], int]:
    from collections import defaultdict

    emp_by_id = {emp.id: emp for emp in employees}
    counts: Dict[Tuple[date, str, Optional[str]], int] = defaultdict(int)
    for assignment in assignments:
        emp = emp_by_id.get(assignment.employee_id)
        if emp is None:
            continue
        qual = infer_qual_code(emp, qual_codes=qual_codes)
        counts[(assignment.assignment_date, assignment.shift_template_id, qual)] += 1
    return counts


def _slot_already_filled(
    slot: ExpandedScheduleSlot,
    fill_counts: Mapping[Tuple[date, str, Optional[str]], int],
    *,
    shift_templates: Optional[Mapping[str, ShiftTemplateInfo]] = None,
) -> bool:
    key = (slot.assignment_date, slot.shift_id, slot.required_qual_code)
    if fill_counts.get(key, 0) > slot.seat_index:
        return True
    return weekend_morning_slot_credited_as_filled(
        slot,
        fill_counts,
        shift_templates=shift_templates,
    )


def _slot_required_for_coverage_gate(
    slot: ExpandedScheduleSlot,
    shift_templates: Optional[Mapping[str, ShiftTemplateInfo]] = None,
) -> bool:
    if is_optional_supplemental_coverage_slot(slot):
        return False
    if shift_templates is not None:
        template = shift_templates.get(slot.shift_id)
        if template is not None and not is_operational_shift_template(template):
            return False
    return True


def _update_slot_fill_metrics(
    result: AutoGenerateResult,
    *,
    expanded_slots: Sequence[ExpandedScheduleSlot],
    fill_counts: Mapping[Tuple[date, str, Optional[str]], int],
    shift_templates: Optional[Mapping[str, ShiftTemplateInfo]] = None,
) -> None:
    result.slots_total = len(expanded_slots)
    result.slots_filled = sum(
        1
        for slot in expanded_slots
        if _slot_already_filled(slot, fill_counts, shift_templates=shift_templates)
    )
    required_slots = [
        slot
        for slot in expanded_slots
        if _slot_required_for_coverage_gate(slot, shift_templates)
    ]
    result.required_slots_total = len(required_slots)
    result.required_slots_filled = sum(
        1
        for slot in required_slots
        if _slot_already_filled(slot, fill_counts, shift_templates=shift_templates)
    )


def _required_coverage_slots_satisfied(
    *,
    expanded_slots: Sequence[ExpandedScheduleSlot],
    fill_counts: Mapping[Tuple[date, str, Optional[str]], int],
    shift_templates: Mapping[str, ShiftTemplateInfo],
) -> bool:
    for slot in expanded_slots:
        if not _slot_required_for_coverage_gate(slot, shift_templates):
            continue
        if not _slot_already_filled(slot, fill_counts, shift_templates=shift_templates):
            return False
    return True


def _trim_assignment_would_unfill_required_slot(
    assignments: Sequence[PlannedAssignment],
    index: int,
    *,
    employees: Sequence[EmployeeProfile],
    expanded_slots: Sequence[ExpandedScheduleSlot],
    shift_templates: Mapping[str, ShiftTemplateInfo],
    qual_codes: Mapping[str, str],
) -> bool:
    """True when removing this assignment would leave a required demand seat open."""

    emp_by_id = {employee.id: employee for employee in employees}
    assignment = assignments[index]
    employee = emp_by_id.get(assignment.employee_id)
    if employee is None:
        return False
    qual = infer_qual_code(employee, qual_codes=qual_codes)
    fill_counts = dict(_seat_fill_counts(assignments, employees, qual_codes))
    seat_key = (assignment.assignment_date, assignment.shift_template_id, qual)
    if fill_counts.get(seat_key, 0) <= 0:
        return False
    fill_counts[seat_key] -= 1
    for slot in expanded_slots:
        if not _slot_required_for_coverage_gate(slot, shift_templates):
            continue
        if (
            slot.assignment_date,
            slot.shift_id,
            slot.required_qual_code,
        ) != seat_key:
            continue
        if not _slot_already_filled(slot, fill_counts, shift_templates=shift_templates):
            return True
    return False


def _compute_required_slot_fill_from_assignments(
    assignments: Sequence[PlannedAssignment],
    employees: Sequence[EmployeeProfile],
    expanded_slots: Sequence[ExpandedScheduleSlot],
    shift_templates: Mapping[str, ShiftTemplateInfo],
    qual_codes: Mapping[str, str],
) -> Tuple[int, int]:
    fill_counts = _seat_fill_counts(assignments, employees, qual_codes)
    required_slots = [
        slot
        for slot in expanded_slots
        if _slot_required_for_coverage_gate(slot, shift_templates)
    ]
    filled = sum(
        1
        for slot in required_slots
        if _slot_already_filled(slot, fill_counts, shift_templates=shift_templates)
    )
    return filled, len(required_slots)


def _daily_pool_band_count(
    assignments: Sequence[PlannedAssignment],
    shift_templates: Mapping[str, ShiftTemplateInfo],
    *,
    assignment_date: date,
    band: str,
) -> int:
    """Count pool-wide Evening (E) or Night (N) assignments on one calendar day."""

    return sum(
        1
        for assignment in assignments
        if assignment.assignment_date == assignment_date
        and _assignment_band(assignment, shift_templates) == band
    )


def _portage_daily_band_cap_allows_delta(
    assignments: Sequence[PlannedAssignment],
    shift_templates: Mapping[str, ShiftTemplateInfo],
    *,
    assignment_date: date,
    band: str,
    delta: int,
) -> bool:
    cap = CLINICAL_FLOOR.get("EVENING" if band == "E" else "NIGHT", 2)
    return _daily_pool_band_count(
        assignments, shift_templates, assignment_date=assignment_date, band=band
    ) + delta <= cap


def _trim_clinical_band_overfill(
    assignments: List[PlannedAssignment],
    states: Dict[str, _EmployeeState],
    *,
    employees: Sequence[EmployeeProfile],
    shift_templates: Dict[str, ShiftTemplateInfo],
    qual_codes: Mapping[str, str],
    period_start: date,
    period_end: date,
    fulltime_target: float = 0.0,
    catalog_targets: Optional[Mapping[str, float]] = None,
    allow_trim_frozen: bool = False,
) -> int:
    """Remove duplicate Evening/Night assignments down to the two-seat operational floor."""

    removed = 0
    demoted = 0
    emp_by_id = {employee.id: employee for employee in employees}
    day_shift_id = _shift_id_for_code("MORNING", shift_templates)
    day_template = (
        shift_templates.get(day_shift_id) if day_shift_id is not None else None
    )
    day_hours = (
        day_template.duration_minutes / 60.0 if day_template is not None else 0.0
    )

    def _has_contract_deficit(employee_id: str) -> bool:
        employee = emp_by_id.get(employee_id)
        if employee is None:
            return False
        if catalog_targets:
            return _has_catalog_contract_deficit(
                employee,
                states[employee_id].total_hours,
                catalog_targets,
            )
        if fulltime_target <= 0.0:
            return False
        return is_fulltime_contract_deficit(
            employee,
            states[employee_id].total_hours,
            fulltime_target=fulltime_target,
        )

    target_seats = CLINICAL_FLOOR
    for shift_code in ("EVENING", "NIGHT"):
        shift_id = _shift_id_for_code(shift_code, shift_templates)
        if shift_id is None:
            continue
        template = shift_templates[shift_id]
        shift_hours = template.duration_minutes / 60.0
        for assignment_date in _daterange(period_start, period_end):
            indices = [
                index
                for index, assignment in enumerate(assignments)
                if assignment.assignment_date == assignment_date
                and assignment.shift_template_id == shift_id
            ]
            if len(indices) <= target_seats[shift_code]:
                continue

            keep_indices: List[int] = []
            quals_kept = {"MLT": 0, "MLA": 0}
            ranked_indices = sorted(
                indices,
                key=lambda index: (
                    0 if _has_contract_deficit(assignments[index].employee_id) else 1,
                    0 if getattr(assignments[index], "master_template_frozen", False) else 1,
                    0 if assignments[index].forced_clinical_ot else 1,
                    index,
                ),
            )
            for index in ranked_indices:
                if len(keep_indices) >= target_seats[shift_code]:
                    break
                assignment = assignments[index]
                employee = emp_by_id.get(assignment.employee_id)
                qual_code = infer_qual_code(employee, qual_codes=qual_codes) if employee else "MLT"
                if quals_kept.get(qual_code, 0) >= 1:
                    continue
                keep_indices.append(index)
                quals_kept[qual_code] = quals_kept.get(qual_code, 0) + 1

            removable = [
                candidate for candidate in indices if candidate not in keep_indices
            ]
            surplus_removable = [
                candidate
                for candidate in removable
                if not _has_contract_deficit(assignments[candidate].employee_id)
            ]
            remove_pool = surplus_removable if surplus_removable else removable
            pool_alt_counts = {
                assignments[candidate].employee_id: _peer_shift_metrics(
                    assignments[candidate].employee_id,
                    assignments,
                    shift_templates,
                    emp_by_id[assignments[candidate].employee_id].contract_line_type,
                    period_start,
                    period_end,
                )[0]
                for candidate in remove_pool
                if assignments[candidate].employee_id in emp_by_id
            }
            median_alt = 0
            if pool_alt_counts:
                sorted_alts = sorted(pool_alt_counts.values())
                median_alt = sorted_alts[len(sorted_alts) // 2]
            ordered_removals = sorted(
                remove_pool,
                key=lambda candidate: (
                    0
                    if pool_alt_counts.get(assignments[candidate].employee_id, 0) > median_alt
                    else 1,
                    1 if _has_contract_deficit(assignments[candidate].employee_id) else 0,
                    1 if getattr(assignments[candidate], "master_template_frozen", False) else 0,
                    -states[assignments[candidate].employee_id].total_hours,
                    candidate,
                ),
            )
            for index in sorted(ordered_removals, reverse=True):
                assignment = assignments[index]
                is_frozen = getattr(assignment, "master_template_frozen", False)
                if is_frozen and not allow_trim_frozen:
                    continue
                employee = emp_by_id.get(assignment.employee_id)
                if (
                    is_frozen
                    and allow_trim_frozen
                    and day_shift_id is not None
                    and day_template is not None
                    and employee is not None
                    and validate_contract_line_eligibility(
                        employee.contract_line_type,
                        day_template.code,
                        qual_code=infer_qual_code(employee, qual_codes=qual_codes),
                    )
                    is None
                ):
                    _remove_assignment_from_state(
                        states[assignment.employee_id],
                        assignment.assignment_date,
                        shift_id,
                        shift_hours,
                    )
                    _apply_assignment_to_state(
                        states[assignment.employee_id],
                        assignment.assignment_date,
                        day_shift_id,
                        day_hours,
                    )
                    assignments[index] = PlannedAssignment(
                        employee_id=assignment.employee_id,
                        shift_template_id=day_shift_id,
                        assignment_date=assignment.assignment_date,
                        master_template_frozen=True,
                    )
                    demoted += 1
                    continue
                _remove_assignment_from_state(
                    states[assignment.employee_id],
                    assignment.assignment_date,
                    shift_id,
                    shift_hours,
                )
                del assignments[index]
                removed += 1
    return removed + demoted


def _enforce_portage_operational_band_caps(
    assignments: List[PlannedAssignment],
    states: Dict[str, _EmployeeState],
    *,
    employees: Sequence[EmployeeProfile],
    shift_templates: Dict[str, ShiftTemplateInfo],
    qual_codes: Mapping[str, str],
    period_start: date,
    period_end: date,
    fulltime_target: float = 0.0,
    catalog_targets: Optional[Mapping[str, float]] = None,
) -> int:
    """Hard 2E/2N daily caps — runs after peer equity and may demote frozen template cells."""

    return _trim_clinical_band_overfill(
        assignments,
        states,
        employees=employees,
        shift_templates=shift_templates,
        qual_codes=qual_codes,
        period_start=period_start,
        period_end=period_end,
        fulltime_target=fulltime_target,
        catalog_targets=catalog_targets,
        allow_trim_frozen=True,
    )






def _weekend_paired_date(assignment_date: date) -> Optional[date]:
    if assignment_date.weekday() == 5:
        return assignment_date + timedelta(days=1)
    if assignment_date.weekday() == 6:
        return assignment_date - timedelta(days=1)
    return None


def _portage_split_weekend_orphan_count(
    assignments: Sequence[PlannedAssignment],
    *,
    employees: Sequence[EmployeeProfile],
    period_start: date,
    period_end: date,
) -> int:
    """Employees working exactly one of Sat/Sun in the same weekend block."""

    employee_ids = {employee.id for employee in employees}
    orphans = 0
    for saturday in _daterange(period_start, period_end):
        if saturday.weekday() != 5:
            continue
        sunday = saturday + timedelta(days=1)
        if sunday > period_end:
            continue
        for employee_id in employee_ids:
            sat_worked = (
                _employee_assignment_on_date(
                    assignments,
                    employee_id=employee_id,
                    assignment_date=saturday,
                )
                is not None
            )
            sun_worked = (
                _employee_assignment_on_date(
                    assignments,
                    employee_id=employee_id,
                    assignment_date=sunday,
                )
                is not None
            )
            if sat_worked != sun_worked:
                orphans += 1
    return orphans


def _can_assign_with_weekend_pairing(
    state: _EmployeeState,
    employee: EmployeeProfile,
    assignment_date: date,
    template: ShiftTemplateInfo,
    *,
    shift_templates: Dict[str, ShiftTemplateInfo],
    rules: JurisdictionRules,
    period_start: date,
    period_end: date,
    availability_blocked: Optional[Mapping[str, Set[date]]],
    qual_codes: Mapping[str, str],
) -> bool:
    """
    Portage default: do not start a one-day-only weekend unless the paired day
    can take the same shift under labor and contract rules.
    """

    paired_date = _weekend_paired_date(assignment_date)
    if paired_date is None:
        return True
    if paired_date in state.work_dates:
        return True
    if (
        availability_blocked
        and paired_date in availability_blocked.get(employee.id, set())
    ):
        return False
    if validate_contract_line_eligibility(
        employee.contract_line_type,
        template.code,
        qual_code=infer_qual_code(employee, qual_codes=qual_codes),
    ):
        return False
    return (
        _would_violate_labor_rules(
            state,
            paired_date,
            template,
            shift_templates,
            rules,
            period_start,
            period_end,
            availability_blocked,
            enforce_fte_target=False,
            relax_dn_contract_completion=True,
        )
        is None
    )


def _employee_assignment_on_date(
    assignments: Sequence[PlannedAssignment],
    *,
    employee_id: str,
    assignment_date: date,
) -> Optional[PlannedAssignment]:
    for assignment in assignments:
        if assignment.employee_id == employee_id and assignment.assignment_date == assignment_date:
            return assignment
    return None


def _weekend_mirror_template_for_employee(
    employee: EmployeeProfile,
    mirror_date: date,
    anchor_template: ShiftTemplateInfo,
    *,
    period_start: date,
    shift_templates: Dict[str, ShiftTemplateInfo],
) -> ShiftTemplateInfo:
    """Prefer catalog Sat/Sun token for vacant lines; else mirror the anchor band."""

    from lab_scheduler.scheduling.portage_template import (
        parse_vacant_portage_line,
        vacant_master_scheduled_shift_code,
    )

    if parse_vacant_portage_line(employee.full_name) is not None:
        expected_code = vacant_master_scheduled_shift_code(
            employee,
            mirror_date,
            period_start,
        )
        if expected_code is not None:
            shift_id = _shift_id_for_code(expected_code, shift_templates)
            if shift_id is not None:
                catalog_template = shift_templates.get(shift_id)
                if catalog_template is not None:
                    return catalog_template
    return anchor_template


def _weekend_mirror_assignment_feasible(
    state: _EmployeeState,
    employee: EmployeeProfile,
    mirror_date: date,
    template: ShiftTemplateInfo,
    *,
    shift_templates: Dict[str, ShiftTemplateInfo],
    rules: JurisdictionRules,
    period_start: date,
    period_end: date,
    availability_blocked: Optional[Mapping[str, Set[date]]],
    qual_codes: Mapping[str, str],
    use_catalog_stamp: bool = False,
) -> bool:
    """True when the paired Sat/Sun can take the same shift as the anchor day."""

    if mirror_date in state.work_dates:
        return True
    if (
        availability_blocked
        and mirror_date in availability_blocked.get(employee.id, set())
    ):
        return False
    if validate_contract_line_eligibility(
        employee.contract_line_type,
        template.code,
        qual_code=infer_qual_code(employee, qual_codes=qual_codes),
    ):
        return False
    for master_catalog_stamp in (False, True) if use_catalog_stamp else (False,):
        if (
            _would_violate_labor_rules(
                state,
                mirror_date,
                template,
                shift_templates,
                rules,
                period_start,
                period_end,
                availability_blocked,
                enforce_fte_target=False,
                relax_dn_contract_completion=True,
                master_catalog_stamp=master_catalog_stamp,
            )
            is None
        ):
            return True
    return False


def _append_weekend_mirror_assignment(
    assignments: List[PlannedAssignment],
    state: _EmployeeState,
    employee: EmployeeProfile,
    mirror_date: date,
    template: ShiftTemplateInfo,
    *,
    shift_templates: Dict[str, ShiftTemplateInfo],
    rules: JurisdictionRules,
    period_start: date,
    period_end: date,
    availability_blocked: Optional[Mapping[str, Set[date]]],
    qual_codes: Mapping[str, str],
    anchor_frozen: bool = False,
) -> bool:
    mirror_template = _weekend_mirror_template_for_employee(
        employee,
        mirror_date,
        template,
        period_start=period_start,
        shift_templates=shift_templates,
    )
    if not _weekend_mirror_assignment_feasible(
        state,
        employee,
        mirror_date,
        mirror_template,
        shift_templates=shift_templates,
        rules=rules,
        period_start=period_start,
        period_end=period_end,
        availability_blocked=availability_blocked,
        qual_codes=qual_codes,
        use_catalog_stamp=True,
    ):
        return False

    shift_hours = mirror_template.duration_minutes / 60.0
    _apply_assignment_to_state(
        state,
        mirror_date,
        mirror_template.id,
        shift_hours,
        rules=rules,
    )
    assignments.append(
        PlannedAssignment(
            employee_id=employee.id,
            shift_template_id=mirror_template.id,
            assignment_date=mirror_date,
            master_template_frozen=anchor_frozen,
        )
    )
    return True


def _weekend_day_is_orphan_for_employee(
    assignments: Sequence[PlannedAssignment],
    *,
    employee_id: str,
    assignment_date: date,
) -> bool:
    paired_date = _weekend_paired_date(assignment_date)
    if paired_date is None:
        return False
    has_anchor = (
        _employee_assignment_on_date(
            assignments,
            employee_id=employee_id,
            assignment_date=assignment_date,
        )
        is not None
    )
    has_paired = (
        _employee_assignment_on_date(
            assignments,
            employee_id=employee_id,
            assignment_date=paired_date,
        )
        is not None
    )
    return has_anchor != has_paired


def _trim_dn_off_catalog_weekend_shifts(
    assignments: List[PlannedAssignment],
    states: Dict[str, _EmployeeState],
    *,
    employees: Sequence[EmployeeProfile],
    shift_templates: Dict[str, ShiftTemplateInfo],
    period_start: date,
    period_end: date,
) -> int:
    """Drop weekend D/E gap-fills on D/N vacant lines when catalog calls for N or off."""

    from lab_scheduler.scheduling.portage_template import (
        parse_vacant_portage_line,
        vacant_master_scheduled_shift_code,
    )

    removed = 0
    to_remove: List[int] = []
    for index, assignment in enumerate(assignments):
        employee = next(
            (row for row in employees if row.id == assignment.employee_id),
            None,
        )
        if employee is None or parse_vacant_portage_line(employee.full_name) is None:
            continue
        if (employee.contract_line_type or "").upper() != "D/N":
            continue
        if assignment.assignment_date.weekday() < 5:
            continue
        if assignment.assignment_date < period_start or assignment.assignment_date > period_end:
            continue
        template = shift_templates.get(assignment.shift_template_id)
        if template is None:
            continue
        expected_code = vacant_master_scheduled_shift_code(
            employee,
            assignment.assignment_date,
            period_start,
        )
        if expected_code is None or template.code == expected_code:
            continue
        if getattr(assignment, "forced_clinical_ot", False):
            continue
        to_remove.append(index)

    for index in sorted(set(to_remove), reverse=True):
        _remove_assignment_at_index(
            assignments,
            index,
            states=states,
            shift_templates=shift_templates,
        )
        removed += 1
    return removed


def _vacant_weekend_surplus_removal_rank(
    employee: EmployeeProfile,
    assignment: PlannedAssignment,
    assignments: Sequence[PlannedAssignment],
    shift_templates: Mapping[str, ShiftTemplateInfo],
    period_start: date,
) -> tuple[int, ...]:
    from lab_scheduler.scheduling.portage_template import vacant_master_scheduled_shift_code

    template = shift_templates.get(assignment.shift_template_id)
    expected_code = vacant_master_scheduled_shift_code(
        employee,
        assignment.assignment_date,
        period_start,
    )
    actual_code = template.code if template is not None else ""
    off_catalog = expected_code is None or actual_code != expected_code
    return (
        0 if getattr(assignment, "forced_clinical_ot", False) else 1,
        0 if getattr(assignment, "master_template_frozen", False) else 1,
        0
        if _weekend_day_is_orphan_for_employee(
            assignments,
            employee_id=employee.id,
            assignment_date=assignment.assignment_date,
        )
        else 1,
        0 if off_catalog else 1,
        0
        if (
            (employee.contract_line_type or "").upper() == "D/N"
            and _assignment_band(assignment, shift_templates) == "D"
        )
        else 1,
        assignment.assignment_date.toordinal(),
    )


def _trim_portage_vacant_weekend_to_target(
    assignments: List[PlannedAssignment],
    states: Dict[str, _EmployeeState],
    *,
    employees: Sequence[EmployeeProfile],
    shift_templates: Dict[str, ShiftTemplateInfo],
    catalog_targets: Mapping[str, float],
    qual_codes: Mapping[str, str],
    period_start: date,
    period_end: date,
) -> int:
    """Shed surplus Sat/Sun work on vacant lines down to pool-scaled catalog targets."""

    from lab_scheduler.scheduling.portage_equity_targets import (
        build_vacant_line_weekend_target_map,
    )
    from lab_scheduler.scheduling.portage_template import (
        parse_vacant_portage_line,
    )

    targets = build_vacant_line_weekend_target_map(
        employees,
        catalog_targets,
        qual_codes,
        period_start=period_start,
        period_end=period_end,
    )
    removed = 0
    for employee in employees:
        if parse_vacant_portage_line(employee.full_name) is None:
            continue
        target = int(targets.get(employee.id, 0))
        if target <= 0:
            continue
        employee_id = employee.id
        while True:
            _, _, weekend_count = _peer_shift_metrics(
                employee_id,
                assignments,
                shift_templates,
                employee.contract_line_type,
                period_start,
                period_end,
            )
            if weekend_count <= target:
                break
            candidate_indices = [
                index
                for index, assignment in enumerate(assignments)
                if assignment.employee_id == employee_id
                and period_start <= assignment.assignment_date <= period_end
                and assignment.assignment_date.weekday() >= 5
            ]
            if not candidate_indices:
                break
            candidate_indices.sort(
                key=lambda index: _vacant_weekend_surplus_removal_rank(
                    employee,
                    assignments[index],
                    assignments,
                    shift_templates,
                    period_start,
                )
            )
            removed_one = False
            for index in candidate_indices:
                _remove_assignment_at_index(
                    assignments,
                    index,
                    states=states,
                    shift_templates=shift_templates,
                )
                removed += 1
                removed_one = True
                break
            if not removed_one:
                break
    return removed


def _remove_assignment_at_index(
    assignments: List[PlannedAssignment],
    index: int,
    *,
    states: Dict[str, _EmployeeState],
    shift_templates: Dict[str, ShiftTemplateInfo],
) -> None:
    assignment = assignments[index]
    state = states.get(assignment.employee_id)
    template = shift_templates.get(assignment.shift_template_id)
    if state is not None and template is not None:
        shift_hours = template.duration_minutes / 60.0
        week_start = workweek_for(assignment.assignment_date).start
        state.work_dates.discard(assignment.assignment_date)
        state.assignment_records[:] = [
            record
            for record in state.assignment_records
            if not (
                record[0] == assignment.assignment_date
                and record[1] == assignment.shift_template_id
            )
        ]
        state.total_hours = max(0.0, state.total_hours - shift_hours)
        if week_start in state.week_hours:
            state.week_hours[week_start] = max(
                0.0,
                state.week_hours[week_start] - shift_hours,
            )
    del assignments[index]


def _remove_assignment_at_index_guarded(
    assignments: List[PlannedAssignment],
    index: int,
    *,
    states: Dict[str, _EmployeeState],
    shift_templates: Dict[str, ShiftTemplateInfo],
    post_pass_guard: Optional[PostPassGuard] = None,
    anchor_violations: Optional[List[str]] = None,
) -> bool:
    if index < 0 or index >= len(assignments):
        return False
    assignment = assignments[index]
    if post_pass_guard is not None and post_pass_guard.blocks_anchor_modification(
        assignments,
        employee_id=assignment.employee_id,
        assignment_date=assignment.assignment_date,
        shift_templates=shift_templates,
    ):
        if anchor_violations is not None:
            anchor_violations.append(
                f"blocked trim on night anchor {assignment.employee_id} "
                f"{assignment.assignment_date.isoformat()}"
            )
        return False
    _remove_assignment_at_index(
        assignments,
        index,
        states=states,
        shift_templates=shift_templates,
    )
    return True


def _remove_employee_assignments_on_date(
    assignments: List[PlannedAssignment],
    *,
    employee_id: str,
    assignment_date: date,
    states: Dict[str, _EmployeeState],
    shift_templates: Dict[str, ShiftTemplateInfo],
) -> int:
    """Remove every assignment row for one employee on one calendar day."""

    removed = 0
    for index in sorted(
        (
            idx
            for idx, assignment in enumerate(assignments)
            if assignment.employee_id == employee_id
            and assignment.assignment_date == assignment_date
        ),
        reverse=True,
    ):
        _remove_assignment_at_index(
            assignments,
            index,
            states=states,
            shift_templates=shift_templates,
        )
        removed += 1
    return removed


def _enforce_weekend_shift_mirror(
    assignments: List[PlannedAssignment],
    states: Dict[str, _EmployeeState],
    *,
    employees: Sequence[EmployeeProfile],
    shift_templates: Dict[str, ShiftTemplateInfo],
    rules: JurisdictionRules,
    period_start: date,
    period_end: date,
    availability_blocked: Optional[Mapping[str, Set[date]]],
    qual_codes: Mapping[str, str],
) -> int:
    """Default Portage rule: same employee works the same band on Sat and Sun."""

    changed = 0
    employee_by_id = {employee.id: employee for employee in employees}

    for saturday in _daterange(period_start, period_end):
        if saturday.weekday() != 5:
            continue
        sunday = saturday + timedelta(days=1)
        if sunday > period_end:
            continue

        for employee in employees:
            state = states.get(employee.id)
            if state is None:
                continue

            sat_assignment = _employee_assignment_on_date(
                assignments,
                employee_id=employee.id,
                assignment_date=saturday,
            )
            sun_assignment = _employee_assignment_on_date(
                assignments,
                employee_id=employee.id,
                assignment_date=sunday,
            )

            if sat_assignment is None and sun_assignment is None:
                continue

            anchor = sat_assignment or sun_assignment
            assert anchor is not None
            anchor_date = anchor.assignment_date
            mirror_date = sunday if anchor_date == saturday else saturday
            anchor_template = shift_templates[anchor.shift_template_id]

            if sat_assignment is not None and sun_assignment is not None:
                sun_template = shift_templates[sun_assignment.shift_template_id]
                if sun_template.code == anchor_template.code:
                    continue
                if getattr(sun_assignment, "master_template_frozen", False):
                    if getattr(sat_assignment, "master_template_frozen", False):
                        continue
                    anchor_date = sunday
                    mirror_date = saturday
                    anchor = sun_assignment
                    anchor_template = sun_template
                else:
                    sun_index = assignments.index(sun_assignment)
                    _remove_assignment_at_index(
                        assignments,
                        sun_index,
                        states=states,
                        shift_templates=shift_templates,
                    )
                    sun_assignment = None

            if _employee_assignment_on_date(
                assignments,
                employee_id=employee.id,
                assignment_date=mirror_date,
            ) is not None:
                continue

            if _append_weekend_mirror_assignment(
                assignments,
                state,
                employee,
                mirror_date,
                anchor_template,
                shift_templates=shift_templates,
                rules=rules,
                period_start=period_start,
                period_end=period_end,
                availability_blocked=availability_blocked,
                qual_codes=qual_codes,
                anchor_frozen=getattr(anchor, "master_template_frozen", False),
            ):
                changed += 1

    return changed


def _drop_unmirrored_weekend_single_days(
    assignments: List[PlannedAssignment],
    states: Dict[str, _EmployeeState],
    *,
    employees: Sequence[EmployeeProfile],
    shift_templates: Dict[str, ShiftTemplateInfo],
    rules: JurisdictionRules,
    period_start: date,
    period_end: date,
    availability_blocked: Optional[Mapping[str, Set[date]]],
    qual_codes: Mapping[str, str],
) -> int:
    """
    Remove non-frozen single-day weekend work when the paired day cannot mirror.

    Prefer dropping the orphan and refilling coverage over leaving split weekends.
    """

    to_remove: List[int] = []

    for saturday in _daterange(period_start, period_end):
        if saturday.weekday() != 5:
            continue
        sunday = saturday + timedelta(days=1)
        if sunday > period_end:
            continue

        for employee in employees:
            state = states.get(employee.id)
            if state is None:
                continue

            sat_assignment = _employee_assignment_on_date(
                assignments,
                employee_id=employee.id,
                assignment_date=saturday,
            )
            sun_assignment = _employee_assignment_on_date(
                assignments,
                employee_id=employee.id,
                assignment_date=sunday,
            )
            if bool(sat_assignment) == bool(sun_assignment):
                continue

            orphan = sat_assignment or sun_assignment
            assert orphan is not None
            mirror_date = sunday if orphan.assignment_date == saturday else saturday
            orphan_template = shift_templates[orphan.shift_template_id]
            if getattr(orphan, "master_template_frozen", False):
                if _append_weekend_mirror_assignment(
                    assignments,
                    state,
                    employee,
                    mirror_date,
                    orphan_template,
                    shift_templates=shift_templates,
                    rules=rules,
                    period_start=period_start,
                    period_end=period_end,
                    availability_blocked=availability_blocked,
                    qual_codes=qual_codes,
                    anchor_frozen=True,
                ):
                    continue
                continue
            if _weekend_mirror_assignment_feasible(
                state,
                employee,
                mirror_date,
                orphan_template,
                shift_templates=shift_templates,
                rules=rules,
                period_start=period_start,
                period_end=period_end,
                availability_blocked=availability_blocked,
                qual_codes=qual_codes,
                use_catalog_stamp=True,
            ):
                continue

            for index, assignment in enumerate(assignments):
                if (
                    assignment.employee_id == employee.id
                    and assignment.assignment_date == orphan.assignment_date
                ):
                    to_remove.append(index)

    dropped = 0
    for index in sorted(set(to_remove), reverse=True):
        _remove_assignment_at_index(
            assignments,
            index,
            states=states,
            shift_templates=shift_templates,
        )
        dropped += 1
    return dropped


def _consolidate_weekend_sat_sun_pairings(
    assignments: List[PlannedAssignment],
    states: Dict[str, _EmployeeState],
    *,
    employees: Sequence[EmployeeProfile],
    shift_templates: Dict[str, ShiftTemplateInfo],
    rules: JurisdictionRules,
    period_start: date,
    period_end: date,
    availability_blocked: Optional[Mapping[str, Set[date]]],
    qual_codes: Mapping[str, str],
) -> int:
    """
    Resolve cross-person split weekends: when one worker is Sat-only and another
    is Sun-only with the same shift, extend the Sat worker and drop the orphan.
    """

    moved = 0
    employee_by_id = {employee.id: employee for employee in employees}

    for saturday in _daterange(period_start, period_end):
        if saturday.weekday() != 5:
            continue
        sunday = saturday + timedelta(days=1)
        if sunday > period_end:
            continue

        sat_only: List[PlannedAssignment] = []
        sun_only: List[PlannedAssignment] = []
        for assignment in assignments:
            state = states.get(assignment.employee_id)
            if state is None:
                continue
            if assignment.assignment_date == saturday and sunday not in state.work_dates:
                sat_only.append(assignment)
            elif assignment.assignment_date == sunday and saturday not in state.work_dates:
                sun_only.append(assignment)

        for sat_assignment in list(sat_only):
            sat_state = states[sat_assignment.employee_id]
            sat_employee = employee_by_id[sat_assignment.employee_id]
            if sunday in sat_state.work_dates:
                continue
            sat_template = shift_templates[sat_assignment.shift_template_id]
            if _weekend_mirror_assignment_feasible(
                sat_state,
                sat_employee,
                sunday,
                sat_template,
                shift_templates=shift_templates,
                rules=rules,
                period_start=period_start,
                period_end=period_end,
                availability_blocked=availability_blocked,
                qual_codes=qual_codes,
            ):
                continue

            for sun_assignment in list(sun_only):
                if sun_assignment.employee_id == sat_assignment.employee_id:
                    continue
                if getattr(sun_assignment, "master_template_frozen", False):
                    continue
                sun_template = shift_templates[sun_assignment.shift_template_id]
                if sun_template.code != sat_template.code:
                    continue
                for index, assignment in enumerate(assignments):
                    if (
                        assignment.employee_id == sun_assignment.employee_id
                        and assignment.assignment_date == sunday
                    ):
                        _remove_assignment_at_index(
                            assignments,
                            index,
                            states=states,
                            shift_templates=shift_templates,
                        )
                        sun_only.remove(sun_assignment)
                        moved += 1
                        break
                else:
                    continue
                break

            if sunday in sat_state.work_dates:
                continue
            if not _weekend_mirror_assignment_feasible(
                sat_state,
                sat_employee,
                sunday,
                sat_template,
                shift_templates=shift_templates,
                rules=rules,
                period_start=period_start,
                period_end=period_end,
                availability_blocked=availability_blocked,
                qual_codes=qual_codes,
            ):
                continue

            shift_hours = sat_template.duration_minutes / 60.0
            _apply_assignment_to_state(
                sat_state,
                sunday,
                sat_assignment.shift_template_id,
                shift_hours,
                rules=rules,
            )
            assignments.append(
                PlannedAssignment(
                    employee_id=sat_assignment.employee_id,
                    shift_template_id=sat_assignment.shift_template_id,
                    assignment_date=sunday,
                )
            )
            moved += 1

        for sun_assignment in list(sun_only):
            sun_state = states[sun_assignment.employee_id]
            sun_employee = employee_by_id[sun_assignment.employee_id]
            if saturday in sun_state.work_dates:
                continue
            sun_template = shift_templates[sun_assignment.shift_template_id]
            if _weekend_mirror_assignment_feasible(
                sun_state,
                sun_employee,
                saturday,
                sun_template,
                shift_templates=shift_templates,
                rules=rules,
                period_start=period_start,
                period_end=period_end,
                availability_blocked=availability_blocked,
                qual_codes=qual_codes,
            ):
                continue

            for sat_assignment in list(sat_only):
                if sat_assignment.employee_id == sun_assignment.employee_id:
                    continue
                if getattr(sat_assignment, "master_template_frozen", False):
                    continue
                sat_template = shift_templates[sat_assignment.shift_template_id]
                if sat_template.code != sun_template.code:
                    continue
                for index, assignment in enumerate(assignments):
                    if (
                        assignment.employee_id == sat_assignment.employee_id
                        and assignment.assignment_date == saturday
                    ):
                        _remove_assignment_at_index(
                            assignments,
                            index,
                            states=states,
                            shift_templates=shift_templates,
                        )
                        sat_only.remove(sat_assignment)
                        moved += 1
                        break
                else:
                    continue
                break

            if saturday in sun_state.work_dates:
                continue
            if not _weekend_mirror_assignment_feasible(
                sun_state,
                sun_employee,
                saturday,
                sun_template,
                shift_templates=shift_templates,
                rules=rules,
                period_start=period_start,
                period_end=period_end,
                availability_blocked=availability_blocked,
                qual_codes=qual_codes,
            ):
                continue

            shift_hours = sun_template.duration_minutes / 60.0
            _apply_assignment_to_state(
                sun_state,
                saturday,
                sun_assignment.shift_template_id,
                shift_hours,
                rules=rules,
            )
            assignments.append(
                PlannedAssignment(
                    employee_id=sun_assignment.employee_id,
                    shift_template_id=sun_assignment.shift_template_id,
                    assignment_date=saturday,
                )
            )
            moved += 1

    moved += _drop_unmirrored_weekend_single_days(
        assignments,
        states,
        employees=employees,
        shift_templates=shift_templates,
        rules=rules,
        period_start=period_start,
        period_end=period_end,
        availability_blocked=availability_blocked,
        qual_codes=qual_codes,
    )
    return moved


def _alternate_band_for_contract_line(contract_line_type: object) -> str:
    from lab_scheduler.models.employee import normalize_contract_line_type

    contract = normalize_contract_line_type(str(contract_line_type or "")) or "D/E"
    return "N" if contract == "D/N" else "E"


def _master_rotation_owns_alternate_band(contract_line_type: object) -> bool:
    """D/N night blocks are stamped from the 8-week master; equity must not reshuffle them."""

    from lab_scheduler.models.employee import normalize_contract_line_type

    contract = normalize_contract_line_type(str(contract_line_type or "")) or "D/E"
    return contract == "D/N"


def _equity_allow_frozen_alternate_swap(
    contract_line_type: object,
    alternate_band: str,
) -> bool:
    """Frozen master nights stay fixed; D/E evening equity may still adjust frozen E cells."""

    if _master_rotation_owns_alternate_band(contract_line_type) and alternate_band == "N":
        return False
    return True


def _peer_shift_metrics(
    employee_id: str,
    assignments: Sequence[PlannedAssignment],
    shift_templates: Dict[str, ShiftTemplateInfo],
    contract_line_type: object,
    period_start: date,
    period_end: date,
) -> Tuple[int, int, int]:
    """Return (alternate_count, day_count, weekend_shift_count) for one employee."""

    alt_band = _alternate_band_for_contract_line(contract_line_type)
    alternate = 0
    day_count = 0
    weekend = 0
    for assignment in assignments:
        if assignment.employee_id != employee_id:
            continue
        if assignment.assignment_date < period_start or assignment.assignment_date > period_end:
            continue
        template = shift_templates.get(assignment.shift_template_id)
        if template is None:
            continue
        band = shift_band_from_template_code(template.code)
        if band == alt_band:
            alternate += 1
        elif band == "D":
            day_count += 1
        if assignment.assignment_date.weekday() >= 5 and band in ("D", "E", "N"):
            weekend += 1
    return alternate, day_count, weekend


def _assignment_band(
    assignment: PlannedAssignment,
    shift_templates: Dict[str, ShiftTemplateInfo],
) -> Optional[str]:
    template = shift_templates.get(assignment.shift_template_id)
    if template is None:
        return None
    return shift_band_from_template_code(template.code)


def _peer_equity_alt_spread_limit(
    member_ids: Sequence[str],
    employee_target_hours: Mapping[str, float],
) -> int:
    """Full-time vacant pools require identical alternate-band counts (spread 0)."""

    from lab_scheduler.solver.cpsat_fill import _is_fulltime_target_hours

    if not member_ids:
        return 1
    if all(
        _is_fulltime_target_hours(float(employee_target_hours.get(employee_id, 0.0)))
        for employee_id in member_ids
    ):
        return 0
    return 1


def _day_shift_template_id(
    shift_templates: Mapping[str, ShiftTemplateInfo],
) -> Optional[str]:
    for template_id, template in shift_templates.items():
        if shift_band_from_template_code(template.code) == "D":
            return template_id
    return None


def _try_convert_alternate_to_day_on_date(
    assignments: List[PlannedAssignment],
    states: Dict[str, _EmployeeState],
    *,
    employee_id: str,
    swap_date: date,
    employees_by_id: Mapping[str, EmployeeProfile],
    shift_templates: Dict[str, ShiftTemplateInfo],
    rules: JurisdictionRules,
    period_start: date,
    period_end: date,
    availability_blocked: Optional[Mapping[str, Set[date]]],
    qual_codes: Mapping[str, str],
    alternate_band: str,
    allow_frozen_swaps: bool = False,
    require_pool_headroom: bool = False,
    post_pass_guard: Optional[PostPassGuard] = None,
) -> bool:
    """Replace one alternate-band shift with a day-band shift on the same line and date."""

    day_template_id = _day_shift_template_id(shift_templates)
    if day_template_id is None:
        return False

    assignment = _employee_assignment_on_date(
        assignments,
        employee_id=employee_id,
        assignment_date=swap_date,
    )
    if assignment is None:
        return False
    if _manager_lock_denies_cell_edit(
        post_pass_guard,
        assignments,
        employee_id=employee_id,
        assignment_date=swap_date,
        shift_templates=shift_templates,
    ):
        return False
    if getattr(assignment, "master_template_frozen", False) and not allow_frozen_swaps:
        return False
    if _assignment_band(assignment, shift_templates) != alternate_band:
        return False

    if require_pool_headroom:
        cap = CLINICAL_FLOOR.get("EVENING" if alternate_band == "E" else "NIGHT", 2)
        if (
            _daily_pool_band_count(
                assignments,
                shift_templates,
                assignment_date=swap_date,
                band=alternate_band,
            )
            <= cap
        ):
            return False

    employee = employees_by_id[employee_id]
    if not vacant_master_rotation_permits_shift(
        employee,
        swap_date,
        period_start,
        "MORNING",
    ):
        return False
    day_template = shift_templates[day_template_id]
    if validate_contract_line_eligibility(
        employee.contract_line_type,
        day_template.code,
        qual_code=infer_qual_code(employee, qual_codes=qual_codes),
    ):
        return False

    alt_template = shift_templates[assignment.shift_template_id]
    alt_hours = alt_template.duration_minutes / 60.0
    day_hours = day_template.duration_minutes / 60.0
    state = states[employee_id]
    _remove_assignment_from_state(
        state,
        swap_date,
        assignment.shift_template_id,
        alt_hours,
    )
    violation = _would_violate_labor_rules(
        state,
        swap_date,
        day_template,
        shift_templates,
        rules,
        period_start,
        period_end,
        availability_blocked,
        enforce_fte_target=False,
        relax_dn_contract_completion=True,
        peer_equity_swap=True,
        allow_provisional=True,
    )
    if violation:
        _apply_assignment_to_state(
            state,
            swap_date,
            assignment.shift_template_id,
            alt_hours,
            rules=rules,
        )
        return False

    frozen = getattr(assignment, "master_template_frozen", False)
    for index, planned in enumerate(assignments):
        if (
            planned.employee_id == employee_id
            and planned.assignment_date == swap_date
        ):
            assignments[index] = PlannedAssignment(
                employee_id=employee_id,
                shift_template_id=day_template_id,
                assignment_date=swap_date,
                master_template_frozen=frozen,
            )
            break

    _apply_assignment_to_state(
        state,
        swap_date,
        day_template_id,
        day_hours,
        rules=rules,
    )
    return True


def _swap_employee_assignments_on_date(
    assignments: List[PlannedAssignment],
    states: Dict[str, _EmployeeState],
    *,
    employee_a_id: str,
    employee_b_id: str,
    assignment_date: date,
    employees_by_id: Mapping[str, EmployeeProfile],
    shift_templates: Dict[str, ShiftTemplateInfo],
    rules: JurisdictionRules,
    period_start: date,
    period_end: date,
    availability_blocked: Optional[Mapping[str, Set[date]]],
    qual_codes: Mapping[str, str],
) -> bool:
    """Exchange two employees' shifts on the same calendar day when labor rules allow."""

    assignment_a = _employee_assignment_on_date(
        assignments, employee_id=employee_a_id, assignment_date=assignment_date
    )
    assignment_b = _employee_assignment_on_date(
        assignments, employee_id=employee_b_id, assignment_date=assignment_date
    )
    if assignment_a is None or assignment_b is None:
        return False

    template_a = shift_templates[assignment_a.shift_template_id]
    template_b = shift_templates[assignment_b.shift_template_id]
    employee_a = employees_by_id[employee_a_id]
    employee_b = employees_by_id[employee_b_id]
    for employee, template in (
        (employee_a, template_b),
        (employee_b, template_a),
    ):
        line_issue = validate_contract_line_eligibility(
            employee.contract_line_type,
            template.code,
            qual_code=infer_qual_code(employee, qual_codes=qual_codes),
        )
        if line_issue and template.code not in ("EVENING", "NIGHT"):
            return False

    hours_a = template_a.duration_minutes / 60.0
    hours_b = template_b.duration_minutes / 60.0
    state_a = states[employee_a_id]
    state_b = states[employee_b_id]
    _remove_assignment_from_state(
        state_a, assignment_date, assignment_a.shift_template_id, hours_a
    )
    _remove_assignment_from_state(
        state_b, assignment_date, assignment_b.shift_template_id, hours_b
    )
    violation_a = _would_violate_labor_rules(
        state_a,
        assignment_date,
        template_b,
        shift_templates,
        rules,
        period_start,
        period_end,
        availability_blocked,
        enforce_fte_target=False,
        relax_dn_contract_completion=True,
        peer_equity_swap=True,
        allow_provisional=True,
    )
    violation_b = _would_violate_labor_rules(
        state_b,
        assignment_date,
        template_a,
        shift_templates,
        rules,
        period_start,
        period_end,
        availability_blocked,
        enforce_fte_target=False,
        relax_dn_contract_completion=True,
        peer_equity_swap=True,
        allow_provisional=True,
    )
    if violation_a or violation_b:
        _apply_assignment_to_state(
            state_a,
            assignment_date,
            assignment_a.shift_template_id,
            hours_a,
            rules=rules,
        )
        _apply_assignment_to_state(
            state_b,
            assignment_date,
            assignment_b.shift_template_id,
            hours_b,
            rules=rules,
        )
        return False

    for index, planned in enumerate(assignments):
        if (
            planned.employee_id == employee_a_id
            and planned.assignment_date == assignment_date
        ):
            assignments[index] = PlannedAssignment(
                employee_id=employee_a_id,
                shift_template_id=assignment_b.shift_template_id,
                assignment_date=assignment_date,
                master_template_frozen=getattr(planned, "master_template_frozen", False),
            )
        elif (
            planned.employee_id == employee_b_id
            and planned.assignment_date == assignment_date
        ):
            assignments[index] = PlannedAssignment(
                employee_id=employee_b_id,
                shift_template_id=assignment_a.shift_template_id,
                assignment_date=assignment_date,
                master_template_frozen=getattr(planned, "master_template_frozen", False),
            )

    _apply_assignment_to_state(
        state_a, assignment_date, template_b.id, hours_b, rules=rules
    )
    _apply_assignment_to_state(
        state_b, assignment_date, template_a.id, hours_a, rules=rules
    )
    return True


def _try_convert_morning_to_evening_on_date(
    assignments: List[PlannedAssignment],
    states: Dict[str, _EmployeeState],
    *,
    employee_id: str,
    swap_date: date,
    employees_by_id: Mapping[str, EmployeeProfile],
    shift_templates: Dict[str, ShiftTemplateInfo],
    rules: JurisdictionRules,
    period_start: date,
    period_end: date,
    availability_blocked: Optional[Mapping[str, Set[date]]],
    qual_codes: Mapping[str, str],
    allow_frozen_swaps: bool = False,
) -> bool:
    """Replace one morning shift with evening on the same line and date."""

    evening_template_id = _shift_id_for_code("EVENING", shift_templates)
    if evening_template_id is None:
        return False
    if (
        _daily_pool_band_count(
            assignments,
            shift_templates,
            assignment_date=swap_date,
            band="E",
        )
        >= CLINICAL_FLOOR.get("EVENING", 2)
    ):
        return False

    assignment = _employee_assignment_on_date(
        assignments,
        employee_id=employee_id,
        assignment_date=swap_date,
    )
    if assignment is None:
        return False
    if getattr(assignment, "master_template_frozen", False) and not allow_frozen_swaps:
        return False
    if shift_templates[assignment.shift_template_id].code != "MORNING":
        return False

    employee = employees_by_id[employee_id]
    evening_template = shift_templates[evening_template_id]
    if validate_contract_line_eligibility(
        employee.contract_line_type,
        evening_template.code,
        qual_code=infer_qual_code(employee, qual_codes=qual_codes),
    ):
        return False

    source_template = shift_templates[assignment.shift_template_id]
    source_hours = source_template.duration_minutes / 60.0
    evening_hours = evening_template.duration_minutes / 60.0
    state = states[employee_id]
    _remove_assignment_from_state(
        state,
        swap_date,
        assignment.shift_template_id,
        source_hours,
    )
    violation = _would_violate_labor_rules(
        state,
        swap_date,
        evening_template,
        shift_templates,
        rules,
        period_start,
        period_end,
        availability_blocked,
        enforce_fte_target=False,
        relax_dn_contract_completion=True,
        forced_clinical_ot=True,
        mandatory_assignment=True,
    )
    if violation:
        _apply_assignment_to_state(
            state,
            swap_date,
            assignment.shift_template_id,
            source_hours,
            rules=rules,
        )
        return False

    frozen = getattr(assignment, "master_template_frozen", False)
    for index, planned in enumerate(assignments):
        if (
            planned.employee_id == employee_id
            and planned.assignment_date == swap_date
        ):
            assignments[index] = PlannedAssignment(
                employee_id=employee_id,
                shift_template_id=evening_template_id,
                assignment_date=swap_date,
                master_template_frozen=frozen,
                forced_clinical_ot=True,
            )
            break

    _apply_assignment_to_state(
        state,
        swap_date,
        evening_template_id,
        evening_hours,
        rules=rules,
    )
    return True


def _try_convert_night_to_evening_on_date(
    assignments: List[PlannedAssignment],
    states: Dict[str, _EmployeeState],
    *,
    employee_id: str,
    swap_date: date,
    employees_by_id: Mapping[str, EmployeeProfile],
    shift_templates: Dict[str, ShiftTemplateInfo],
    rules: JurisdictionRules,
    period_start: date,
    period_end: date,
    availability_blocked: Optional[Mapping[str, Set[date]]],
    qual_codes: Mapping[str, str],
    allow_frozen_swaps: bool = False,
) -> bool:
    """Replace one night-band shift with evening on the same line and date."""

    evening_template_id = _shift_id_for_code("EVENING", shift_templates)
    if evening_template_id is None:
        return False
    if (
        _daily_pool_band_count(
            assignments,
            shift_templates,
            assignment_date=swap_date,
            band="E",
        )
        >= CLINICAL_FLOOR.get("EVENING", 2)
    ):
        return False

    assignment = _employee_assignment_on_date(
        assignments,
        employee_id=employee_id,
        assignment_date=swap_date,
    )
    if assignment is None:
        return False
    if getattr(assignment, "master_template_frozen", False) and not allow_frozen_swaps:
        return False
    if _assignment_band(assignment, shift_templates) != "N":
        return False

    employee = employees_by_id[employee_id]
    evening_template = shift_templates[evening_template_id]
    if validate_contract_line_eligibility(
        employee.contract_line_type,
        evening_template.code,
        qual_code=infer_qual_code(employee, qual_codes=qual_codes),
    ):
        return False

    night_template = shift_templates[assignment.shift_template_id]
    night_hours = night_template.duration_minutes / 60.0
    evening_hours = evening_template.duration_minutes / 60.0
    state = states[employee_id]
    _remove_assignment_from_state(
        state,
        swap_date,
        assignment.shift_template_id,
        night_hours,
    )
    violation = _would_violate_labor_rules(
        state,
        swap_date,
        evening_template,
        shift_templates,
        rules,
        period_start,
        period_end,
        availability_blocked,
        enforce_fte_target=False,
        relax_dn_contract_completion=True,
        peer_equity_swap=True,
        allow_provisional=True,
    )
    if violation:
        _apply_assignment_to_state(
            state,
            swap_date,
            assignment.shift_template_id,
            night_hours,
            rules=rules,
        )
        return False

    frozen = getattr(assignment, "master_template_frozen", False)
    for index, planned in enumerate(assignments):
        if (
            planned.employee_id == employee_id
            and planned.assignment_date == swap_date
        ):
            assignments[index] = PlannedAssignment(
                employee_id=employee_id,
                shift_template_id=evening_template_id,
                assignment_date=swap_date,
                master_template_frozen=frozen,
            )
            break

    _apply_assignment_to_state(
        state,
        swap_date,
        evening_template_id,
        evening_hours,
        rules=rules,
    )
    return True


def _try_convert_day_to_alternate_on_date(
    assignments: List[PlannedAssignment],
    states: Dict[str, _EmployeeState],
    *,
    employee_id: str,
    swap_date: date,
    employees_by_id: Mapping[str, EmployeeProfile],
    shift_templates: Dict[str, ShiftTemplateInfo],
    rules: JurisdictionRules,
    period_start: date,
    period_end: date,
    availability_blocked: Optional[Mapping[str, Set[date]]],
    qual_codes: Mapping[str, str],
    alternate_band: str,
    allow_frozen_swaps: bool = False,
    post_pass_guard: Optional[PostPassGuard] = None,
) -> bool:
    """Replace one day-band shift with an alternate-band shift when the daily pool cap allows."""

    alt_shift_id = _shift_id_for_code(
        "EVENING" if alternate_band == "E" else "NIGHT",
        shift_templates,
    )
    if alt_shift_id is None:
        return False
    cap = CLINICAL_FLOOR.get("EVENING" if alternate_band == "E" else "NIGHT", 2)
    if (
        _daily_pool_band_count(
            assignments,
            shift_templates,
            assignment_date=swap_date,
            band=alternate_band,
        )
        >= cap
    ):
        return False

    assignment = _employee_assignment_on_date(
        assignments,
        employee_id=employee_id,
        assignment_date=swap_date,
    )
    if assignment is None:
        return False
    if _manager_lock_denies_cell_edit(
        post_pass_guard,
        assignments,
        employee_id=employee_id,
        assignment_date=swap_date,
        shift_templates=shift_templates,
    ):
        return False
    if getattr(assignment, "master_template_frozen", False) and not allow_frozen_swaps:
        return False
    if _assignment_band(assignment, shift_templates) != "D":
        return False

    employee = employees_by_id[employee_id]
    target_code = "EVENING" if alternate_band == "E" else "NIGHT"
    if not vacant_master_rotation_permits_shift(
        employee,
        swap_date,
        period_start,
        target_code,
    ):
        return False
    alt_template = shift_templates[alt_shift_id]
    if validate_contract_line_eligibility(
        employee.contract_line_type,
        alt_template.code,
        qual_code=infer_qual_code(employee, qual_codes=qual_codes),
    ):
        return False

    day_template = shift_templates[assignment.shift_template_id]
    day_hours = day_template.duration_minutes / 60.0
    alt_hours = alt_template.duration_minutes / 60.0
    state = states[employee_id]
    _remove_assignment_from_state(
        state,
        swap_date,
        assignment.shift_template_id,
        day_hours,
    )
    violation = _would_violate_labor_rules(
        state,
        swap_date,
        alt_template,
        shift_templates,
        rules,
        period_start,
        period_end,
        availability_blocked,
        enforce_fte_target=False,
        relax_dn_contract_completion=True,
        peer_equity_swap=True,
    )
    if violation:
        _apply_assignment_to_state(
            state,
            swap_date,
            assignment.shift_template_id,
            day_hours,
            rules=rules,
        )
        return False

    for index, existing in enumerate(assignments):
        if (
            existing.employee_id == employee_id
            and existing.assignment_date == swap_date
        ):
            frozen = getattr(existing, "master_template_frozen", False)
            assignments[index] = PlannedAssignment(
                employee_id=employee_id,
                shift_template_id=alt_shift_id,
                assignment_date=swap_date,
                master_template_frozen=frozen,
            )
            break

    _apply_assignment_to_state(
        state,
        swap_date,
        alt_shift_id,
        alt_hours,
        rules=rules,
    )
    return True


def _daily_band_counts_for_period(
    assignments: Sequence[PlannedAssignment],
    shift_templates: Mapping[str, ShiftTemplateInfo],
    *,
    period_dates: Sequence[date],
    band: str,
) -> Dict[date, int]:
    """Pool-wide alternate-band counts by day (rebuilt after each successful edit)."""

    counts = {assignment_date: 0 for assignment_date in period_dates}
    for assignment in assignments:
        assignment_date = assignment.assignment_date
        if assignment_date not in counts:
            continue
        if _assignment_band(assignment, shift_templates) == band:
            counts[assignment_date] += 1
    return counts


def _finalize_fulltime_peer_alt_parity(
    assignments: List[PlannedAssignment],
    states: Dict[str, _EmployeeState],
    *,
    employees: Sequence[EmployeeProfile],
    shift_templates: Dict[str, ShiftTemplateInfo],
    rules: JurisdictionRules,
    period_start: date,
    period_end: date,
    availability_blocked: Optional[Mapping[str, Set[date]]],
    qual_codes: Mapping[str, str],
    employee_target_hours: Mapping[str, float],
    max_rounds: int = 48,
) -> int:
    """
    Push vacant peer pools toward the catalog 20% alternate-band target per line.

    Uses convert D↔E/N and same-day swaps; respects the 2E/2N daily pool cap.
    """

    from lab_scheduler.scheduling.portage_equity_targets import portage_alt_shift_target
    from lab_scheduler.solver.cpsat_fill import _vacant_line_type_groups

    employee_by_id = {employee.id: employee for employee in employees}
    line_groups = _vacant_line_type_groups(employees, employee_target_hours)
    period_dates = list(_daterange(period_start, period_end))
    changed = 0

    for member_ids in line_groups.values():
        if len(member_ids) < 1:
            continue
        contract_line = employee_by_id[member_ids[0]].contract_line_type
        if _master_rotation_owns_alternate_band(contract_line):
            continue
        alternate_band = _alternate_band_for_contract_line(contract_line)
        allow_frozen = _equity_allow_frozen_alternate_swap(contract_line, alternate_band)
        cap = CLINICAL_FLOOR.get("EVENING" if alternate_band == "E" else "NIGHT", 2)
        daily_alt_counts = _daily_band_counts_for_period(
            assignments,
            shift_templates,
            period_dates=period_dates,
            band=alternate_band,
        )
        targets = {
            employee_id: portage_alt_shift_target(
                float(employee_target_hours.get(employee_id, 0.0))
            )
            for employee_id in member_ids
        }

        for _ in range(max_rounds):
            metrics = {
                employee_id: _peer_shift_metrics(
                    employee_id,
                    assignments,
                    shift_templates,
                    contract_line,
                    period_start,
                    period_end,
                )[0]
                for employee_id in member_ids
            }
            if all(metrics[employee_id] == targets[employee_id] for employee_id in member_ids):
                break
            counts = list(metrics.values())
            if len(member_ids) >= 2 and max(counts) - min(counts) == 0 and counts[0] != targets[member_ids[0]]:
                # Pool is equal but off catalog target — still convert toward target.
                pass

            moved = False
            for donor_id in sorted(member_ids, key=lambda eid: -metrics[eid]):
                while metrics[donor_id] > targets[donor_id]:
                    step = False
                    for swap_date in period_dates:
                        if _try_convert_alternate_to_day_on_date(
                            assignments,
                            states,
                            employee_id=donor_id,
                            swap_date=swap_date,
                            employees_by_id=employee_by_id,
                            shift_templates=shift_templates,
                            rules=rules,
                            period_start=period_start,
                            period_end=period_end,
                            availability_blocked=availability_blocked,
                            qual_codes=qual_codes,
                            alternate_band=alternate_band,
                            allow_frozen_swaps=allow_frozen,
                        ):
                            daily_alt_counts[swap_date] = max(
                                0, daily_alt_counts.get(swap_date, 0) - 1
                            )
                            metrics[donor_id] -= 1
                            changed += 1
                            step = True
                            moved = True
                            break
                    if not step:
                        break

            for recipient_id in sorted(member_ids, key=lambda eid: metrics[eid]):
                while metrics[recipient_id] < targets[recipient_id]:
                    step = False
                    for swap_date in period_dates:
                        if daily_alt_counts.get(swap_date, 0) >= cap:
                            continue
                        if _try_convert_day_to_alternate_on_date(
                            assignments,
                            states,
                            employee_id=recipient_id,
                            swap_date=swap_date,
                            employees_by_id=employee_by_id,
                            shift_templates=shift_templates,
                            rules=rules,
                            period_start=period_start,
                            period_end=period_end,
                            availability_blocked=availability_blocked,
                            qual_codes=qual_codes,
                            alternate_band=alternate_band,
                            allow_frozen_swaps=allow_frozen,
                        ):
                            daily_alt_counts[swap_date] = (
                                daily_alt_counts.get(swap_date, 0) + 1
                            )
                            metrics[recipient_id] += 1
                            changed += 1
                            step = True
                            moved = True
                            break
                    if not step:
                        break

            donor_id = max(member_ids, key=lambda eid: metrics[eid])
            recipient_id = min(member_ids, key=lambda eid: metrics[eid])
            if metrics[donor_id] <= metrics[recipient_id]:
                if not moved:
                    break
                continue
            for swap_date in period_dates:
                if _try_peer_same_day_band_swap(
                    assignments,
                    states,
                    donor_id=donor_id,
                    recipient_id=recipient_id,
                    swap_date=swap_date,
                    employees_by_id=employee_by_id,
                    shift_templates=shift_templates,
                    rules=rules,
                    period_start=period_start,
                    period_end=period_end,
                    availability_blocked=availability_blocked,
                    qual_codes=qual_codes,
                    alternate_band=alternate_band,
                    allow_frozen_swaps=allow_frozen,
                ):
                    changed += 1
                    moved = True
                    break
            if not moved:
                break

    return changed


def _enforce_catalog_alt_shift_density(
    assignments: List[PlannedAssignment],
    states: Dict[str, _EmployeeState],
    *,
    employees: Sequence[EmployeeProfile],
    shift_templates: Dict[str, ShiftTemplateInfo],
    rules: JurisdictionRules,
    period_start: date,
    period_end: date,
    availability_blocked: Optional[Mapping[str, Set[date]]],
    qual_codes: Mapping[str, str],
    employee_target_hours: Mapping[str, float],
    max_rounds: int = 96,
    alt_equity_scope: str = "all_peers",
) -> int:
    """
    Drive vacant lines toward the catalog 20% alternate-band count.

    When ``alt_equity_scope`` is ``ft_peers_only``, only full-time catalog lines
    are equalized; part-time gap-fill lines are not pushed down from higher alt share.
    """

    from lab_scheduler.scheduling.portage_equity_targets import (
        portage_alt_shift_target,
        portage_alt_shift_target_for_employee,
        portage_is_fulltime_catalog_hours,
    )
    from lab_scheduler.scheduling.portage_template import parse_vacant_portage_line

    employees_by_id = {employee.id: employee for employee in employees}
    vacant_employees = [
        employee
        for employee in employees
        if parse_vacant_portage_line(employee.full_name) is not None
        and not _master_rotation_owns_alternate_band(employee.contract_line_type)
    ]
    ft_only = alt_equity_scope == "ft_peers_only"
    if ft_only:
        vacant_employees = [
            employee
            for employee in vacant_employees
            if portage_is_fulltime_catalog_hours(
                float(employee_target_hours.get(employee.id, 0.0))
            )
        ]
    period_dates = list(_daterange(period_start, period_end))
    changed = 0

    def _alt_count(employee_id: str, contract_line: object) -> int:
        return _peer_shift_metrics(
            employee_id,
            assignments,
            shift_templates,
            contract_line,
            period_start,
            period_end,
        )[0]

    for _ in range(max_rounds):
        progress = False

        if not ft_only:
            for employee in sorted(
                vacant_employees,
                key=lambda item: _alt_count(item.id, item.contract_line_type)
                - portage_alt_shift_target(
                    float(employee_target_hours.get(item.id, 0.0))
                ),
                reverse=True,
            ):
                target_alt = portage_alt_shift_target(
                    float(employee_target_hours.get(employee.id, 0.0))
                )
                if target_alt <= 0:
                    continue
                alternate_band = _alternate_band_for_contract_line(employee.contract_line_type)
                while _alt_count(employee.id, employee.contract_line_type) > target_alt:
                    step = False
                    for swap_date in period_dates:
                        if _try_convert_alternate_to_day_on_date(
                            assignments,
                            states,
                            employee_id=employee.id,
                            swap_date=swap_date,
                            employees_by_id=employees_by_id,
                            shift_templates=shift_templates,
                            rules=rules,
                            period_start=period_start,
                            period_end=period_end,
                            availability_blocked=availability_blocked,
                            qual_codes=qual_codes,
                            alternate_band=alternate_band,
                            allow_frozen_swaps=True,
                        ):
                            step = True
                            progress = True
                            changed += 1
                            break
                    if not step:
                        break
        else:
            for employee in sorted(
                vacant_employees,
                key=lambda item: _alt_count(item.id, item.contract_line_type)
                - portage_alt_shift_target(
                    float(employee_target_hours.get(item.id, 0.0))
                ),
                reverse=True,
            ):
                target_alt = portage_alt_shift_target(
                    float(employee_target_hours.get(employee.id, 0.0))
                )
                if target_alt <= 0:
                    continue
                alternate_band = _alternate_band_for_contract_line(employee.contract_line_type)
                while _alt_count(employee.id, employee.contract_line_type) > target_alt + 1:
                    step = False
                    for swap_date in period_dates:
                        if _try_convert_alternate_to_day_on_date(
                            assignments,
                            states,
                            employee_id=employee.id,
                            swap_date=swap_date,
                            employees_by_id=employees_by_id,
                            shift_templates=shift_templates,
                            rules=rules,
                            period_start=period_start,
                            period_end=period_end,
                            availability_blocked=availability_blocked,
                            qual_codes=qual_codes,
                            alternate_band=alternate_band,
                            allow_frozen_swaps=True,
                        ):
                            step = True
                            progress = True
                            changed += 1
                            break
                    if not step:
                        break

        for employee in sorted(
            vacant_employees,
            key=lambda item: portage_alt_shift_target(
                float(employee_target_hours.get(item.id, 0.0))
            )
            - _alt_count(item.id, item.contract_line_type),
            reverse=True,
        ):
            target_alt = portage_alt_shift_target(
                float(employee_target_hours.get(employee.id, 0.0))
            )
            if target_alt <= 0:
                continue
            alternate_band = _alternate_band_for_contract_line(employee.contract_line_type)
            while _alt_count(employee.id, employee.contract_line_type) < target_alt:
                step = False
                for swap_date in period_dates:
                    if _try_convert_day_to_alternate_on_date(
                        assignments,
                        states,
                        employee_id=employee.id,
                        swap_date=swap_date,
                        employees_by_id=employees_by_id,
                        shift_templates=shift_templates,
                        rules=rules,
                        period_start=period_start,
                        period_end=period_end,
                        availability_blocked=availability_blocked,
                        qual_codes=qual_codes,
                        alternate_band=alternate_band,
                        allow_frozen_swaps=True,
                    ):
                        step = True
                        progress = True
                        changed += 1
                        break
                if not step:
                    break

        if not progress:
            break

    return changed


def _try_cross_day_alt_transfer(
    assignments: List[PlannedAssignment],
    states: Dict[str, _EmployeeState],
    *,
    donor_id: str,
    recipient_id: str,
    donor_date: date,
    recipient_date: date,
    employees_by_id: Mapping[str, EmployeeProfile],
    shift_templates: Dict[str, ShiftTemplateInfo],
    rules: JurisdictionRules,
    period_start: date,
    period_end: date,
    availability_blocked: Optional[Mapping[str, Set[date]]],
    qual_codes: Mapping[str, str],
    alternate_band: str,
    allow_frozen_swaps: bool = False,
) -> bool:
    """Move one alternate shift from donor to recipient using two dates (each day pool-neutral)."""

    if donor_date == recipient_date:
        return False
    donor_assignment = _employee_assignment_on_date(
        assignments, employee_id=donor_id, assignment_date=donor_date
    )
    recipient_assignment = _employee_assignment_on_date(
        assignments, employee_id=recipient_id, assignment_date=recipient_date
    )
    if donor_assignment is None or recipient_assignment is None:
        return False
    if getattr(donor_assignment, "master_template_frozen", False) and not allow_frozen_swaps:
        return False
    if getattr(recipient_assignment, "master_template_frozen", False) and not allow_frozen_swaps:
        return False
    if _assignment_band(donor_assignment, shift_templates) != alternate_band:
        return False
    if _assignment_band(recipient_assignment, shift_templates) != "D":
        return False
    cap = CLINICAL_FLOOR.get("EVENING" if alternate_band == "E" else "NIGHT", 2)
    if (
        _daily_pool_band_count(
            assignments,
            shift_templates,
            assignment_date=donor_date,
            band=alternate_band,
        )
        <= cap
    ):
        return False
    if (
        _daily_pool_band_count(
            assignments,
            shift_templates,
            assignment_date=recipient_date,
            band=alternate_band,
        )
        >= cap
    ):
        return False

    if not _try_convert_alternate_to_day_on_date(
        assignments,
        states,
        employee_id=donor_id,
        swap_date=donor_date,
        employees_by_id=employees_by_id,
        shift_templates=shift_templates,
        rules=rules,
        period_start=period_start,
        period_end=period_end,
        availability_blocked=availability_blocked,
        qual_codes=qual_codes,
        alternate_band=alternate_band,
        allow_frozen_swaps=allow_frozen_swaps,
    ):
        return False
    if _try_convert_day_to_alternate_on_date(
        assignments,
        states,
        employee_id=recipient_id,
        swap_date=recipient_date,
        employees_by_id=employees_by_id,
        shift_templates=shift_templates,
        rules=rules,
        period_start=period_start,
        period_end=period_end,
        availability_blocked=availability_blocked,
        qual_codes=qual_codes,
        alternate_band=alternate_band,
        allow_frozen_swaps=allow_frozen_swaps,
    ):
        return True

    _try_convert_day_to_alternate_on_date(
        assignments,
        states,
        employee_id=donor_id,
        swap_date=donor_date,
        employees_by_id=employees_by_id,
        shift_templates=shift_templates,
        rules=rules,
        period_start=period_start,
        period_end=period_end,
        availability_blocked=availability_blocked,
        qual_codes=qual_codes,
        alternate_band=alternate_band,
        allow_frozen_swaps=allow_frozen_swaps,
    )
    return False


def _enforce_alt_shift_peer_day_swaps(
    assignments: List[PlannedAssignment],
    states: Dict[str, _EmployeeState],
    *,
    employees: Sequence[EmployeeProfile],
    shift_templates: Dict[str, ShiftTemplateInfo],
    rules: JurisdictionRules,
    period_start: date,
    period_end: date,
    availability_blocked: Optional[Mapping[str, Set[date]]],
    qual_codes: Mapping[str, str],
    employee_target_hours: Mapping[str, float],
    max_rounds: int = 128,
    alt_equity_scope: str = "all_peers",
    peer_tier: str = "all",
    parity_mode: str = "catalog_target",
    allow_cross_day: bool = True,
    relaxed_swap_labor: bool = False,
    post_pass_guard: Optional[PostPassGuard] = None,
) -> int:
    """
    Move alternate-band shifts between peer lines via same-day E↔D swaps.

    Pool-wide daily E/N counts stay fixed; only per-line totals change.
    """

    from lab_scheduler.scheduling.portage_equity_targets import (
        portage_alt_shift_target,
        portage_alt_shift_target_for_employee,
        portage_is_fulltime_catalog_hours,
    )
    from lab_scheduler.solver.cpsat_fill import _vacant_line_type_groups

    employee_by_id = {employee.id: employee for employee in employees}
    line_groups = _vacant_line_type_groups(employees, employee_target_hours)
    period_dates = list(_daterange(period_start, period_end))
    changed = 0
    ft_only = alt_equity_scope == "ft_peers_only"

    def _tier_member_ids(member_ids: List[str]) -> List[str]:
        if peer_tier == "ft":
            return [
                employee_id
                for employee_id in member_ids
                if portage_is_fulltime_catalog_hours(
                    float(employee_target_hours.get(employee_id, 0.0))
                )
            ]
        if peer_tier == "pt":
            return [
                employee_id
                for employee_id in member_ids
                if not portage_is_fulltime_catalog_hours(
                    float(employee_target_hours.get(employee_id, 0.0))
                )
            ]
        if ft_only:
            return [
                employee_id
                for employee_id in member_ids
                if portage_is_fulltime_catalog_hours(
                    float(employee_target_hours.get(employee_id, 0.0))
                )
            ]
        return list(member_ids)

    for member_ids in line_groups.values():
        member_ids = _tier_member_ids(list(member_ids))
        if len(member_ids) < 2:
            continue
        contract_line = employee_by_id[member_ids[0]].contract_line_type
        if _master_rotation_owns_alternate_band(contract_line):
            continue
        alternate_band = _alternate_band_for_contract_line(contract_line)
        allow_frozen = _equity_allow_frozen_alternate_swap(contract_line, alternate_band)

        for _ in range(max_rounds):
            alt_counts = {
                employee_id: _peer_shift_metrics(
                    employee_id,
                    assignments,
                    shift_templates,
                    contract_line,
                    period_start,
                    period_end,
                )[0]
                for employee_id in member_ids
            }
            if parity_mode == "peer_median":
                sorted_counts = sorted(alt_counts.values())
                median = sorted_counts[len(sorted_counts) // 2]
                targets = {employee_id: median for employee_id in member_ids}
                if max(alt_counts.values()) - min(alt_counts.values()) <= 1:
                    break
            else:
                targets = {
                    employee_id: portage_alt_shift_target_for_employee(
                        employee_by_id[employee_id],
                        float(employee_target_hours.get(employee_id, 0.0)),
                    )
                    for employee_id in member_ids
                }
                if max(alt_counts.values()) - min(alt_counts.values()) <= 1 and all(
                    abs(alt_counts[employee_id] - targets[employee_id]) <= 1
                    for employee_id in member_ids
                ):
                    break

            pool_min = min(alt_counts.values())
            pool_max = max(alt_counts.values())
            progress = False
            donor_ids = sorted(
                member_ids,
                key=lambda employee_id: alt_counts[employee_id],
                reverse=True,
            )
            recipient_ids = sorted(
                member_ids,
                key=lambda employee_id: alt_counts[employee_id],
            )
            for donor_id in donor_ids:
                if alt_counts[donor_id] <= pool_min:
                    continue
                if (
                    parity_mode != "peer_median"
                    and alt_counts[donor_id] <= targets[donor_id]
                    and alt_counts[donor_id] - pool_min <= 1
                ):
                    continue
                for recipient_id in recipient_ids:
                    if recipient_id == donor_id:
                        continue
                    if alt_counts[recipient_id] >= alt_counts[donor_id]:
                        continue
                    if alt_counts[donor_id] - alt_counts[recipient_id] <= 1:
                        continue
                    if (
                        parity_mode != "peer_median"
                        and alt_counts[recipient_id] >= targets[recipient_id]
                        and pool_max - alt_counts[recipient_id] <= 1
                    ):
                        continue
                    for swap_date in period_dates:
                        if _try_peer_same_day_band_swap(
                            assignments,
                            states,
                            donor_id=donor_id,
                            recipient_id=recipient_id,
                            swap_date=swap_date,
                            employees_by_id=employee_by_id,
                            shift_templates=shift_templates,
                            rules=rules,
                            period_start=period_start,
                            period_end=period_end,
                            availability_blocked=availability_blocked,
                            qual_codes=qual_codes,
                            alternate_band=alternate_band,
                            allow_frozen_swaps=allow_frozen,
                            relaxed_labor=relaxed_swap_labor,
                            post_pass_guard=post_pass_guard,
                        ):
                            progress = True
                            changed += 1
                            alt_counts[donor_id] -= 1
                            alt_counts[recipient_id] += 1
                            break
                    if progress:
                        break
                    if not allow_cross_day:
                        continue
                    for donor_date in period_dates:
                        if progress:
                            break
                        for recipient_date in period_dates:
                            if _try_cross_day_alt_transfer(
                                assignments,
                                states,
                                donor_id=donor_id,
                                recipient_id=recipient_id,
                                donor_date=donor_date,
                                recipient_date=recipient_date,
                                employees_by_id=employee_by_id,
                                shift_templates=shift_templates,
                                rules=rules,
                                period_start=period_start,
                                period_end=period_end,
                                availability_blocked=availability_blocked,
                                qual_codes=qual_codes,
                                alternate_band=alternate_band,
                                allow_frozen_swaps=allow_frozen,
                            ):
                                progress = True
                                changed += 1
                                alt_counts[donor_id] -= 1
                                alt_counts[recipient_id] += 1
                                break
                if progress:
                    break
            if not progress:
                break

    return changed


def _enforce_peer_weekend_shift_targets(
    assignments: List[PlannedAssignment],
    states: Dict[str, _EmployeeState],
    *,
    employees: Sequence[EmployeeProfile],
    shift_templates: Dict[str, ShiftTemplateInfo],
    rules: JurisdictionRules,
    period_start: date,
    period_end: date,
    availability_blocked: Optional[Mapping[str, Set[date]]],
    qual_codes: Mapping[str, str],
    employee_target_hours: Mapping[str, float],
    payroll_targets: Mapping[str, float] | None = None,
    catalog_targets: Mapping[str, float] | None = None,
    max_rounds: int = 64,
) -> int:
    """Even out weekend shift days across qual+contract pools (shared weekend ops cap)."""

    from lab_scheduler.scheduling.portage_feasibility import (
        _qual_contract_groups,
        portage_qual_contract_weekend_target_map,
    )

    employee_by_id = {employee.id: employee for employee in employees}
    period_dates = list(_daterange(period_start, period_end))
    weekend_dates = [day for day in period_dates if day.weekday() >= 5]
    changed = 0

    for (qual_code, contract), members in _qual_contract_groups(
        employees, employee_target_hours, qual_codes
    ).items():
        if len(members) < 2:
            continue
        member_ids = [employee_id for employee_id, _hours in members]
        contract_line = employee_by_id[member_ids[0]].contract_line_type or "D/E"
        alternate_band = _alternate_band_for_contract_line(contract_line)
        weekend_targets = portage_qual_contract_weekend_target_map(
            members,
            qual_code=qual_code,
            weekend_day_count=len(weekend_dates),
        )

        for _ in range(max_rounds):
            metrics = {
                employee_id: _peer_shift_metrics(
                    employee_id,
                    assignments,
                    shift_templates,
                    contract_line,
                    period_start,
                    period_end,
                )
                for employee_id in member_ids
            }
            weekend_counts = {employee_id: metrics[employee_id][2] for employee_id in member_ids}
            if all(
                weekend_counts[employee_id] == weekend_targets[employee_id]
                for employee_id in member_ids
            ):
                break

            progress = False
            donor_id = max(
                member_ids,
                key=lambda employee_id: weekend_counts[employee_id]
                - weekend_targets[employee_id],
            )
            recipient_id = max(
                member_ids,
                key=lambda employee_id: weekend_targets[employee_id]
                - weekend_counts[employee_id],
            )
            if weekend_counts[donor_id] <= weekend_targets[donor_id]:
                break
            if weekend_counts[recipient_id] >= weekend_targets[recipient_id]:
                break

            weekend_allow_frozen = _equity_allow_frozen_alternate_swap(
                contract_line,
                alternate_band,
            )
            nights_locked = _master_rotation_owns_alternate_band(contract_line)

            for swap_date in weekend_dates:
                if not nights_locked and _try_peer_same_day_band_swap(
                    assignments,
                    states,
                    donor_id=donor_id,
                    recipient_id=recipient_id,
                    swap_date=swap_date,
                    employees_by_id=employee_by_id,
                    shift_templates=shift_templates,
                    rules=rules,
                    period_start=period_start,
                    period_end=period_end,
                    availability_blocked=availability_blocked,
                    qual_codes=qual_codes,
                    alternate_band=alternate_band,
                    allow_frozen_swaps=weekend_allow_frozen,
                ):
                    progress = True
                    changed += 1
                    break
                if not nights_locked and _try_peer_transfer_shift_on_date(
                    assignments,
                    states,
                    donor_id=donor_id,
                    recipient_id=recipient_id,
                    swap_date=swap_date,
                    employees_by_id=employee_by_id,
                    shift_templates=shift_templates,
                    rules=rules,
                    period_start=period_start,
                    period_end=period_end,
                    availability_blocked=availability_blocked,
                    qual_codes=qual_codes,
                    expected_donor_band=alternate_band,
                    allow_frozen_swaps=weekend_allow_frozen,
                    payroll_targets=payroll_targets,
                    catalog_targets=catalog_targets,
                ) or _try_peer_transfer_shift_on_date(
                    assignments,
                    states,
                    donor_id=donor_id,
                    recipient_id=recipient_id,
                    swap_date=swap_date,
                    employees_by_id=employee_by_id,
                    shift_templates=shift_templates,
                    rules=rules,
                    period_start=period_start,
                    period_end=period_end,
                    availability_blocked=availability_blocked,
                    qual_codes=qual_codes,
                    expected_donor_band="D",
                    allow_frozen_swaps=True,
                    payroll_targets=payroll_targets,
                    catalog_targets=catalog_targets,
                ):
                    progress = True
                    changed += 1
                    break
                if _try_peer_give_shift_on_date(
                    assignments,
                    states,
                    donor_id=donor_id,
                    recipient_id=recipient_id,
                    swap_date=swap_date,
                    employees_by_id=employee_by_id,
                    shift_templates=shift_templates,
                    rules=rules,
                    period_start=period_start,
                    period_end=period_end,
                    availability_blocked=availability_blocked,
                    qual_codes=qual_codes,
                    alternate_band=alternate_band,
                    allow_frozen_swaps=True,
                    payroll_targets=payroll_targets,
                    catalog_targets=catalog_targets,
                ):
                    progress = True
                    changed += 1
                    break
            if not progress:
                break

    return changed


def _conversion_first_clinical_floor_fill(
    assignments: List[PlannedAssignment],
    states: Dict[str, _EmployeeState],
    *,
    employees: Sequence[EmployeeProfile],
    shift_templates: Dict[str, ShiftTemplateInfo],
    rules: JurisdictionRules,
    period_start: date,
    period_end: date,
    availability_blocked: Optional[Mapping[str, Set[date]]],
    qual_codes: Mapping[str, str],
    weekends_only: bool = False,
) -> int:
    """
    Pool-neutral D→E/N on matching contract lines before net-new clinical assignments.

    Prefer D-heavy vacant lines so weekday day-shift capacity is not increased.
    """

    changed = 0
    employees_by_id = {employee.id: employee for employee in employees}
    day_span = max((period_end - period_start).days + 1, 1)
    band_specs = (
        ("N", ("D/N",)),
        ("E", ("D/E",)),
    )

    for _round in range(day_span * 4):
        progress = False
        candidate_dates = [
            assignment_date
            for assignment_date in _daterange(period_start, period_end)
            if not weekends_only or assignment_date.weekday() >= 5
        ]
        dates_by_deficit = sorted(
            candidate_dates,
            key=lambda assignment_date: (
                _daily_pool_band_count(
                    assignments,
                    shift_templates,
                    assignment_date=assignment_date,
                    band="N",
                ),
                _daily_pool_band_count(
                    assignments,
                    shift_templates,
                    assignment_date=assignment_date,
                    band="E",
                ),
            ),
        )
        for assignment_date in dates_by_deficit:
            for alternate_band, contract_lines in band_specs:
                cap = CLINICAL_FLOOR.get(
                    "EVENING" if alternate_band == "E" else "NIGHT",
                    2,
                )
                if (
                    _daily_pool_band_count(
                        assignments,
                        shift_templates,
                        assignment_date=assignment_date,
                        band=alternate_band,
                    )
                    >= cap
                ):
                    continue

                candidates: List[EmployeeProfile] = []
                for employee in employees:
                    if parse_vacant_portage_line(employee.full_name) is None:
                        continue
                    if (employee.contract_line_type or "").upper() not in contract_lines:
                        continue
                    existing = _employee_assignment_on_date(
                        assignments,
                        employee_id=employee.id,
                        assignment_date=assignment_date,
                    )
                    if existing is None:
                        continue
                    if _assignment_band(existing, shift_templates) != "D":
                        continue
                    candidates.append(employee)

                candidates.sort(
                    key=lambda employee: states[employee.id].total_hours,
                    reverse=True,
                )
                for employee in candidates:
                    if _try_convert_day_to_alternate_on_date(
                        assignments,
                        states,
                        employee_id=employee.id,
                        swap_date=assignment_date,
                        employees_by_id=employees_by_id,
                        shift_templates=shift_templates,
                        rules=rules,
                        period_start=period_start,
                        period_end=period_end,
                        availability_blocked=availability_blocked,
                        qual_codes=qual_codes,
                        alternate_band=alternate_band,
                        allow_frozen_swaps=True,
                    ):
                        changed += 1
                        progress = True
                        break
        if not progress:
            break

    return changed


def _fill_remaining_clinical_seats_by_conversion(
    assignments: List[PlannedAssignment],
    states: Dict[str, _EmployeeState],
    *,
    employees: Sequence[EmployeeProfile],
    expanded_slots: Sequence[ExpandedScheduleSlot],
    shift_templates: Dict[str, ShiftTemplateInfo],
    shift_required_qualifications: Dict[str, Set[str]],
    rules: JurisdictionRules,
    period_start: date,
    period_end: date,
    availability_blocked: Optional[Mapping[str, Set[date]]],
    qual_codes: Mapping[str, str],
    payroll_targets: Mapping[str, float] | None = None,
    catalog_targets: Mapping[str, float] | None = None,
) -> int:
    """Convert day-band shifts on unfilled clinical E/N seats before net-new assignments."""

    from lab_scheduler.scheduling.clinical_seats import slot_is_filled

    employees_by_id = {employee.id: employee for employee in employees}
    _restore_missing_catalog_master_assignments(
        assignments,
        states,
        employees=employees,
        shift_templates=shift_templates,
        rules=rules,
        period_start=period_start,
        period_end=period_end,
        availability_blocked=availability_blocked,
        qual_codes=qual_codes,
        weekends_only=True,
        payroll_targets=payroll_targets or catalog_targets,
        catalog_targets=catalog_targets,
    )
    _rebuild_states_from_assignments(states, assignments, shift_templates)
    fill_counts = _seat_fill_counts(assignments, employees, qual_codes)
    changed = 0
    fixed_dates: set[date] = set()

    open_slots = sorted(
        (
            slot
            for slot in expanded_slots
            if is_clinical_floor_pool(slot.role_pool_id)
            and shift_templates[slot.shift_id].code in ("MORNING", "EVENING", "NIGHT")
            and period_start <= slot.assignment_date <= period_end
            and not slot_is_filled(slot, fill_counts)
        ),
        key=lambda slot: (slot.assignment_date, shift_templates[slot.shift_id].code, slot.seat_index),
    )

    for slot in open_slots:
        if slot_is_filled(slot, fill_counts):
            continue
        shift_code = shift_templates[slot.shift_id].code
        if shift_code == "MORNING":
            alternate_band = "D"
            contract_lines = ("D/E", "D/N")
        elif shift_code == "EVENING":
            alternate_band = "E"
            contract_lines = ("D/E",)
        else:
            alternate_band = "N"
            contract_lines = ("D/N",)
        required_qual = slot.required_qual_code
        slot_filled = False
        if shift_code in ("EVENING", "NIGHT"):
            pool_cap = CLINICAL_FLOOR.get(
                "EVENING" if shift_code == "EVENING" else "NIGHT",
                2,
            )
            pool_count = _daily_pool_band_count(
                assignments,
                shift_templates,
                assignment_date=slot.assignment_date,
                band=alternate_band,
            )
            if pool_count < pool_cap:
                pool_boost = [
                    employee
                    for employee in employees
                    if parse_vacant_portage_line(employee.full_name) is not None
                    and (employee.contract_line_type or "").upper() in contract_lines
                ]
                pool_boost.sort(
                    key=lambda employee: states[employee.id].total_hours,
                    reverse=True,
                )
                for employee in pool_boost:
                    if _try_convert_day_to_alternate_on_date(
                        assignments,
                        states,
                        employee_id=employee.id,
                        swap_date=slot.assignment_date,
                        employees_by_id=employees_by_id,
                        shift_templates=shift_templates,
                        rules=rules,
                        period_start=period_start,
                        period_end=period_end,
                        availability_blocked=availability_blocked,
                        qual_codes=qual_codes,
                        alternate_band=alternate_band,
                        allow_frozen_swaps=True,
                    ):
                        changed += 1
                        pool_count += 1
                        if pool_count >= pool_cap:
                            break
        candidates = [
            employee
            for employee in employees
            if parse_vacant_portage_line(employee.full_name) is not None
            and (employee.contract_line_type or "").upper() in contract_lines
            and infer_qual_code(employee, qual_codes=qual_codes) == required_qual
        ]
        candidates.sort(key=lambda employee: states[employee.id].total_hours, reverse=True)
        if shift_code == "EVENING" and required_qual == "MLA":
            mlt_on_evening = [
                assignment.employee_id
                for assignment in assignments
                if assignment.assignment_date == slot.assignment_date
                and _assignment_band(assignment, shift_templates) == "E"
                and infer_qual_code(
                    employees_by_id[assignment.employee_id], qual_codes=qual_codes
                )
                == "MLT"
            ]
            if len(mlt_on_evening) >= 2:
                _try_convert_alternate_to_day_on_date(
                    assignments,
                    states,
                    employee_id=mlt_on_evening[0],
                    swap_date=slot.assignment_date,
                    employees_by_id=employees_by_id,
                    shift_templates=shift_templates,
                    rules=rules,
                    period_start=period_start,
                    period_end=period_end,
                    availability_blocked=availability_blocked,
                    qual_codes=qual_codes,
                    alternate_band="E",
                    allow_frozen_swaps=True,
                )
            for mlt_id in mlt_on_evening:
                mla_partners = [
                    employee
                    for employee in employees
                    if infer_qual_code(employee, qual_codes=qual_codes) == "MLA"
                    and employee.id != mlt_id
                    and slot.assignment_date in states[employee.id].work_dates
                ]
                for employee in mla_partners:
                    if _swap_employee_assignments_on_date(
                        assignments,
                        states,
                        employee_a_id=mlt_id,
                        employee_b_id=employee.id,
                        assignment_date=slot.assignment_date,
                        employees_by_id=employees_by_id,
                        shift_templates=shift_templates,
                        rules=rules,
                        period_start=period_start,
                        period_end=period_end,
                        availability_blocked=availability_blocked,
                        qual_codes=qual_codes,
                    ):
                        seat_key = (
                            slot.assignment_date,
                            slot.shift_id,
                            slot.required_qual_code,
                        )
                        fill_counts[seat_key] = fill_counts.get(seat_key, 0) + 1
                        changed += 1
                        slot_filled = True
                        break
                if slot_filled:
                    break
            if slot_filled:
                fixed_dates.add(slot.assignment_date)
                continue
        if slot.assignment_date in fixed_dates:
            continue
        if _rebalance_clinical_seat_for_unfilled_slot(
            assignments,
            states,
            slot=slot,
            employees=employees,
            shift_templates=shift_templates,
            rules=rules,
            period_start=period_start,
            period_end=period_end,
            availability_blocked=availability_blocked,
            qual_codes=qual_codes,
        ):
            seat_key = (slot.assignment_date, slot.shift_id, slot.required_qual_code)
            fill_counts[seat_key] = fill_counts.get(seat_key, 0) + 1
            changed += 1
            continue
        if alternate_band == "D":
            for employee in candidates:
                for from_band in ("N", "E"):
                    if _try_convert_alternate_to_day_on_date(
                        assignments,
                        states,
                        employee_id=employee.id,
                        swap_date=slot.assignment_date,
                        employees_by_id=employees_by_id,
                        shift_templates=shift_templates,
                        rules=rules,
                        period_start=period_start,
                        period_end=period_end,
                        availability_blocked=availability_blocked,
                        qual_codes=qual_codes,
                        alternate_band=from_band,
                        allow_frozen_swaps=True,
                    ):
                        seat_key = (
                            slot.assignment_date,
                            slot.shift_id,
                            slot.required_qual_code,
                        )
                        fill_counts[seat_key] = fill_counts.get(seat_key, 0) + 1
                        changed += 1
                        slot_filled = True
                        break
                if slot_filled:
                    break
            if slot_filled:
                continue
        if alternate_band != "D":
            for employee in candidates:
                if _try_convert_day_to_alternate_on_date(
                    assignments,
                    states,
                    employee_id=employee.id,
                    swap_date=slot.assignment_date,
                    employees_by_id=employees_by_id,
                    shift_templates=shift_templates,
                    rules=rules,
                    period_start=period_start,
                    period_end=period_end,
                    availability_blocked=availability_blocked,
                    qual_codes=qual_codes,
                    alternate_band=alternate_band,
                    allow_frozen_swaps=True,
                ):
                    seat_key = (slot.assignment_date, slot.shift_id, slot.required_qual_code)
                    fill_counts[seat_key] = fill_counts.get(seat_key, 0) + 1
                    changed += 1
                    slot_filled = True
                    break
                if alternate_band == "E" and _try_convert_night_to_evening_on_date(
                    assignments,
                    states,
                    employee_id=employee.id,
                    swap_date=slot.assignment_date,
                    employees_by_id=employees_by_id,
                    shift_templates=shift_templates,
                    rules=rules,
                    period_start=period_start,
                    period_end=period_end,
                    availability_blocked=availability_blocked,
                    qual_codes=qual_codes,
                    allow_frozen_swaps=True,
                ):
                    seat_key = (slot.assignment_date, slot.shift_id, slot.required_qual_code)
                    fill_counts[seat_key] = fill_counts.get(seat_key, 0) + 1
                    changed += 1
                    slot_filled = True
                    break
                if alternate_band == "E" and _try_convert_morning_to_evening_on_date(
                    assignments,
                    states,
                    employee_id=employee.id,
                    swap_date=slot.assignment_date,
                    employees_by_id=employees_by_id,
                    shift_templates=shift_templates,
                    rules=rules,
                    period_start=period_start,
                    period_end=period_end,
                    availability_blocked=availability_blocked,
                    qual_codes=qual_codes,
                    allow_frozen_swaps=True,
                ):
                    seat_key = (slot.assignment_date, slot.shift_id, slot.required_qual_code)
                    fill_counts[seat_key] = fill_counts.get(seat_key, 0) + 1
                    changed += 1
                    slot_filled = True
                    break
        if slot_filled:
            continue

        shift_id = slot.shift_id
        template = shift_templates[shift_id]
        required = shift_required_qualifications.get(shift_id, set())
        chosen, provisional, _rejections = _resolve_mandatory_clinical_pick(
            employees=employees,
            required=required,
            states=states,
            assignment_date=slot.assignment_date,
            template=template,
            qual_codes=qual_codes,
            required_qual_code=slot.required_qual_code,
            availability_blocked=availability_blocked,
            role_pool_id=slot.role_pool_id,
            shift_templates=shift_templates,
            clinical_mandatory=True,
            period_start=period_start,
        )
        if chosen is None:
            continue
        supersede_day = False
        if slot.assignment_date in states[chosen.id].work_dates:
            from lab_scheduler.scheduling.clinical_seats import (
                vacant_may_supersede_for_clinical_band,
            )

            supersede_day = vacant_may_supersede_for_clinical_band(
                profile=chosen,
                assignment_date=slot.assignment_date,
                target_shift_code=template.code,
                state=states[chosen.id],
                shift_templates=shift_templates,
                period_start=period_start,
            )
            if not supersede_day:
                continue
        violation = _would_violate_labor_rules(
            states[chosen.id],
            slot.assignment_date,
            template,
            shift_templates,
            rules,
            period_start,
            period_end,
            availability_blocked,
            forced_clinical_ot=True,
            mandatory_assignment=True,
        )
        if violation:
            continue
        shift_hours = template.duration_minutes / 60.0
        if supersede_day:
            if not _supersede_vacant_clinical_day_assignment(
                employee=chosen,
                assignment_date=slot.assignment_date,
                new_shift_id=shift_id,
                assignments=assignments,
                states=states,
                shift_templates=shift_templates,
                fill_counts=fill_counts,
                qual_codes=qual_codes,
                allow_frozen_supersede=True,
                period_start=period_start,
            ):
                continue
        _apply_assignment_to_state(
            states[chosen.id],
            slot.assignment_date,
            shift_id,
            shift_hours,
        )
        assignments.append(
            PlannedAssignment(
                employee_id=chosen.id,
                shift_template_id=shift_id,
                assignment_date=slot.assignment_date,
                forced_clinical_ot=True,
                contract_line_exception=provisional is not None,
            )
        )
        seat_key = (slot.assignment_date, slot.shift_id, slot.required_qual_code)
        fill_counts[seat_key] = fill_counts.get(seat_key, 0) + 1
        changed += 1

    return changed


def _rebalance_clinical_seat_for_unfilled_slot(
    assignments: List[PlannedAssignment],
    states: Dict[str, _EmployeeState],
    *,
    slot: ExpandedScheduleSlot,
    employees: Sequence[EmployeeProfile],
    shift_templates: Dict[str, ShiftTemplateInfo],
    rules: JurisdictionRules,
    period_start: date,
    period_end: date,
    availability_blocked: Optional[Mapping[str, Set[date]]],
    qual_codes: Mapping[str, str],
) -> bool:
    """
    When the pool band is full but the wrong qual holds a seat, swap MLT off E/N
    and move matching MLA from day band onto the open clinical seat.
    """

    required_qual = slot.required_qual_code
    if required_qual not in ("MLA", "MLT"):
        return False

    employees_by_id = {employee.id: employee for employee in employees}
    shift_code = shift_templates[slot.shift_id].code
    assignment_date = slot.assignment_date

    if shift_code == "MORNING":
        for alternate_band in ("N", "E"):
            for assignment in assignments:
                if assignment.assignment_date != assignment_date:
                    continue
                if _assignment_band(assignment, shift_templates) != alternate_band:
                    continue
                employee = employees_by_id.get(assignment.employee_id)
                if employee is None:
                    continue
                if infer_qual_code(employee, qual_codes=qual_codes) != required_qual:
                    continue
                if _try_convert_alternate_to_day_on_date(
                    assignments,
                    states,
                    employee_id=employee.id,
                    swap_date=assignment_date,
                    employees_by_id=employees_by_id,
                    shift_templates=shift_templates,
                    rules=rules,
                    period_start=period_start,
                    period_end=period_end,
                    availability_blocked=availability_blocked,
                    qual_codes=qual_codes,
                    alternate_band=alternate_band,
                    allow_frozen_swaps=True,
                ):
                    return True
        return False

    if shift_code not in ("EVENING", "NIGHT"):
        return False
    alternate_band = "E" if shift_code == "EVENING" else "N"
    cap = CLINICAL_FLOOR.get("EVENING" if shift_code == "EVENING" else "NIGHT", 2)
    pool_count = _daily_pool_band_count(
        assignments,
        shift_templates,
        assignment_date=assignment_date,
        band=alternate_band,
    )
    if pool_count < cap:
        has_wrong_qual_in_band = False
        for assignment in assignments:
            if assignment.assignment_date != assignment_date:
                continue
            if _assignment_band(assignment, shift_templates) != alternate_band:
                continue
            employee = employees_by_id.get(assignment.employee_id)
            if employee is None:
                continue
            if infer_qual_code(employee, qual_codes=qual_codes) != required_qual:
                has_wrong_qual_in_band = True
                break
        if not has_wrong_qual_in_band:
            return False

    excess_qual = "MLT" if required_qual == "MLA" else "MLA"

    excess_employee_id: Optional[str] = None
    for assignment in assignments:
        if assignment.assignment_date != assignment_date:
            continue
        if _assignment_band(assignment, shift_templates) != alternate_band:
            continue
        employee = employees_by_id.get(assignment.employee_id)
        if employee is None:
            continue
        if infer_qual_code(employee, qual_codes=qual_codes) != excess_qual:
            continue
        excess_employee_id = employee.id
        break
    if excess_employee_id is None:
        return False

    deficit_employee_id: Optional[str] = None
    for assignment in assignments:
        if assignment.assignment_date != assignment_date:
            continue
        if _assignment_band(assignment, shift_templates) != "D":
            continue
        employee = employees_by_id.get(assignment.employee_id)
        if employee is None:
            continue
        if infer_qual_code(employee, qual_codes=qual_codes) != required_qual:
            continue
        deficit_employee_id = employee.id
        break

    if deficit_employee_id is not None:
        if not _try_convert_alternate_to_day_on_date(
            assignments,
            states,
            employee_id=excess_employee_id,
            swap_date=assignment_date,
            employees_by_id=employees_by_id,
            shift_templates=shift_templates,
            rules=rules,
            period_start=period_start,
            period_end=period_end,
            availability_blocked=availability_blocked,
            qual_codes=qual_codes,
            alternate_band=alternate_band,
            allow_frozen_swaps=True,
        ):
            return False
        if _try_convert_day_to_alternate_on_date(
            assignments,
            states,
            employee_id=deficit_employee_id,
            swap_date=assignment_date,
            employees_by_id=employees_by_id,
            shift_templates=shift_templates,
            rules=rules,
            period_start=period_start,
            period_end=period_end,
            availability_blocked=availability_blocked,
            qual_codes=qual_codes,
            alternate_band=alternate_band,
            allow_frozen_swaps=True,
        ):
            return True

    if not _try_convert_alternate_to_day_on_date(
        assignments,
        states,
        employee_id=excess_employee_id,
        swap_date=assignment_date,
        employees_by_id=employees_by_id,
        shift_templates=shift_templates,
        rules=rules,
        period_start=period_start,
        period_end=period_end,
        availability_blocked=availability_blocked,
        qual_codes=qual_codes,
        alternate_band=alternate_band,
        allow_frozen_swaps=True,
    ):
        return False

    contract_lines = ("D/N",) if alternate_band == "N" else ("D/E",)
    for employee in sorted(
        (
            candidate
            for candidate in employees
            if parse_vacant_portage_line(candidate.full_name) is not None
            and (candidate.contract_line_type or "").upper() in contract_lines
            and infer_qual_code(candidate, qual_codes=qual_codes) == required_qual
        ),
        key=lambda candidate: states[candidate.id].total_hours,
    ):
        if _try_convert_day_to_alternate_on_date(
            assignments,
            states,
            employee_id=employee.id,
            swap_date=assignment_date,
            employees_by_id=employees_by_id,
            shift_templates=shift_templates,
            rules=rules,
            period_start=period_start,
            period_end=period_end,
            availability_blocked=availability_blocked,
            qual_codes=qual_codes,
            alternate_band=alternate_band,
            allow_frozen_swaps=True,
        ):
            return True
    return False


def _lift_parttime_alt_toward_pool_median(
    assignments: List[PlannedAssignment],
    states: Dict[str, _EmployeeState],
    *,
    employees: Sequence[EmployeeProfile],
    shift_templates: Dict[str, ShiftTemplateInfo],
    rules: JurisdictionRules,
    period_start: date,
    period_end: date,
    availability_blocked: Optional[Mapping[str, Set[date]]],
    qual_codes: Mapping[str, str],
    employee_target_hours: Mapping[str, float],
    max_rounds: int = 96,
) -> int:
    """
    Raise part-time vacant lines toward the within-pool alternate-band median when
    daily E/N pool capacity allows (gap-fill after full-time rotation).
    """

    from lab_scheduler.scheduling.portage_equity_targets import (
        portage_alt_shift_target,
        portage_alt_shift_target_for_employee,
        portage_is_fulltime_catalog_hours,
    )
    from lab_scheduler.solver.cpsat_fill import _vacant_line_type_groups

    employee_by_id = {employee.id: employee for employee in employees}
    line_groups = _vacant_line_type_groups(employees, employee_target_hours)
    period_dates = list(_daterange(period_start, period_end))
    changed = 0

    def _lift_pt_toward_goal(employee_id: str, goal: int) -> bool:
        if goal <= 0:
            return False
        contract_line = employee_by_id[employee_id].contract_line_type
        alternate_band = _alternate_band_for_contract_line(contract_line)
        current = _peer_shift_metrics(
            employee_id,
            assignments,
            shift_templates,
            contract_line,
            period_start,
            period_end,
        )[0]
        if current >= goal:
            return False
        for swap_date in period_dates:
            if _try_convert_day_to_alternate_on_date(
                assignments,
                states,
                employee_id=employee_id,
                swap_date=swap_date,
                employees_by_id=employee_by_id,
                shift_templates=shift_templates,
                rules=rules,
                period_start=period_start,
                period_end=period_end,
                availability_blocked=availability_blocked,
                qual_codes=qual_codes,
                alternate_band=alternate_band,
                allow_frozen_swaps=True,
            ):
                return True
        return False

    for member_ids in line_groups.values():
        pt_ids = [
            employee_id
            for employee_id in member_ids
            if not portage_is_fulltime_catalog_hours(
                float(employee_target_hours.get(employee_id, 0.0))
            )
            and not _master_rotation_owns_alternate_band(
                employee_by_id[employee_id].contract_line_type
            )
        ]
        if not pt_ids:
            continue
        catalog_alt_targets = {
            employee_id: portage_alt_shift_target_for_employee(
                employee_by_id[employee_id],
                float(employee_target_hours.get(employee_id, 0.0)),
            )
            for employee_id in pt_ids
        }

        for _ in range(max_rounds):
            alt_counts = {
                employee_id: _peer_shift_metrics(
                    employee_id,
                    assignments,
                    shift_templates,
                    employee_by_id[employee_id].contract_line_type,
                    period_start,
                    period_end,
                )[0]
                for employee_id in pt_ids
            }
            sorted_counts = sorted(alt_counts.values())
            median = sorted_counts[len(sorted_counts) // 2]
            spread = max(alt_counts.values()) - min(alt_counts.values())
            below_catalog = any(
                alt_counts[employee_id] < catalog_alt_targets[employee_id]
                for employee_id in pt_ids
            )
            if spread <= 1 and not below_catalog:
                break
            progress = False
            for recipient_id in sorted(
                pt_ids,
                key=lambda employee_id: (
                    catalog_alt_targets[employee_id] - alt_counts[employee_id],
                    median - alt_counts[employee_id],
                ),
                reverse=True,
            ):
                goal = max(median, catalog_alt_targets[recipient_id])
                if alt_counts[recipient_id] >= goal:
                    continue
                if _lift_pt_toward_goal(recipient_id, goal):
                    progress = True
                    changed += 1
                    break
            if not progress:
                break

    return changed


def _post_clinical_alt_equity_pass(
    result: AutoGenerateResult,
    *,
    states: Dict[str, _EmployeeState],
    employees: Sequence[EmployeeProfile],
    expanded_slots: Sequence[ExpandedScheduleSlot],
    shift_templates: Dict[str, ShiftTemplateInfo],
    shift_required_qualifications: Dict[str, Set[str]],
    rules: JurisdictionRules,
    period_start: date,
    period_end: date,
    availability_blocked: Optional[Mapping[str, Set[date]]],
    qual_codes: Mapping[str, str],
    catalog_targets: Mapping[str, float],
    scheduling_policy: Optional["PortageSchedulingPolicy"] = None,
    post_pass_guard: Optional[PostPassGuard] = None,
    payroll_targets: Mapping[str, float] | None = None,
    ft_alt_equity: bool = True,
) -> int:
    """
    Pool-neutral alternate-band rebalance after clinical lock.

    Full-time peers are evened to the catalog ~20% target; part-time peers are
    evened to the within-pool median so gap-fill lines stay similar to each other.
    Same-day swaps preserve daily E/N pool counts; cross-day moves only when the
    donor date can give up an alternate band without dropping below 2E/2N.
    """

    from lab_scheduler.scheduling.portage_equity_policy import (
        CLINICAL_AND_HOURS_FIRST,
        PortageSchedulingPolicy,
    )

    policy = scheduling_policy or CLINICAL_AND_HOURS_FIRST
    if policy.id != CLINICAL_AND_HOURS_FIRST.id:
        return 0

    alt_scope = policy.alt_equity_scope
    total_edits = 0
    guard = post_pass_guard or _post_pass_guard_for_result(result)

    for _ in range(6):
        edits = 0
        if ft_alt_equity:
            edits += _enforce_alt_shift_peer_day_swaps(
                result.assignments,
                states,
                employees=employees,
                shift_templates=shift_templates,
                rules=rules,
                period_start=period_start,
                period_end=period_end,
                availability_blocked=availability_blocked,
                qual_codes=qual_codes,
                employee_target_hours=catalog_targets,
                max_rounds=256,
                alt_equity_scope=alt_scope,
                peer_tier="ft",
                parity_mode="catalog_target",
                allow_cross_day=True,
                relaxed_swap_labor=True,
                post_pass_guard=guard,
            )
        edits += _enforce_alt_shift_peer_day_swaps(
            result.assignments,
            states,
            employees=employees,
            shift_templates=shift_templates,
            rules=rules,
            period_start=period_start,
            period_end=period_end,
            availability_blocked=availability_blocked,
            qual_codes=qual_codes,
            employee_target_hours=catalog_targets,
            max_rounds=192,
            alt_equity_scope="all_peers",
            peer_tier="pt",
            parity_mode="peer_median",
            allow_cross_day=True,
            relaxed_swap_labor=True,
            post_pass_guard=guard,
        )
        edits += _lift_parttime_alt_toward_pool_median(
            result.assignments,
            states,
            employees=employees,
            shift_templates=shift_templates,
            rules=rules,
            period_start=period_start,
            period_end=period_end,
            availability_blocked=availability_blocked,
            qual_codes=qual_codes,
            employee_target_hours=catalog_targets,
            max_rounds=96,
        )
        if ft_alt_equity:
            edits += _finalize_fulltime_peer_alt_parity(
                result.assignments,
                states,
                employees=employees,
                shift_templates=shift_templates,
                rules=rules,
                period_start=period_start,
                period_end=period_end,
                availability_blocked=availability_blocked,
                qual_codes=qual_codes,
                employee_target_hours=catalog_targets,
                max_rounds=64,
            )
        if edits == 0:
            break
        total_edits += edits

    if payroll_targets is not None:
        for _ in range(4):
            weekend_edits = _enforce_peer_weekend_shift_targets(
                result.assignments,
                states,
                employees=employees,
                shift_templates=shift_templates,
                rules=rules,
                period_start=period_start,
                period_end=period_end,
                availability_blocked=availability_blocked,
                qual_codes=qual_codes,
                employee_target_hours=catalog_targets,
                payroll_targets=payroll_targets,
                catalog_targets=catalog_targets,
                max_rounds=32,
            )
            if not weekend_edits:
                break
            total_edits += weekend_edits
            _rebuild_states_from_assignments(states, result.assignments, shift_templates)

    fill_counts = _seat_fill_counts(result.assignments, employees, qual_codes)
    if evening_night_clinical_seats_satisfied(
        fill_counts=fill_counts,
        expanded_slots=expanded_slots,
        shift_templates=shift_templates,
        period_start=period_start,
        period_end=period_end,
    ):
        return total_edits

    guard = post_pass_guard or _post_pass_guard_for_result(result)
    before = len(result.assignments)
    _extend_evening_night_clinical_lockdown(
        result,
        employees=employees,
        states=states,
        expanded_slots=expanded_slots,
        shift_templates=shift_templates,
        shift_required_qualifications=shift_required_qualifications,
        rules=rules,
        fill_counts=fill_counts,
        availability_blocked=availability_blocked,
        qual_codes=qual_codes,
        period_start=period_start,
        period_end=period_end,
        log_critical_gaps=False,
        post_pass_guard=guard,
        allow_frozen_clinical_supersede=True,
        max_rounds=12,
        weekend_first=True,
        payroll_targets=payroll_targets,
        catalog_targets=catalog_targets,
    )
    if len(result.assignments) != before:
        total_edits += 1
    return total_edits


def _heal_required_coverage_after_catalog_trim(
    result: AutoGenerateResult,
    *,
    states: Dict[str, _EmployeeState],
    employees: Sequence[EmployeeProfile],
    expanded_slots: Sequence[ExpandedScheduleSlot],
    shift_templates: Dict[str, ShiftTemplateInfo],
    shift_required_qualifications: Dict[str, Set[str]],
    rules: JurisdictionRules,
    period_start: date,
    period_end: date,
    availability_blocked: Optional[Mapping[str, Set[date]]],
    qual_codes: Mapping[str, str],
    catalog_targets: Mapping[str, float],
    target_hours_map: Mapping[str, float],
    period_target_hours: Mapping[str, float],
    filled_smooth_seats: Set[Tuple[date, str, Optional[str], int]],
    post_pass_guard: PostPassGuard,
) -> None:
    """Re-close qual-specific demand seats after catalog/day trims removed assignments."""

    fill_counts = _seat_fill_counts(result.assignments, employees, qual_codes)
    if _required_coverage_slots_satisfied(
        expanded_slots=expanded_slots,
        fill_counts=fill_counts,
        shift_templates=shift_templates,
    ):
        return

    _extend_evening_night_clinical_lockdown(
        result,
        employees=employees,
        states=states,
        expanded_slots=expanded_slots,
        shift_templates=shift_templates,
        shift_required_qualifications=shift_required_qualifications,
        rules=rules,
        fill_counts=fill_counts,
        availability_blocked=availability_blocked,
        qual_codes=qual_codes,
        period_start=period_start,
        period_end=period_end,
        log_critical_gaps=False,
        post_pass_guard=post_pass_guard,
        allow_frozen_clinical_supersede=True,
        max_rounds=8,
        weekend_first=True,
        payroll_targets=target_hours_map,
        catalog_targets=catalog_targets,
    )
    _rebuild_states_from_assignments(states, result.assignments, shift_templates)
    fill_counts = _seat_fill_counts(result.assignments, employees, qual_codes)
    if not _required_coverage_slots_satisfied(
        expanded_slots=expanded_slots,
        fill_counts=fill_counts,
        shift_templates=shift_templates,
    ):
        _fill_remaining_clinical_seats_by_conversion(
            result.assignments,
            states,
            employees=employees,
            expanded_slots=expanded_slots,
            shift_templates=shift_templates,
            shift_required_qualifications=shift_required_qualifications,
            rules=rules,
            period_start=period_start,
            period_end=period_end,
            availability_blocked=availability_blocked,
            qual_codes=qual_codes,
            payroll_targets=target_hours_map,
            catalog_targets=catalog_targets,
        )
        _rebuild_states_from_assignments(states, result.assignments, shift_templates)
        fill_counts = _seat_fill_counts(result.assignments, employees, qual_codes)

    for _ in range(6):
        fill_counts = _seat_fill_counts(result.assignments, employees, qual_codes)
        if _required_coverage_slots_satisfied(
            expanded_slots=expanded_slots,
            fill_counts=fill_counts,
            shift_templates=shift_templates,
        ):
            break
        batch = _clinical_floor_lock_pass(
            employees=employees,
            states=states,
            expanded_slots=expanded_slots,
            shift_templates=shift_templates,
            shift_required_qualifications=shift_required_qualifications,
            rules=rules,
            period_start=period_start,
            period_end=period_end,
            availability_blocked=availability_blocked,
            qual_codes=qual_codes,
            target_hours_map=target_hours_map,
            period_target_hours=period_target_hours,
            fill_counts=fill_counts,
            filled_smooth_seats=filled_smooth_seats,
            prioritize_coverage=True,
            shift_codes=("MORNING", "EVENING", "NIGHT"),
            clinical_mandatory=True,
            allow_forced_clinical_ot=True,
            single_pass=False,
            post_pass_guard=post_pass_guard,
            guard_assignments=result.assignments,
        )
        if batch:
            result.assignments.extend(batch)
            _rebuild_states_from_assignments(states, result.assignments, shift_templates)
            continue
        _close_portage_operational_tally_gaps(
            result.assignments,
            states,
            employees=employees,
            shift_templates=shift_templates,
            rules=rules,
            period_start=period_start,
            period_end=period_end,
            availability_blocked=availability_blocked,
            qual_codes=qual_codes,
            catalog_targets=catalog_targets,
        )
        _rebuild_states_from_assignments(states, result.assignments, shift_templates)
        break


def _clinical_first_finalize(
    result: AutoGenerateResult,
    *,
    states: Dict[str, _EmployeeState],
    employees: Sequence[EmployeeProfile],
    expanded_slots: Sequence[ExpandedScheduleSlot],
    shift_templates: Dict[str, ShiftTemplateInfo],
    shift_required_qualifications: Dict[str, Set[str]],
    rules: JurisdictionRules,
    period_start: date,
    period_end: date,
    weeks_in_period: int,
    availability_blocked: Optional[Mapping[str, Set[date]]],
    qual_codes: Mapping[str, str],
    catalog_targets: Mapping[str, float],
    fulltime_target: float,
    target_hours_map: Mapping[str, float],
    period_target_hours: Mapping[str, float],
    filled_smooth_seats: Optional[Set[Tuple[date, str, Optional[str], int]]] = None,
    scheduling_policy: Optional["PortageSchedulingPolicy"] = None,
) -> None:
    """
    Last pass for clinical_and_hours_first: re-lock E/N after equity, heal nights, refresh gaps.
    """

    from lab_scheduler.scheduling.night_streak_corrector import correct_portage_night_streaks

    post_pass_guard = _post_pass_guard_for_result(result)
    smooth_seats = filled_smooth_seats or set()

    _reassign_parttime_shifts_to_fulltime_contract(
        employees=employees,
        states=states,
        assignments=result.assignments,
        shift_templates=shift_templates,
        shift_required_qualifications=shift_required_qualifications,
        rules=rules,
        period_start=period_start,
        period_end=period_end,
        availability_blocked=availability_blocked,
        qual_codes=qual_codes,
        fulltime_target=fulltime_target,
        payroll_targets=target_hours_map,
        catalog_targets=catalog_targets,
    )
    _rebuild_states_from_assignments(states, result.assignments, shift_templates)

    for _ in range(4):
        _restore_missing_catalog_master_assignments(
            result.assignments,
            states,
            employees=employees,
            shift_templates=shift_templates,
            rules=rules,
            period_start=period_start,
            period_end=period_end,
            availability_blocked=availability_blocked,
            qual_codes=qual_codes,
            weekends_only=True,
            payroll_targets=target_hours_map,
            catalog_targets=catalog_targets,
        )
        _rebuild_states_from_assignments(states, result.assignments, shift_templates)
        fill_counts = _seat_fill_counts(result.assignments, employees, qual_codes)
        _extend_evening_night_clinical_lockdown(
            result,
            employees=employees,
            states=states,
            expanded_slots=expanded_slots,
            shift_templates=shift_templates,
            shift_required_qualifications=shift_required_qualifications,
            rules=rules,
            fill_counts=fill_counts,
            availability_blocked=availability_blocked,
            qual_codes=qual_codes,
            period_start=period_start,
            period_end=period_end,
            log_critical_gaps=False,
            post_pass_guard=post_pass_guard,
            allow_frozen_clinical_supersede=True,
            max_rounds=12,
            weekend_first=True,
                payroll_targets=target_hours_map,
        catalog_targets=catalog_targets,
    )
        fill_counts = _seat_fill_counts(result.assignments, employees, qual_codes)
        if evening_night_clinical_seats_satisfied(
            fill_counts=fill_counts,
            expanded_slots=expanded_slots,
            shift_templates=shift_templates,
            period_start=period_start,
            period_end=period_end,
        ):
            break

    for _cycle in range(5):
        _restore_missing_catalog_master_assignments(
            result.assignments,
            states,
            employees=employees,
            shift_templates=shift_templates,
            rules=rules,
            period_start=period_start,
            period_end=period_end,
            availability_blocked=availability_blocked,
            qual_codes=qual_codes,
            payroll_targets=target_hours_map,
            catalog_targets=catalog_targets,
        )
        _rebuild_states_from_assignments(states, result.assignments, shift_templates)
        fill_counts = _seat_fill_counts(result.assignments, employees, qual_codes)
        _extend_evening_night_clinical_lockdown(
            result,
            employees=employees,
            states=states,
            expanded_slots=expanded_slots,
            shift_templates=shift_templates,
            shift_required_qualifications=shift_required_qualifications,
            rules=rules,
            fill_counts=fill_counts,
            availability_blocked=availability_blocked,
            qual_codes=qual_codes,
            period_start=period_start,
            period_end=period_end,
            log_critical_gaps=False,
            post_pass_guard=post_pass_guard,
            allow_frozen_clinical_supersede=True,
            max_rounds=16,
                payroll_targets=target_hours_map,
        catalog_targets=catalog_targets,
    )
        fill_counts = _seat_fill_counts(result.assignments, employees, qual_codes)
        for shift_codes in (("EVENING", "NIGHT"),):
            locked = _clinical_floor_lock_pass(
                employees=employees,
                states=states,
                expanded_slots=expanded_slots,
                shift_templates=shift_templates,
                shift_required_qualifications=shift_required_qualifications,
                rules=rules,
                period_start=period_start,
                period_end=period_end,
                availability_blocked=availability_blocked,
                qual_codes=qual_codes,
                target_hours_map=target_hours_map,
                period_target_hours=period_target_hours,
                fill_counts=fill_counts,
                filled_smooth_seats=smooth_seats,
                prioritize_coverage=True,
                shift_codes=shift_codes,
                allow_forced_clinical_ot=True,
                clinical_mandatory=True,
                single_pass=True,
                post_pass_guard=post_pass_guard,
                guard_assignments=result.assignments,
            )
            if locked:
                result.assignments.extend(locked)
                fill_counts = _seat_fill_counts(result.assignments, employees, qual_codes)
        if evening_night_clinical_seats_satisfied(
            fill_counts=fill_counts,
            expanded_slots=expanded_slots,
            shift_templates=shift_templates,
            period_start=period_start,
            period_end=period_end,
        ):
            break

    for _ in range(12):
        night_correction = correct_portage_night_streaks(
            result.assignments,
            employees=employees,
            shift_templates=shift_templates,
            shift_required_qualifications=shift_required_qualifications,
            rules=rules,
            period_start=period_start,
            period_end=period_end,
            weeks_in_period=weeks_in_period,
            employee_target_hours=catalog_targets,
            availability_blocked=availability_blocked,
        )
        converted = _break_portage_night_streaks_by_rest_day(
            result.assignments,
            states,
            employees=employees,
            shift_templates=shift_templates,
            rules=rules,
            period_start=period_start,
            period_end=period_end,
            availability_blocked=availability_blocked,
            qual_codes=qual_codes,
        )
        _rebuild_states_from_assignments(states, result.assignments, shift_templates)
        if not night_correction.swaps_applied and converted == 0:
            break

    _close_portage_operational_tally_gaps(
        result.assignments,
        states,
        employees=employees,
        shift_templates=shift_templates,
        rules=rules,
        period_start=period_start,
        period_end=period_end,
        availability_blocked=availability_blocked,
        qual_codes=qual_codes,
        catalog_targets=catalog_targets,
    )
    _rebuild_states_from_assignments(states, result.assignments, shift_templates)
    _enforce_portage_operational_band_caps(
        result.assignments,
        states,
        employees=employees,
        shift_templates=shift_templates,
        qual_codes=qual_codes,
        period_start=period_start,
        period_end=period_end,
        fulltime_target=fulltime_target,
        catalog_targets=catalog_targets,
    )
    _rebuild_states_from_assignments(states, result.assignments, shift_templates)

    fill_counts = _seat_fill_counts(result.assignments, employees, qual_codes)
    _fill_weekend_morning_clinical_gaps(
        result.assignments,
        states,
        employees=employees,
        expanded_slots=expanded_slots,
        shift_templates=shift_templates,
        shift_required_qualifications=shift_required_qualifications,
        rules=rules,
        period_start=period_start,
        period_end=period_end,
        availability_blocked=availability_blocked,
        qual_codes=qual_codes,
        fill_counts=fill_counts,
        filled_smooth_seats=smooth_seats,
    )
    _rebuild_states_from_assignments(states, result.assignments, shift_templates)

    _trim_weekend_morning_overfill(
        result.assignments,
        states,
        employees=employees,
        shift_templates=shift_templates,
        qual_codes=qual_codes,
        period_start=period_start,
        period_end=period_end,
    )
    _rebuild_states_from_assignments(states, result.assignments, shift_templates)

    for _ in range(4):
        _restore_missing_catalog_master_assignments(
            result.assignments,
            states,
            employees=employees,
            shift_templates=shift_templates,
            rules=rules,
            period_start=period_start,
            period_end=period_end,
            availability_blocked=availability_blocked,
            qual_codes=qual_codes,
            weekends_only=True,
            payroll_targets=catalog_targets,
            catalog_targets=catalog_targets,
        )
        _rebuild_states_from_assignments(states, result.assignments, shift_templates)
        fill_counts = _seat_fill_counts(result.assignments, employees, qual_codes)
        _extend_evening_night_clinical_lockdown(
            result,
            employees=employees,
            states=states,
            expanded_slots=expanded_slots,
            shift_templates=shift_templates,
            shift_required_qualifications=shift_required_qualifications,
            rules=rules,
            fill_counts=fill_counts,
            availability_blocked=availability_blocked,
            qual_codes=qual_codes,
            period_start=period_start,
            period_end=period_end,
            log_critical_gaps=False,
            post_pass_guard=post_pass_guard,
            allow_frozen_clinical_supersede=True,
            max_rounds=16,
            weekend_first=True,
                payroll_targets=catalog_targets,
        catalog_targets=catalog_targets,
    )
        _close_portage_operational_tally_gaps(
            result.assignments,
            states,
            employees=employees,
            shift_templates=shift_templates,
            rules=rules,
            period_start=period_start,
            period_end=period_end,
            availability_blocked=availability_blocked,
            qual_codes=qual_codes,
            catalog_targets=catalog_targets,
        )
        _rebuild_states_from_assignments(states, result.assignments, shift_templates)
        fill_counts = _seat_fill_counts(result.assignments, employees, qual_codes)
        if evening_night_clinical_seats_satisfied(
            fill_counts=fill_counts,
            expanded_slots=expanded_slots,
            shift_templates=shift_templates,
            period_start=period_start,
            period_end=period_end,
        ):
            break

    for _ in range(12):
        night_correction = correct_portage_night_streaks(
            result.assignments,
            employees=employees,
            shift_templates=shift_templates,
            shift_required_qualifications=shift_required_qualifications,
            rules=rules,
            period_start=period_start,
            period_end=period_end,
            weeks_in_period=weeks_in_period,
            employee_target_hours=catalog_targets,
            availability_blocked=availability_blocked,
        )
        converted = _break_portage_night_streaks_by_rest_day(
            result.assignments,
            states,
            employees=employees,
            shift_templates=shift_templates,
            rules=rules,
            period_start=period_start,
            period_end=period_end,
            availability_blocked=availability_blocked,
            qual_codes=qual_codes,
        )
        _rebuild_states_from_assignments(states, result.assignments, shift_templates)
        if not night_correction.swaps_applied and converted == 0:
            break

    for _ in range(6):
        _close_portage_operational_tally_gaps(
            result.assignments,
            states,
            employees=employees,
            shift_templates=shift_templates,
            rules=rules,
            period_start=period_start,
            period_end=period_end,
            availability_blocked=availability_blocked,
            qual_codes=qual_codes,
            catalog_targets=catalog_targets,
        )
        _rebuild_states_from_assignments(states, result.assignments, shift_templates)
        _enforce_portage_operational_band_caps(
            result.assignments,
            states,
            employees=employees,
            shift_templates=shift_templates,
            qual_codes=qual_codes,
            period_start=period_start,
            period_end=period_end,
            fulltime_target=fulltime_target,
            catalog_targets=catalog_targets,
        )
        _rebuild_states_from_assignments(states, result.assignments, shift_templates)
        fill_counts = _seat_fill_counts(result.assignments, employees, qual_codes)
        if evening_night_clinical_seats_satisfied(
            fill_counts=fill_counts,
            expanded_slots=expanded_slots,
            shift_templates=shift_templates,
            period_start=period_start,
            period_end=period_end,
        ):
            break
        _extend_evening_night_clinical_lockdown(
            result,
            employees=employees,
            states=states,
            expanded_slots=expanded_slots,
            shift_templates=shift_templates,
            shift_required_qualifications=shift_required_qualifications,
            rules=rules,
            fill_counts=fill_counts,
            availability_blocked=availability_blocked,
            qual_codes=qual_codes,
            period_start=period_start,
            period_end=period_end,
            log_critical_gaps=False,
            post_pass_guard=post_pass_guard,
            allow_frozen_clinical_supersede=True,
            max_rounds=20,
            weekend_first=True,
                payroll_targets=catalog_targets,
        catalog_targets=catalog_targets,
    )
        _rebuild_states_from_assignments(states, result.assignments, shift_templates)

    fill_counts = _seat_fill_counts(result.assignments, employees, qual_codes)
    _fill_weekend_morning_clinical_gaps(
        result.assignments,
        states,
        employees=employees,
        expanded_slots=expanded_slots,
        shift_templates=shift_templates,
        shift_required_qualifications=shift_required_qualifications,
        rules=rules,
        period_start=period_start,
        period_end=period_end,
        availability_blocked=availability_blocked,
        qual_codes=qual_codes,
        fill_counts=fill_counts,
        filled_smooth_seats=smooth_seats,
    )
    _rebuild_states_from_assignments(states, result.assignments, shift_templates)
    _trim_weekend_morning_overfill(
        result.assignments,
        states,
        employees=employees,
        shift_templates=shift_templates,
        qual_codes=qual_codes,
        period_start=period_start,
        period_end=period_end,
    )
    _rebuild_states_from_assignments(states, result.assignments, shift_templates)

    for _ in range(12):
        night_correction = correct_portage_night_streaks(
            result.assignments,
            employees=employees,
            shift_templates=shift_templates,
            shift_required_qualifications=shift_required_qualifications,
            rules=rules,
            period_start=period_start,
            period_end=period_end,
            weeks_in_period=weeks_in_period,
            employee_target_hours=catalog_targets,
            availability_blocked=availability_blocked,
        )
        converted = _break_portage_night_streaks_by_rest_day(
            result.assignments,
            states,
            employees=employees,
            shift_templates=shift_templates,
            rules=rules,
            period_start=period_start,
            period_end=period_end,
            availability_blocked=availability_blocked,
            qual_codes=qual_codes,
        )
        _rebuild_states_from_assignments(states, result.assignments, shift_templates)
        if not night_correction.swaps_applied and converted == 0:
            break

    _close_portage_operational_tally_gaps(
        result.assignments,
        states,
        employees=employees,
        shift_templates=shift_templates,
        rules=rules,
        period_start=period_start,
        period_end=period_end,
        availability_blocked=availability_blocked,
        qual_codes=qual_codes,
        catalog_targets=catalog_targets,
    )
    _rebuild_states_from_assignments(states, result.assignments, shift_templates)
    _enforce_portage_operational_band_caps(
        result.assignments,
        states,
        employees=employees,
        shift_templates=shift_templates,
        qual_codes=qual_codes,
        period_start=period_start,
        period_end=period_end,
        fulltime_target=fulltime_target,
        catalog_targets=catalog_targets,
    )
    _rebuild_states_from_assignments(states, result.assignments, shift_templates)

    _restore_missing_catalog_master_assignments(
        result.assignments,
        states,
        employees=employees,
        shift_templates=shift_templates,
        rules=rules,
        period_start=period_start,
        period_end=period_end,
        availability_blocked=availability_blocked,
        qual_codes=qual_codes,
        payroll_targets=target_hours_map,
        catalog_targets=catalog_targets,
    )
    _rebuild_states_from_assignments(states, result.assignments, shift_templates)
    _trim_catalog_contract_surplus(
        result.assignments,
        states,
        employees=employees,
        shift_templates=shift_templates,
        catalog_targets=catalog_targets,
        period_start=period_start,
        period_end=period_end,
        allow_trim_frozen=True,
        payroll_targets=target_hours_map,
    )
    _rebuild_states_from_assignments(states, result.assignments, shift_templates)

    fill_counts = _seat_fill_counts(result.assignments, employees, qual_codes)
    if not evening_night_clinical_seats_satisfied(
        fill_counts=fill_counts,
        expanded_slots=expanded_slots,
        shift_templates=shift_templates,
        period_start=period_start,
        period_end=period_end,
    ):
        for shift_codes in (("EVENING", "NIGHT"),):
            locked = _clinical_floor_lock_pass(
                employees=employees,
                states=states,
                expanded_slots=expanded_slots,
                shift_templates=shift_templates,
                shift_required_qualifications=shift_required_qualifications,
                rules=rules,
                period_start=period_start,
                period_end=period_end,
                availability_blocked=availability_blocked,
                qual_codes=qual_codes,
                target_hours_map=target_hours_map,
                period_target_hours=period_target_hours,
                fill_counts=fill_counts,
                filled_smooth_seats=smooth_seats,
                prioritize_coverage=True,
                shift_codes=shift_codes,
                allow_forced_clinical_ot=True,
                clinical_mandatory=True,
                single_pass=False,
                post_pass_guard=post_pass_guard,
                guard_assignments=result.assignments,
            )
            if locked:
                result.assignments.extend(locked)
                fill_counts = _seat_fill_counts(result.assignments, employees, qual_codes)
        _extend_evening_night_clinical_lockdown(
            result,
            employees=employees,
            states=states,
            expanded_slots=expanded_slots,
            shift_templates=shift_templates,
            shift_required_qualifications=shift_required_qualifications,
            rules=rules,
            fill_counts=fill_counts,
            availability_blocked=availability_blocked,
            qual_codes=qual_codes,
            period_start=period_start,
            period_end=period_end,
            log_critical_gaps=False,
            post_pass_guard=post_pass_guard,
            allow_frozen_clinical_supersede=True,
            max_rounds=24,
            weekend_first=True,
                payroll_targets=target_hours_map,
        catalog_targets=catalog_targets,
    )
        _rebuild_states_from_assignments(states, result.assignments, shift_templates)
        fill_counts = _seat_fill_counts(result.assignments, employees, qual_codes)

    _fill_remaining_clinical_seats_by_conversion(
        result.assignments,
        states,
        employees=employees,
        expanded_slots=expanded_slots,
        shift_templates=shift_templates,
        shift_required_qualifications=shift_required_qualifications,
        rules=rules,
        period_start=period_start,
        period_end=period_end,
        availability_blocked=availability_blocked,
        qual_codes=qual_codes,
        payroll_targets=target_hours_map,
        catalog_targets=catalog_targets,
    )
    _rebuild_states_from_assignments(states, result.assignments, shift_templates)

    _rebuild_states_from_assignments(states, result.assignments, shift_templates)

    for _ in range(8):
        _enforce_portage_operational_band_caps(
            result.assignments,
            states,
            employees=employees,
            shift_templates=shift_templates,
            qual_codes=qual_codes,
            period_start=period_start,
            period_end=period_end,
            fulltime_target=fulltime_target,
            catalog_targets=catalog_targets,
        )
        _rebuild_states_from_assignments(states, result.assignments, shift_templates)
        _trim_catalog_contract_surplus(
            result.assignments,
            states,
            employees=employees,
            shift_templates=shift_templates,
            catalog_targets=catalog_targets,
            period_start=period_start,
            period_end=period_end,
            allow_trim_frozen=True,
        )
        _rebuild_states_from_assignments(states, result.assignments, shift_templates)
        _close_portage_operational_tally_gaps(
            result.assignments,
            states,
            employees=employees,
            shift_templates=shift_templates,
            rules=rules,
            period_start=period_start,
            period_end=period_end,
            availability_blocked=availability_blocked,
            qual_codes=qual_codes,
            catalog_targets=catalog_targets,
        )
        _rebuild_states_from_assignments(states, result.assignments, shift_templates)

    fill_counts = _seat_fill_counts(result.assignments, employees, qual_codes)
    if not evening_night_clinical_seats_satisfied(
        fill_counts=fill_counts,
        expanded_slots=expanded_slots,
        shift_templates=shift_templates,
        period_start=period_start,
        period_end=period_end,
    ):
        _fill_remaining_clinical_seats_by_conversion(
            result.assignments,
            states,
            employees=employees,
            expanded_slots=expanded_slots,
            shift_templates=shift_templates,
            shift_required_qualifications=shift_required_qualifications,
            rules=rules,
            period_start=period_start,
            period_end=period_end,
            availability_blocked=availability_blocked,
            qual_codes=qual_codes,
            payroll_targets=target_hours_map,
            catalog_targets=catalog_targets,
        )
        _rebuild_states_from_assignments(states, result.assignments, shift_templates)

    _trim_portage_day_shift_overfill(
        result.assignments,
        states,
        employees=employees,
        shift_templates=shift_templates,
        qual_codes=qual_codes,
        rules=rules,
        period_start=period_start,
        period_end=period_end,
        availability_blocked=availability_blocked,
        catalog_targets=catalog_targets,
    )
    _rebuild_states_from_assignments(states, result.assignments, shift_templates)
    _trim_catalog_contract_surplus(
        result.assignments,
        states,
        employees=employees,
        shift_templates=shift_templates,
        catalog_targets=catalog_targets,
        period_start=period_start,
        period_end=period_end,
        allow_trim_frozen=True,
        payroll_targets=target_hours_map,
    )
    _rebuild_states_from_assignments(states, result.assignments, shift_templates)
    _restore_missing_catalog_master_assignments(
        result.assignments,
        states,
        employees=employees,
        shift_templates=shift_templates,
        rules=rules,
        period_start=period_start,
        period_end=period_end,
        availability_blocked=availability_blocked,
        qual_codes=qual_codes,
        payroll_targets=target_hours_map,
        catalog_targets=catalog_targets,
    )
    _rebuild_states_from_assignments(states, result.assignments, shift_templates)
    _close_portage_operational_tally_gaps(
        result.assignments,
        states,
        employees=employees,
        shift_templates=shift_templates,
        rules=rules,
        period_start=period_start,
        period_end=period_end,
        availability_blocked=availability_blocked,
        qual_codes=qual_codes,
        catalog_targets=catalog_targets,
    )
    _rebuild_states_from_assignments(states, result.assignments, shift_templates)

    fill_counts = _seat_fill_counts(result.assignments, employees, qual_codes)
    if not evening_night_clinical_seats_satisfied(
        fill_counts=fill_counts,
        expanded_slots=expanded_slots,
        shift_templates=shift_templates,
        period_start=period_start,
        period_end=period_end,
    ):
        _extend_evening_night_clinical_lockdown(
            result,
            employees=employees,
            states=states,
            expanded_slots=expanded_slots,
            shift_templates=shift_templates,
            shift_required_qualifications=shift_required_qualifications,
            rules=rules,
            fill_counts=fill_counts,
            availability_blocked=availability_blocked,
            qual_codes=qual_codes,
            period_start=period_start,
            period_end=period_end,
            log_critical_gaps=False,
            post_pass_guard=post_pass_guard,
            allow_frozen_clinical_supersede=True,
            max_rounds=8,
            weekend_first=True,
                payroll_targets=target_hours_map,
        catalog_targets=catalog_targets,
    )
        _rebuild_states_from_assignments(states, result.assignments, shift_templates)
        fill_counts = _seat_fill_counts(result.assignments, employees, qual_codes)
        if not evening_night_clinical_seats_satisfied(
            fill_counts=fill_counts,
            expanded_slots=expanded_slots,
            shift_templates=shift_templates,
            period_start=period_start,
            period_end=period_end,
        ):
            _restore_missing_catalog_master_assignments(
                result.assignments,
                states,
                employees=employees,
                shift_templates=shift_templates,
                rules=rules,
                period_start=period_start,
                period_end=period_end,
                availability_blocked=availability_blocked,
                qual_codes=qual_codes,
                weekends_only=True,
                payroll_targets=target_hours_map,
                catalog_targets=catalog_targets,
            )
            _rebuild_states_from_assignments(states, result.assignments, shift_templates)
            fill_counts = _seat_fill_counts(result.assignments, employees, qual_codes)
            _extend_evening_night_clinical_lockdown(
                result,
                employees=employees,
                states=states,
                expanded_slots=expanded_slots,
                shift_templates=shift_templates,
                shift_required_qualifications=shift_required_qualifications,
                rules=rules,
                fill_counts=fill_counts,
                availability_blocked=availability_blocked,
                qual_codes=qual_codes,
                period_start=period_start,
                period_end=period_end,
                log_critical_gaps=False,
                post_pass_guard=post_pass_guard,
                allow_frozen_clinical_supersede=True,
                max_rounds=8,
                weekend_first=True,
                    payroll_targets=catalog_targets,
        catalog_targets=catalog_targets,
    )
            _rebuild_states_from_assignments(states, result.assignments, shift_templates)

    for _ in range(8):
        _rebuild_states_from_assignments(states, result.assignments, shift_templates)
        trimmed = _trim_catalog_contract_surplus(
            result.assignments,
            states,
            employees=employees,
            shift_templates=shift_templates,
            catalog_targets=catalog_targets,
            period_start=period_start,
            period_end=period_end,
            allow_trim_frozen=True,
            payroll_targets=target_hours_map,
            expanded_slots=expanded_slots,
            qual_codes=qual_codes,
        )
        trimmed += _trim_parttime_vacant_day_surplus(
            result.assignments,
            states,
            employees=employees,
            shift_templates=shift_templates,
            catalog_targets=catalog_targets,
            period_start=period_start,
            period_end=period_end,
            allow_trim_frozen=True,
            expanded_slots=expanded_slots,
            qual_codes=qual_codes,
        )
        trimmed += _trim_portage_day_shift_overfill(
            result.assignments,
            states,
            employees=employees,
            shift_templates=shift_templates,
            qual_codes=qual_codes,
            rules=rules,
            period_start=period_start,
            period_end=period_end,
            availability_blocked=availability_blocked,
            catalog_targets=catalog_targets,
        )
        _rebuild_states_from_assignments(states, result.assignments, shift_templates)
        if trimmed == 0:
            break
        _heal_required_coverage_after_catalog_trim(
            result,
            states=states,
            employees=employees,
            expanded_slots=expanded_slots,
            shift_templates=shift_templates,
            shift_required_qualifications=shift_required_qualifications,
            rules=rules,
            period_start=period_start,
            period_end=period_end,
            availability_blocked=availability_blocked,
            qual_codes=qual_codes,
            catalog_targets=catalog_targets,
            target_hours_map=target_hours_map,
            period_target_hours=period_target_hours,
            filled_smooth_seats=smooth_seats,
            post_pass_guard=post_pass_guard,
        )

    _heal_required_coverage_after_catalog_trim(
        result,
        states=states,
        employees=employees,
        expanded_slots=expanded_slots,
        shift_templates=shift_templates,
        shift_required_qualifications=shift_required_qualifications,
        rules=rules,
        period_start=period_start,
        period_end=period_end,
        availability_blocked=availability_blocked,
        qual_codes=qual_codes,
        catalog_targets=catalog_targets,
        target_hours_map=target_hours_map,
        period_target_hours=period_target_hours,
        filled_smooth_seats=smooth_seats,
        post_pass_guard=post_pass_guard,
    )

    filled, total = _compute_required_slot_fill_from_assignments(
        result.assignments,
        employees,
        expanded_slots,
        shift_templates,
        qual_codes,
    )
    if total > 0 and filled < total:
        fill_counts = _seat_fill_counts(result.assignments, employees, qual_codes)
        for shift_codes in (("EVENING", "NIGHT"), ("MORNING",)):
            for _ in range(8):
                if _required_coverage_slots_satisfied(
                    expanded_slots=expanded_slots,
                    fill_counts=fill_counts,
                    shift_templates=shift_templates,
                ):
                    break
                batch = _clinical_floor_lock_pass(
                    employees=employees,
                    states=states,
                    expanded_slots=expanded_slots,
                    shift_templates=shift_templates,
                    shift_required_qualifications=shift_required_qualifications,
                    rules=rules,
                    period_start=period_start,
                    period_end=period_end,
                    availability_blocked=availability_blocked,
                    qual_codes=qual_codes,
                    target_hours_map=target_hours_map,
                    period_target_hours=period_target_hours,
                    fill_counts=fill_counts,
                    filled_smooth_seats=smooth_seats,
                    prioritize_coverage=True,
                    shift_codes=shift_codes,
                    allow_forced_clinical_ot=True,
                    clinical_mandatory=True,
                    single_pass=False,
                    post_pass_guard=post_pass_guard,
                    guard_assignments=result.assignments,
                )
                if not batch:
                    break
                result.assignments.extend(batch)
                _rebuild_states_from_assignments(states, result.assignments, shift_templates)
                fill_counts = _seat_fill_counts(result.assignments, employees, qual_codes)

    _post_clinical_alt_equity_pass(
        result,
        states=states,
        employees=employees,
        expanded_slots=expanded_slots,
        shift_templates=shift_templates,
        shift_required_qualifications=shift_required_qualifications,
        rules=rules,
        period_start=period_start,
        period_end=period_end,
        availability_blocked=availability_blocked,
        qual_codes=qual_codes,
        catalog_targets=catalog_targets,
        scheduling_policy=scheduling_policy,
        post_pass_guard=post_pass_guard,
        payroll_targets=target_hours_map,
    )
    _rebuild_states_from_assignments(states, result.assignments, shift_templates)

    fill_counts = _seat_fill_counts(result.assignments, employees, qual_codes)
    result.clinical_gap_reports = _collect_clinical_gap_reports(
        expanded_slots=expanded_slots,
        fill_counts=fill_counts,
        shift_templates=shift_templates,
        period_start=period_start,
        period_end=period_end,
    )
    _update_slot_fill_metrics(
        result,
        expanded_slots=expanded_slots,
        fill_counts=fill_counts,
        shift_templates=shift_templates,
    )


# Must match persist_validation.CATALOG_PERSIST_HOUR_TOLERANCE for seal trim passes.
CATALOG_PERSIST_TRIM_TOLERANCE = 0.25


def _any_contract_finalize_surplus(
    employees: Sequence[EmployeeProfile],
    states: Mapping[str, _EmployeeState],
    *,
    payroll_targets: Mapping[str, float],
    catalog_targets: Mapping[str, float],
) -> bool:
    return any(
        _has_contract_finalize_surplus(
            employee,
            states[employee.id].total_hours,
            payroll_targets=payroll_targets,
            catalog_targets=catalog_targets,
        )
        for employee in employees
    )


def _persist_preflight_violations(
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
    catalog_targets: Mapping[str, float],
    coverage_complete: bool,
    coverage_gap_count: int,
) -> list:
    from lab_scheduler.scheduling.persist_validation import find_core_persist_violations

    return find_core_persist_violations(
        assignments=assignments,
        employees=employees,
        shift_templates=shift_templates,
        period_start=period_start,
        period_end=period_end,
        weeks_in_period=weeks_in_period,
        rules=rules,
        qual_codes=qual_codes,
        template_id_to_band=template_id_to_band,
        coverage_complete=coverage_complete,
        coverage_gap_count=coverage_gap_count,
        compliance_first=False,
    )


def _run_persist_clinical_coverage_heal(
    result: AutoGenerateResult,
    *,
    states: Dict[str, _EmployeeState],
    employees: Sequence[EmployeeProfile],
    expanded_slots: Sequence[ExpandedScheduleSlot],
    shift_templates: Dict[str, ShiftTemplateInfo],
    shift_required_qualifications: Dict[str, Set[str]],
    rules: JurisdictionRules,
    period_start: date,
    period_end: date,
    availability_blocked: Optional[Mapping[str, Set[date]]],
    qual_codes: Mapping[str, str],
    payroll_targets: Mapping[str, float],
    catalog_targets: Mapping[str, float],
    filled_smooth_seats: Set[Tuple[date, str, Optional[str], int]],
    post_pass_guard: PostPassGuard,
    persist_gate: bool = True,
) -> bool:
    """Re-close E/N tallies, demand seats, and clinical floors after contract trim."""

    progress = False
    fill_counts = _seat_fill_counts(result.assignments, employees, qual_codes)
    weeks_in_period = max(1, round(((period_end - period_start).days + 1) / 7))
    fulltime_target = fulltime_period_contract_hours(
        rules=rules,
        weeks_in_period=weeks_in_period,
    )

    _enforce_portage_operational_band_caps(
        result.assignments,
        states,
        employees=employees,
        shift_templates=shift_templates,
        qual_codes=qual_codes,
        period_start=period_start,
        period_end=period_end,
        fulltime_target=fulltime_target,
        catalog_targets=catalog_targets,
    )
    _rebuild_states_from_assignments(states, result.assignments, shift_templates)

    tally_changed = _close_portage_operational_tally_gaps(
        result.assignments,
        states,
        employees=employees,
        shift_templates=shift_templates,
        rules=rules,
        period_start=period_start,
        period_end=period_end,
        availability_blocked=availability_blocked,
        qual_codes=qual_codes,
        catalog_targets=catalog_targets,
        payroll_targets=payroll_targets,
        persist_gate=persist_gate,
    )
    if tally_changed:
        progress = True
    _rebuild_states_from_assignments(states, result.assignments, shift_templates)
    fill_counts = _seat_fill_counts(result.assignments, employees, qual_codes)

    if not _required_coverage_slots_satisfied(
        expanded_slots=expanded_slots,
        fill_counts=fill_counts,
        shift_templates=shift_templates,
    ):
        added = _force_fill_all_remaining_slots(
            result,
            employees=employees,
            states=states,
            expanded_slots=expanded_slots,
            shift_templates=shift_templates,
            shift_required_qualifications=shift_required_qualifications,
            rules=rules,
            period_start=period_start,
            period_end=period_end,
            availability_blocked=availability_blocked,
            qual_codes=qual_codes,
            fill_counts=fill_counts,
            filled_smooth_seats=filled_smooth_seats,
            clinical_mandatory=True,
            payroll_targets=payroll_targets,
            catalog_targets=catalog_targets,
        )
        if added:
            progress = True
        _rebuild_states_from_assignments(states, result.assignments, shift_templates)
        fill_counts = _seat_fill_counts(result.assignments, employees, qual_codes)

    if not evening_night_clinical_seats_satisfied(
        fill_counts=fill_counts,
        expanded_slots=expanded_slots,
        shift_templates=shift_templates,
        period_start=period_start,
        period_end=period_end,
    ):
        before = len(result.assignments)
        _extend_evening_night_clinical_lockdown(
            result,
            employees=employees,
            states=states,
            expanded_slots=expanded_slots,
            shift_templates=shift_templates,
            shift_required_qualifications=shift_required_qualifications,
            rules=rules,
            fill_counts=fill_counts,
            availability_blocked=availability_blocked,
            qual_codes=qual_codes,
            period_start=period_start,
            period_end=period_end,
            log_critical_gaps=False,
            post_pass_guard=post_pass_guard,
            allow_frozen_clinical_supersede=True,
            max_rounds=12,
            weekend_first=True,
            payroll_targets=payroll_targets,
            catalog_targets=catalog_targets,
        )
        if len(result.assignments) != before:
            progress = True
        _rebuild_states_from_assignments(states, result.assignments, shift_templates)

    return progress


def _run_persist_equity_pass(
    result: AutoGenerateResult,
    *,
    states: Dict[str, _EmployeeState],
    employees: Sequence[EmployeeProfile],
    expanded_slots: Sequence[ExpandedScheduleSlot],
    shift_templates: Dict[str, ShiftTemplateInfo],
    shift_required_qualifications: Dict[str, Set[str]],
    rules: JurisdictionRules,
    period_start: date,
    period_end: date,
    availability_blocked: Optional[Mapping[str, Set[date]]],
    qual_codes: Mapping[str, str],
    catalog_targets: Mapping[str, float],
    payroll_targets: Mapping[str, float],
    post_pass_guard: PostPassGuard,
) -> bool:
    """Weekend + alternate-band peer equity after contract/clinical passes."""

    fill_counts = _seat_fill_counts(result.assignments, employees, qual_codes)
    clinical_ok = evening_night_clinical_seats_satisfied(
        fill_counts=fill_counts,
        expanded_slots=expanded_slots,
        shift_templates=shift_templates,
        period_start=period_start,
        period_end=period_end,
    )

    edits = _post_clinical_alt_equity_pass(
        result,
        states=states,
        employees=employees,
        expanded_slots=expanded_slots,
        shift_templates=shift_templates,
        shift_required_qualifications=shift_required_qualifications,
        rules=rules,
        period_start=period_start,
        period_end=period_end,
        availability_blocked=availability_blocked,
        qual_codes=qual_codes,
        catalog_targets=catalog_targets,
        post_pass_guard=post_pass_guard,
        payroll_targets=payroll_targets,
        ft_alt_equity=clinical_ok,
    )
    _rebuild_states_from_assignments(states, result.assignments, shift_templates)
    return edits > 0


def _run_persist_preflight_pass(
    result: AutoGenerateResult,
    *,
    states: Dict[str, _EmployeeState],
    employees: Sequence[EmployeeProfile],
    expanded_slots: Sequence[ExpandedScheduleSlot],
    shift_templates: Dict[str, ShiftTemplateInfo],
    shift_required_qualifications: Dict[str, Set[str]],
    rules: JurisdictionRules,
    period_start: date,
    period_end: date,
    availability_blocked: Optional[Mapping[str, Set[date]]],
    qual_codes: Mapping[str, str],
    catalog_targets: Mapping[str, float],
    payroll_targets: Mapping[str, float],
    filled_smooth_seats: Set[Tuple[date, str, Optional[str], int]],
    post_pass_guard: PostPassGuard,
    template_bands: Mapping[str, str],
) -> bool:
    """One trim → top-up → coverage → weekend pass. Returns True if progress was made."""

    persist_tol = CATALOG_PERSIST_TRIM_TOLERANCE
    progress = False
    total_trimmed = 0

    total_trimmed += _trim_catalog_contract_surplus(
        result.assignments,
        states,
        employees=employees,
        shift_templates=shift_templates,
        catalog_targets=catalog_targets,
        period_start=period_start,
        period_end=period_end,
        allow_trim_frozen=True,
        tolerance=persist_tol,
        payroll_targets=payroll_targets,
        expanded_slots=expanded_slots,
        qual_codes=qual_codes,
    )
    _rebuild_states_from_assignments(states, result.assignments, shift_templates)

    total_trimmed += _trim_parttime_contract_overrun(
        result.assignments,
        states,
        employees=employees,
        shift_templates=shift_templates,
        catalog_targets=catalog_targets,
        period_start=period_start,
        period_end=period_end,
        tolerance=persist_tol,
        expanded_slots=expanded_slots,
        qual_codes=qual_codes,
    )
    total_trimmed += _trim_parttime_vacant_day_surplus(
        result.assignments,
        states,
        employees=employees,
        shift_templates=shift_templates,
        catalog_targets=catalog_targets,
        period_start=period_start,
        period_end=period_end,
        allow_trim_frozen=True,
        tolerance=persist_tol,
        expanded_slots=expanded_slots,
        qual_codes=qual_codes,
    )
    if total_trimmed:
        progress = True
    _rebuild_states_from_assignments(states, result.assignments, shift_templates)

    topped_up = _catalog_contract_top_up_pass(
        employees=employees,
        states=states,
        assignments=result.assignments,
        shift_templates=shift_templates,
        rules=rules,
        period_start=period_start,
        period_end=period_end,
        availability_blocked=availability_blocked,
        qual_codes=qual_codes,
        catalog_targets=catalog_targets,
        payroll_targets=payroll_targets,
        persist_gate=True,
    )
    if topped_up:
        progress = True
    _rebuild_states_from_assignments(states, result.assignments, shift_templates)

    restored = _restore_missing_catalog_master_assignments(
        result.assignments,
        states,
        employees=employees,
        shift_templates=shift_templates,
        rules=rules,
        period_start=period_start,
        period_end=period_end,
        availability_blocked=availability_blocked,
        qual_codes=qual_codes,
        fulltime_only=False,
        payroll_targets=payroll_targets,
        catalog_targets=catalog_targets,
    )
    if restored:
        progress = True
    _rebuild_states_from_assignments(states, result.assignments, shift_templates)

    fill_counts = _seat_fill_counts(result.assignments, employees, qual_codes)
    if not _required_coverage_slots_satisfied(
        expanded_slots=expanded_slots,
        fill_counts=fill_counts,
        shift_templates=shift_templates,
    ):
        added = _force_fill_all_remaining_slots(
            result,
            employees=employees,
            states=states,
            expanded_slots=expanded_slots,
            shift_templates=shift_templates,
            shift_required_qualifications=shift_required_qualifications,
            rules=rules,
            period_start=period_start,
            period_end=period_end,
            availability_blocked=availability_blocked,
            qual_codes=qual_codes,
            fill_counts=fill_counts,
            filled_smooth_seats=filled_smooth_seats,
            clinical_mandatory=True,
            payroll_targets=payroll_targets,
            catalog_targets=catalog_targets,
        )
        if added:
            progress = True
        _rebuild_states_from_assignments(states, result.assignments, shift_templates)
        fill_counts = _seat_fill_counts(result.assignments, employees, qual_codes)

    if not evening_night_clinical_seats_satisfied(
        fill_counts=fill_counts,
        expanded_slots=expanded_slots,
        shift_templates=shift_templates,
        period_start=period_start,
        period_end=period_end,
    ):
        before = len(result.assignments)
        _extend_evening_night_clinical_lockdown(
            result,
            employees=employees,
            states=states,
            expanded_slots=expanded_slots,
            shift_templates=shift_templates,
            shift_required_qualifications=shift_required_qualifications,
            rules=rules,
            fill_counts=fill_counts,
            availability_blocked=availability_blocked,
            qual_codes=qual_codes,
            period_start=period_start,
            period_end=period_end,
            log_critical_gaps=False,
            post_pass_guard=post_pass_guard,
            allow_frozen_clinical_supersede=True,
            max_rounds=8,
            weekend_first=True,
            payroll_targets=payroll_targets,
            catalog_targets=catalog_targets,
        )
        if len(result.assignments) != before:
            progress = True
        _rebuild_states_from_assignments(states, result.assignments, shift_templates)

    final_trim = _trim_catalog_contract_surplus(
        result.assignments,
        states,
        employees=employees,
        shift_templates=shift_templates,
        catalog_targets=catalog_targets,
        period_start=period_start,
        period_end=period_end,
        allow_trim_frozen=True,
        tolerance=persist_tol,
        payroll_targets=payroll_targets,
        expanded_slots=expanded_slots,
        qual_codes=qual_codes,
    )
    if final_trim:
        progress = True
    _rebuild_states_from_assignments(states, result.assignments, shift_templates)

    fill_counts = _seat_fill_counts(result.assignments, employees, qual_codes)
    _deterministic_resolve_day_night_transitions(
        result.assignments,
        states,
        employees=employees,
        shift_templates=shift_templates,
        shift_required_qualifications=shift_required_qualifications,
        expanded_slots=expanded_slots,
        rules=rules,
        period_start=period_start,
        period_end=period_end,
        availability_blocked=availability_blocked,
        qual_codes=qual_codes,
        target_hours_map=payroll_targets,
        period_target_hours=catalog_targets,
        fill_counts=fill_counts,
    )
    _rebuild_states_from_assignments(states, result.assignments, shift_templates)

    if _run_persist_clinical_coverage_heal(
        result,
        states=states,
        employees=employees,
        expanded_slots=expanded_slots,
        shift_templates=shift_templates,
        shift_required_qualifications=shift_required_qualifications,
        rules=rules,
        period_start=period_start,
        period_end=period_end,
        availability_blocked=availability_blocked,
        qual_codes=qual_codes,
        payroll_targets=payroll_targets,
        catalog_targets=catalog_targets,
        filled_smooth_seats=filled_smooth_seats,
        post_pass_guard=post_pass_guard,
        persist_gate=True,
    ):
        progress = True

    if _run_persist_equity_pass(
        result,
        states=states,
        employees=employees,
        expanded_slots=expanded_slots,
        shift_templates=shift_templates,
        shift_required_qualifications=shift_required_qualifications,
        rules=rules,
        period_start=period_start,
        period_end=period_end,
        availability_blocked=availability_blocked,
        qual_codes=qual_codes,
        catalog_targets=catalog_targets,
        payroll_targets=payroll_targets,
        post_pass_guard=post_pass_guard,
    ):
        progress = True

    if _run_persist_clinical_coverage_heal(
        result,
        states=states,
        employees=employees,
        expanded_slots=expanded_slots,
        shift_templates=shift_templates,
        shift_required_qualifications=shift_required_qualifications,
        rules=rules,
        period_start=period_start,
        period_end=period_end,
        availability_blocked=availability_blocked,
        qual_codes=qual_codes,
        payroll_targets=payroll_targets,
        catalog_targets=catalog_targets,
        filled_smooth_seats=filled_smooth_seats,
        post_pass_guard=post_pass_guard,
        persist_gate=True,
    ):
        progress = True

    if _apply_portage_weekend_pairing_policy(
        result.assignments,
        states,
        employees=employees,
        shift_templates=shift_templates,
        rules=rules,
        period_start=period_start,
        period_end=period_end,
        availability_blocked=availability_blocked,
        qual_codes=qual_codes,
        catalog_targets=catalog_targets,
    ):
        progress = True
    _rebuild_states_from_assignments(states, result.assignments, shift_templates)

    return progress


def _run_persist_final_scrub(
    result: AutoGenerateResult,
    *,
    states: Dict[str, _EmployeeState],
    employees: Sequence[EmployeeProfile],
    expanded_slots: Sequence[ExpandedScheduleSlot],
    shift_templates: Dict[str, ShiftTemplateInfo],
    shift_required_qualifications: Dict[str, Set[str]],
    rules: JurisdictionRules,
    period_start: date,
    period_end: date,
    availability_blocked: Optional[Mapping[str, Set[date]]],
    qual_codes: Mapping[str, str],
    catalog_targets: Mapping[str, float],
    payroll_targets: Mapping[str, float],
    filled_smooth_seats: Set[Tuple[date, str, Optional[str], int]],
    post_pass_guard: PostPassGuard,
) -> None:
    """Last-chance trim → top-up → weekend rebalance → clinical heal before export."""

    persist_tol = CATALOG_PERSIST_TRIM_TOLERANCE
    for _ in range(6):
        progress = False
        trimmed = _trim_catalog_contract_surplus(
            result.assignments,
            states,
            employees=employees,
            shift_templates=shift_templates,
            catalog_targets=catalog_targets,
            period_start=period_start,
            period_end=period_end,
            allow_trim_frozen=True,
            tolerance=persist_tol,
            payroll_targets=payroll_targets,
            expanded_slots=expanded_slots,
            qual_codes=qual_codes,
        )
        trimmed += _trim_parttime_contract_overrun(
            result.assignments,
            states,
            employees=employees,
            shift_templates=shift_templates,
            catalog_targets=catalog_targets,
            period_start=period_start,
            period_end=period_end,
            tolerance=persist_tol,
            expanded_slots=expanded_slots,
            qual_codes=qual_codes,
        )
        if trimmed:
            progress = True
        _rebuild_states_from_assignments(states, result.assignments, shift_templates)

        topped = _catalog_contract_top_up_pass(
            employees=employees,
            states=states,
            assignments=result.assignments,
            shift_templates=shift_templates,
            rules=rules,
            period_start=period_start,
            period_end=period_end,
            availability_blocked=availability_blocked,
            qual_codes=qual_codes,
            catalog_targets=catalog_targets,
            payroll_targets=payroll_targets,
            persist_gate=True,
        )
        if topped:
            progress = True
        _rebuild_states_from_assignments(states, result.assignments, shift_templates)

        restored = _restore_missing_catalog_master_assignments(
            result.assignments,
            states,
            employees=employees,
            shift_templates=shift_templates,
            rules=rules,
            period_start=period_start,
            period_end=period_end,
            availability_blocked=availability_blocked,
            qual_codes=qual_codes,
            fulltime_only=False,
            payroll_targets=payroll_targets,
            catalog_targets=catalog_targets,
        )
        if restored:
            progress = True
        _rebuild_states_from_assignments(states, result.assignments, shift_templates)

        if _run_persist_clinical_coverage_heal(
            result,
            states=states,
            employees=employees,
            expanded_slots=expanded_slots,
            shift_templates=shift_templates,
            shift_required_qualifications=shift_required_qualifications,
            rules=rules,
            period_start=period_start,
            period_end=period_end,
            availability_blocked=availability_blocked,
            qual_codes=qual_codes,
            payroll_targets=payroll_targets,
            catalog_targets=catalog_targets,
            filled_smooth_seats=filled_smooth_seats,
            post_pass_guard=post_pass_guard,
            persist_gate=True,
        ):
            progress = True

        if _run_persist_equity_pass(
            result,
            states=states,
            employees=employees,
            expanded_slots=expanded_slots,
            shift_templates=shift_templates,
            shift_required_qualifications=shift_required_qualifications,
            rules=rules,
            period_start=period_start,
            period_end=period_end,
            availability_blocked=availability_blocked,
            qual_codes=qual_codes,
            catalog_targets=catalog_targets,
            payroll_targets=payroll_targets,
            post_pass_guard=post_pass_guard,
        ):
            progress = True

        if _apply_portage_weekend_pairing_policy(
            result.assignments,
            states,
            employees=employees,
            shift_templates=shift_templates,
            rules=rules,
            period_start=period_start,
            period_end=period_end,
            availability_blocked=availability_blocked,
            qual_codes=qual_codes,
            catalog_targets=catalog_targets,
        ):
            progress = True
        _rebuild_states_from_assignments(states, result.assignments, shift_templates)

        if not progress:
            break


def _finalize_for_persist_gate(
    result: AutoGenerateResult,
    *,
    states: Dict[str, _EmployeeState],
    employees: Sequence[EmployeeProfile],
    expanded_slots: Sequence[ExpandedScheduleSlot],
    shift_templates: Dict[str, ShiftTemplateInfo],
    shift_required_qualifications: Dict[str, Set[str]],
    rules: JurisdictionRules,
    period_start: date,
    period_end: date,
    availability_blocked: Optional[Mapping[str, Set[date]]],
    qual_codes: Mapping[str, str],
    catalog_targets: Mapping[str, float],
    payroll_targets: Mapping[str, float] | None = None,
    filled_smooth_seats: Set[Tuple[date, str, Optional[str], int]],
    post_pass_guard: PostPassGuard,
    template_bands: Mapping[str, str],
    weeks_in_period: int = 8,
) -> None:
    """Persist preflight: trim surplus, top up FT/PT contract, fill gaps legally, rebalance weekends."""

    if payroll_targets is None:
        payroll_targets = catalog_targets

    for _ in range(16):
        made_progress = _run_persist_preflight_pass(
            result,
            states=states,
            employees=employees,
            expanded_slots=expanded_slots,
            shift_templates=shift_templates,
            shift_required_qualifications=shift_required_qualifications,
            rules=rules,
            period_start=period_start,
            period_end=period_end,
            availability_blocked=availability_blocked,
            qual_codes=qual_codes,
            catalog_targets=catalog_targets,
            payroll_targets=payroll_targets,
            filled_smooth_seats=filled_smooth_seats,
            post_pass_guard=post_pass_guard,
            template_bands=template_bands,
        )
        fill_counts = _seat_fill_counts(result.assignments, employees, qual_codes)
        coverage_gap_count = max(
            0,
            result.required_slots_total - result.required_slots_filled,
        )
        violations = _persist_preflight_violations(
            assignments=result.assignments,
            employees=employees,
            shift_templates=shift_templates,
            period_start=period_start,
            period_end=period_end,
            weeks_in_period=weeks_in_period,
            rules=rules,
            qual_codes=qual_codes,
            template_id_to_band=template_bands,
            catalog_targets=catalog_targets,
            coverage_complete=result.coverage_complete,
            coverage_gap_count=coverage_gap_count,
        )
        if not violations:
            break

    _run_persist_final_scrub(
        result,
        states=states,
        employees=employees,
        expanded_slots=expanded_slots,
        shift_templates=shift_templates,
        shift_required_qualifications=shift_required_qualifications,
        rules=rules,
        period_start=period_start,
        period_end=period_end,
        availability_blocked=availability_blocked,
        qual_codes=qual_codes,
        catalog_targets=catalog_targets,
        payroll_targets=payroll_targets,
        filled_smooth_seats=filled_smooth_seats,
        post_pass_guard=post_pass_guard,
    )

    from lab_scheduler.scheduling.auto_pilot import dedupe_planned_assignments

    result.assignments = dedupe_planned_assignments(
        result.assignments,
        template_id_to_band=template_bands,
    )
    _rebuild_states_from_assignments(states, result.assignments, shift_templates)


def _portage_ft_vacant_ids(
    employees: Sequence[EmployeeProfile],
    contract_line_type: str,
) -> Set[str]:
    """Employee ids for full-time vacant Portage master lines on one contract group."""

    ids: Set[str] = set()
    for emp in employees:
        if parse_vacant_portage_line(emp.full_name) is None:
            continue
        if (emp.contract_line_type or "") != contract_line_type:
            continue
        if not _vacant_master_fulltime_line(emp):
            continue
        ids.add(emp.id)
    return ids


def _portage_dn_ft_vacant_ids(employees: Sequence[EmployeeProfile]) -> Set[str]:
    return _portage_ft_vacant_ids(employees, "D/N")


def _hard_freeze_portage_ft_catalog(
    assignments: List[PlannedAssignment],
    states: Dict[str, _EmployeeState],
    *,
    employees: Sequence[EmployeeProfile],
    shift_templates: Dict[str, ShiftTemplateInfo],
    rules: JurisdictionRules,
    period_start: date,
    period_end: date,
    availability_blocked: Optional[Mapping[str, Set[date]]],
    qual_codes: Mapping[str, str],
    contract_line_type: str,
) -> int:
    """Wipe in-period full-time vacant rows for one contract group and re-stamp catalog."""

    ft_ids = _portage_ft_vacant_ids(employees, contract_line_type)
    if not ft_ids:
        return 0

    removed = 0
    for index in range(len(assignments) - 1, -1, -1):
        assignment = assignments[index]
        if assignment.employee_id not in ft_ids:
            continue
        if assignment.assignment_date < period_start or assignment.assignment_date > period_end:
            continue
        template = shift_templates.get(assignment.shift_template_id)
        shift_hours = (template.duration_minutes / 60.0) if template is not None else 0.0
        _remove_assignment_from_state(
            states[assignment.employee_id],
            assignment.assignment_date,
            assignment.shift_template_id,
            shift_hours,
        )
        assignments.pop(index)
        removed += 1

    _rebuild_states_from_assignments(states, assignments, shift_templates)

    weekend_dates_by_employee: Dict[str, List[date]] = {}
    if contract_line_type.upper() == "D/N":
        for employee, assignment_date in _pool_interleave_dn_weekend_catalog_stamps(
            employees,
            period_start,
            period_end,
        ):
            if employee.id not in ft_ids:
                continue
            weekend_dates_by_employee.setdefault(employee.id, []).append(assignment_date)

    added = 0
    for emp in employees:
        if emp.id not in ft_ids:
            continue
        state = states[emp.id]
        emp_qual = infer_qual_code(emp, qual_codes=qual_codes)
        stamp_dates = (
            _catalog_stamp_dates_for_employee(
                emp,
                period_start=period_start,
                period_end=period_end,
                weekend_dates_by_employee=weekend_dates_by_employee,
            )
            if contract_line_type.upper() == "D/N"
            else list(_daterange(period_start, period_end))
        )
        for assignment_date in stamp_dates:
            if availability_blocked and assignment_date in availability_blocked.get(emp.id, set()):
                continue
            expected_code = vacant_master_scheduled_shift_code(
                emp,
                assignment_date,
                period_start,
                assignments=assignments,
                shift_templates=shift_templates,
            )
            if expected_code is None:
                continue
            line_violation = validate_contract_line_eligibility(
                emp.contract_line_type or "",
                expected_code,
                qual_code=emp_qual,
            )
            if line_violation:
                continue
            shift_id = _shift_id_for_code(expected_code, shift_templates)
            if shift_id is None:
                continue
            template = shift_templates[shift_id]
            violation = _would_violate_labor_rules(
                state,
                assignment_date,
                template,
                shift_templates,
                rules,
                period_start,
                period_end,
                availability_blocked,
                enforce_fte_target=False,
                master_catalog_stamp=True,
            )
            if violation:
                continue
            shift_hours = template.duration_minutes / 60.0
            week_start = workweek_for(assignment_date).start
            state.work_dates.add(assignment_date)
            state.assignment_records.append((assignment_date, shift_id))
            state.total_hours += shift_hours
            state.week_hours[week_start] = state.week_hours.get(week_start, 0.0) + shift_hours
            assignments.append(
                PlannedAssignment(
                    employee_id=emp.id,
                    shift_template_id=shift_id,
                    assignment_date=assignment_date,
                    master_template_frozen=True,
                )
            )
            added += 1

    return removed + added


def _hard_freeze_portage_de_ft_catalog(
    assignments: List[PlannedAssignment],
    states: Dict[str, _EmployeeState],
    *,
    employees: Sequence[EmployeeProfile],
    shift_templates: Dict[str, ShiftTemplateInfo],
    rules: JurisdictionRules,
    period_start: date,
    period_end: date,
    availability_blocked: Optional[Mapping[str, Set[date]]],
    qual_codes: Mapping[str, str],
) -> int:
    """Wipe in-period D/E full-time vacant rows and re-stamp the master catalog."""

    return _hard_freeze_portage_ft_catalog(
        assignments,
        states,
        employees=employees,
        shift_templates=shift_templates,
        rules=rules,
        period_start=period_start,
        period_end=period_end,
        availability_blocked=availability_blocked,
        qual_codes=qual_codes,
        contract_line_type="D/E",
    )


def _hard_freeze_portage_dn_ft_catalog(
    assignments: List[PlannedAssignment],
    states: Dict[str, _EmployeeState],
    *,
    employees: Sequence[EmployeeProfile],
    shift_templates: Dict[str, ShiftTemplateInfo],
    rules: JurisdictionRules,
    period_start: date,
    period_end: date,
    availability_blocked: Optional[Mapping[str, Set[date]]],
    qual_codes: Mapping[str, str],
) -> int:
    """Wipe in-period D/N full-time vacant rows and re-stamp the screenshot catalog."""

    return _hard_freeze_portage_ft_catalog(
        assignments,
        states,
        employees=employees,
        shift_templates=shift_templates,
        rules=rules,
        period_start=period_start,
        period_end=period_end,
        availability_blocked=availability_blocked,
        qual_codes=qual_codes,
        contract_line_type="D/N",
    )


def _terminal_vacant_dn_catalog_seal(
    result: AutoGenerateResult,
    *,
    states: Dict[str, _EmployeeState],
    employees: Sequence[EmployeeProfile],
    shift_templates: Dict[str, ShiftTemplateInfo],
    shift_required_qualifications: Dict[str, Set[str]],
    expanded_slots: Sequence[ExpandedScheduleSlot],
    rules: JurisdictionRules,
    period_start: date,
    period_end: date,
    availability_blocked: Optional[Mapping[str, Set[date]]],
    qual_codes: Mapping[str, str],
    target_hours_map: Mapping[str, float],
    period_target_hours: Mapping[str, float],
    post_pass_guard: Optional[PostPassGuard] = None,
    payroll_targets: Mapping[str, float] | None = None,
    catalog_targets: Mapping[str, float] | None = None,
) -> None:
    """Re-close D/N master catalog after clinical/trim passes; scrub any D→N leaks."""

    from lab_scheduler.engine.demand import find_day_night_transition_violations

    for _ in range(4):
        _hard_freeze_portage_dn_ft_catalog(
            result.assignments,
            states,
            employees=employees,
            shift_templates=shift_templates,
            rules=rules,
            period_start=period_start,
            period_end=period_end,
            availability_blocked=availability_blocked,
            qual_codes=qual_codes,
        )
        _rebuild_states_from_assignments(states, result.assignments, shift_templates)
        fill_counts = _seat_fill_counts(result.assignments, employees, qual_codes)
        _deterministic_resolve_day_night_transitions(
            result.assignments,
            states,
            employees=employees,
            shift_templates=shift_templates,
            shift_required_qualifications=shift_required_qualifications,
            expanded_slots=expanded_slots,
            rules=rules,
            period_start=period_start,
            period_end=period_end,
            availability_blocked=availability_blocked,
            qual_codes=qual_codes,
            target_hours_map=target_hours_map,
            period_target_hours=period_target_hours,
            fill_counts=fill_counts,
            dn_only=True,
        )
        _rebuild_states_from_assignments(states, result.assignments, shift_templates)
        scan_rows = [
            (assignment.employee_id, assignment.assignment_date, assignment.shift_template_id)
            for assignment in result.assignments
        ]
        if not find_day_night_transition_violations(scan_rows, shift_templates):
            break


def _terminal_vacant_de_catalog_seal(
    result: AutoGenerateResult,
    *,
    states: Dict[str, _EmployeeState],
    employees: Sequence[EmployeeProfile],
    shift_templates: Dict[str, ShiftTemplateInfo],
    rules: JurisdictionRules,
    period_start: date,
    period_end: date,
    availability_blocked: Optional[Mapping[str, Set[date]]],
    qual_codes: Mapping[str, str],
    post_pass_guard: Optional[PostPassGuard] = None,
    payroll_targets: Mapping[str, float] | None = None,
    catalog_targets: Mapping[str, float] | None = None,
) -> None:
    """Re-close D/E master catalog after clinical/trim passes."""

    for _ in range(4):
        edits = _enforce_de_fulltime_master_catalog(
            result.assignments,
            states,
            employees=employees,
            shift_templates=shift_templates,
            rules=rules,
            period_start=period_start,
            period_end=period_end,
            availability_blocked=availability_blocked,
            qual_codes=qual_codes,
            post_pass_guard=post_pass_guard,
            payroll_targets=payroll_targets,
            catalog_targets=catalog_targets,
        )
        _rebuild_states_from_assignments(states, result.assignments, shift_templates)
        if edits == 0:
            break


def _portage_finalize_catalog_and_clinical_floors(
    result: AutoGenerateResult,
    *,
    states: Dict[str, _EmployeeState],
    employees: Sequence[EmployeeProfile],
    shift_templates: Dict[str, ShiftTemplateInfo],
    shift_required_qualifications: Dict[str, Set[str]],
    expanded_slots: Sequence[ExpandedScheduleSlot],
    rules: JurisdictionRules,
    period_start: date,
    period_end: date,
    availability_blocked: Optional[Mapping[str, Set[date]]],
    qual_codes: Mapping[str, str],
    target_hours_map: Mapping[str, float],
    period_target_hours: Mapping[str, float],
    post_pass_guard: Optional[PostPassGuard] = None,
    catalog_targets: Mapping[str, float] | None = None,
) -> None:
    """
    Last-mile Portage integrity: freeze D/N catalog, fill 2E/2N clinical seats, re-freeze D/N.
    """

    _hard_freeze_portage_dn_ft_catalog(
        result.assignments,
        states,
        employees=employees,
        shift_templates=shift_templates,
        rules=rules,
        period_start=period_start,
        period_end=period_end,
        availability_blocked=availability_blocked,
        qual_codes=qual_codes,
    )
    _hard_freeze_portage_de_ft_catalog(
        result.assignments,
        states,
        employees=employees,
        shift_templates=shift_templates,
        rules=rules,
        period_start=period_start,
        period_end=period_end,
        availability_blocked=availability_blocked,
        qual_codes=qual_codes,
    )
    _rebuild_states_from_assignments(states, result.assignments, shift_templates)
    fill_counts = _seat_fill_counts(result.assignments, employees, qual_codes)
    _deterministic_resolve_day_night_transitions(
        result.assignments,
        states,
        employees=employees,
        shift_templates=shift_templates,
        shift_required_qualifications=shift_required_qualifications,
        expanded_slots=expanded_slots,
        rules=rules,
        period_start=period_start,
        period_end=period_end,
        availability_blocked=availability_blocked,
        qual_codes=qual_codes,
        target_hours_map=target_hours_map,
        period_target_hours=period_target_hours,
        fill_counts=fill_counts,
        dn_only=True,
    )
    _rebuild_states_from_assignments(states, result.assignments, shift_templates)
    fill_counts = _seat_fill_counts(result.assignments, employees, qual_codes)
    _extend_evening_night_clinical_lockdown(
        result,
        employees=employees,
        states=states,
        expanded_slots=expanded_slots,
        shift_templates=shift_templates,
        shift_required_qualifications=shift_required_qualifications,
        rules=rules,
        fill_counts=fill_counts,
        availability_blocked=availability_blocked,
        qual_codes=qual_codes,
        period_start=period_start,
        period_end=period_end,
        log_critical_gaps=False,
        post_pass_guard=None,
        allow_frozen_clinical_supersede=False,
        max_rounds=48,
        weekend_first=True,
        payroll_targets=target_hours_map,
        catalog_targets=catalog_targets,
    )
    _rebuild_states_from_assignments(states, result.assignments, shift_templates)
    _terminal_vacant_dn_catalog_seal(
        result,
        states=states,
        employees=employees,
        shift_templates=shift_templates,
        shift_required_qualifications=shift_required_qualifications,
        expanded_slots=expanded_slots,
        rules=rules,
        period_start=period_start,
        period_end=period_end,
        availability_blocked=availability_blocked,
        qual_codes=qual_codes,
        target_hours_map=target_hours_map,
        period_target_hours=period_target_hours,
        post_pass_guard=None,
        payroll_targets=target_hours_map,
        catalog_targets=catalog_targets,
    )
    _terminal_vacant_de_catalog_seal(
        result,
        states=states,
        employees=employees,
        shift_templates=shift_templates,
        rules=rules,
        period_start=period_start,
        period_end=period_end,
        availability_blocked=availability_blocked,
        qual_codes=qual_codes,
        post_pass_guard=None,
        payroll_targets=target_hours_map,
        catalog_targets=catalog_targets,
    )
    _rebuild_states_from_assignments(states, result.assignments, shift_templates)
    fill_counts = _seat_fill_counts(result.assignments, employees, qual_codes)
    if not evening_night_clinical_seats_satisfied(
        fill_counts=fill_counts,
        expanded_slots=expanded_slots,
        shift_templates=shift_templates,
        period_start=period_start,
        period_end=period_end,
    ):
        _extend_evening_night_clinical_lockdown(
            result,
            employees=employees,
            states=states,
            expanded_slots=expanded_slots,
            shift_templates=shift_templates,
            shift_required_qualifications=shift_required_qualifications,
            rules=rules,
            fill_counts=fill_counts,
            availability_blocked=availability_blocked,
            qual_codes=qual_codes,
            period_start=period_start,
            period_end=period_end,
            log_critical_gaps=False,
            post_pass_guard=None,
            allow_frozen_clinical_supersede=False,
            max_rounds=24,
            weekend_first=True,
            payroll_targets=target_hours_map,
            catalog_targets=catalog_targets,
        )
    _hard_freeze_portage_dn_ft_catalog(
        result.assignments,
        states,
        employees=employees,
        shift_templates=shift_templates,
        rules=rules,
        period_start=period_start,
        period_end=period_end,
        availability_blocked=availability_blocked,
        qual_codes=qual_codes,
    )
    _rebuild_states_from_assignments(states, result.assignments, shift_templates)
    fill_counts = _seat_fill_counts(result.assignments, employees, qual_codes)
    _deterministic_resolve_day_night_transitions(
        result.assignments,
        states,
        employees=employees,
        shift_templates=shift_templates,
        shift_required_qualifications=shift_required_qualifications,
        expanded_slots=expanded_slots,
        rules=rules,
        period_start=period_start,
        period_end=period_end,
        availability_blocked=availability_blocked,
        qual_codes=qual_codes,
        target_hours_map=target_hours_map,
        period_target_hours=period_target_hours,
        fill_counts=fill_counts,
        dn_only=True,
    )
    _rebuild_states_from_assignments(states, result.assignments, shift_templates)
    _apply_portage_weekend_pairing_policy(
        result.assignments,
        states,
        employees=employees,
        shift_templates=shift_templates,
        rules=rules,
        period_start=period_start,
        period_end=period_end,
        availability_blocked=availability_blocked,
        qual_codes=qual_codes,
        catalog_targets=catalog_targets,
    )


def _portage_post_finalize_hours_balance(
    result: AutoGenerateResult,
    *,
    states: Dict[str, _EmployeeState],
    employees: Sequence[EmployeeProfile],
    expanded_slots: Sequence[ExpandedScheduleSlot],
    shift_templates: Dict[str, ShiftTemplateInfo],
    shift_required_qualifications: Dict[str, Set[str]],
    rules: JurisdictionRules,
    period_start: date,
    period_end: date,
    weeks_in_period: int,
    availability_blocked: Optional[Mapping[str, Set[date]]],
    qual_codes: Mapping[str, str],
    catalog_targets: Mapping[str, float],
    payroll_targets: Mapping[str, float],
    period_target_hours: Mapping[str, float],
    filled_smooth_seats: Set[Tuple[date, str, Optional[str], int]],
    post_pass_guard: Optional[PostPassGuard] = None,
) -> None:
    """Shed surplus hours re-introduced by catalog hard-freeze without re-opening D/N drift."""

    from lab_scheduler.scheduling.contract_payroll import fulltime_period_contract_hours

    fulltime_target = fulltime_period_contract_hours(
        rules=rules,
        weeks_in_period=weeks_in_period,
    )
    guard = post_pass_guard or _post_pass_guard_for_result(
        result,
        employees=employees,
        period_start=period_start,
    )

    def _terminal_dn_scrub() -> None:
        fill_counts = _seat_fill_counts(result.assignments, employees, qual_codes)
        _deterministic_resolve_day_night_transitions(
            result.assignments,
            states,
            employees=employees,
            shift_templates=shift_templates,
            shift_required_qualifications=shift_required_qualifications,
            expanded_slots=expanded_slots,
            rules=rules,
            period_start=period_start,
            period_end=period_end,
            availability_blocked=availability_blocked,
            qual_codes=qual_codes,
            target_hours_map=payroll_targets,
            period_target_hours=period_target_hours,
            fill_counts=fill_counts,
            dn_only=True,
        )
        _rebuild_states_from_assignments(states, result.assignments, shift_templates)

    if not _any_contract_finalize_surplus(
        employees,
        states,
        payroll_targets=payroll_targets,
        catalog_targets=catalog_targets,
    ):
        _terminal_dn_scrub()
        return

    for _ in range(8):
        _enforce_portage_operational_band_caps(
            result.assignments,
            states,
            employees=employees,
            shift_templates=shift_templates,
            qual_codes=qual_codes,
            period_start=period_start,
            period_end=period_end,
            fulltime_target=fulltime_target,
            catalog_targets=catalog_targets,
        )
        _rebuild_states_from_assignments(states, result.assignments, shift_templates)
        trimmed = 0
        for _inner in range(4):
            inner_trimmed = 0
            inner_trimmed += _trim_parttime_contract_overrun(
                result.assignments,
                states,
                employees=employees,
                shift_templates=shift_templates,
                catalog_targets=catalog_targets,
                period_start=period_start,
                period_end=period_end,
                expanded_slots=expanded_slots,
                qual_codes=qual_codes,
                post_pass_guard=guard,
                anchor_violations=result.anchor_violations,
            )
            inner_trimmed += _trim_parttime_vacant_day_surplus(
                result.assignments,
                states,
                employees=employees,
                shift_templates=shift_templates,
                catalog_targets=catalog_targets,
                period_start=period_start,
                period_end=period_end,
                allow_trim_frozen=True,
                expanded_slots=expanded_slots,
                qual_codes=qual_codes,
            )
            inner_trimmed += _trim_catalog_contract_surplus(
                result.assignments,
                states,
                employees=employees,
                shift_templates=shift_templates,
                catalog_targets=catalog_targets,
                period_start=period_start,
                period_end=period_end,
                allow_trim_frozen=True,
                expanded_slots=expanded_slots,
                qual_codes=qual_codes,
                post_pass_guard=guard,
                anchor_violations=result.anchor_violations,
            )
            inner_trimmed += _trim_vacant_lines_over_catalog_band(
                result.assignments,
                states,
                employees=employees,
                shift_templates=shift_templates,
                catalog_targets=catalog_targets,
                period_start=period_start,
                period_end=period_end,
                expanded_slots=expanded_slots,
                qual_codes=qual_codes,
            )
            trimmed += inner_trimmed
            _rebuild_states_from_assignments(states, result.assignments, shift_templates)
            if inner_trimmed == 0:
                break

        fill_counts = _seat_fill_counts(result.assignments, employees, qual_codes)
        coverage_ok = _required_coverage_slots_satisfied(
            expanded_slots=expanded_slots,
            fill_counts=fill_counts,
            shift_templates=shift_templates,
        )
        clinical_ok = evening_night_clinical_seats_satisfied(
            fill_counts=fill_counts,
            expanded_slots=expanded_slots,
            shift_templates=shift_templates,
            period_start=period_start,
            period_end=period_end,
        )
        surplus_ok = not _any_contract_finalize_surplus(
            employees,
            states,
            payroll_targets=payroll_targets,
            catalog_targets=catalog_targets,
        )

        if trimmed == 0 and surplus_ok and coverage_ok and clinical_ok:
            break

        if not coverage_ok or not clinical_ok:
            fill_counts = _seat_fill_counts(result.assignments, employees, qual_codes)
            _extend_evening_night_clinical_lockdown(
                result,
                employees=employees,
                states=states,
                expanded_slots=expanded_slots,
                shift_templates=shift_templates,
                shift_required_qualifications=shift_required_qualifications,
                rules=rules,
                fill_counts=fill_counts,
                availability_blocked=availability_blocked,
                qual_codes=qual_codes,
                period_start=period_start,
                period_end=period_end,
                log_critical_gaps=False,
                post_pass_guard=guard,
                allow_frozen_clinical_supersede=False,
                max_rounds=8,
                weekend_first=True,
                payroll_targets=payroll_targets,
                catalog_targets=catalog_targets,
            )
            _rebuild_states_from_assignments(states, result.assignments, shift_templates)
            _terminal_dn_scrub()

    _terminal_dn_scrub()
    _apply_portage_weekend_pairing_policy(
        result.assignments,
        states,
        employees=employees,
        shift_templates=shift_templates,
        rules=rules,
        period_start=period_start,
        period_end=period_end,
        availability_blocked=availability_blocked,
        qual_codes=qual_codes,
        catalog_targets=catalog_targets,
    )


def _seal_portage_generate_result(
    result: AutoGenerateResult,
    *,
    states: Dict[str, _EmployeeState],
    employees: Sequence[EmployeeProfile],
    expanded_slots: Sequence[ExpandedScheduleSlot],
    shift_templates: Dict[str, ShiftTemplateInfo],
    shift_required_qualifications: Dict[str, Set[str]],
    rules: JurisdictionRules,
    period_start: date,
    period_end: date,
    weeks_in_period: int,
    availability_blocked: Optional[Mapping[str, Set[date]]],
    qual_codes: Mapping[str, str],
    catalog_targets: Mapping[str, float],
    target_hours_map: Mapping[str, float],
    period_target_hours: Mapping[str, float],
    filled_smooth_seats: Set[Tuple[date, str, Optional[str], int]],
    coverage_targets: Optional[Sequence[CoverageTierTarget]] = None,
    impossible_tier_ids: Optional[Set[str]] = None,
) -> None:
    """
    Dedupe, heal required demand seats, and refresh coverage metrics after finalize.

    Persist and breakroom export both dedupe assignments; sealing here keeps
    ``required_slots_filled`` aligned with what the UI will validate.
    """

    from lab_scheduler.engine.constraints import (
        evaluate_coverage_tier_results,
        is_schedule_coverage_complete,
    )
    from lab_scheduler.scheduling.auto_pilot import dedupe_planned_assignments
    from lab_scheduler.scheduling.schedule_tallies import shift_band_from_template_code

    template_bands = {
        template_id: shift_band_from_template_code(info.code)
        for template_id, info in shift_templates.items()
    }
    result.assignments = dedupe_planned_assignments(
        result.assignments,
        template_id_to_band=template_bands,
    )
    _rebuild_states_from_assignments(states, result.assignments, shift_templates)
    post_pass_guard = _post_pass_guard_for_result(
        result,
        employees=employees,
        period_start=period_start,
    )
    _enforce_dn_fulltime_master_catalog(
        result.assignments,
        states,
        employees=employees,
        shift_templates=shift_templates,
        rules=rules,
        period_start=period_start,
        period_end=period_end,
        availability_blocked=availability_blocked,
        qual_codes=qual_codes,
        post_pass_guard=post_pass_guard,
        payroll_targets=target_hours_map,
        catalog_targets=catalog_targets,
    )
    _enforce_de_fulltime_master_catalog(
        result.assignments,
        states,
        employees=employees,
        shift_templates=shift_templates,
        rules=rules,
        period_start=period_start,
        period_end=period_end,
        availability_blocked=availability_blocked,
        qual_codes=qual_codes,
        post_pass_guard=post_pass_guard,
        payroll_targets=target_hours_map,
        catalog_targets=catalog_targets,
    )
    _rebuild_states_from_assignments(states, result.assignments, shift_templates)
    _restore_missing_catalog_master_assignments(
        result.assignments,
        states,
        employees=employees,
        shift_templates=shift_templates,
        rules=rules,
        period_start=period_start,
        period_end=period_end,
        availability_blocked=availability_blocked,
        qual_codes=qual_codes,
        payroll_targets=target_hours_map,
        catalog_targets=catalog_targets,
    )
    _rebuild_states_from_assignments(states, result.assignments, shift_templates)
    _heal_required_coverage_after_catalog_trim(
        result,
        states=states,
        employees=employees,
        expanded_slots=expanded_slots,
        shift_templates=shift_templates,
        shift_required_qualifications=shift_required_qualifications,
        rules=rules,
        period_start=period_start,
        period_end=period_end,
        availability_blocked=availability_blocked,
        qual_codes=qual_codes,
        catalog_targets=catalog_targets,
        target_hours_map=target_hours_map,
        period_target_hours=period_target_hours,
        filled_smooth_seats=filled_smooth_seats,
        post_pass_guard=post_pass_guard,
    )
    fill_counts = _seat_fill_counts(result.assignments, employees, qual_codes)
    from lab_scheduler.scheduling.load_balancing import trim_weekend_daily_qual_over_cap

    for _ in range(4):
        _restore_missing_catalog_master_assignments(
            result.assignments,
            states,
            employees=employees,
            shift_templates=shift_templates,
            rules=rules,
            period_start=period_start,
            period_end=period_end,
            availability_blocked=availability_blocked,
            qual_codes=qual_codes,
            payroll_targets=target_hours_map,
            catalog_targets=catalog_targets,
        )
        _rebuild_states_from_assignments(states, result.assignments, shift_templates)
        fill_counts = _seat_fill_counts(result.assignments, employees, qual_codes)
        if not _required_coverage_slots_satisfied(
            expanded_slots=expanded_slots,
            fill_counts=fill_counts,
            shift_templates=shift_templates,
        ):
            _extend_evening_night_clinical_lockdown(
                result,
                employees=employees,
                states=states,
                expanded_slots=expanded_slots,
                shift_templates=shift_templates,
                shift_required_qualifications=shift_required_qualifications,
                rules=rules,
                fill_counts=fill_counts,
                availability_blocked=availability_blocked,
                qual_codes=qual_codes,
                period_start=period_start,
                period_end=period_end,
                log_critical_gaps=False,
                post_pass_guard=post_pass_guard,
                allow_frozen_clinical_supersede=True,
                max_rounds=12,
                weekend_first=True,
                    payroll_targets=target_hours_map,
        catalog_targets=catalog_targets,
    )
            _rebuild_states_from_assignments(states, result.assignments, shift_templates)
        trim_weekend_daily_qual_over_cap(
            result.assignments,
            states=states,
            employees=employees,
            qual_codes=qual_codes,
            shift_templates=shift_templates,
            period_start=period_start,
            period_end=period_end,
        )
        _rebuild_states_from_assignments(states, result.assignments, shift_templates)
        fill_counts = _seat_fill_counts(result.assignments, employees, qual_codes)
        if _required_coverage_slots_satisfied(
            expanded_slots=expanded_slots,
            fill_counts=fill_counts,
            shift_templates=shift_templates,
        ):
            break
    from lab_scheduler.scheduling.night_streak_corrector import (
        correct_portage_night_streaks,
        trim_consecutive_night_overruns,
    )

    for _ in range(8):
        correct_portage_night_streaks(
            result.assignments,
            employees=employees,
            shift_templates=shift_templates,
            shift_required_qualifications=shift_required_qualifications,
            rules=rules,
            period_start=period_start,
            period_end=period_end,
            weeks_in_period=weeks_in_period,
            employee_target_hours=catalog_targets,
            availability_blocked=availability_blocked,
        )
        _break_portage_night_streaks_by_rest_day(
            result.assignments,
            states,
            employees=employees,
            shift_templates=shift_templates,
            rules=rules,
            period_start=period_start,
            period_end=period_end,
            availability_blocked=availability_blocked,
            qual_codes=qual_codes,
        )
        trim_consecutive_night_overruns(
            result.assignments,
            employees=employees,
            shift_templates=shift_templates,
            period_start=period_start,
            period_end=period_end,
            post_pass_guard=post_pass_guard,
            anchor_violations=result.anchor_violations,
        )
        _rebuild_states_from_assignments(states, result.assignments, shift_templates)
        for _ in range(4):
            trimmed = _trim_parttime_contract_overrun(
                result.assignments,
                states,
                employees=employees,
                shift_templates=shift_templates,
                catalog_targets=catalog_targets,
                period_start=period_start,
                period_end=period_end,
                expanded_slots=expanded_slots,
                qual_codes=qual_codes,
                post_pass_guard=post_pass_guard,
                anchor_violations=result.anchor_violations,
            )
            trimmed += _trim_vacant_lines_over_catalog_band(
                result.assignments,
                states,
                employees=employees,
                shift_templates=shift_templates,
                catalog_targets=catalog_targets,
                period_start=period_start,
                period_end=period_end,
                expanded_slots=expanded_slots,
                qual_codes=qual_codes,
            )
            trimmed += _trim_catalog_contract_surplus(
                result.assignments,
                states,
                employees=employees,
                shift_templates=shift_templates,
                catalog_targets=catalog_targets,
                period_start=period_start,
                period_end=period_end,
                allow_trim_frozen=True,
                expanded_slots=expanded_slots,
                qual_codes=qual_codes,
                post_pass_guard=post_pass_guard,
                anchor_violations=result.anchor_violations,
            )
            trimmed += _trim_parttime_vacant_day_surplus(
                result.assignments,
                states,
                employees=employees,
                shift_templates=shift_templates,
                catalog_targets=catalog_targets,
                period_start=period_start,
                period_end=period_end,
                allow_trim_frozen=True,
                expanded_slots=expanded_slots,
                qual_codes=qual_codes,
            )
            _rebuild_states_from_assignments(states, result.assignments, shift_templates)
            if trimmed == 0:
                break
        fill_counts = _seat_fill_counts(result.assignments, employees, qual_codes)
        if not _required_coverage_slots_satisfied(
            expanded_slots=expanded_slots,
            fill_counts=fill_counts,
            shift_templates=shift_templates,
        ) or not evening_night_clinical_seats_satisfied(
            fill_counts=fill_counts,
            expanded_slots=expanded_slots,
            shift_templates=shift_templates,
            period_start=period_start,
            period_end=period_end,
        ):
            _restore_missing_catalog_master_assignments(
                result.assignments,
                states,
                employees=employees,
                shift_templates=shift_templates,
                rules=rules,
                period_start=period_start,
                period_end=period_end,
                availability_blocked=availability_blocked,
                qual_codes=qual_codes,
                payroll_targets=target_hours_map,
                catalog_targets=catalog_targets,
            )
            _rebuild_states_from_assignments(states, result.assignments, shift_templates)
            fill_counts = _seat_fill_counts(result.assignments, employees, qual_codes)
            _extend_evening_night_clinical_lockdown(
                result,
                employees=employees,
                states=states,
                expanded_slots=expanded_slots,
                shift_templates=shift_templates,
                shift_required_qualifications=shift_required_qualifications,
                rules=rules,
                fill_counts=fill_counts,
                availability_blocked=availability_blocked,
                qual_codes=qual_codes,
                period_start=period_start,
                period_end=period_end,
                log_critical_gaps=False,
                post_pass_guard=post_pass_guard,
                allow_frozen_clinical_supersede=True,
                max_rounds=8,
                weekend_first=True,
                    payroll_targets=target_hours_map,
        catalog_targets=catalog_targets,
    )
            _rebuild_states_from_assignments(states, result.assignments, shift_templates)
        fill_counts = _seat_fill_counts(result.assignments, employees, qual_codes)
        contract_surplus = _any_contract_finalize_surplus(
            employees,
            states,
            payroll_targets=target_hours_map,
            catalog_targets=catalog_targets,
        )
        if (
            _required_coverage_slots_satisfied(
                expanded_slots=expanded_slots,
                fill_counts=fill_counts,
                shift_templates=shift_templates,
            )
            and evening_night_clinical_seats_satisfied(
                fill_counts=fill_counts,
                expanded_slots=expanded_slots,
                shift_templates=shift_templates,
                period_start=period_start,
                period_end=period_end,
            )
            and not contract_surplus
        ):
            break
    _finalize_for_persist_gate(
        result,
        states=states,
        employees=employees,
        expanded_slots=expanded_slots,
        shift_templates=shift_templates,
        shift_required_qualifications=shift_required_qualifications,
        rules=rules,
        period_start=period_start,
        period_end=period_end,
        availability_blocked=availability_blocked,
        qual_codes=qual_codes,
        catalog_targets=catalog_targets,
        payroll_targets=target_hours_map,
        filled_smooth_seats=filled_smooth_seats,
        post_pass_guard=post_pass_guard,
        template_bands=template_bands,
        weeks_in_period=weeks_in_period,
    )
    _portage_finalize_catalog_and_clinical_floors(
        result,
        states=states,
        employees=employees,
        shift_templates=shift_templates,
        shift_required_qualifications=shift_required_qualifications,
        expanded_slots=expanded_slots,
        rules=rules,
        period_start=period_start,
        period_end=period_end,
        availability_blocked=availability_blocked,
        qual_codes=qual_codes,
        target_hours_map=target_hours_map,
        period_target_hours=period_target_hours,
        post_pass_guard=post_pass_guard,
        catalog_targets=catalog_targets,
    )
    _portage_post_finalize_hours_balance(
        result,
        states=states,
        employees=employees,
        expanded_slots=expanded_slots,
        shift_templates=shift_templates,
        shift_required_qualifications=shift_required_qualifications,
        rules=rules,
        period_start=period_start,
        period_end=period_end,
        weeks_in_period=weeks_in_period,
        availability_blocked=availability_blocked,
        qual_codes=qual_codes,
        catalog_targets=catalog_targets,
        payroll_targets=target_hours_map,
        period_target_hours=period_target_hours,
        filled_smooth_seats=filled_smooth_seats,
        post_pass_guard=post_pass_guard,
    )
    _rebuild_states_from_assignments(states, result.assignments, shift_templates)
    _apply_portage_weekend_pairing_policy(
        result.assignments,
        states,
        employees=employees,
        shift_templates=shift_templates,
        rules=rules,
        period_start=period_start,
        period_end=period_end,
        availability_blocked=availability_blocked,
        qual_codes=qual_codes,
        catalog_targets=catalog_targets,
    )
    _enforce_de_fulltime_master_catalog(
        result.assignments,
        states,
        employees=employees,
        shift_templates=shift_templates,
        rules=rules,
        period_start=period_start,
        period_end=period_end,
        availability_blocked=availability_blocked,
        qual_codes=qual_codes,
        post_pass_guard=post_pass_guard,
        payroll_targets=target_hours_map,
        catalog_targets=catalog_targets,
    )
    _rebuild_states_from_assignments(states, result.assignments, shift_templates)
    fill_counts = _seat_fill_counts(result.assignments, employees, qual_codes)
    result.clinical_gap_reports = _collect_clinical_gap_reports(
        expanded_slots=expanded_slots,
        fill_counts=fill_counts,
        shift_templates=shift_templates,
        period_start=period_start,
        period_end=period_end,
    )
    _update_slot_fill_metrics(
        result,
        expanded_slots=expanded_slots,
        fill_counts=fill_counts,
        shift_templates=shift_templates,
    )
    if coverage_targets:
        employee_hours = {employee_id: state.total_hours for employee_id, state in states.items()}
        tier_results = evaluate_coverage_tier_results(
            targets=coverage_targets,
            employee_hours=employee_hours,
            rules=rules,
            weeks_in_period=weeks_in_period,
            impossible_tier_ids=impossible_tier_ids or set(),
            shift_templates=shift_templates,
            period_start=period_start,
            period_end=period_end,
        )
        result.coverage_tier_results = list(tier_results)
        result.coverage_complete = is_schedule_coverage_complete(
            unfilled_coverage_gaps=result.coverage_gap_count,
            tier_results=tier_results,
        )


def _peer_same_day_band_swap_labor_ok(
    state: _EmployeeState,
    assignment_date: date,
    template: ShiftTemplateInfo,
    shift_templates: Dict[str, ShiftTemplateInfo],
    rules: JurisdictionRules,
    period_start: date,
    period_end: date,
) -> Optional[str]:
    """Fatigue/week-rest/night-streak checks for pool-neutral vacant-line band swaps."""

    if template.code == "NIGHT":
        from lab_scheduler.scheduling.night_streak_corrector import (
            PORTAGE_MAX_CONSECUTIVE_NIGHTS,
            find_consecutive_night_streaks,
        )

        night_id = next(
            (template_id for template_id, info in shift_templates.items() if info.code == "NIGHT"),
            None,
        )
        if night_id is not None:
            simulated = [
                PlannedAssignment(state.profile.id, template_id, work_date)
                for work_date, template_id in state.assignment_records
            ] + [PlannedAssignment(state.profile.id, night_id, assignment_date)]
            streaks = find_consecutive_night_streaks(
                employee_id=state.profile.id,
                period_start=period_start,
                period_end=period_end,
                assignments=simulated,
                shift_templates=shift_templates,
                min_length=PORTAGE_MAX_CONSECUTIVE_NIGHTS + 1,
            )
            if streaks:
                return (
                    f"would exceed {PORTAGE_MAX_CONSECUTIVE_NIGHTS} consecutive night shifts"
                )

    simulated_dates = sorted(state.work_dates | {assignment_date})
    streak_limit = (
        rules.max_consecutive_work_days
        if state.profile.modified_work_schedule
        else PORTAGE_MAX_CONSECUTIVE_WORK_DAYS
    )
    for _start, _end, length in _consecutive_work_day_streaks(simulated_dates):
        if length > streak_limit:
            return f"would exceed {streak_limit} consecutive work days"
        if not state.profile.modified_work_schedule and length > rules.max_consecutive_work_days:
            return f"would exceed {rules.max_consecutive_work_days} consecutive work days"

    work_set = set(simulated_dates)
    for ws, we in _iter_week_bounds(period_start, period_end):
        days_in_week = [ws + timedelta(days=i) for i in range((we - ws).days + 1)]
        worked = sum(1 for d in days_in_week if d in work_set)
        if worked > rules.max_work_days_per_work_week:
            return f"would leave insufficient weekly rest in week starting {ws.isoformat()}"

    return None


def _try_peer_same_day_band_swap(
    assignments: List[PlannedAssignment],
    states: Dict[str, _EmployeeState],
    *,
    donor_id: str,
    recipient_id: str,
    swap_date: date,
    employees_by_id: Mapping[str, EmployeeProfile],
    shift_templates: Dict[str, ShiftTemplateInfo],
    rules: JurisdictionRules,
    period_start: date,
    period_end: date,
    availability_blocked: Optional[Mapping[str, Set[date]]],
    qual_codes: Mapping[str, str],
    alternate_band: str,
    allow_frozen_swaps: bool = False,
    relaxed_labor: bool = False,
    post_pass_guard: Optional[PostPassGuard] = None,
) -> bool:
    """Swap donor alternate band for recipient day band on one date (coverage-neutral)."""

    donor_assignment = _employee_assignment_on_date(
        assignments,
        employee_id=donor_id,
        assignment_date=swap_date,
    )
    recipient_assignment = _employee_assignment_on_date(
        assignments,
        employee_id=recipient_id,
        assignment_date=swap_date,
    )
    if donor_assignment is None or recipient_assignment is None:
        return False
    if _manager_lock_denies_cell_edit(
        post_pass_guard,
        assignments,
        employee_id=donor_id,
        assignment_date=swap_date,
        shift_templates=shift_templates,
    ) or _manager_lock_denies_cell_edit(
        post_pass_guard,
        assignments,
        employee_id=recipient_id,
        assignment_date=swap_date,
        shift_templates=shift_templates,
    ):
        return False
    donor_frozen = getattr(donor_assignment, "master_template_frozen", False)
    recipient_frozen = getattr(recipient_assignment, "master_template_frozen", False)
    if (donor_frozen or recipient_frozen) and not allow_frozen_swaps:
        return False

    donor_band = _assignment_band(donor_assignment, shift_templates)
    recipient_band = _assignment_band(recipient_assignment, shift_templates)
    if donor_band != alternate_band or recipient_band != "D":
        return False
    cap = CLINICAL_FLOOR.get("EVENING" if alternate_band == "E" else "NIGHT", 2)
    if (
        _daily_pool_band_count(
            assignments,
            shift_templates,
            assignment_date=swap_date,
            band=alternate_band,
        )
        > cap
    ):
        return False

    donor = employees_by_id[donor_id]
    recipient = employees_by_id[recipient_id]
    donor_template = shift_templates[donor_assignment.shift_template_id]
    recipient_template = shift_templates[recipient_assignment.shift_template_id]
    donor_hours = donor_template.duration_minutes / 60.0
    recipient_hours = recipient_template.duration_minutes / 60.0

    if validate_contract_line_eligibility(
        donor.contract_line_type,
        recipient_template.code,
        qual_code=infer_qual_code(donor, qual_codes=qual_codes),
    ):
        return False
    if validate_contract_line_eligibility(
        recipient.contract_line_type,
        donor_template.code,
        qual_code=infer_qual_code(recipient, qual_codes=qual_codes),
    ):
        return False

    donor_state = states[donor_id]
    recipient_state = states[recipient_id]
    _remove_assignment_from_state(
        donor_state,
        swap_date,
        donor_assignment.shift_template_id,
        donor_hours,
    )
    _remove_assignment_from_state(
        recipient_state,
        swap_date,
        recipient_assignment.shift_template_id,
        recipient_hours,
    )

    donor_violation = (
        _peer_same_day_band_swap_labor_ok(
            donor_state,
            swap_date,
            recipient_template,
            shift_templates,
            rules,
            period_start,
            period_end,
        )
        if relaxed_labor
        else _would_violate_labor_rules(
            donor_state,
            swap_date,
            recipient_template,
            shift_templates,
            rules,
            period_start,
            period_end,
            availability_blocked,
            enforce_fte_target=False,
            relax_dn_contract_completion=True,
            peer_equity_swap=True,
        )
    )
    recipient_violation = (
        _peer_same_day_band_swap_labor_ok(
            recipient_state,
            swap_date,
            donor_template,
            shift_templates,
            rules,
            period_start,
            period_end,
        )
        if relaxed_labor
        else _would_violate_labor_rules(
            recipient_state,
            swap_date,
            donor_template,
            shift_templates,
            rules,
            period_start,
            period_end,
            availability_blocked,
            enforce_fte_target=False,
            relax_dn_contract_completion=True,
            peer_equity_swap=True,
        )
    )
    if donor_violation or recipient_violation:
        _apply_assignment_to_state(
            donor_state,
            swap_date,
            donor_assignment.shift_template_id,
            donor_hours,
            rules=rules,
        )
        _apply_assignment_to_state(
            recipient_state,
            swap_date,
            recipient_assignment.shift_template_id,
            recipient_hours,
            rules=rules,
        )
        return False

    for index, assignment in enumerate(assignments):
        if (
            assignment.employee_id == donor_id
            and assignment.assignment_date == swap_date
        ):
            assignments[index] = PlannedAssignment(
                employee_id=donor_id,
                shift_template_id=recipient_assignment.shift_template_id,
                assignment_date=swap_date,
                master_template_frozen=donor_frozen or recipient_frozen,
            )
        elif (
            assignment.employee_id == recipient_id
            and assignment.assignment_date == swap_date
        ):
            assignments[index] = PlannedAssignment(
                employee_id=recipient_id,
                shift_template_id=donor_assignment.shift_template_id,
                assignment_date=swap_date,
                master_template_frozen=donor_frozen or recipient_frozen,
            )

    _apply_assignment_to_state(
        recipient_state,
        swap_date,
        donor_assignment.shift_template_id,
        donor_hours,
        rules=rules,
    )
    _apply_assignment_to_state(
        donor_state,
        swap_date,
        recipient_assignment.shift_template_id,
        recipient_hours,
        rules=rules,
    )
    return True


def _try_peer_transfer_shift_on_date(
    assignments: List[PlannedAssignment],
    states: Dict[str, _EmployeeState],
    *,
    donor_id: str,
    recipient_id: str,
    swap_date: date,
    employees_by_id: Mapping[str, EmployeeProfile],
    shift_templates: Dict[str, ShiftTemplateInfo],
    rules: JurisdictionRules,
    period_start: date,
    period_end: date,
    availability_blocked: Optional[Mapping[str, Set[date]]],
    qual_codes: Mapping[str, str],
    expected_donor_band: str,
    allow_frozen_swaps: bool = False,
    payroll_targets: Optional[Mapping[str, float]] = None,
    catalog_targets: Optional[Mapping[str, float]] = None,
) -> bool:
    """Move one shift from donor to off-day recipient when bands match expected_donor_band."""

    donor_assignment = _employee_assignment_on_date(
        assignments,
        employee_id=donor_id,
        assignment_date=swap_date,
    )
    if donor_assignment is None:
        return False
    donor_frozen = getattr(donor_assignment, "master_template_frozen", False)
    if donor_frozen and not allow_frozen_swaps:
        return False
    if _employee_assignment_on_date(
        assignments,
        employee_id=recipient_id,
        assignment_date=swap_date,
    ) is not None:
        return False

    donor_band = _assignment_band(donor_assignment, shift_templates)
    if donor_band != expected_donor_band:
        return False
    cap = CLINICAL_FLOOR.get("EVENING" if expected_donor_band == "E" else "NIGHT", 2)
    if (
        _daily_pool_band_count(
            assignments,
            shift_templates,
            assignment_date=swap_date,
            band=expected_donor_band,
        )
        > cap
    ):
        return False

    donor = employees_by_id[donor_id]
    recipient = employees_by_id[recipient_id]
    donor_template = shift_templates[donor_assignment.shift_template_id]
    shift_hours = donor_template.duration_minutes / 60.0

    if validate_contract_line_eligibility(
        recipient.contract_line_type,
        donor_template.code,
        qual_code=infer_qual_code(recipient, qual_codes=qual_codes),
    ):
        return False

    recipient_state = states[recipient_id]
    if not _can_assign_with_weekend_pairing(
        recipient_state,
        recipient,
        swap_date,
        donor_template,
        shift_templates=shift_templates,
        rules=rules,
        period_start=period_start,
        period_end=period_end,
        availability_blocked=availability_blocked,
        qual_codes=qual_codes,
    ):
        return False

    violation = _would_violate_labor_rules(
        recipient_state,
        swap_date,
        donor_template,
        shift_templates,
        rules,
        period_start,
        period_end,
        availability_blocked,
        enforce_fte_target=False,
        relax_dn_contract_completion=True,
        peer_equity_swap=True,
        payroll_targets=payroll_targets,
        catalog_targets=catalog_targets,
    )
    if violation:
        return False

    donor_state = states[donor_id]
    _remove_assignment_from_state(
        donor_state,
        swap_date,
        donor_assignment.shift_template_id,
        shift_hours,
    )
    for index, assignment in enumerate(assignments):
        if (
            assignment.employee_id == donor_id
            and assignment.assignment_date == swap_date
        ):
            del assignments[index]
            break

    _apply_assignment_to_state(
        recipient_state,
        swap_date,
        donor_assignment.shift_template_id,
        shift_hours,
        rules=rules,
    )
    assignments.append(
        PlannedAssignment(
            employee_id=recipient_id,
            shift_template_id=donor_assignment.shift_template_id,
            assignment_date=swap_date,
            master_template_frozen=donor_frozen,
        )
    )
    return True


def _try_peer_give_shift_on_date(
    assignments: List[PlannedAssignment],
    states: Dict[str, _EmployeeState],
    *,
    donor_id: str,
    recipient_id: str,
    swap_date: date,
    employees_by_id: Mapping[str, EmployeeProfile],
    shift_templates: Dict[str, ShiftTemplateInfo],
    rules: JurisdictionRules,
    period_start: date,
    period_end: date,
    availability_blocked: Optional[Mapping[str, Set[date]]],
    qual_codes: Mapping[str, str],
    alternate_band: str,
    allow_frozen_swaps: bool = False,
    payroll_targets: Optional[Mapping[str, float]] = None,
    catalog_targets: Optional[Mapping[str, float]] = None,
) -> bool:
    """Move one alternate-band shift from donor to off-day recipient."""

    return _try_peer_transfer_shift_on_date(
        assignments,
        states,
        donor_id=donor_id,
        recipient_id=recipient_id,
        swap_date=swap_date,
        employees_by_id=employees_by_id,
        shift_templates=shift_templates,
        rules=rules,
        period_start=period_start,
        period_end=period_end,
        availability_blocked=availability_blocked,
        qual_codes=qual_codes,
        expected_donor_band=alternate_band,
        allow_frozen_swaps=allow_frozen_swaps,
        payroll_targets=payroll_targets,
        catalog_targets=catalog_targets,
    )


def _rebalance_vacant_peer_equity(
    assignments: List[PlannedAssignment],
    states: Dict[str, _EmployeeState],
    *,
    employees: Sequence[EmployeeProfile],
    shift_templates: Dict[str, ShiftTemplateInfo],
    rules: JurisdictionRules,
    period_start: date,
    period_end: date,
    availability_blocked: Optional[Mapping[str, Set[date]]],
    qual_codes: Mapping[str, str],
    employee_target_hours: Mapping[str, float],
    max_alt_spread: Optional[int] = None,
    max_weekend_spread: int = 0,
    max_rounds: int = 64,
) -> int:
    """
    Even out alternate-band counts and weekend shift days across identical vacant lines.

    Peer groups match CP-SAT equity pools: (role, contract, catalog target hours).
    Full-time pools (320h catalog) require ``max_alt_spread`` 0 — every line gets the
    same evening (D/E) or night (D/N) count. Part-time pools default to spread 1.
    D/N night blocks are master-stamp owned: alt rebalance skips D/N pools; weekend
    rebalance uses day-band swaps only so night rotations stay stable.
    """

    from lab_scheduler.solver.cpsat_fill import _vacant_line_type_groups

    employee_by_id = {employee.id: employee for employee in employees}
    line_groups = _vacant_line_type_groups(employees, employee_target_hours)
    period_dates = list(_daterange(period_start, period_end))
    weekend_dates = [day for day in period_dates if day.weekday() >= 5]
    weekday_dates = [day for day in period_dates if day.weekday() < 5]
    changed = 0

    for member_ids in line_groups.values():
        if len(member_ids) < 2:
            continue
        contract_line = employee_by_id[member_ids[0]].contract_line_type
        alternate_band = _alternate_band_for_contract_line(contract_line)
        nights_locked = _master_rotation_owns_alternate_band(contract_line)
        group_alt_limit = (
            max_alt_spread
            if max_alt_spread is not None
            else _peer_equity_alt_spread_limit(member_ids, employee_target_hours)
        )
        allow_frozen_swaps = (
            group_alt_limit == 0
            and _equity_allow_frozen_alternate_swap(contract_line, alternate_band)
        )

        for _ in range(max_rounds):
            metrics = {
                employee_id: _peer_shift_metrics(
                    employee_id,
                    assignments,
                    shift_templates,
                    contract_line,
                    period_start,
                    period_end,
                )
                for employee_id in member_ids
            }
            alt_counts = [metrics[employee_id][0] for employee_id in member_ids]
            weekend_counts = [metrics[employee_id][2] for employee_id in member_ids]
            alt_spread = max(alt_counts) - min(alt_counts)
            weekend_spread = max(weekend_counts) - min(weekend_counts)
            if alt_spread <= group_alt_limit and weekend_spread <= max_weekend_spread:
                break

            progress = False
            alt_spread_before = alt_spread

            if alt_spread > group_alt_limit and not nights_locked:
                donor_recipient_pairs = sorted(
                    [
                        (donor, recipient)
                        for donor in member_ids
                        for recipient in member_ids
                        if donor != recipient
                        and metrics[donor][0] > metrics[recipient][0]
                    ],
                    key=lambda pair: metrics[pair[0]][0] - metrics[pair[1]][0],
                    reverse=True,
                )
                if not donor_recipient_pairs:
                    donor_id = max(member_ids, key=lambda eid: metrics[eid][0])
                    recipient_id = min(member_ids, key=lambda eid: metrics[eid][0])
                    donor_recipient_pairs = [(donor_id, recipient_id)]

                for pair_donor, pair_recipient in donor_recipient_pairs:
                    if metrics[pair_donor][0] <= metrics[pair_recipient][0]:
                        continue
                    if (
                        metrics[pair_donor][0] - metrics[pair_recipient][0]
                        <= group_alt_limit
                    ):
                        continue
                    for swap_date in weekday_dates + weekend_dates:
                        if _try_peer_same_day_band_swap(
                            assignments,
                            states,
                            donor_id=pair_donor,
                            recipient_id=pair_recipient,
                            swap_date=swap_date,
                            employees_by_id=employee_by_id,
                            shift_templates=shift_templates,
                            rules=rules,
                            period_start=period_start,
                            period_end=period_end,
                            availability_blocked=availability_blocked,
                            qual_codes=qual_codes,
                            alternate_band=alternate_band,
                            allow_frozen_swaps=allow_frozen_swaps,
                        ):
                            progress = True
                            changed += 1
                            break
                        if _try_peer_give_shift_on_date(
                            assignments,
                            states,
                            donor_id=pair_donor,
                            recipient_id=pair_recipient,
                            swap_date=swap_date,
                            employees_by_id=employee_by_id,
                            shift_templates=shift_templates,
                            rules=rules,
                            period_start=period_start,
                            period_end=period_end,
                            availability_blocked=availability_blocked,
                            qual_codes=qual_codes,
                            alternate_band=alternate_band,
                            allow_frozen_swaps=allow_frozen_swaps,
                        ):
                            progress = True
                            changed += 1
                            break
                    if progress:
                        break
                if progress:
                    continue

                if (
                    not progress
                    and group_alt_limit == 0
                    and sum(alt_counts) % len(member_ids) != 0
                ):
                    target_alt = min(alt_counts)
                    for employee_id in sorted(
                        member_ids,
                        key=lambda eid: metrics[eid][0],
                        reverse=True,
                    ):
                        if metrics[employee_id][0] <= target_alt:
                            continue
                        for swap_date in weekday_dates + weekend_dates:
                            if _try_convert_alternate_to_day_on_date(
                                assignments,
                                states,
                                employee_id=employee_id,
                                swap_date=swap_date,
                                employees_by_id=employee_by_id,
                                shift_templates=shift_templates,
                                rules=rules,
                                period_start=period_start,
                                period_end=period_end,
                                availability_blocked=availability_blocked,
                                qual_codes=qual_codes,
                                alternate_band=alternate_band,
                                allow_frozen_swaps=allow_frozen_swaps,
                            ):
                                progress = True
                                changed += 1
                                break
                        if progress:
                            break
                    if progress:
                        continue

            donor_id = max(member_ids, key=lambda eid: metrics[eid][0])
            recipient_id = min(member_ids, key=lambda eid: metrics[eid][0])

            if weekend_spread > max_weekend_spread:
                wknd_donor = max(member_ids, key=lambda eid: metrics[eid][2])
                wknd_recipient = min(member_ids, key=lambda eid: metrics[eid][2])
                wknd_allow_frozen = _equity_allow_frozen_alternate_swap(
                    contract_line,
                    alternate_band,
                )
                for swap_date in weekend_dates:
                    if not nights_locked and _try_peer_same_day_band_swap(
                        assignments,
                        states,
                        donor_id=wknd_donor,
                        recipient_id=wknd_recipient,
                        swap_date=swap_date,
                        employees_by_id=employee_by_id,
                        shift_templates=shift_templates,
                        rules=rules,
                        period_start=period_start,
                        period_end=period_end,
                        availability_blocked=availability_blocked,
                        qual_codes=qual_codes,
                        alternate_band=alternate_band,
                        allow_frozen_swaps=wknd_allow_frozen,
                    ):
                        progress = True
                        changed += 1
                        break
                    if (not nights_locked and _try_peer_transfer_shift_on_date(
                        assignments,
                        states,
                        donor_id=wknd_donor,
                        recipient_id=wknd_recipient,
                        swap_date=swap_date,
                        employees_by_id=employee_by_id,
                        shift_templates=shift_templates,
                        rules=rules,
                        period_start=period_start,
                        period_end=period_end,
                        availability_blocked=availability_blocked,
                        qual_codes=qual_codes,
                        expected_donor_band=alternate_band,
                        allow_frozen_swaps=wknd_allow_frozen,
                    )) or _try_peer_transfer_shift_on_date(
                        assignments,
                        states,
                        donor_id=wknd_donor,
                        recipient_id=wknd_recipient,
                        swap_date=swap_date,
                        employees_by_id=employee_by_id,
                        shift_templates=shift_templates,
                        rules=rules,
                        period_start=period_start,
                        period_end=period_end,
                        availability_blocked=availability_blocked,
                        qual_codes=qual_codes,
                        expected_donor_band="D",
                        allow_frozen_swaps=True,
                    ):
                        progress = True
                        changed += 1
                        break

            if not progress:
                break

        if group_alt_limit != 0:
            continue

        for _ in range(max_rounds):
            metrics = {
                employee_id: _peer_shift_metrics(
                    employee_id,
                    assignments,
                    shift_templates,
                    contract_line,
                    period_start,
                    period_end,
                )
                for employee_id in member_ids
            }
            alt_counts = [metrics[employee_id][0] for employee_id in member_ids]
            alt_spread = max(alt_counts) - min(alt_counts)
            if alt_spread <= 0:
                break
            # Redistribution-only skew (e.g. 17 vs 15 nights) must stay swap-only.
            if max(alt_counts) > min(alt_counts) + 1:
                break
            target_alt = min(alt_counts)
            converted = False
            for employee_id in sorted(
                member_ids,
                key=lambda eid: metrics[eid][0],
                reverse=True,
            ):
                if metrics[employee_id][0] <= target_alt:
                    continue
                for swap_date in weekday_dates + weekend_dates:
                    if _try_convert_alternate_to_day_on_date(
                        assignments,
                        states,
                        employee_id=employee_id,
                        swap_date=swap_date,
                        employees_by_id=employee_by_id,
                        shift_templates=shift_templates,
                        rules=rules,
                        period_start=period_start,
                        period_end=period_end,
                        availability_blocked=availability_blocked,
                        qual_codes=qual_codes,
                        alternate_band=alternate_band,
                        allow_frozen_swaps=allow_frozen_swaps,
                    ):
                        changed += 1
                        converted = True
                        break
                if converted:
                    break
            if not converted:
                break

    return changed


def _apply_portage_weekend_pairing_policy(
    assignments: List[PlannedAssignment],
    states: Dict[str, _EmployeeState],
    *,
    employees: Sequence[EmployeeProfile],
    shift_templates: Dict[str, ShiftTemplateInfo],
    rules: JurisdictionRules,
    period_start: date,
    period_end: date,
    availability_blocked: Optional[Mapping[str, Set[date]]],
    qual_codes: Mapping[str, str],
    catalog_targets: Optional[Mapping[str, float]] = None,
    max_passes: int = 8,
) -> int:
    """Repair split weekends: same person, same shift on Sat+Sun unless blocked."""

    total = 0
    for _ in range(max_passes):
        progress = _enforce_weekend_shift_mirror(
            assignments,
            states,
            employees=employees,
            shift_templates=shift_templates,
            rules=rules,
            period_start=period_start,
            period_end=period_end,
            availability_blocked=availability_blocked,
            qual_codes=qual_codes,
        )
        progress += _consolidate_weekend_sat_sun_pairings(
            assignments,
            states,
            employees=employees,
            shift_templates=shift_templates,
            rules=rules,
            period_start=period_start,
            period_end=period_end,
            availability_blocked=availability_blocked,
            qual_codes=qual_codes,
        )
        progress += _enforce_weekend_shift_mirror(
            assignments,
            states,
            employees=employees,
            shift_templates=shift_templates,
            rules=rules,
            period_start=period_start,
            period_end=period_end,
            availability_blocked=availability_blocked,
            qual_codes=qual_codes,
        )
        progress += _trim_dn_off_catalog_weekend_shifts(
            assignments,
            states,
            employees=employees,
            shift_templates=shift_templates,
            period_start=period_start,
            period_end=period_end,
        )
        if catalog_targets is not None:
            progress += _trim_portage_vacant_weekend_to_target(
                assignments,
                states,
                employees=employees,
                shift_templates=shift_templates,
                catalog_targets=catalog_targets,
                qual_codes=qual_codes,
                period_start=period_start,
                period_end=period_end,
            )
        total += progress
        if progress == 0:
            break
    return total


def _fill_weekend_morning_clinical_gaps(
    assignments: List[PlannedAssignment],
    states: Dict[str, _EmployeeState],
    *,
    employees: Sequence[EmployeeProfile],
    expanded_slots: Sequence[ExpandedScheduleSlot],
    shift_templates: Dict[str, ShiftTemplateInfo],
    shift_required_qualifications: Dict[str, Set[str]],
    rules: JurisdictionRules,
    period_start: date,
    period_end: date,
    availability_blocked: Optional[Mapping[str, Set[date]]],
    qual_codes: Mapping[str, str],
    fill_counts: Dict[Tuple[date, str, Optional[str]], int],
    filled_smooth_seats: Set[Tuple[date, str, Optional[str], int]],
) -> int:
    """Assign missing weekend morning MLT/MLA clinical seats (1 each)."""

    added = 0
    weekend_slots = sorted(
        (
            slot
            for slot in expanded_slots
            if slot.assignment_date.weekday() >= 5
            and shift_templates[slot.shift_id].code == "MORNING"
            and is_clinical_floor_pool(slot.role_pool_id)
        ),
        key=lambda slot: (slot.assignment_date, slot.required_qual_code or "", slot.seat_index),
    )

    for _round in range(8):
        progress = False
        for slot in weekend_slots:
            if _slot_already_filled(slot, fill_counts):
                continue
            required_qual = slot.required_qual_code
            if not required_qual:
                continue
            counts = _weekend_qual_assignment_counts(
                assignments,
                employees=employees,
                qual_codes=qual_codes,
                assignment_date=slot.assignment_date,
                shift_templates=shift_templates,
                morning_only=True,
            )
            if weekend_morning_clinical_floor_satisfied(counts):
                continue
            if counts.get(required_qual, 0) >= WEEKEND_CLINICAL_MIN_PER_QUAL.get(required_qual, 1):
                continue
            if _slot_blocked_by_weekend_cap(
                slot,
                states=states,
                employees=employees,
                qual_codes=qual_codes,
                shift_templates=shift_templates,
            ):
                continue

            shift_id = slot.shift_id
            template = shift_templates[shift_id]
            required = shift_required_qualifications.get(shift_id, set())
            ranked = _mandatory_clinical_candidates(
                employees=employees,
                required=required,
                states=states,
                assignment_date=slot.assignment_date,
                template=template,
                qual_codes=qual_codes,
                required_qual_code=required_qual,
                availability_blocked=availability_blocked,
                role_pool_id=slot.role_pool_id,
            )
            if not ranked:
                continue

            chosen = None
            for candidate in ranked:
                violation = _would_violate_labor_rules(
                    states[candidate.id],
                    slot.assignment_date,
                    template,
                    shift_templates,
                    rules,
                    period_start,
                    period_end,
                    availability_blocked,
                    forced_clinical_ot=True,
                    mandatory_assignment=True,
                )
                if violation is None:
                    chosen = candidate
                    break
            if chosen is None:
                continue

            shift_hours = template.duration_minutes / 60.0
            _apply_assignment_to_state(
                states[chosen.id],
                slot.assignment_date,
                shift_id,
                shift_hours,
            )
            assignments.append(
                PlannedAssignment(
                    employee_id=chosen.id,
                    shift_template_id=shift_id,
                    assignment_date=slot.assignment_date,
                    forced_clinical_ot=True,
                )
            )
            seat_key = (
                slot.assignment_date,
                shift_id,
                slot.required_qual_code,
                slot.seat_index,
            )
            if is_smooth_day_balance_pool(slot.role_pool_id):
                filled_smooth_seats.add(seat_key)
            seat_key_count = (slot.assignment_date, shift_id, slot.required_qual_code)
            fill_counts[seat_key_count] = fill_counts.get(seat_key_count, 0) + 1
            added += 1
            progress = True
        if not progress:
            break
    return added


def _catalog_contract_target(
    employee: EmployeeProfile,
    catalog_targets: Mapping[str, float],
) -> float:
    return float(catalog_targets.get(employee.id, 0.0))




def _payroll_contract_target(
    employee: EmployeeProfile,
    payroll_targets: Mapping[str, float],
) -> float:
    return float(payroll_targets.get(employee.id, 0.0))


def _is_fulltime_vacant_master_line(
    employee: EmployeeProfile,
    payroll_targets: Mapping[str, float],
) -> bool:
    from lab_scheduler.scheduling.portage_equity_targets import (
        portage_is_fulltime_catalog_hours,
    )

    if parse_vacant_portage_line(employee.full_name) is None:
        return False
    return portage_is_fulltime_catalog_hours(
        _payroll_contract_target(employee, payroll_targets)
    )


def _contract_finalize_target(
    employee: EmployeeProfile,
    *,
    payroll_targets: Mapping[str, float],
    catalog_targets: Mapping[str, float],
) -> float:
    """FT vacant lines use payroll FTE (320h); PT vacant keep catalog for rotation."""

    if _is_fulltime_vacant_master_line(employee, payroll_targets):
        return _payroll_contract_target(employee, payroll_targets)
    return _catalog_contract_target(employee, catalog_targets)


def _contract_finalize_tolerance(
    employee: EmployeeProfile,
    payroll_targets: Mapping[str, float],
) -> float:
    if _is_fulltime_vacant_master_line(employee, payroll_targets):
        return CATALOG_PERSIST_TRIM_TOLERANCE
    return 8.0


def _has_contract_finalize_deficit(
    employee: EmployeeProfile,
    total_hours: float,
    *,
    payroll_targets: Mapping[str, float],
    catalog_targets: Mapping[str, float],
) -> bool:
    target = _contract_finalize_target(
        employee,
        payroll_targets=payroll_targets,
        catalog_targets=catalog_targets,
    )
    if target <= 0.0 or not _employee_subject_to_catalog_contract(employee, catalog_targets):
        return False
    tolerance = _contract_finalize_tolerance(employee, payroll_targets)
    return total_hours < target - tolerance - 0.25


def _any_ft_vacant_payroll_deficit(
    employees: Sequence[EmployeeProfile],
    states: Mapping[str, _EmployeeState],
    *,
    payroll_targets: Mapping[str, float],
    catalog_targets: Mapping[str, float],
) -> bool:
    for employee in employees:
        if not _is_fulltime_vacant_master_line(employee, payroll_targets):
            continue
        if _has_contract_finalize_deficit(
            employee,
            states[employee.id].total_hours,
            payroll_targets=payroll_targets,
            catalog_targets=catalog_targets,
        ):
            return True
    return False


def _parttime_vacant_at_or_over_catalog(
    employee: EmployeeProfile,
    total_hours: float,
    catalog_targets: Mapping[str, float],
) -> bool:
    if parse_vacant_portage_line(employee.full_name) is None:
        return False
    target = _catalog_contract_target(employee, catalog_targets)
    if target <= 0.0:
        return False
    return total_hours >= target - 0.25


def _top_up_injection_blocked_for_employee(
    employee: EmployeeProfile,
    states: Mapping[str, _EmployeeState],
    *,
    employees: Sequence[EmployeeProfile],
    payroll_targets: Mapping[str, float] | None,
    catalog_targets: Mapping[str, float],
    persist_gate: bool = False,
) -> bool:
    """While any FT vacant line sits below payroll target, block PT shift injection."""

    if payroll_targets is None:
        return False
    if _is_fulltime_vacant_master_line(employee, payroll_targets):
        return False
    if parse_vacant_portage_line(employee.full_name) is None:
        return False
    if persist_gate:
        # Persist validates PT lines against catalog hours. FT payroll deficit alone
        # is display-only union risk and must not block PT catalog top-up at finalize.
        for ft_employee in employees:
            if not _is_fulltime_vacant_master_line(ft_employee, payroll_targets):
                continue
            if _has_contract_finalize_surplus(
                ft_employee,
                states[ft_employee.id].total_hours,
                payroll_targets=payroll_targets,
                catalog_targets=catalog_targets,
            ):
                return True
        return False
    return _any_ft_vacant_payroll_deficit(
        employees=employees,
        states=states,
        payroll_targets=payroll_targets,
        catalog_targets=catalog_targets,
    )


def _employee_subject_to_catalog_contract(
    employee: EmployeeProfile,
    catalog_targets: Mapping[str, float],
) -> bool:
    """Vacant master lines follow catalog payroll targets regardless of profile FTE."""

    if parse_vacant_portage_line(employee.full_name) is not None:
        return _catalog_contract_target(employee, catalog_targets) > 0.0
    return employee.fte >= FULLTIME_FTE_THRESHOLD


def _has_catalog_contract_deficit(
    employee: EmployeeProfile,
    total_hours: float,
    catalog_targets: Mapping[str, float],
    *,
    tolerance: float = 8.0,
) -> bool:
    target = _catalog_contract_target(employee, catalog_targets)
    if target <= 0.0 or not _employee_subject_to_catalog_contract(employee, catalog_targets):
        return False
    return total_hours < target - tolerance - 0.25


def _all_at_catalog_contract_targets(
    employees: Sequence[EmployeeProfile],
    states: Mapping[str, _EmployeeState],
    catalog_targets: Mapping[str, float],
    *,
    tolerance: float = 8.0,
) -> bool:
    for employee in employees:
        target = _catalog_contract_target(employee, catalog_targets)
        if target <= 0.0:
            continue
        delta = abs(states[employee.id].total_hours - target)
        if delta > tolerance + 0.25:
            return False
    return True


def _all_at_contract_finalize_targets(
    employees: Sequence[EmployeeProfile],
    states: Mapping[str, _EmployeeState],
    catalog_targets: Mapping[str, float],
    payroll_targets: Mapping[str, float],
) -> bool:
    """FT vacant vs payroll 320h ±0.25h; PT vacant vs catalog ±8h."""

    for employee in employees:
        target = _contract_finalize_target(
            employee,
            payroll_targets=payroll_targets,
            catalog_targets=catalog_targets,
        )
        if target <= 0.0:
            continue
        if not _employee_subject_to_catalog_contract(employee, catalog_targets):
            continue
        tolerance = _contract_finalize_tolerance(employee, payroll_targets)
        delta = abs(states[employee.id].total_hours - target)
        if delta > tolerance + 0.25:
            return False
    return True


def _contract_hours_add_ceiling(
    employee: EmployeeProfile,
    catalog_targets: Mapping[str, float],
    payroll_targets: Mapping[str, float] | None = None,
) -> float:
    if payroll_targets is not None:
        target = _contract_finalize_target(
            employee,
            payroll_targets=payroll_targets,
            catalog_targets=catalog_targets,
        )
        tolerance = _contract_finalize_tolerance(employee, payroll_targets)
        return target + tolerance
    target = _catalog_contract_target(employee, catalog_targets)
    return target + 8.0


def _has_contract_finalize_surplus(
    employee: EmployeeProfile,
    total_hours: float,
    *,
    payroll_targets: Mapping[str, float],
    catalog_targets: Mapping[str, float],
) -> bool:
    target = _contract_finalize_target(
        employee,
        payroll_targets=payroll_targets,
        catalog_targets=catalog_targets,
    )
    if target <= 0.0 or not _employee_subject_to_catalog_contract(employee, catalog_targets):
        return False
    tolerance = _contract_finalize_tolerance(employee, payroll_targets)
    return total_hours > target + tolerance + 0.25


def _has_catalog_contract_surplus(
    employee: EmployeeProfile,
    total_hours: float,
    catalog_targets: Mapping[str, float],
    *,
    tolerance: float = 8.0,
) -> bool:
    target = _catalog_contract_target(employee, catalog_targets)
    if target <= 0.0 or not _employee_subject_to_catalog_contract(employee, catalog_targets):
        return False
    return total_hours > target + tolerance + 0.25


def _catalog_surplus_removal_rank(
    assignment: PlannedAssignment,
    shift_templates: Mapping[str, ShiftTemplateInfo],
    *,
    allow_trim_frozen: bool,
    part_time_catalog: bool,
) -> tuple[int, ...]:
    template = shift_templates.get(assignment.shift_template_id)
    band = shift_band_from_template_code(template.code) if template is not None else "D"
    frozen = getattr(assignment, "master_template_frozen", False)
    forced = getattr(assignment, "forced_clinical_ot", False)
    trimmable_frozen = allow_trim_frozen or part_time_catalog
    if part_time_catalog:
        # Part-time gap-fill: shed unneeded day shifts before touching alternates.
        band_rank = 0 if band == "D" else 1
    else:
        band_rank = 0 if band in ("E", "N") else 1
    return (
        0 if forced else 1,
        band_rank,
        0 if assignment.assignment_date.weekday() >= 5 else 1,
        0 if (not frozen or trimmable_frozen) else 1,
    )


def _trim_parttime_contract_overrun(
    assignments: List[PlannedAssignment],
    states: Dict[str, _EmployeeState],
    *,
    employees: Sequence[EmployeeProfile],
    shift_templates: Dict[str, ShiftTemplateInfo],
    catalog_targets: Mapping[str, float],
    period_start: date,
    period_end: date,
    tolerance: float = 8.0,
    expanded_slots: Optional[Sequence[ExpandedScheduleSlot]] = None,
    qual_codes: Optional[Mapping[str, str]] = None,
    post_pass_guard: Optional[PostPassGuard] = None,
    anchor_violations: Optional[List[str]] = None,
) -> int:
    """Shed non-catalog part-time overflow before persist (clinical OT / gap fills)."""

    from lab_scheduler.scheduling.portage_equity_targets import (
        portage_is_fulltime_catalog_hours,
    )

    removed = 0
    for allow_frozen in (False, True):
        for employee in employees:
            if not _has_catalog_contract_surplus(
                employee,
                states[employee.id].total_hours,
                catalog_targets,
                tolerance=tolerance,
            ):
                continue
            if portage_is_fulltime_catalog_hours(
                _catalog_contract_target(employee, catalog_targets)
            ):
                continue
            employee_id = employee.id
            while _has_catalog_contract_surplus(
                employee,
                states[employee_id].total_hours,
                catalog_targets,
                tolerance=tolerance,
            ):
                candidate_indices = [
                    index
                    for index, assignment in enumerate(assignments)
                    if assignment.employee_id == employee_id
                    and period_start <= assignment.assignment_date <= period_end
                    and (
                        allow_frozen
                        or not getattr(assignment, "master_template_frozen", False)
                        or not _catalog_master_stamp_protected(
                            employee,
                            assignment,
                            period_start,
                            shift_templates,
                        )
                    )
                ]
                if not candidate_indices:
                    break
                candidate_indices.sort(
                    key=lambda index: (
                        0 if getattr(assignments[index], "forced_clinical_ot", False) else 1,
                        0 if not getattr(assignments[index], "master_template_frozen", False) else 1,
                        0 if assignments[index].assignment_date.weekday() >= 5 else 1,
                    )
                )
                removed_one = False
                for index in candidate_indices:
                    if index >= len(assignments):
                        continue
                    assignment = assignments[index]
                    if assignment.employee_id != employee_id:
                        continue
                    template = shift_templates.get(assignment.shift_template_id)
                    if template is not None and template.code in ("EVENING", "NIGHT"):
                        if (
                            _clinical_band_assignment_count(
                                assignments,
                                assignment_date=assignment.assignment_date,
                                shift_code=template.code,
                                shift_templates=shift_templates,
                            )
                            <= CLINICAL_FLOOR.get(template.code, 2)
                        ):
                            continue
                    if (
                        expanded_slots is not None
                        and qual_codes is not None
                        and _trim_assignment_would_unfill_required_slot(
                            assignments,
                            index,
                            employees=employees,
                            expanded_slots=expanded_slots,
                            shift_templates=shift_templates,
                            qual_codes=qual_codes,
                        )
                    ):
                        continue
                    if _remove_assignment_at_index_guarded(
                        assignments,
                        index,
                        states=states,
                        shift_templates=shift_templates,
                        post_pass_guard=post_pass_guard,
                        anchor_violations=anchor_violations,
                    ):
                        removed += 1
                        removed_one = True
                    break
                if not removed_one:
                    break
    return removed


def _trim_vacant_lines_over_catalog_band(
    assignments: List[PlannedAssignment],
    states: Dict[str, _EmployeeState],
    *,
    employees: Sequence[EmployeeProfile],
    shift_templates: Dict[str, ShiftTemplateInfo],
    catalog_targets: Mapping[str, float],
    period_start: date,
    period_end: date,
    tolerance: float = 8.0,
    expanded_slots: Optional[Sequence[ExpandedScheduleSlot]] = None,
    qual_codes: Optional[Mapping[str, str]] = None,
) -> int:
    """Drop non-catalog surplus shifts on vacant lines until within union-risk band."""

    from lab_scheduler.scheduling.portage_equity_targets import (
        portage_is_fulltime_catalog_hours,
    )

    removed = 0
    for employee in employees:
        if parse_vacant_portage_line(employee.full_name) is None:
            continue
        part_time_line = not portage_is_fulltime_catalog_hours(
            _catalog_contract_target(employee, catalog_targets)
        )
        while _has_catalog_contract_surplus(
            employee,
            states[employee.id].total_hours,
            catalog_targets,
            tolerance=tolerance,
        ):
            employee_id = employee.id
            trim_dates = sorted(
                {
                    assignment.assignment_date
                    for assignment in assignments
                    if assignment.employee_id == employee_id
                    and period_start <= assignment.assignment_date <= period_end
                    and not _catalog_master_stamp_protected(
                        employee,
                        assignment,
                        period_start,
                        shift_templates,
                    )
                },
                key=lambda day: (0 if day.weekday() >= 5 else 1, day),
            )
            if not trim_dates:
                break
            trimmed_one = False
            for assignment_date in trim_dates:
                if not _has_catalog_contract_surplus(
                    employee,
                    states[employee_id].total_hours,
                    catalog_targets,
                    tolerance=tolerance,
                ):
                    break
                if (
                    not part_time_line
                    and expanded_slots is not None
                    and qual_codes is not None
                ):
                    blocking = any(
                        _trim_assignment_would_unfill_required_slot(
                            assignments,
                            index,
                            employees=employees,
                            expanded_slots=expanded_slots,
                            shift_templates=shift_templates,
                            qual_codes=qual_codes,
                        )
                        for index, assignment in enumerate(assignments)
                        if assignment.employee_id == employee_id
                        and assignment.assignment_date == assignment_date
                    )
                    if blocking:
                        continue
                removed += _remove_employee_assignments_on_date(
                    assignments,
                    employee_id=employee_id,
                    assignment_date=assignment_date,
                    states=states,
                    shift_templates=shift_templates,
                )
                trimmed_one = True
                break
            if not trimmed_one:
                break
    return removed


def _trim_catalog_contract_surplus(
    assignments: List[PlannedAssignment],
    states: Dict[str, _EmployeeState],
    *,
    employees: Sequence[EmployeeProfile],
    shift_templates: Dict[str, ShiftTemplateInfo],
    catalog_targets: Mapping[str, float],
    period_start: date,
    period_end: date,
    allow_trim_frozen: bool = False,
    tolerance: float = 8.0,
    payroll_targets: Mapping[str, float] | None = None,
    expanded_slots: Optional[Sequence[ExpandedScheduleSlot]] = None,
    qual_codes: Optional[Mapping[str, str]] = None,
    post_pass_guard: Optional[PostPassGuard] = None,
    anchor_violations: Optional[List[str]] = None,
) -> int:
    """Drop shifts from vacant lines scheduled over contract target + persist band."""

    from lab_scheduler.scheduling.portage_equity_targets import (
        portage_is_fulltime_catalog_hours,
    )

    def _has_surplus(employee: EmployeeProfile, total_hours: float) -> bool:
        if payroll_targets is not None:
            return _has_contract_finalize_surplus(
                employee,
                total_hours,
                payroll_targets=payroll_targets,
                catalog_targets=catalog_targets,
            )
        return _has_catalog_contract_surplus(
            employee,
            total_hours,
            catalog_targets,
            tolerance=tolerance,
        )

    def _surplus_target(employee: EmployeeProfile) -> float:
        if payroll_targets is not None:
            return _contract_finalize_target(
                employee,
                payroll_targets=payroll_targets,
                catalog_targets=catalog_targets,
            )
        return _catalog_contract_target(employee, catalog_targets)

    removed = 0
    max_passes = max(len(assignments), 1) * 3

    for _ in range(max_passes):
        over_employees = sorted(
            (
                employee
                for employee in employees
                if _has_surplus(employee, states[employee.id].total_hours)
            ),
            key=lambda employee: (
                states[employee.id].total_hours - _surplus_target(employee)
            ),
            reverse=True,
        )
        if not over_employees:
            break

        progress = False
        for employee in over_employees:
            employee_id = employee.id
            if not _has_surplus(employee, states[employee_id].total_hours):
                continue
            part_time_catalog = not portage_is_fulltime_catalog_hours(
                _surplus_target(employee)
            )
            candidate_indices = [
                index
                for index, assignment in enumerate(assignments)
                if assignment.employee_id == employee_id
                and period_start <= assignment.assignment_date <= period_end
            ]
            candidate_indices.sort(
                key=lambda index: _catalog_surplus_removal_rank(
                    assignments[index],
                    shift_templates,
                    allow_trim_frozen=allow_trim_frozen,
                    part_time_catalog=part_time_catalog,
                )
            )
            for index in candidate_indices:
                assignment = assignments[index]
                template = shift_templates.get(assignment.shift_template_id)
                if template is None:
                    if _remove_assignment_at_index_guarded(
                        assignments,
                        index,
                        states=states,
                        shift_templates=shift_templates,
                        post_pass_guard=post_pass_guard,
                        anchor_violations=anchor_violations,
                    ):
                        removed += 1
                        progress = True
                    break
                frozen = getattr(assignment, "master_template_frozen", False)
                trimmable_frozen = allow_trim_frozen or part_time_catalog
                if frozen and not trimmable_frozen:
                    continue
                if _catalog_master_stamp_protected(
                    employee,
                    assignment,
                    period_start,
                    shift_templates,
                ):
                    continue
                if template.code in ("EVENING", "NIGHT"):
                    if (
                        _clinical_band_assignment_count(
                            assignments,
                            assignment_date=assignment.assignment_date,
                            shift_code=template.code,
                            shift_templates=shift_templates,
                        )
                        <= CLINICAL_FLOOR.get(template.code, 2)
                    ):
                        continue
                if (
                    expanded_slots is not None
                    and qual_codes is not None
                    and _trim_assignment_would_unfill_required_slot(
                        assignments,
                        index,
                        employees=employees,
                        expanded_slots=expanded_slots,
                        shift_templates=shift_templates,
                        qual_codes=qual_codes,
                    )
                ):
                    continue
                if _remove_assignment_at_index_guarded(
                    assignments,
                    index,
                    states=states,
                    shift_templates=shift_templates,
                    post_pass_guard=post_pass_guard,
                    anchor_violations=anchor_violations,
                ):
                    removed += 1
                    progress = True
                break
            if progress:
                break
        if not progress:
            break

    return removed


def _trim_parttime_vacant_day_surplus(
    assignments: List[PlannedAssignment],
    states: Dict[str, _EmployeeState],
    *,
    employees: Sequence[EmployeeProfile],
    shift_templates: Dict[str, ShiftTemplateInfo],
    catalog_targets: Mapping[str, float],
    period_start: date,
    period_end: date,
    allow_trim_frozen: bool = True,
    tolerance: float = 8.0,
    expanded_slots: Optional[Sequence[ExpandedScheduleSlot]] = None,
    qual_codes: Optional[Mapping[str, str]] = None,
) -> int:
    """
    Strip weekday day shifts from part-time vacant master lines that exceed catalog
    hours. Gap-fill PT lines should not carry full-rotation day stacks.
    """

    from lab_scheduler.scheduling.portage_equity_targets import (
        portage_is_fulltime_catalog_hours,
    )

    removed = 0
    morning_ids = {
        shift_id
        for shift_id, template in shift_templates.items()
        if template.code == "MORNING"
    }

    for employee in employees:
        if parse_vacant_portage_line(employee.full_name) is None:
            continue
        target = _catalog_contract_target(employee, catalog_targets)
        if target <= 0.0 or portage_is_fulltime_catalog_hours(target):
            continue
        state = states[employee.id]
        max_passes = max(len(state.assignment_records), 1) * 2
        for _ in range(max_passes):
            if state.total_hours <= target + tolerance + 0.25:
                break
            candidate_indices = [
                index
                for index, assignment in enumerate(assignments)
                if assignment.employee_id == employee.id
                and period_start <= assignment.assignment_date <= period_end
                and assignment.shift_template_id in morning_ids
                and assignment.assignment_date.weekday() < 5
            ]
            if not candidate_indices:
                break
            candidate_indices.sort(
                key=lambda index: _catalog_surplus_removal_rank(
                    assignments[index],
                    shift_templates,
                    allow_trim_frozen=allow_trim_frozen,
                    part_time_catalog=True,
                )
            )
            index = candidate_indices[0]
            assignment = assignments[index]
            frozen = getattr(assignment, "master_template_frozen", False)
            if frozen and not allow_trim_frozen:
                break
            if (
                expanded_slots is not None
                and qual_codes is not None
                and _trim_assignment_would_unfill_required_slot(
                    assignments,
                    index,
                    employees=employees,
                    expanded_slots=expanded_slots,
                    shift_templates=shift_templates,
                    qual_codes=qual_codes,
                )
            ):
                break
            _remove_assignment_at_index(
                assignments,
                index,
                states=states,
                shift_templates=shift_templates,
            )
            removed += 1

    return removed


def _close_portage_operational_tally_gaps(
    assignments: List[PlannedAssignment],
    states: Dict[str, _EmployeeState],
    *,
    employees: Sequence[EmployeeProfile],
    shift_templates: Dict[str, ShiftTemplateInfo],
    rules: JurisdictionRules,
    period_start: date,
    period_end: date,
    availability_blocked: Optional[Mapping[str, Set[date]]],
    qual_codes: Mapping[str, str],
    catalog_targets: Mapping[str, float],
    payroll_targets: Mapping[str, float] | None = None,
    persist_gate: bool = False,
) -> int:
    """
    Raise daily Evening/Night operational tallies (need 2 each) by converting frozen
    template day shifts or adding union-safe alternate-band assignments.
    """

    from lab_scheduler.scheduling.schedule_tallies import (
        find_portage_operational_tally_violations,
    )

    template_id_to_band = {
        template_id: shift_band_from_template_code(info.code)
        for template_id, info in shift_templates.items()
    }
    changed = 0

    for _round in range(max((period_end - period_start).days + 1, 1) * 8):
        violations = find_portage_operational_tally_violations(
            assignments,
            period_start=period_start,
            period_end=period_end,
            template_id_to_band=template_id_to_band,
        )
        under_target = [item for item in violations if item.actual < item.target]
        if not under_target:
            break

        under_target.sort(key=lambda item: (item.actual, item.assignment_date))
        gap = under_target[0]
        gap_date = gap.assignment_date
        alternate_band = gap.band
        shift_code = "EVENING" if alternate_band == "E" else "NIGHT"
        contract_lines = ("D/E",) if alternate_band == "E" else ("D/N",)
        candidates = sorted(
            (
                employee
                for employee in employees
                if parse_vacant_portage_line(employee.full_name) is not None
                and (employee.contract_line_type or "").upper() in contract_lines
                and not _top_up_injection_blocked_for_employee(
                    employee,
                    states,
                    employees=employees,
                    payroll_targets=payroll_targets,
                    catalog_targets=catalog_targets,
                    persist_gate=persist_gate,
                )
            ),
            key=lambda employee: (
                1
                if payroll_targets is not None
                and not _is_fulltime_vacant_master_line(employee, payroll_targets)
                else 0,
                states[employee.id].total_hours,
            ),
        )
        progress = False

        for employee in candidates:
            if _try_convert_day_to_alternate_on_date(
                assignments,
                states,
                employee_id=employee.id,
                swap_date=gap_date,
                employees_by_id={employee.id: employee for employee in employees},
                shift_templates=shift_templates,
                rules=rules,
                period_start=period_start,
                period_end=period_end,
                availability_blocked=availability_blocked,
                qual_codes=qual_codes,
                alternate_band=alternate_band,
                allow_frozen_swaps=True,
            ):
                changed += 1
                progress = True
                break

        if progress:
            continue

        shift_id = _shift_id_for_code(shift_code, shift_templates)
        if shift_id is None:
            break
        template = shift_templates[shift_id]
        shift_hours = template.duration_minutes / 60.0
        for employee in candidates:
            state = states[employee.id]
            if gap_date in state.work_dates:
                continue
            hours_ceiling = _contract_hours_add_ceiling(
                employee,
                catalog_targets,
                payroll_targets=payroll_targets,
            )
            if (
                hours_ceiling > 0.0
                and state.total_hours + shift_hours > hours_ceiling + 0.25
            ):
                continue
            emp_qual = infer_qual_code(employee, qual_codes=qual_codes)
            if validate_contract_line_eligibility(
                employee.contract_line_type,
                template.code,
                qual_code=emp_qual,
            ):
                continue
            violation = _would_violate_labor_rules(
                state,
                gap_date,
                template,
                shift_templates,
                rules,
                period_start,
                period_end,
                availability_blocked,
                enforce_fte_target=False,
                relax_dn_contract_completion=True,
                forced_clinical_ot=True,
            )
            if violation:
                continue
            _apply_assignment_to_state(
                state,
                gap_date,
                shift_id,
                shift_hours,
                rules=rules,
            )
            assignments.append(
                PlannedAssignment(
                    employee_id=employee.id,
                    shift_template_id=shift_id,
                    assignment_date=gap_date,
                    forced_clinical_ot=True,
                )
            )
            changed += 1
            progress = True
            break

        if not progress:
            break

    return changed


def _rebalance_catalog_contract_hours(
    *,
    employees: Sequence[EmployeeProfile],
    states: Dict[str, _EmployeeState],
    assignments: List[PlannedAssignment],
    shift_templates: Dict[str, ShiftTemplateInfo],
    shift_required_qualifications: Dict[str, Set[str]],
    rules: JurisdictionRules,
    period_start: date,
    period_end: date,
    availability_blocked: Optional[Mapping[str, Set[date]]],
    qual_codes: Mapping[str, str],
    catalog_targets: Mapping[str, float],
) -> int:
    """Swap shifts from higher-hour full-time lines onto catalog-deficit peers (zero-sum)."""

    employee_by_id = {employee.id: employee for employee in employees}
    moved = 0
    recipients = sorted(
        (
            employee
            for employee in employees
            if _has_catalog_contract_deficit(
                employee,
                states[employee.id].total_hours,
                catalog_targets,
            )
        ),
        key=lambda employee: states[employee.id].total_hours,
    )

    for recipient in recipients:
        recipient_state = states[recipient.id]
        while _has_catalog_contract_deficit(
            recipient,
            recipient_state.total_hours,
            catalog_targets,
        ):
            donor_candidates: List[Tuple[int, PlannedAssignment]] = []
            for index, planned in enumerate(assignments):
                if planned.employee_id == recipient.id:
                    continue
                donor = employee_by_id.get(planned.employee_id)
                if donor is None:
                    continue
                if not _employee_subject_to_catalog_contract(donor, catalog_targets):
                    continue
                donor_state = states[donor.id]
                template = shift_templates[planned.shift_template_id]
                shift_hours = template.duration_minutes / 60.0
                donor_target = _catalog_contract_target(donor, catalog_targets)
                if getattr(planned, "master_template_frozen", False):
                    if donor_state.total_hours <= donor_target + 8.0 + 0.25:
                        continue
                if donor_state.total_hours <= recipient_state.total_hours + 0.25:
                    continue
                if (
                    donor_state.total_hours - shift_hours
                    < donor_target - 8.0 - 0.25
                ):
                    continue
                if infer_qual_code(donor, qual_codes=qual_codes) != infer_qual_code(
                    recipient,
                    qual_codes=qual_codes,
                ):
                    continue

                required = shift_required_qualifications.get(planned.shift_template_id, set())
                if not _is_qualified(recipient, required):
                    continue
                recipient_qual = infer_qual_code(recipient, qual_codes=qual_codes)
                line_violation = validate_contract_line_eligibility(
                    recipient.contract_line_type,
                    template.code,
                    qual_code=recipient_qual,
                )
                if line_violation:
                    continue
                donor_line_violation = validate_contract_line_eligibility(
                    donor.contract_line_type,
                    template.code,
                    qual_code=infer_qual_code(donor, qual_codes=qual_codes),
                )
                if donor_line_violation:
                    continue
                violation = _would_violate_labor_rules(
                    recipient_state,
                    planned.assignment_date,
                    template,
                    shift_templates,
                    rules,
                    period_start,
                    period_end,
                    availability_blocked,
                    enforce_fte_target=False,
                    relax_dn_contract_completion=True,
                    mandatory_assignment=_is_isolated_work_day(
                        recipient_state,
                        planned.assignment_date,
                    ),
                )
                if violation:
                    continue
                donor_candidates.append((index, planned))

            if not donor_candidates:
                break

            donor_candidates.sort(
                key=lambda item: (
                    0
                    if shift_templates[item[1].shift_template_id].code == "MORNING"
                    else 1,
                    -states[item[1].employee_id].total_hours,
                ),
            )
            index, planned = donor_candidates[0]
            donor = employee_by_id[planned.employee_id]
            donor_state = states[donor.id]
            template = shift_templates[planned.shift_template_id]
            shift_hours = template.duration_minutes / 60.0
            _remove_assignment_from_state(
                donor_state,
                planned.assignment_date,
                planned.shift_template_id,
                shift_hours,
            )
            _apply_assignment_to_state(
                recipient_state,
                planned.assignment_date,
                planned.shift_template_id,
                shift_hours,
            )
            assignments[index] = PlannedAssignment(
                employee_id=recipient.id,
                shift_template_id=planned.shift_template_id,
                assignment_date=planned.assignment_date,
            )
            moved += 1

    moved += _rebalance_catalog_contract_hours_cross_date(
        employees=employees,
        states=states,
        assignments=assignments,
        shift_templates=shift_templates,
        shift_required_qualifications=shift_required_qualifications,
        rules=rules,
        period_start=period_start,
        period_end=period_end,
        availability_blocked=availability_blocked,
        qual_codes=qual_codes,
        catalog_targets=catalog_targets,
    )
    return moved


def _rebalance_catalog_contract_hours_cross_date(
    *,
    employees: Sequence[EmployeeProfile],
    states: Dict[str, _EmployeeState],
    assignments: List[PlannedAssignment],
    shift_templates: Dict[str, ShiftTemplateInfo],
    shift_required_qualifications: Dict[str, Set[str]],
    rules: JurisdictionRules,
    period_start: date,
    period_end: date,
    availability_blocked: Optional[Mapping[str, Set[date]]],
    qual_codes: Mapping[str, str],
    catalog_targets: Mapping[str, float],
) -> int:
    """Move a worked shift from an over-band donor onto a deficit peer's off day."""

    employee_by_id = {employee.id: employee for employee in employees}
    period_days = list(_daterange(period_start, period_end))
    moved = 0
    recipients = sorted(
        (
            employee
            for employee in employees
            if _has_catalog_contract_deficit(
                employee,
                states[employee.id].total_hours,
                catalog_targets,
            )
        ),
        key=lambda employee: states[employee.id].total_hours,
    )

    for recipient in recipients:
        recipient_state = states[recipient.id]
        recipient_qual = infer_qual_code(recipient, qual_codes=qual_codes)
        while _has_catalog_contract_deficit(
            recipient,
            recipient_state.total_hours,
            catalog_targets,
        ):
            off_days = sorted(
                (
                    day
                    for day in period_days
                    if day not in recipient_state.work_dates
                ),
                key=lambda day: (
                    day.weekday() >= 5,
                    weekday_morning_shift_count_from_states(
                        states,
                        day,
                        shift_templates=shift_templates,
                    ),
                ),
            )
            progress = False
            for target_date in off_days:
                donor_candidates: List[Tuple[int, PlannedAssignment]] = []
                for index, planned in enumerate(assignments):
                    if planned.employee_id == recipient.id:
                        continue
                    donor = employee_by_id.get(planned.employee_id)
                    if donor is None:
                        continue
                    if not _employee_subject_to_catalog_contract(donor, catalog_targets):
                        continue
                    if infer_qual_code(donor, qual_codes=qual_codes) != recipient_qual:
                        continue
                    donor_state = states[donor.id]
                    template = shift_templates[planned.shift_template_id]
                    shift_hours = template.duration_minutes / 60.0
                    donor_target = _catalog_contract_target(donor, catalog_targets)
                    if getattr(planned, "master_template_frozen", False):
                        if donor_state.total_hours <= donor_target + 8.0 + 0.25:
                            continue
                    if donor_state.total_hours - shift_hours < donor_target - 8.0 - 0.25:
                        continue
                    if target_date in donor_state.work_dates:
                        continue
                    required = shift_required_qualifications.get(planned.shift_template_id, set())
                    if not _is_qualified(recipient, required):
                        continue
                    if validate_contract_line_eligibility(
                        recipient.contract_line_type,
                        template.code,
                        qual_code=recipient_qual,
                    ):
                        continue
                    if validate_contract_line_eligibility(
                        donor.contract_line_type,
                        template.code,
                        qual_code=infer_qual_code(donor, qual_codes=qual_codes),
                    ):
                        continue
                    violation = _would_violate_labor_rules(
                        recipient_state,
                        target_date,
                        template,
                        shift_templates,
                        rules,
                        period_start,
                        period_end,
                        availability_blocked,
                        enforce_fte_target=False,
                        relax_dn_contract_completion=True,
                        mandatory_assignment=_is_isolated_work_day(
                            recipient_state,
                            target_date,
                        ),
                    )
                    if violation:
                        continue
                    donor_candidates.append((index, planned))

                if not donor_candidates:
                    continue

                donor_candidates.sort(
                    key=lambda item: (
                        0
                        if shift_templates[item[1].shift_template_id].code == "MORNING"
                        else 1,
                        -states[item[1].employee_id].total_hours,
                    )
                )
                index, planned = donor_candidates[0]
                donor = employee_by_id[planned.employee_id]
                donor_state = states[donor.id]
                template = shift_templates[planned.shift_template_id]
                shift_hours = template.duration_minutes / 60.0
                _remove_assignment_from_state(
                    donor_state,
                    planned.assignment_date,
                    planned.shift_template_id,
                    shift_hours,
                )
                _apply_assignment_to_state(
                    recipient_state,
                    target_date,
                    planned.shift_template_id,
                    shift_hours,
                )
                assignments[index] = PlannedAssignment(
                    employee_id=recipient.id,
                    shift_template_id=planned.shift_template_id,
                    assignment_date=target_date,
                )
                moved += 1
                progress = True
                break
            if not progress:
                break

    return moved


def _clinical_band_assignment_count(
    assignments: Sequence[PlannedAssignment],
    *,
    assignment_date: date,
    shift_code: str,
    shift_templates: Mapping[str, ShiftTemplateInfo],
) -> int:
    shift_id = _shift_id_for_code(shift_code, shift_templates)
    if shift_id is None:
        return 0
    return sum(
        1
        for assignment in assignments
        if assignment.assignment_date == assignment_date
        and assignment.shift_template_id == shift_id
    )


def _is_isolated_work_day(state: _EmployeeState, assignment_date: date) -> bool:
    day_before = assignment_date - timedelta(days=1)
    day_after = assignment_date + timedelta(days=1)
    return day_before not in state.work_dates or day_after not in state.work_dates


def _catalog_contract_top_up_pass(
    *,
    employees: Sequence[EmployeeProfile],
    states: Dict[str, _EmployeeState],
    assignments: List[PlannedAssignment],
    shift_templates: Dict[str, ShiftTemplateInfo],
    rules: JurisdictionRules,
    period_start: date,
    period_end: date,
    availability_blocked: Optional[Mapping[str, Set[date]]],
    qual_codes: Mapping[str, str],
    catalog_targets: Mapping[str, float],
    payroll_targets: Mapping[str, float] | None = None,
    persist_gate: bool = False,
) -> int:
    """Add weekday shifts for contract-deficit vacant lines (finalize-only)."""

    added = 0
    period_days = list(_daterange(period_start, period_end))

    def _top_up_target(employee: EmployeeProfile) -> float:
        if payroll_targets is not None:
            return _contract_finalize_target(
                employee,
                payroll_targets=payroll_targets,
                catalog_targets=catalog_targets,
            )
        return _catalog_contract_target(employee, catalog_targets)

    def _has_top_up_deficit(employee: EmployeeProfile, total_hours: float) -> bool:
        if payroll_targets is not None:
            return _has_contract_finalize_deficit(
                employee,
                total_hours,
                payroll_targets=payroll_targets,
                catalog_targets=catalog_targets,
            )
        return _has_catalog_contract_deficit(
            employee,
            total_hours,
            catalog_targets,
        )

    def _top_up_surplus_band(employee: EmployeeProfile) -> float:
        if payroll_targets is not None:
            return _contract_finalize_tolerance(employee, payroll_targets)
        return 8.0

    under_target = sorted(
        (
            employee
            for employee in employees
            if parse_vacant_portage_line(employee.full_name) is not None
            and _has_top_up_deficit(employee, states[employee.id].total_hours)
            and not _top_up_injection_blocked_for_employee(
                employee,
                states,
                employees=employees,
                payroll_targets=payroll_targets,
                catalog_targets=catalog_targets,
                persist_gate=persist_gate,
            )
        ),
        key=lambda employee: (
            1
            if payroll_targets is not None
            and not _is_fulltime_vacant_master_line(employee, payroll_targets)
            else 0,
            -(_top_up_target(employee) - states[employee.id].total_hours),
        ),
    )

    for employee in under_target:
        state = states[employee.id]
        contract_line = (employee.contract_line_type or "D/E").upper()
        emp_qual = infer_qual_code(employee, qual_codes=qual_codes)
        remaining_deficit = _top_up_target(employee) - state.total_hours
        shift_codes = ("MORNING",) if contract_line == "D/N" else (
            ("EVENING", "MORNING") if remaining_deficit <= 32.0 else ("MORNING", "EVENING")
        )

        for _round in range(len(period_days)):
            if not _has_top_up_deficit(employee, state.total_hours):
                break
            round_progress = False
            candidate_dates = sorted(
                period_days,
                key=lambda assignment_date: (
                    assignment_date.weekday() >= 5,
                    weekday_morning_shift_count_from_states(
                        states,
                        assignment_date,
                        shift_templates=shift_templates,
                    ),
                ),
            )
            for assignment_date in candidate_dates:
                if not _has_top_up_deficit(employee, state.total_hours):
                    break
                for shift_code in shift_codes:
                    if shift_code in {"EVENING", "NIGHT"}:
                        if _clinical_band_assignment_count(
                            assignments,
                            assignment_date=assignment_date,
                            shift_code=shift_code,
                            shift_templates=shift_templates,
                        ) >= CLINICAL_FLOOR.get(shift_code, 2):
                            continue
                    shift_id = _shift_id_for_code(shift_code, shift_templates)
                    if shift_id is None:
                        continue
                    template = shift_templates[shift_id]
                    if shift_code == "MORNING" and assignment_date.weekday() < 5:
                        morning_count = weekday_morning_shift_count_from_states(
                            states,
                            assignment_date,
                            shift_templates=shift_templates,
                        )
                        if weekday_day_shift_capacity_block(
                            assignment_date,
                            morning_count,
                            shift_code=template.code,
                        ):
                            continue
                        from lab_scheduler.scheduling.schedule_tallies import (
                            WEEKDAY_DAY_BALANCE_TOLERANCE,
                        )

                        weekday_counts = [
                            weekday_morning_shift_count_from_states(
                                states,
                                day,
                                shift_templates=shift_templates,
                            )
                            for day in period_days
                            if day.weekday() < 5
                        ]
                        if weekday_counts:
                            lo = min(weekday_counts)
                            hi = max(weekday_counts)
                            projected = morning_count + 1
                            if (
                                projected > lo + WEEKDAY_DAY_BALANCE_TOLERANCE
                                and hi - lo >= WEEKDAY_DAY_BALANCE_TOLERANCE
                                and remaining_deficit > 24.0 + 0.25
                            ):
                                continue
                    if shift_code == "MORNING" and assignment_date.weekday() >= 5:
                        from lab_scheduler.scheduling.load_balancing import (
                            weekend_qual_cap_reached,
                            weekend_qual_counts_from_states,
                        )

                        weekend_counts = weekend_qual_counts_from_states(
                            states,
                            employees=employees,
                            qual_codes=qual_codes,
                            assignment_date=assignment_date,
                            shift_templates=shift_templates,
                            morning_only=True,
                        )
                        if weekend_qual_cap_reached(weekend_counts, emp_qual):
                            continue
                    line_violation = validate_contract_line_eligibility(
                        contract_line,
                        template.code,
                        qual_code=emp_qual,
                    )
                    if line_violation:
                        continue
                    if not _can_assign_with_weekend_pairing(
                        state,
                        employee,
                        assignment_date,
                        template,
                        shift_templates=shift_templates,
                        rules=rules,
                        period_start=period_start,
                        period_end=period_end,
                        availability_blocked=availability_blocked,
                        qual_codes=qual_codes,
                    ):
                        continue
                    shift_hours = template.duration_minutes / 60.0
                    if payroll_targets is not None and would_exceed_vacant_assignment_ceiling(
                        state.total_hours,
                        shift_hours,
                        employee,
                        payroll_targets,
                        catalog_targets,
                    ):
                        continue
                    surplus_band = _top_up_surplus_band(employee)
                    if state.total_hours + shift_hours > _top_up_target(employee) + surplus_band + 0.25:
                        continue
                    remaining = _top_up_target(employee) - state.total_hours
                    isolated_day = _is_isolated_work_day(state, assignment_date)
                    violation = _would_violate_labor_rules(
                        state,
                        assignment_date,
                        template,
                        shift_templates,
                        rules,
                        period_start,
                        period_end,
                        availability_blocked,
                        enforce_fte_target=False,
                        relax_dn_contract_completion=True,
                        mandatory_assignment=isolated_day
                        and remaining >= 8.0 - 0.25,
                        payroll_targets=payroll_targets,
                        catalog_targets=catalog_targets,
                    )
                    if violation:
                        continue
                    _apply_assignment_to_state(
                        state,
                        assignment_date,
                        shift_id,
                        shift_hours,
                    )
                    assignments.append(
                        PlannedAssignment(
                            employee_id=employee.id,
                            shift_template_id=shift_id,
                            assignment_date=assignment_date,
                        )
                    )
                    added += 1
                    round_progress = True
                    break
                if round_progress:
                    break
            if not round_progress:
                break

    return added


def _break_portage_night_streaks_by_rest_day(
    assignments: List[PlannedAssignment],
    states: Dict[str, _EmployeeState],
    *,
    employees: Sequence[EmployeeProfile],
    shift_templates: Dict[str, ShiftTemplateInfo],
    rules: JurisdictionRules,
    period_start: date,
    period_end: date,
    availability_blocked: Optional[Mapping[str, Set[date]]],
    qual_codes: Mapping[str, str],
    max_passes: int = 24,
) -> int:
    """Break >4-night runs by converting one night shift to day on the same line/date."""

    from lab_scheduler.scheduling.night_streak_corrector import (
        _break_dates_for_streak,
        find_consecutive_night_streaks,
        validate_night_streak_sequences,
    )

    employees_by_id = {employee.id: employee for employee in employees}
    converted = 0

    for _ in range(max_passes):
        violations = validate_night_streak_sequences(
            assignments,
            employees=employees,
            shift_templates=shift_templates,
            period_start=period_start,
            period_end=period_end,
        )
        if not violations:
            break

        progress = False
        for violation in violations:
            streaks = find_consecutive_night_streaks(
                employee_id=violation.employee_id,
                period_start=period_start,
                period_end=period_end,
                assignments=assignments,
                shift_templates=shift_templates,
            )
            if not streaks:
                continue
            streak = max(streaks, key=lambda item: item.length)
            for break_date in _break_dates_for_streak(streak):
                if _try_convert_alternate_to_day_on_date(
                    assignments,
                    states,
                    employee_id=violation.employee_id,
                    swap_date=break_date,
                    employees_by_id=employees_by_id,
                    shift_templates=shift_templates,
                    rules=rules,
                    period_start=period_start,
                    period_end=period_end,
                    availability_blocked=availability_blocked,
                    qual_codes=qual_codes,
                    alternate_band="N",
                    allow_frozen_swaps=True,
                ):
                    converted += 1
                    progress = True
                    break
                if _try_convert_night_to_evening_on_date(
                    assignments,
                    states,
                    employee_id=violation.employee_id,
                    swap_date=break_date,
                    employees_by_id=employees_by_id,
                    shift_templates=shift_templates,
                    rules=rules,
                    period_start=period_start,
                    period_end=period_end,
                    availability_blocked=availability_blocked,
                    qual_codes=qual_codes,
                    allow_frozen_swaps=True,
                ):
                    converted += 1
                    progress = True
                    break
            if progress:
                break
        if not progress:
            break

    return converted


def _break_portage_work_streaks(
    assignments: List[PlannedAssignment],
    states: Dict[str, _EmployeeState],
    *,
    employees: Sequence[EmployeeProfile],
    shift_templates: Dict[str, ShiftTemplateInfo],
    shift_required_qualifications: Dict[str, Set[str]],
    rules: JurisdictionRules,
    period_start: date,
    period_end: date,
    availability_blocked: Optional[Mapping[str, Set[date]]],
    qual_codes: Mapping[str, str],
    max_passes: int = 24,
) -> int:
    """Break >6-day work runs via off-day swaps with same qual + contract-line peers."""

    from lab_scheduler.scheduling.streak_validator import validate_work_streaks_from_assignments

    employee_by_id = {employee.id: employee for employee in employees}
    swaps = 0

    for _ in range(max_passes):
        violations = validate_work_streaks_from_assignments(
            assignments,
            employees=employees,
            shift_templates=shift_templates,
            period_start=period_start,
            period_end=period_end,
        )
        if not violations:
            break

        progress = False
        violation = max(violations, key=lambda item: item.length)
        target = employee_by_id.get(violation.employee_id)
        if target is None:
            break
        target_state = states[target.id]
        streak_dates = list(_daterange(violation.start_date, violation.end_date))
        if len(streak_dates) < 2:
            break
        target_qual = infer_qual_code(target, qual_codes=qual_codes)

        peers = sorted(
            (
                employee
                for employee in employees
                if employee.id != target.id
                and infer_qual_code(employee, qual_codes=qual_codes) == target_qual
            ),
            key=lambda employee: states[employee.id].total_hours,
        )

        candidate_break_dates = [
            streak_dates[len(streak_dates) // 2],
            streak_dates[0],
            streak_dates[-1],
        ]

        for break_date in candidate_break_dates:
            target_index = next(
                (
                    index
                    for index, assignment in enumerate(assignments)
                    if assignment.employee_id == target.id
                    and assignment.assignment_date == break_date
                    and not getattr(assignment, "master_template_frozen", False)
                ),
                None,
            )
            if target_index is None:
                continue
            target_assignment = assignments[target_index]
            target_template = shift_templates[target_assignment.shift_template_id]
            target_hours = target_template.duration_minutes / 60.0

            for peer in peers:
                if break_date in states[peer.id].work_dates:
                    continue
                peer_index = next(
                    (
                        index
                        for index, assignment in enumerate(assignments)
                        if assignment.employee_id == peer.id
                        and assignment.assignment_date not in target_state.work_dates
                        and not getattr(assignment, "master_template_frozen", False)
                    ),
                    None,
                )
                if peer_index is None:
                    continue

                peer_assignment = assignments[peer_index]
                peer_template = shift_templates[peer_assignment.shift_template_id]
                peer_hours = peer_template.duration_minutes / 60.0

                trial = list(assignments)
                trial[target_index] = PlannedAssignment(
                    employee_id=target.id,
                    shift_template_id=peer_assignment.shift_template_id,
                    assignment_date=peer_assignment.assignment_date,
                )
                trial[peer_index] = PlannedAssignment(
                    employee_id=peer.id,
                    shift_template_id=target_assignment.shift_template_id,
                    assignment_date=break_date,
                )
                trial_violations = validate_work_streaks_from_assignments(
                    trial,
                    employees=[target, peer],
                    shift_templates=shift_templates,
                    period_start=period_start,
                    period_end=period_end,
                )
                if any(item.length > rules.max_consecutive_work_days for item in trial_violations):
                    continue
                peer_shift = shift_templates[peer_assignment.shift_template_id]
                target_shift = shift_templates[target_assignment.shift_template_id]
                if validate_contract_line_eligibility(
                    target.contract_line_type,
                    peer_shift.code,
                    qual_code=target_qual,
                ):
                    continue
                if validate_contract_line_eligibility(
                    peer.contract_line_type,
                    target_shift.code,
                    qual_code=infer_qual_code(peer, qual_codes=qual_codes),
                ):
                    continue

                assignments[target_index] = trial[target_index]
                assignments[peer_index] = trial[peer_index]
                _rebuild_states_from_assignments(states, assignments, shift_templates)
                swaps += 1
                progress = True
                break
            if progress:
                break

        if not progress:
            break

    return swaps


def _rebuild_states_from_assignments(
    states: Dict[str, _EmployeeState],
    assignments: Sequence[PlannedAssignment],
    shift_templates: Mapping[str, ShiftTemplateInfo],
) -> None:
    for state in states.values():
        state.total_hours = 0.0
        state.work_dates.clear()
        state.week_hours.clear()
        state.assignment_records.clear()
        state.contract_completion_ot_used = False
    for assignment in assignments:
        state = states.get(assignment.employee_id)
        template = shift_templates.get(assignment.shift_template_id)
        if state is None or template is None:
            continue
        _apply_assignment_to_state(
            state,
            assignment.assignment_date,
            assignment.shift_template_id,
            template.duration_minutes / 60.0,
        )


def _is_portage_topup_assignment(
    assignment: PlannedAssignment,
    shift_templates: Mapping[str, ShiftTemplateInfo],
) -> bool:
    template_id = str(assignment.shift_template_id).lower()
    if "topup" in template_id or "top-up" in template_id or "top_up" in template_id:
        return True
    template = shift_templates.get(assignment.shift_template_id)
    if template is None:
        return False
    return str(template.code or "").strip().upper().startswith("TOPUP")


def _assignment_contract_line_violation(
    employee: EmployeeProfile,
    shift_template: ShiftTemplateInfo,
    *,
    qual_codes: Mapping[str, str],
) -> Optional[str]:
    return validate_contract_line_eligibility(
        employee.contract_line_type,
        shift_template.code,
        qual_code=infer_qual_code(employee, qual_codes=qual_codes),
    )


def _strip_portage_topup_assignments(
    assignments: List[PlannedAssignment],
    states: Dict[str, _EmployeeState],
    *,
    shift_templates: Mapping[str, ShiftTemplateInfo],
) -> int:
    removed = 0
    for index in reversed(range(len(assignments))):
        assignment = assignments[index]
        if not _is_portage_topup_assignment(assignment, shift_templates):
            continue
        template = shift_templates.get(assignment.shift_template_id)
        if template is None:
            continue
        shift_hours = template.duration_minutes / 60.0
        _remove_assignment_from_state(
            states[assignment.employee_id],
            assignment.assignment_date,
            assignment.shift_template_id,
            shift_hours,
        )
        del assignments[index]
        removed += 1
    return removed


def _scrub_portage_contract_line_violations(
    assignments: List[PlannedAssignment],
    states: Dict[str, _EmployeeState],
    *,
    employees: Sequence[EmployeeProfile],
    shift_templates: Dict[str, ShiftTemplateInfo],
    shift_required_qualifications: Dict[str, Set[str]],
    rules: JurisdictionRules,
    period_start: date,
    period_end: date,
    availability_blocked: Optional[Mapping[str, Set[date]]],
    qual_codes: Mapping[str, str],
    catalog_targets: Optional[Mapping[str, float]] = None,
    max_passes: int = 64,
) -> int:
    """Rehome or drop D/E vs D/N band violations (MLT<->MLT, MLA<->MLA only)."""

    employee_by_id = {employee.id: employee for employee in employees}
    fixed = 0

    for _ in range(max_passes):
        progress = False
        for index, planned in enumerate(list(assignments)):
            employee = employee_by_id.get(planned.employee_id)
            template = shift_templates.get(planned.shift_template_id)
            if employee is None or template is None:
                continue
            if _is_portage_topup_assignment(planned, shift_templates):
                continue
            if _assignment_contract_line_violation(
                employee,
                template,
                qual_codes=qual_codes,
            ) is None:
                continue

            shift_hours = template.duration_minutes / 60.0
            emp_qual = infer_qual_code(employee, qual_codes=qual_codes)
            peer_pool = sorted(
                (
                    peer
                    for peer in employees
                    if peer.id != employee.id
                    and infer_qual_code(peer, qual_codes=qual_codes) == emp_qual
                ),
                key=lambda peer: (
                    _catalog_contract_target(peer, catalog_targets)
                    - states[peer.id].total_hours
                    if catalog_targets
                    else states[peer.id].total_hours
                ),
                reverse=bool(catalog_targets),
            )

            rehomed = False
            for peer in peer_pool:
                if planned.assignment_date in states[peer.id].work_dates:
                    continue
                if _assignment_contract_line_violation(
                    peer,
                    template,
                    qual_codes=qual_codes,
                ):
                    continue
                required = shift_required_qualifications.get(planned.shift_template_id, set())
                if not _is_qualified(peer, required):
                    continue
                peer_state = states[peer.id]
                violation = _would_violate_labor_rules(
                    peer_state,
                    planned.assignment_date,
                    template,
                    shift_templates,
                    rules,
                    period_start,
                    period_end,
                    availability_blocked,
                    enforce_fte_target=False,
                    relax_dn_contract_completion=True,
                )
                if violation:
                    continue

                _remove_assignment_from_state(
                    states[employee.id],
                    planned.assignment_date,
                    planned.shift_template_id,
                    shift_hours,
                )
                _apply_assignment_to_state(
                    peer_state,
                    planned.assignment_date,
                    planned.shift_template_id,
                    shift_hours,
                )
                assignments[index] = PlannedAssignment(
                    employee_id=peer.id,
                    shift_template_id=planned.shift_template_id,
                    assignment_date=planned.assignment_date,
                    forced_clinical_ot=planned.forced_clinical_ot,
                    master_template_frozen=getattr(planned, "master_template_frozen", False),
                )
                fixed += 1
                progress = True
                rehomed = True
                break

            if rehomed:
                continue

            if getattr(planned, "master_template_frozen", False):
                continue

            _remove_assignment_from_state(
                states[employee.id],
                planned.assignment_date,
                planned.shift_template_id,
                shift_hours,
            )
            del assignments[index]
            fixed += 1
            progress = True
            break

        if not progress:
            break

    return fixed


def _portage_scrub_topup_and_rebuild(
    result: AutoGenerateResult,
    states: Dict[str, _EmployeeState],
    *,
    employees: Sequence[EmployeeProfile],
    expanded_slots: Sequence[ExpandedScheduleSlot],
    shift_templates: Dict[str, ShiftTemplateInfo],
    shift_required_qualifications: Dict[str, Set[str]],
    rules: JurisdictionRules,
    period_start: date,
    period_end: date,
    availability_blocked: Optional[Mapping[str, Set[date]]],
    qual_codes: Mapping[str, str],
    catalog_targets: Optional[Mapping[str, float]] = None,
    payroll_targets: Mapping[str, float] | None = None,
    refill_clinical_en: bool = False,
) -> int:
    """Strip TOPUP tokens, scrub contract-line band violations, optionally refill E/N."""

    stripped = _strip_portage_topup_assignments(
        result.assignments,
        states,
        shift_templates=shift_templates,
    )
    scrubbed = _scrub_portage_contract_line_violations(
        result.assignments,
        states,
        employees=employees,
        shift_templates=shift_templates,
        shift_required_qualifications=shift_required_qualifications,
        rules=rules,
        period_start=period_start,
        period_end=period_end,
        availability_blocked=availability_blocked,
        qual_codes=qual_codes,
        catalog_targets=catalog_targets,
    )
    changed = stripped + scrubbed
    if changed:
        _rebuild_states_from_assignments(states, result.assignments, shift_templates)
    if scrubbed and catalog_targets:
        _catalog_contract_top_up_pass(
            employees=employees,
            states=states,
            assignments=result.assignments,
            shift_templates=shift_templates,
            rules=rules,
            period_start=period_start,
            period_end=period_end,
            availability_blocked=availability_blocked,
            qual_codes=qual_codes,
            catalog_targets=catalog_targets,
            payroll_targets=payroll_targets,
        )
        _rebuild_states_from_assignments(states, result.assignments, shift_templates)
    if changed and refill_clinical_en:
        fill_counts = _seat_fill_counts(result.assignments, employees, qual_codes)
        post_pass_guard = _post_pass_guard_for_result(result)
        _extend_evening_night_clinical_lockdown(
            result,
            employees=employees,
            states=states,
            expanded_slots=expanded_slots,
            shift_templates=shift_templates,
            shift_required_qualifications=shift_required_qualifications,
            rules=rules,
            fill_counts=fill_counts,
            availability_blocked=availability_blocked,
            qual_codes=qual_codes,
            period_start=period_start,
            period_end=period_end,
            post_pass_guard=post_pass_guard,
                payroll_targets=payroll_targets,
        catalog_targets=catalog_targets,
    )
        _rebuild_states_from_assignments(states, result.assignments, shift_templates)
    return changed


def _compliance_safe_finalize_has_union_blockers(
    assignments: Sequence[PlannedAssignment],
    *,
    employees: Sequence[EmployeeProfile],
    shift_templates: Mapping[str, ShiftTemplateInfo],
    rules: JurisdictionRules,
    period_start: date,
    period_end: date,
    weeks_in_period: int,
    qual_codes: Mapping[str, str],
) -> bool:
    """Return True when adding more shifts would breach union persist gates."""

    from lab_scheduler.scheduling.auto_pilot import dedupe_planned_assignments
    from lab_scheduler.scheduling.persist_validation import (
        UNION_PERSIST_CODES,
        find_core_persist_violations,
    )
    from lab_scheduler.scheduling.schedule_tallies import shift_band_from_template_code

    template_id_to_band = {
        template_id: shift_band_from_template_code(info.code)
        for template_id, info in shift_templates.items()
    }
    deduped = dedupe_planned_assignments(
        assignments,
        template_id_to_band=template_id_to_band,
    )
    union_hits = [
        violation
        for violation in find_core_persist_violations(
            assignments=deduped,
            employees=employees,
            shift_templates=shift_templates,
            period_start=period_start,
            period_end=period_end,
            weeks_in_period=weeks_in_period,
            rules=rules,
            qual_codes=qual_codes,
            template_id_to_band=template_id_to_band,
            compliance_first=True,
        )
        if violation.code in UNION_PERSIST_CODES
    ]
    return bool(union_hits)


def _compliance_safe_vacant_finalize(
    result: AutoGenerateResult,
    *,
    employees: Sequence[EmployeeProfile],
    states: Dict[str, _EmployeeState],
    expanded_slots: Sequence[ExpandedScheduleSlot],
    shift_templates: Dict[str, ShiftTemplateInfo],
    shift_required_qualifications: Dict[str, Set[str]],
    rules: JurisdictionRules,
    period_start: date,
    period_end: date,
    weeks_in_period: int,
    availability_blocked: Optional[Mapping[str, Set[date]]],
    qual_codes: Mapping[str, str],
    catalog_targets: Mapping[str, float],
    payroll_targets: Mapping[str, float],
    fulltime_target: float,
) -> None:
    """
    Fill alternate (E/N) clinical seats and close catalog contract deficits without
    running the aggressive post-CP-SAT healing that reintroduces union violations.

    Compliance-first CP-SAT uses exact E/N band caps (night coverage over PT hour limits);
    this pass adds union-safe evening/night clinical assignments, evens alternate-band equity
    across vacant pools, and tops up under-target lines when union gates stay clean.
    """

    _trim_catalog_contract_surplus(
        result.assignments,
        states,
        employees=employees,
        shift_templates=shift_templates,
        catalog_targets=catalog_targets,
        period_start=period_start,
        period_end=period_end,
        allow_trim_frozen=True,
        payroll_targets=payroll_targets,
    )
    _rebuild_states_from_assignments(states, result.assignments, shift_templates)
    _reassign_parttime_shifts_to_fulltime_contract(
        employees=employees,
        states=states,
        assignments=result.assignments,
        shift_templates=shift_templates,
        shift_required_qualifications=shift_required_qualifications,
        rules=rules,
        period_start=period_start,
        period_end=period_end,
        availability_blocked=availability_blocked,
        qual_codes=qual_codes,
        fulltime_target=fulltime_target,
        payroll_targets=payroll_targets,
        catalog_targets=catalog_targets,
    )
    _rebuild_states_from_assignments(states, result.assignments, shift_templates)

    post_pass_guard = _post_pass_guard_for_result(result)
    fill_counts = _seat_fill_counts(result.assignments, employees, qual_codes)

    _extend_evening_night_clinical_lockdown(
        result,
        employees=employees,
        states=states,
        expanded_slots=expanded_slots,
        shift_templates=shift_templates,
        shift_required_qualifications=shift_required_qualifications,
        rules=rules,
        fill_counts=fill_counts,
        availability_blocked=availability_blocked,
        qual_codes=qual_codes,
        period_start=period_start,
        period_end=period_end,
        log_critical_gaps=False,
        post_pass_guard=post_pass_guard,
        allow_frozen_clinical_supersede=True,
            payroll_targets=payroll_targets,
        catalog_targets=catalog_targets,
    )
    _close_portage_operational_tally_gaps(
        result.assignments,
        states,
        employees=employees,
        shift_templates=shift_templates,
        rules=rules,
        period_start=period_start,
        period_end=period_end,
        availability_blocked=availability_blocked,
        qual_codes=qual_codes,
        catalog_targets=catalog_targets,
        payroll_targets=payroll_targets,
    )
    fill_counts = _seat_fill_counts(result.assignments, employees, qual_codes)

    from lab_scheduler.scheduling.night_streak_corrector import (
        correct_portage_night_streaks,
        trim_consecutive_night_overruns,
    )

    correct_portage_night_streaks(
        result.assignments,
        employees=employees,
        shift_templates=shift_templates,
        shift_required_qualifications=shift_required_qualifications,
        rules=rules,
        period_start=period_start,
        period_end=period_end,
        weeks_in_period=weeks_in_period,
        employee_target_hours=catalog_targets,
        availability_blocked=availability_blocked,
    )
    trim_consecutive_night_overruns(
        result.assignments,
        employees=employees,
        shift_templates=shift_templates,
        period_start=period_start,
        period_end=period_end,
    )
    for _ in range(6):
        if not _trim_clinical_band_overfill(
            result.assignments,
            states,
            employees=employees,
            shift_templates=shift_templates,
            qual_codes=qual_codes,
            period_start=period_start,
            period_end=period_end,
            fulltime_target=fulltime_target,
            catalog_targets=catalog_targets,
            allow_trim_frozen=True,
        ):
            break

    _close_portage_operational_tally_gaps(
        result.assignments,
        states,
        employees=employees,
        shift_templates=shift_templates,
        rules=rules,
        period_start=period_start,
        period_end=period_end,
        availability_blocked=availability_blocked,
        qual_codes=qual_codes,
        catalog_targets=catalog_targets,
        payroll_targets=payroll_targets,
    )
    _rebuild_states_from_assignments(states, result.assignments, shift_templates)
    fill_counts = _seat_fill_counts(result.assignments, employees, qual_codes)

    for _cycle in range(6):
        if _compliance_safe_finalize_has_union_blockers(
            result.assignments,
            employees=employees,
            shift_templates=shift_templates,
            rules=rules,
            period_start=period_start,
            period_end=period_end,
            weeks_in_period=weeks_in_period,
            qual_codes=qual_codes,
        ):
            break
        _reassign_parttime_shifts_to_fulltime_contract(
            employees=employees,
            states=states,
            assignments=result.assignments,
            shift_templates=shift_templates,
            shift_required_qualifications=shift_required_qualifications,
            rules=rules,
            period_start=period_start,
            period_end=period_end,
            availability_blocked=availability_blocked,
            qual_codes=qual_codes,
            fulltime_target=fulltime_target,
            payroll_targets=payroll_targets,
            catalog_targets=catalog_targets,
        )
        topped_up = _catalog_contract_top_up_pass(
            employees=employees,
            states=states,
            assignments=result.assignments,
            shift_templates=shift_templates,
            rules=rules,
            period_start=period_start,
            period_end=period_end,
            availability_blocked=availability_blocked,
            qual_codes=qual_codes,
            catalog_targets=catalog_targets,
            payroll_targets=payroll_targets,
        )
        rebalanced = _rebalance_catalog_contract_hours(
            employees=employees,
            states=states,
            assignments=result.assignments,
            shift_templates=shift_templates,
            shift_required_qualifications=shift_required_qualifications,
            rules=rules,
            period_start=period_start,
            period_end=period_end,
            availability_blocked=availability_blocked,
            qual_codes=qual_codes,
            catalog_targets=catalog_targets,
        )
        if not topped_up and not rebalanced:
            break
        fill_counts = _seat_fill_counts(result.assignments, employees, qual_codes)
        _extend_evening_night_clinical_lockdown(
            result,
            employees=employees,
            states=states,
            expanded_slots=expanded_slots,
            shift_templates=shift_templates,
            shift_required_qualifications=shift_required_qualifications,
            rules=rules,
            fill_counts=fill_counts,
            availability_blocked=availability_blocked,
            qual_codes=qual_codes,
            period_start=period_start,
            period_end=period_end,
            log_critical_gaps=False,
            post_pass_guard=post_pass_guard,
            allow_frozen_clinical_supersede=True,
                payroll_targets=payroll_targets,
        catalog_targets=catalog_targets,
    )
        _close_portage_operational_tally_gaps(
            result.assignments,
            states,
            employees=employees,
            shift_templates=shift_templates,
            rules=rules,
            period_start=period_start,
            period_end=period_end,
            availability_blocked=availability_blocked,
            qual_codes=qual_codes,
            catalog_targets=catalog_targets,
            payroll_targets=payroll_targets,
        )
        fill_counts = _seat_fill_counts(result.assignments, employees, qual_codes)
        _deterministic_resolve_day_night_transitions(
            result.assignments,
            states,
            employees=employees,
            shift_templates=shift_templates,
            shift_required_qualifications=shift_required_qualifications,
            expanded_slots=expanded_slots,
            rules=rules,
            period_start=period_start,
            period_end=period_end,
            availability_blocked=availability_blocked,
            qual_codes=qual_codes,
            target_hours_map=payroll_targets,
            period_target_hours=catalog_targets,
            fill_counts=fill_counts,
        )
        trim_consecutive_night_overruns(
            result.assignments,
            employees=employees,
            shift_templates=shift_templates,
            period_start=period_start,
            period_end=period_end,
        )
        for _ in range(4):
            if not _trim_clinical_band_overfill(
                result.assignments,
                states,
                employees=employees,
                shift_templates=shift_templates,
                qual_codes=qual_codes,
                period_start=period_start,
                period_end=period_end,
                fulltime_target=fulltime_target,
                catalog_targets=catalog_targets,
                allow_trim_frozen=True,
            ):
                break

    _trim_catalog_contract_surplus(
        result.assignments,
        states,
        employees=employees,
        shift_templates=shift_templates,
        catalog_targets=catalog_targets,
        period_start=period_start,
        period_end=period_end,
        allow_trim_frozen=True,
        payroll_targets=payroll_targets,
    )
    fill_counts = _seat_fill_counts(result.assignments, employees, qual_codes)
    _extend_evening_night_clinical_lockdown(
        result,
        employees=employees,
        states=states,
        expanded_slots=expanded_slots,
        shift_templates=shift_templates,
        shift_required_qualifications=shift_required_qualifications,
        rules=rules,
        fill_counts=fill_counts,
        availability_blocked=availability_blocked,
        qual_codes=qual_codes,
        period_start=period_start,
        period_end=period_end,
        max_rounds=16,
        log_critical_gaps=False,
        post_pass_guard=post_pass_guard,
        allow_frozen_clinical_supersede=True,
            payroll_targets=target_hours_map,
        catalog_targets=catalog_targets,
    )
    _close_portage_operational_tally_gaps(
        result.assignments,
        states,
        employees=employees,
        shift_templates=shift_templates,
        rules=rules,
        period_start=period_start,
        period_end=period_end,
        availability_blocked=availability_blocked,
        qual_codes=qual_codes,
        catalog_targets=catalog_targets,
        payroll_targets=payroll_targets,
    )
    correct_portage_night_streaks(
        result.assignments,
        employees=employees,
        shift_templates=shift_templates,
        shift_required_qualifications=shift_required_qualifications,
        rules=rules,
        period_start=period_start,
        period_end=period_end,
        weeks_in_period=weeks_in_period,
        employee_target_hours=catalog_targets,
        availability_blocked=availability_blocked,
    )
    trim_consecutive_night_overruns(
        result.assignments,
        employees=employees,
        shift_templates=shift_templates,
        period_start=period_start,
        period_end=period_end,
    )
    _trim_catalog_contract_surplus(
        result.assignments,
        states,
        employees=employees,
        shift_templates=shift_templates,
        catalog_targets=catalog_targets,
        period_start=period_start,
        period_end=period_end,
        allow_trim_frozen=True,
        payroll_targets=payroll_targets,
    )
    for _ in range(6):
        if not _trim_clinical_band_overfill(
            result.assignments,
            states,
            employees=employees,
            shift_templates=shift_templates,
            qual_codes=qual_codes,
            period_start=period_start,
            period_end=period_end,
            fulltime_target=fulltime_target,
            catalog_targets=catalog_targets,
            allow_trim_frozen=True,
        ):
            break
    trim_weekend_daily_qual_over_cap(
        result.assignments,
        states=states,
        employees=employees,
        qual_codes=qual_codes,
        shift_templates=shift_templates,
        period_start=period_start,
        period_end=period_end,
    )
    _rebuild_states_from_assignments(states, result.assignments, shift_templates)

    _rebuild_states_from_assignments(states, result.assignments, shift_templates)
    fill_counts = _seat_fill_counts(result.assignments, employees, qual_codes)
    _deterministic_resolve_day_night_transitions(
        result.assignments,
        states,
        employees=employees,
        shift_templates=shift_templates,
        shift_required_qualifications=shift_required_qualifications,
        expanded_slots=expanded_slots,
        rules=rules,
        period_start=period_start,
        period_end=period_end,
        availability_blocked=availability_blocked,
        qual_codes=qual_codes,
        target_hours_map=payroll_targets,
        period_target_hours=catalog_targets,
        fill_counts=fill_counts,
    )
    _rebuild_states_from_assignments(states, result.assignments, shift_templates)


def _finalize_portage_schedule(
    result: AutoGenerateResult,
    *,
    employees: Sequence[EmployeeProfile],
    states: Dict[str, _EmployeeState],
    expanded_slots: Sequence[ExpandedScheduleSlot],
    shift_templates: Dict[str, ShiftTemplateInfo],
    shift_required_qualifications: Dict[str, Set[str]],
    rules: JurisdictionRules,
    period_start: date,
    period_end: date,
    weeks_in_period: int,
    availability_blocked: Optional[Mapping[str, Set[date]]],
    qual_codes: Mapping[str, str],
    target_hours_map: Mapping[str, float],
    period_target_hours: Mapping[str, float],
    fulltime_target: float,
    filled_smooth_seats: Set[Tuple[date, str, Optional[str], int]],
) -> None:
    """Last-mile pass: exact E/N tallies, close coverage gaps, top up 1.0 FTE hours."""

    if result.compliance_first:
        return

    catalog_targets = apply_catalog_targets_for_vacant_master_lines(
        employees,
        target_hours_map,
        rules=rules,
        weeks_in_period=weeks_in_period,
        period_start=period_start,
        period_end=period_end,
    )
    post_pass_guard = _post_pass_guard_for_result(result)
    fill_counts = _seat_fill_counts(result.assignments, employees, qual_codes)

    for _cycle in range(5):
        _trim_clinical_band_overfill(
            result.assignments,
            states,
            employees=employees,
            shift_templates=shift_templates,
            qual_codes=qual_codes,
            period_start=period_start,
            period_end=period_end,
            fulltime_target=fulltime_target,
        )
        fill_counts = _seat_fill_counts(result.assignments, employees, qual_codes)
        _extend_evening_night_clinical_lockdown(
            result,
            employees=employees,
            states=states,
            expanded_slots=expanded_slots,
            shift_templates=shift_templates,
            shift_required_qualifications=shift_required_qualifications,
            rules=rules,
            fill_counts=fill_counts,
            availability_blocked=availability_blocked,
            qual_codes=qual_codes,
            period_start=period_start,
            period_end=period_end,
            log_critical_gaps=False,
            post_pass_guard=post_pass_guard,
                payroll_targets=target_hours_map,
        catalog_targets=catalog_targets,
    )
        fill_counts = _seat_fill_counts(result.assignments, employees, qual_codes)
        for shift_codes in (("MORNING", "EVENING", "NIGHT"),):
            locked = _clinical_floor_lock_pass(
                employees=employees,
                states=states,
                expanded_slots=expanded_slots,
                shift_templates=shift_templates,
                shift_required_qualifications=shift_required_qualifications,
                rules=rules,
                period_start=period_start,
                period_end=period_end,
                availability_blocked=availability_blocked,
                qual_codes=qual_codes,
                target_hours_map=target_hours_map,
                period_target_hours=period_target_hours,
                fill_counts=fill_counts,
                filled_smooth_seats=filled_smooth_seats,
                prioritize_coverage=True,
                shift_codes=shift_codes,
                allow_forced_clinical_ot=True,
                clinical_mandatory=True,
                single_pass=True,
                post_pass_guard=post_pass_guard,
                guard_assignments=result.assignments,
            )
            if locked:
                result.assignments.extend(locked)
                fill_counts = _seat_fill_counts(result.assignments, employees, qual_codes)
        if evening_night_clinical_seats_satisfied(
            fill_counts=fill_counts,
            expanded_slots=expanded_slots,
            shift_templates=shift_templates,
            period_start=period_start,
            period_end=period_end,
        ):
            break

    _force_fill_all_remaining_slots(
        result,
        employees=employees,
        states=states,
        expanded_slots=expanded_slots,
        shift_templates=shift_templates,
        shift_required_qualifications=shift_required_qualifications,
        rules=rules,
        period_start=period_start,
        period_end=period_end,
        availability_blocked=availability_blocked,
        qual_codes=qual_codes,
        fill_counts=fill_counts,
        filled_smooth_seats=filled_smooth_seats,
        clinical_mandatory=True,
    )
    fill_counts = _seat_fill_counts(result.assignments, employees, qual_codes)

    for _ in range(3):
        _trim_clinical_band_overfill(
            result.assignments,
            states,
            employees=employees,
            shift_templates=shift_templates,
            qual_codes=qual_codes,
            period_start=period_start,
            period_end=period_end,
            fulltime_target=fulltime_target,
        )
        fill_counts = _seat_fill_counts(result.assignments, employees, qual_codes)
        _extend_evening_night_clinical_lockdown(
            result,
            employees=employees,
            states=states,
            expanded_slots=expanded_slots,
            shift_templates=shift_templates,
            shift_required_qualifications=shift_required_qualifications,
            rules=rules,
            fill_counts=fill_counts,
            availability_blocked=availability_blocked,
            qual_codes=qual_codes,
            period_start=period_start,
            period_end=period_end,
            log_critical_gaps=False,
            post_pass_guard=post_pass_guard,
                payroll_targets=target_hours_map,
        catalog_targets=target_hours_map,
    )
        fill_counts = _seat_fill_counts(result.assignments, employees, qual_codes)

    prune_weekend_assignments_to_cap(
        result.assignments,
        states=states,
        employees=employees,
        qual_codes=qual_codes,
        shift_templates=shift_templates,
        period_start=period_start,
        period_end=period_end,
    )
    fill_counts = _seat_fill_counts(result.assignments, employees, qual_codes)

    for _ in range(24):
        if _all_at_contract_finalize_targets(
            employees, states, catalog_targets, target_hours_map
        ):
            break
        fill_counts = _seat_fill_counts(result.assignments, employees, qual_codes)
        repaired = _tail_repair_fulltime_contract(
            employees=employees,
            states=states,
            assignments=result.assignments,
            expanded_slots=expanded_slots,
            shift_templates=shift_templates,
            shift_required_qualifications=shift_required_qualifications,
            rules=rules,
            period_start=period_start,
            period_end=period_end,
            availability_blocked=availability_blocked,
            qual_codes=qual_codes,
            fill_counts=fill_counts,
            filled_smooth_seats=filled_smooth_seats,
            prioritize_coverage=True,
            fulltime_target=fulltime_target,
        )
        if repaired:
            _trim_clinical_band_overfill(
                result.assignments,
                states,
                employees=employees,
                shift_templates=shift_templates,
                qual_codes=qual_codes,
                period_start=period_start,
                period_end=period_end,
                fulltime_target=fulltime_target,
            )
            continue
        extra = _mandatory_fulltime_contract_pass(
            employees=employees,
            states=states,
            expanded_slots=expanded_slots,
            shift_templates=shift_templates,
            shift_required_qualifications=shift_required_qualifications,
            rules=rules,
            period_start=period_start,
            period_end=period_end,
            weeks_in_period=weeks_in_period,
            availability_blocked=availability_blocked,
            qual_codes=qual_codes,
            fill_counts=fill_counts,
            filled_smooth_seats=filled_smooth_seats,
            prioritize_coverage=False,
            relax_dn_contract_completion=True,
        )
        if extra:
            result.assignments.extend(extra)
            _trim_clinical_band_overfill(
                result.assignments,
                states,
                employees=employees,
                shift_templates=shift_templates,
                qual_codes=qual_codes,
                period_start=period_start,
                period_end=period_end,
                fulltime_target=fulltime_target,
                catalog_targets=catalog_targets,
            )
            continue
        rebalanced = _reassign_loaded_fulltime_shifts_to_contract_deficit(
            employees=employees,
            states=states,
            assignments=result.assignments,
            shift_templates=shift_templates,
            shift_required_qualifications=shift_required_qualifications,
            rules=rules,
            period_start=period_start,
            period_end=period_end,
            availability_blocked=availability_blocked,
            qual_codes=qual_codes,
            fulltime_target=fulltime_target,
        )
        if rebalanced:
            _trim_clinical_band_overfill(
                result.assignments,
                states,
                employees=employees,
                shift_templates=shift_templates,
                qual_codes=qual_codes,
                period_start=period_start,
                period_end=period_end,
                fulltime_target=fulltime_target,
            )
            continue
        catalog_rebalanced = _rebalance_catalog_contract_hours(
            employees=employees,
            states=states,
            assignments=result.assignments,
            shift_templates=shift_templates,
            shift_required_qualifications=shift_required_qualifications,
            rules=rules,
            period_start=period_start,
            period_end=period_end,
            availability_blocked=availability_blocked,
            qual_codes=qual_codes,
            catalog_targets=catalog_targets,
        )
        if catalog_rebalanced:
            _trim_clinical_band_overfill(
                result.assignments,
                states,
                employees=employees,
                shift_templates=shift_templates,
                qual_codes=qual_codes,
                period_start=period_start,
                period_end=period_end,
                fulltime_target=fulltime_target,
                catalog_targets=catalog_targets,
            )
            continue
        topped_up = _catalog_contract_top_up_pass(
            employees=employees,
            states=states,
            assignments=result.assignments,
            shift_templates=shift_templates,
            rules=rules,
            period_start=period_start,
            period_end=period_end,
            availability_blocked=availability_blocked,
            qual_codes=qual_codes,
            catalog_targets=catalog_targets,
            payroll_targets=target_hours_map,
        )
        if topped_up:
            _trim_clinical_band_overfill(
                result.assignments,
                states,
                employees=employees,
                shift_templates=shift_templates,
                qual_codes=qual_codes,
                period_start=period_start,
                period_end=period_end,
                fulltime_target=fulltime_target,
                catalog_targets=catalog_targets,
            )
            continue
        break

    _apply_portage_weekend_pairing_policy(
        result.assignments,
        states,
        employees=employees,
        shift_templates=shift_templates,
        rules=rules,
        period_start=period_start,
        period_end=period_end,
        availability_blocked=availability_blocked,
        qual_codes=qual_codes,
        catalog_targets=catalog_targets,
    )

    fill_counts = _seat_fill_counts(result.assignments, employees, qual_codes)
    _fill_weekend_morning_clinical_gaps(
        result.assignments,
        states,
        employees=employees,
        expanded_slots=expanded_slots,
        shift_templates=shift_templates,
        shift_required_qualifications=shift_required_qualifications,
        rules=rules,
        period_start=period_start,
        period_end=period_end,
        availability_blocked=availability_blocked,
        qual_codes=qual_codes,
        fill_counts=fill_counts,
        filled_smooth_seats=filled_smooth_seats,
    )

    _apply_portage_weekend_pairing_policy(
        result.assignments,
        states,
        employees=employees,
        shift_templates=shift_templates,
        rules=rules,
        period_start=period_start,
        period_end=period_end,
        availability_blocked=availability_blocked,
        qual_codes=qual_codes,
        catalog_targets=catalog_targets,
    )
    _rebalance_vacant_peer_equity(
        result.assignments,
        states,
        employees=employees,
        shift_templates=shift_templates,
        rules=rules,
        period_start=period_start,
        period_end=period_end,
        availability_blocked=availability_blocked,
        qual_codes=qual_codes,
        employee_target_hours=catalog_targets,
    )
    _enforce_portage_operational_band_caps(
        result.assignments,
        states,
        employees=employees,
        shift_templates=shift_templates,
        qual_codes=qual_codes,
        period_start=period_start,
        period_end=period_end,
        fulltime_target=fulltime_target,
        catalog_targets=catalog_targets,
    )
    _rebuild_states_from_assignments(states, result.assignments, shift_templates)
    _apply_portage_weekend_pairing_policy(
        result.assignments,
        states,
        employees=employees,
        shift_templates=shift_templates,
        rules=rules,
        period_start=period_start,
        period_end=period_end,
        availability_blocked=availability_blocked,
        qual_codes=qual_codes,
        catalog_targets=catalog_targets,
    )

    fill_counts = _seat_fill_counts(result.assignments, employees, qual_codes)
    for _ in range(32):
        if _all_at_contract_finalize_targets(
            employees, states, catalog_targets, target_hours_map
        ):
            break
        catalog_rebalanced = _rebalance_catalog_contract_hours(
            employees=employees,
            states=states,
            assignments=result.assignments,
            shift_templates=shift_templates,
            shift_required_qualifications=shift_required_qualifications,
            rules=rules,
            period_start=period_start,
            period_end=period_end,
            availability_blocked=availability_blocked,
            qual_codes=qual_codes,
            catalog_targets=catalog_targets,
        )
        topped_up = _catalog_contract_top_up_pass(
            employees=employees,
            states=states,
            assignments=result.assignments,
            shift_templates=shift_templates,
            rules=rules,
            period_start=period_start,
            period_end=period_end,
            availability_blocked=availability_blocked,
            qual_codes=qual_codes,
            catalog_targets=catalog_targets,
            payroll_targets=target_hours_map,
        )
        if not catalog_rebalanced and not topped_up:
            break
        _trim_clinical_band_overfill(
            result.assignments,
            states,
            employees=employees,
            shift_templates=shift_templates,
            qual_codes=qual_codes,
            period_start=period_start,
            period_end=period_end,
            fulltime_target=fulltime_target,
            catalog_targets=catalog_targets,
        )
        fill_counts = _seat_fill_counts(result.assignments, employees, qual_codes)

    _break_portage_work_streaks(
        result.assignments,
        states,
        employees=employees,
        shift_templates=shift_templates,
        shift_required_qualifications=shift_required_qualifications,
        rules=rules,
        period_start=period_start,
        period_end=period_end,
        availability_blocked=availability_blocked,
        qual_codes=qual_codes,
    )

    from lab_scheduler.scheduling.night_streak_corrector import correct_portage_night_streaks

    night_correction = correct_portage_night_streaks(
        result.assignments,
        employees=employees,
        shift_templates=shift_templates,
        shift_required_qualifications=shift_required_qualifications,
        rules=rules,
        period_start=period_start,
        period_end=period_end,
        weeks_in_period=weeks_in_period,
        employee_target_hours=catalog_targets,
        availability_blocked=availability_blocked,
    )
    if night_correction.swaps_applied:
        _rebuild_states_from_assignments(
            states,
            result.assignments,
            shift_templates,
        )
        fill_counts = _seat_fill_counts(result.assignments, employees, qual_codes)
        _trim_clinical_band_overfill(
            result.assignments,
            states,
            employees=employees,
            shift_templates=shift_templates,
            qual_codes=qual_codes,
            period_start=period_start,
            period_end=period_end,
            fulltime_target=fulltime_target,
            catalog_targets=catalog_targets,
        )

    for _ in range(16):
        if _all_at_contract_finalize_targets(
            employees, states, catalog_targets, target_hours_map
        ):
            break
        rebalanced = _rebalance_catalog_contract_hours(
            employees=employees,
            states=states,
            assignments=result.assignments,
            shift_templates=shift_templates,
            shift_required_qualifications=shift_required_qualifications,
            rules=rules,
            period_start=period_start,
            period_end=period_end,
            availability_blocked=availability_blocked,
            qual_codes=qual_codes,
            catalog_targets=catalog_targets,
        )
        topped_up = _catalog_contract_top_up_pass(
            employees=employees,
            states=states,
            assignments=result.assignments,
            shift_templates=shift_templates,
            rules=rules,
            period_start=period_start,
            period_end=period_end,
            availability_blocked=availability_blocked,
            qual_codes=qual_codes,
            catalog_targets=catalog_targets,
            payroll_targets=target_hours_map,
        )
        if not rebalanced and not topped_up:
            break
        _trim_clinical_band_overfill(
            result.assignments,
            states,
            employees=employees,
            shift_templates=shift_templates,
            qual_codes=qual_codes,
            period_start=period_start,
            period_end=period_end,
            fulltime_target=fulltime_target,
            catalog_targets=catalog_targets,
        )

    fill_counts = _seat_fill_counts(result.assignments, employees, qual_codes)
    _trim_portage_day_shift_overfill(
        result.assignments,
        states,
        employees=employees,
        shift_templates=shift_templates,
        qual_codes=qual_codes,
        rules=rules,
        period_start=period_start,
        period_end=period_end,
        availability_blocked=availability_blocked,
        catalog_targets=catalog_targets,
    )
    for _ in range(8):
        if _all_at_contract_finalize_targets(
            employees, states, catalog_targets, target_hours_map
        ):
            break
        topped_up = _catalog_contract_top_up_pass(
            employees=employees,
            states=states,
            assignments=result.assignments,
            shift_templates=shift_templates,
            rules=rules,
            period_start=period_start,
            period_end=period_end,
            availability_blocked=availability_blocked,
            qual_codes=qual_codes,
            catalog_targets=catalog_targets,
            payroll_targets=target_hours_map,
        )
        if not topped_up:
            break
        _trim_clinical_band_overfill(
            result.assignments,
            states,
            employees=employees,
            shift_templates=shift_templates,
            qual_codes=qual_codes,
            period_start=period_start,
            period_end=period_end,
            fulltime_target=fulltime_target,
            catalog_targets=catalog_targets,
        )
        _trim_portage_day_shift_overfill(
            result.assignments,
            states,
            employees=employees,
            shift_templates=shift_templates,
            qual_codes=qual_codes,
            rules=rules,
            period_start=period_start,
            period_end=period_end,
            availability_blocked=availability_blocked,
            catalog_targets=catalog_targets,
        )
    result.clinical_gap_reports = _collect_clinical_gap_reports(
        expanded_slots=expanded_slots,
        fill_counts=fill_counts,
        shift_templates=shift_templates,
        period_start=period_start,
        period_end=period_end,
    )

    _update_slot_fill_metrics(
        result,
        expanded_slots=expanded_slots,
        fill_counts=_seat_fill_counts(result.assignments, employees, qual_codes),
        shift_templates=shift_templates,
    )


def _trim_weekend_morning_overfill(
    assignments: List[PlannedAssignment],
    states: Dict[str, _EmployeeState],
    *,
    employees: Sequence[EmployeeProfile],
    shift_templates: Dict[str, ShiftTemplateInfo],
    qual_codes: Mapping[str, str],
    period_start: date,
    period_end: date,
) -> int:
    """Remove excess Sat/Sun morning assignments beyond the two-seat weekend cap."""

    return prune_weekend_assignments_to_cap(
        assignments,
        states=states,
        employees=employees,
        qual_codes=qual_codes,
        shift_templates=shift_templates,
        period_start=period_start,
        period_end=period_end,
    )


def _trim_weekday_day_shift_imbalance(
    assignments: List[PlannedAssignment],
    states: Dict[str, _EmployeeState],
    *,
    employees: Sequence[EmployeeProfile],
    shift_templates: Dict[str, ShiftTemplateInfo],
    qual_codes: Mapping[str, str],
    rules: JurisdictionRules,
    period_start: date,
    period_end: date,
    availability_blocked: Optional[Mapping[str, Set[date]]] = None,
    catalog_targets: Optional[Mapping[str, float]] = None,
) -> int:
    """Move or drop weekday morning shifts until day counts stay within ±1."""

    from lab_scheduler.scheduling.schedule_tallies import WEEKDAY_DAY_BALANCE_TOLERANCE

    changed = 0
    emp_by_id = {employee.id: employee for employee in employees}
    morning_ids = {
        shift_id
        for shift_id, template in shift_templates.items()
        if template.code == "MORNING"
    }
    weekday_dates = [
        assignment_date
        for assignment_date in _daterange(period_start, period_end)
        if assignment_date.weekday() < 5
    ]
    if not weekday_dates:
        return 0

    def _weekday_counts() -> Dict[date, int]:
        return {
            assignment_date: weekday_morning_shift_count_from_states(
                states,
                assignment_date,
                shift_templates=shift_templates,
            )
            for assignment_date in weekday_dates
        }

    def _over_catalog_band(employee_id: str) -> bool:
        employee = emp_by_id.get(employee_id)
        if employee is None or catalog_targets is None:
            return False
        target = _catalog_contract_target(employee, catalog_targets)
        if target <= 0.0:
            return False
        return states[employee_id].total_hours > target + 8.0 + 0.25

    for _attempt in range(len(weekday_dates) * 40):
        counts = _weekday_counts()
        lo = min(counts.values())
        hi = max(counts.values())
        over_cap_dates = [
            assignment_date
            for assignment_date in weekday_dates
            if counts[assignment_date] > WEEKDAY_DAY_SHIFT_CAPACITY_LIMIT
        ]
        if over_cap_dates:
            heavy_date = max(over_cap_dates, key=lambda day: counts[day])
            light_date = min(weekday_dates, key=lambda day: counts[day])
        elif hi - lo <= WEEKDAY_DAY_BALANCE_TOLERANCE:
            break
        else:
            heavy_date = max(weekday_dates, key=lambda day: counts[day])
            light_date = min(weekday_dates, key=lambda day: counts[day])
        moved_one = False

        heavy_indices = [
            index
            for index, assignment in enumerate(assignments)
            if assignment.assignment_date == heavy_date
            and assignment.shift_template_id in morning_ids
        ]
        heavy_indices.sort(
            key=lambda index: (
                0 if _over_catalog_band(assignments[index].employee_id) else 1,
                1 if getattr(assignments[index], "master_template_frozen", False) else 0,
                -states[assignments[index].employee_id].total_hours,
                index,
            )
        )

        for index in heavy_indices:
            assignment = assignments[index]
            state = states.get(assignment.employee_id)
            if state is None or light_date in state.work_dates:
                continue
            template = shift_templates[assignment.shift_template_id]
            violation = _would_violate_labor_rules(
                state,
                light_date,
                template,
                shift_templates,
                rules,
                period_start,
                period_end,
                availability_blocked,
                enforce_fte_target=False,
                relax_dn_contract_completion=True,
            )
            if violation:
                continue
            shift_hours = template.duration_minutes / 60.0
            week_start_old = workweek_for(heavy_date).start
            week_start_new = workweek_for(light_date).start
            state.work_dates.discard(heavy_date)
            state.assignment_records[:] = [
                record
                for record in state.assignment_records
                if not (record[0] == heavy_date and record[1] == assignment.shift_template_id)
            ]
            if week_start_old in state.week_hours:
                state.week_hours[week_start_old] = max(
                    0.0,
                    state.week_hours[week_start_old] - shift_hours,
                )
            state.work_dates.add(light_date)
            state.assignment_records.append((light_date, assignment.shift_template_id))
            state.week_hours[week_start_new] = state.week_hours.get(week_start_new, 0.0) + shift_hours
            assignments[index] = PlannedAssignment(
                employee_id=assignment.employee_id,
                shift_template_id=assignment.shift_template_id,
                assignment_date=light_date,
                forced_clinical_ot=assignment.forced_clinical_ot,
                overtime_compliance_bypassed=assignment.overtime_compliance_bypassed,
                approved_stretch=assignment.approved_stretch,
                clinical_floor_stretch=assignment.clinical_floor_stretch,
                master_template_frozen=assignment.master_template_frozen,
            )
            changed += 1
            moved_one = True
            break

        if moved_one:
            continue

        for index in sorted(heavy_indices, reverse=True):
            assignment = assignments[index]
            if catalog_targets is not None and not _over_catalog_band(assignment.employee_id):
                continue
            if getattr(assignment, "master_template_frozen", False) and not _over_catalog_band(
                assignment.employee_id
            ):
                continue
            template = shift_templates[assignment.shift_template_id]
            shift_hours = template.duration_minutes / 60.0
            _remove_assignment_from_state(
                states[assignment.employee_id],
                assignment.assignment_date,
                assignment.shift_template_id,
                shift_hours,
            )
            del assignments[index]
            changed += 1
            break
        else:
            break

    return changed


def _trim_portage_day_shift_overfill(
    assignments: List[PlannedAssignment],
    states: Dict[str, _EmployeeState],
    *,
    employees: Sequence[EmployeeProfile],
    shift_templates: Dict[str, ShiftTemplateInfo],
    qual_codes: Mapping[str, str],
    rules: JurisdictionRules,
    period_start: date,
    period_end: date,
    availability_blocked: Optional[Mapping[str, Set[date]]] = None,
    catalog_targets: Optional[Mapping[str, float]] = None,
) -> int:
    """Final day-band cleanup: weekend morning cap and weekday ±1 balance."""

    removed = _trim_weekend_morning_overfill(
        assignments,
        states,
        employees=employees,
        shift_templates=shift_templates,
        qual_codes=qual_codes,
        period_start=period_start,
        period_end=period_end,
    )
    removed += _trim_weekday_day_shift_imbalance(
        assignments,
        states,
        employees=employees,
        shift_templates=shift_templates,
        qual_codes=qual_codes,
        rules=rules,
        period_start=period_start,
        period_end=period_end,
        availability_blocked=availability_blocked,
        catalog_targets=catalog_targets,
    )
    return removed


def _deterministic_single_pass_contract_fill(
    *,
    employees: Sequence[EmployeeProfile],
    states: Dict[str, _EmployeeState],
    expanded_slots: Sequence[ExpandedScheduleSlot],
    shift_templates: Dict[str, ShiftTemplateInfo],
    shift_required_qualifications: Dict[str, Set[str]],
    rules: JurisdictionRules,
    period_start: date,
    period_end: date,
    availability_blocked: Optional[Mapping[str, Set[date]]],
    qual_codes: Mapping[str, str],
    fill_counts: Dict[Tuple[date, str, Optional[str]], int],
    filled_smooth_seats: Set[Tuple[date, str, Optional[str], int]],
    weeks_in_period: int,
    load_reference_hours: Optional[Mapping[str, float]] = None,
) -> List[PlannedAssignment]:
    """Single-pass greedy contract fill for 1.0 FTE lines — no retry rounds."""

    fulltime_target = fulltime_period_contract_hours(
        rules=rules,
        weeks_in_period=weeks_in_period,
    )
    contract_slots = sorted(
        expanded_slots,
        key=lambda slot: (
            slot.assignment_date,
            autonomous_balance_slot_sort_key(slot),
            slot.seat_index,
        ),
    )
    planned: List[PlannedAssignment] = []

    for slot in contract_slots:
        if shift_templates[slot.shift_id].code in {"NIGHT", "EVENING"}:
            continue
        if _slot_already_filled(slot, fill_counts):
            continue
        if is_smooth_day_balance_pool(slot.role_pool_id):
            smooth_key = (
                slot.assignment_date,
                slot.shift_id,
                slot.required_qual_code,
                slot.seat_index,
            )
            if smooth_key in filled_smooth_seats:
                continue

        chosen = _pick_mandatory_fulltime_candidate(
            employees=employees,
            states=states,
            slot=slot,
            shift_templates=shift_templates,
            shift_required_qualifications=shift_required_qualifications,
            rules=rules,
            period_start=period_start,
            period_end=period_end,
            availability_blocked=availability_blocked,
            qual_codes=qual_codes,
            fulltime_target=fulltime_target,
            load_reference_hours=load_reference_hours,
        )
        if chosen is None:
            continue

        shift_id = slot.shift_id
        template = shift_templates[shift_id]
        shift_hours = template.duration_minutes / 60.0
        _apply_assignment_to_state(states[chosen.id], slot.assignment_date, shift_id, shift_hours)
        planned.append(
            PlannedAssignment(
                employee_id=chosen.id,
                shift_template_id=shift_id,
                assignment_date=slot.assignment_date,
            )
        )
        if is_smooth_day_balance_pool(slot.role_pool_id):
            filled_smooth_seats.add(
                (slot.assignment_date, shift_id, slot.required_qual_code, slot.seat_index)
            )
        seat_key_count = (slot.assignment_date, shift_id, slot.required_qual_code)
        fill_counts[seat_key_count] = fill_counts.get(seat_key_count, 0) + 1

    return planned


def _deterministic_single_pass_remaining_fill(
    *,
    employees: Sequence[EmployeeProfile],
    states: Dict[str, _EmployeeState],
    expanded_slots: Sequence[ExpandedScheduleSlot],
    shift_templates: Dict[str, ShiftTemplateInfo],
    shift_required_qualifications: Dict[str, Set[str]],
    rules: JurisdictionRules,
    period_start: date,
    period_end: date,
    availability_blocked: Optional[Mapping[str, Set[date]]],
    qual_codes: Mapping[str, str],
    target_hours_map: Mapping[str, float],
    period_target_hours: Mapping[str, float],
    fill_counts: Dict[Tuple[date, str, Optional[str]], int],
    filled_smooth_seats: Set[Tuple[date, str, Optional[str], int]],
    weekday_staffing_plan: Optional[WeekdayDailyStaffingPlan] = None,
) -> List[PlannedAssignment]:
    """One deterministic sweep over open demand seats using ranked pool order."""

    planned: List[PlannedAssignment] = []
    open_slots = sorted(
        expanded_slots,
        key=lambda slot: (
            clinical_demand_slot_sort_key(
                slot,
                shift_templates=shift_templates,
                fill_counts=fill_counts,
                expanded_slots=expanded_slots,
            ),
            slot.assignment_date,
            slot.shift_id,
            slot.seat_index,
        ),
    )

    for slot in open_slots:
        if is_smooth_day_balance_pool(slot.role_pool_id):
            smooth_key = (
                slot.assignment_date,
                slot.shift_id,
                slot.required_qual_code,
                slot.seat_index,
            )
            if smooth_key in filled_smooth_seats:
                continue

        if _slot_already_filled(slot, fill_counts):
            continue
        if _slot_blocked_by_weekend_cap(
            slot,
            states=states,
            employees=employees,
            qual_codes=qual_codes,
            shift_templates=shift_templates,
        ):
            continue

        if non_clinical_fill_blocked_until_clinical_floor(
            slot,
            fill_counts=fill_counts,
            expanded_slots=expanded_slots,
            shift_templates=shift_templates,
        ):
            continue

        if not _can_assign_clinical_floor_slot(
            slot,
            fill_counts,
            shift_templates=shift_templates,
            expanded_slots=expanded_slots,
        ):
            continue

        shift_id = slot.shift_id
        template = shift_templates[shift_id]
        required = shift_required_qualifications.get(shift_id, set())
        ranked, _, _ = _rank_slot_candidates(
            employees=employees,
            required=required,
            states=states,
            assignment_date=slot.assignment_date,
            shift_id=shift_id,
            template=template,
            shift_templates=shift_templates,
            rules=rules,
            period_start=period_start,
            period_end=period_end,
            availability_blocked=availability_blocked,
            prioritize_coverage=True,
            period_target_hours=period_target_hours,
            required_qual_code=slot.required_qual_code,
            qual_codes=qual_codes,
            employee_target_hours=target_hours_map,
            role_pool_id=slot.role_pool_id,
            fill_counts=fill_counts,
            weekday_staffing_plan=weekday_staffing_plan,
        )
        if not ranked:
            continue

        chosen = ranked[0]
        shift_hours = template.duration_minutes / 60.0
        _apply_assignment_to_state(
            states[chosen.id],
            slot.assignment_date,
            shift_id,
            shift_hours,
        )
        planned.append(
            PlannedAssignment(
                employee_id=chosen.id,
                shift_template_id=shift_id,
                assignment_date=slot.assignment_date,
            )
        )
        if is_smooth_day_balance_pool(slot.role_pool_id):
            filled_smooth_seats.add(
                (slot.assignment_date, shift_id, slot.required_qual_code, slot.seat_index)
            )
        seat_key_count = (slot.assignment_date, shift_id, slot.required_qual_code)
        fill_counts[seat_key_count] = fill_counts.get(seat_key_count, 0) + 1

    return planned


def _collect_clinical_gap_reports(
    *,
    expanded_slots: Sequence[ExpandedScheduleSlot],
    fill_counts: Mapping[Tuple[date, str, Optional[str]], int],
    shift_templates: Mapping[str, ShiftTemplateInfo],
    period_start: date,
    period_end: date,
) -> List[ClinicalGapReport]:
    gaps: List[ClinicalGapReport] = []

    for assignment_date in _daterange(period_start, period_end):
        for shift_code in ("EVENING", "NIGHT", "MORNING"):
            floor_slots = clinical_floor_slots_for_day(
                assignment_date,
                shift_code,
                expanded_slots,
                shift_templates=shift_templates,
            )
            if not floor_slots:
                continue
            filled = clinical_floor_filled_for_day(
                assignment_date,
                shift_code,
                fill_counts=fill_counts,
                expanded_slots=expanded_slots,
                shift_templates=shift_templates,
            )
            required = len(floor_slots)
            if filled < required:
                gaps.append(
                    ClinicalGapReport(
                        assignment_date=assignment_date,
                        shift_code=shift_code,
                        required_seats=required,
                        filled_seats=filled,
                        reason=(
                            f"Clinical floor shortfall: {filled}/{required} "
                            f"{shift_code} seats on {assignment_date.isoformat()}"
                        ),
                    )
                )

    return gaps


def _collect_remaining_open_slot_triage(
    *,
    triage_sink: GenerationTriageSink,
    expanded_slots: Sequence[ExpandedScheduleSlot],
    fill_counts: Mapping[Tuple[date, str, Optional[str]], int],
    filled_smooth_seats: Set[Tuple[date, str, Optional[str], int]],
    impossible_seats: Set[Tuple[date, str, Optional[str], int]],
    impossible_slots: Set[Tuple[date, str]],
    employees: Sequence[EmployeeProfile],
    shift_templates: Dict[str, ShiftTemplateInfo],
    shift_required_qualifications: Dict[str, Set[str]],
    qual_codes: Mapping[str, str],
    prioritize_coverage: bool,
) -> None:
    """Record triage rows for every expanded slot that remains open after generation."""

    for slot in expanded_slots:
        if is_optional_supplemental_coverage_slot(slot):
            continue
        if prioritize_coverage and _slot_already_filled(slot, fill_counts):
            continue
        if is_smooth_day_balance_pool(slot.role_pool_id):
            smooth_key = (
                slot.assignment_date,
                slot.shift_id,
                slot.required_qual_code,
                slot.seat_index,
            )
            if smooth_key in filled_smooth_seats:
                continue

        assignment_date = slot.assignment_date
        shift_id = slot.shift_id
        template = shift_templates[shift_id]
        required = shift_required_qualifications.get(shift_id, set())
        seat_key = (assignment_date, shift_id, slot.required_qual_code, slot.seat_index)
        slot_key = (assignment_date, shift_id)
        slot_is_impossible = seat_key in impossible_seats or slot_key in impossible_slots
        qualified_names = [
            emp.full_name
            for emp in employees
            if _is_qualified(emp, required)
            and employee_matches_seat_qual(
                emp,
                slot.required_qual_code,
                qual_codes=qual_codes,
                shift_required_qualification_ids=required or None,
            )
        ]
        handle_unfillable_slot(
            triage_sink=triage_sink,
            strict_raise=False,
            assignment_date=assignment_date,
            shift_code=template.code,
            shift_id=shift_id,
            role_pool_id=slot.role_pool_id,
            seat_index=slot.seat_index,
            required_qual_code=slot.required_qual_code,
            shift_hours=template.duration_minutes / 60.0,
            slot_is_impossible=slot_is_impossible,
            qualified_staff_exist=bool(qualified_names),
            constraint_summary=(
                "qualified staff blocked by labor rules" if qualified_names else None
            ),
        )


def _fail_on_remaining_open_slots(
    *,
    expanded_slots: Sequence[ExpandedScheduleSlot],
    fill_counts: Mapping[Tuple[date, str, Optional[str]], int],
    filled_smooth_seats: Set[Tuple[date, str, Optional[str], int]],
    impossible_seats: Set[Tuple[date, str, Optional[str], int]],
    impossible_slots: Set[Tuple[date, str]],
    employees: Sequence[EmployeeProfile],
    shift_templates: Dict[str, ShiftTemplateInfo],
    shift_required_qualifications: Dict[str, Set[str]],
    qual_codes: Mapping[str, str],
    prioritize_coverage: bool,
    strict_complete_block: bool,
    emit_triage: bool,
    triage_sink: Optional[GenerationTriageSink],
) -> None:
    """Fail hard or record triage when any expanded slot remains open."""

    if emit_triage and triage_sink is not None:
        _collect_remaining_open_slot_triage(
            triage_sink=triage_sink,
            expanded_slots=expanded_slots,
            fill_counts=fill_counts,
            filled_smooth_seats=filled_smooth_seats,
            impossible_seats=impossible_seats,
            impossible_slots=impossible_slots,
            employees=employees,
            shift_templates=shift_templates,
            shift_required_qualifications=shift_required_qualifications,
            qual_codes=qual_codes,
            prioritize_coverage=prioritize_coverage,
        )
        return

    if not strict_complete_block:
        return

    for slot in expanded_slots:
        if is_optional_supplemental_coverage_slot(slot):
            continue
        if prioritize_coverage and _slot_already_filled(slot, fill_counts):
            continue

        assignment_date = slot.assignment_date
        shift_id = slot.shift_id
        template = shift_templates[shift_id]
        required = shift_required_qualifications.get(shift_id, set())
        seat_key = (assignment_date, shift_id, slot.required_qual_code, slot.seat_index)
        slot_key = (assignment_date, shift_id)
        slot_is_impossible = seat_key in impossible_seats or slot_key in impossible_slots
        qualified_names = [
            emp.full_name
            for emp in employees
            if _is_qualified(emp, required)
            and employee_matches_seat_qual(
                emp,
                slot.required_qual_code,
                qual_codes=qual_codes,
                shift_required_qualification_ids=required or None,
            )
        ]
        raise_unfillable_slot_failure(
            assignment_date=assignment_date,
            shift_code=template.code,
            slot_is_impossible=slot_is_impossible,
            qualified_staff_exist=bool(qualified_names),
            constraint_summary=(
                "qualified staff blocked by labor rules" if qualified_names else None
            ),
        )


def _weekend_qual_assignment_counts(
    assignments: Sequence[PlannedAssignment],
    *,
    employees: Sequence[EmployeeProfile],
    qual_codes: Mapping[str, str],
    assignment_date: date,
    shift_templates: Mapping[str, ShiftTemplateInfo],
    morning_only: bool = True,
) -> Dict[str, int]:
    counts = {qual: 0 for qual in WEEKEND_CLINICAL_MIN_PER_QUAL}
    morning_ids = {
        shift_id
        for shift_id, template in shift_templates.items()
        if template.code == "MORNING"
    }
    employees_by_id = {employee.id: employee for employee in employees}
    for assignment in assignments:
        if assignment.assignment_date != assignment_date:
            continue
        if morning_only and assignment.shift_template_id not in morning_ids:
            continue
        employee = employees_by_id.get(assignment.employee_id)
        if employee is None:
            continue
        qual_code = infer_qual_code(employee, qual_codes=qual_codes)
        if qual_code in counts:
            counts[qual_code] += 1
    return counts


def _slot_blocked_by_weekend_cap(
    slot: ExpandedScheduleSlot,
    *,
    states: Mapping[str, _EmployeeState],
    employees: Sequence[EmployeeProfile],
    qual_codes: Mapping[str, str],
    shift_templates: Mapping[str, ShiftTemplateInfo],
) -> bool:
    if slot.assignment_date.weekday() < 5 or not slot.required_qual_code:
        return False
    if shift_templates[slot.shift_id].code != "MORNING":
        return False
    counts = weekend_qual_counts_from_states(
        states,
        employees=employees,
        qual_codes=qual_codes,
        assignment_date=slot.assignment_date,
        shift_templates=shift_templates,
        morning_only=True,
    )
    return weekend_morning_fill_blocked(counts, slot.required_qual_code)


def _enforce_weekend_qual_limits(
    assignments: Sequence[PlannedAssignment],
    *,
    employees: Sequence[EmployeeProfile],
    qual_codes: Mapping[str, str],
    shift_templates: Mapping[str, ShiftTemplateInfo],
    period_start: date,
    period_end: date,
) -> None:
    """Every Saturday/Sunday day shift must schedule exactly one MLT and one MLA."""

    for assignment_date in _daterange(period_start, period_end):
        if assignment_date.weekday() < 5:
            continue
        counts = _weekend_qual_assignment_counts(
            assignments,
            employees=employees,
            qual_codes=qual_codes,
            assignment_date=assignment_date,
            shift_templates=shift_templates,
            morning_only=True,
        )
        if weekend_morning_clinical_floor_satisfied(counts):
            missing = []
        else:
            missing = [
                qual_code
                for qual_code, required in WEEKEND_CLINICAL_MIN_PER_QUAL.items()
                if counts.get(qual_code, 0) < required
                and not (
                    qual_code == "MLA"
                    and counts.get("MLT", 0) >= WEEKEND_MORNING_TOTAL_CAP
                )
            ]
        if missing:
            raise ImmediateClinicalFailure(
                assignment_date,
                reason=(
                    "Weekend Clinical Floor unmet — requires exactly "
                    f"1 MLT and 1 MLA (missing: {', '.join(missing)}; "
                    f"counts MLT={counts.get('MLT', 0)} MLA={counts.get('MLA', 0)})"
                ),
            )
        exceeded_total = sum(counts.values()) > 2
        exceeded = [
            qual_code
            for qual_code, maximum in WEEKEND_CLINICAL_MAX_PER_QUAL.items()
            if counts.get(qual_code, 0) > maximum
        ]
        if counts.get("MLA", 0) > 1:
            exceeded_total = True
        if exceeded or exceeded_total:
            raise ImmediateClinicalFailure(
                assignment_date,
                reason=(
                    "Weekend staffing cap exceeded — max 2 day shifts "
                    "(prefer 2 MLT, else 1 MLT + 1 MLA; "
                    f"over: {', '.join(exceeded) if exceeded else 'total'}; "
                    f"counts MLT={counts.get('MLT', 0)} MLA={counts.get('MLA', 0)})"
                ),
            )


def _enforce_weekend_clinical_floor(
    assignments: List[PlannedAssignment],
    *,
    employees: Sequence[EmployeeProfile],
    qual_codes: Mapping[str, str],
    shift_templates: Mapping[str, ShiftTemplateInfo],
    period_start: date,
    period_end: date,
    states: Optional[Dict[str, _EmployeeState]] = None,
) -> None:
    if states is not None:
        _trim_weekend_morning_overfill(
            assignments,
            states,
            employees=employees,
            shift_templates=dict(shift_templates),
            qual_codes=qual_codes,
            period_start=period_start,
            period_end=period_end,
        )
    _enforce_weekend_qual_limits(
        assignments,
        employees=employees,
        qual_codes=qual_codes,
        shift_templates=shift_templates,
        period_start=period_start,
        period_end=period_end,
    )


def _force_fill_all_remaining_slots(
    result: AutoGenerateResult,
    *,
    employees: Sequence[EmployeeProfile],
    states: Dict[str, _EmployeeState],
    expanded_slots: Sequence[ExpandedScheduleSlot],
    shift_templates: Dict[str, ShiftTemplateInfo],
    shift_required_qualifications: Dict[str, Set[str]],
    rules: JurisdictionRules,
    period_start: date,
    period_end: date,
    availability_blocked: Optional[Mapping[str, Set[date]]],
    qual_codes: Mapping[str, str],
    fill_counts: Dict[Tuple[date, str, Optional[str]], int],
    filled_smooth_seats: Set[Tuple[date, str, Optional[str], int]],
    clinical_mandatory: bool = True,
    payroll_targets: Mapping[str, float] | None = None,
    catalog_targets: Mapping[str, float] | None = None,
) -> int:
    """
    Fill remaining open seats using qualified staff that respect contract ceilings.

    Weekly-hour union limits may still be relaxed for clinical seats; payroll/catalog
    ceilings are never bypassed.
    """

    added = 0
    open_slots = sorted(
        _collect_unassigned_pool_slots(
            expanded_slots,
            fill_counts=fill_counts,
            shift_templates=shift_templates,
            filled_smooth_seats=filled_smooth_seats,
        ),
        key=lambda slot: (
            0 if slot.assignment_date.weekday() >= 5 else 1,
            0 if shift_templates[slot.shift_id].code == "MORNING" else 1,
            slot.assignment_date,
            slot.seat_index,
        ),
    )

    for slot in open_slots:
        if _slot_already_filled(slot, fill_counts):
            continue
        if _slot_blocked_by_weekend_cap(
            slot,
            states=states,
            employees=employees,
            qual_codes=qual_codes,
            shift_templates=shift_templates,
        ):
            continue

        if non_clinical_fill_blocked_until_clinical_floor(
            slot,
            fill_counts=fill_counts,
            expanded_slots=expanded_slots,
            shift_templates=shift_templates,
        ):
            continue

        is_clinical = is_clinical_floor_pool(slot.role_pool_id) or shift_templates[
            slot.shift_id
        ].code in CLINICAL_FLOOR
        if is_clinical and not _can_assign_clinical_floor_slot(
            slot,
            fill_counts,
            shift_templates=shift_templates,
            expanded_slots=expanded_slots,
        ):
            continue

        shift_id = slot.shift_id
        template = shift_templates[shift_id]
        required = shift_required_qualifications.get(shift_id, set())
        provisional: Optional[ClinicalContractLineProvisional] = None
        chosen: Optional[EmployeeProfile] = None
        if (
            is_clinical
            and template.code in {"EVENING", "NIGHT"}
            and is_clinical_floor_pool(slot.role_pool_id)
        ):
            chosen, provisional, _rejections = _resolve_mandatory_clinical_placement(
                employees=employees,
                required=required,
                states=states,
                assignment_date=slot.assignment_date,
                template=template,
                shift_templates=shift_templates,
                rules=rules,
                period_start=period_start,
                period_end=period_end,
                qual_codes=qual_codes,
                required_qual_code=slot.required_qual_code,
                availability_blocked=availability_blocked,
                role_pool_id=slot.role_pool_id,
                guard_assignments=result.assignments,
                payroll_targets=payroll_targets,
                catalog_targets=catalog_targets,
            )
        else:
            ranked = _mandatory_clinical_candidates(
                employees=employees,
                required=required,
                states=states,
                assignment_date=slot.assignment_date,
                template=template,
                qual_codes=qual_codes,
                required_qual_code=slot.required_qual_code,
                availability_blocked=availability_blocked,
                role_pool_id=slot.role_pool_id,
            )
            if ranked:
                chosen = ranked[0]
        if chosen is None and not clinical_mandatory:
            continue
        if chosen is None:
            continue

        violation = _would_violate_labor_rules(
            states[chosen.id],
            slot.assignment_date,
            template,
            shift_templates,
            rules,
            period_start,
            period_end,
            availability_blocked,
            enforce_fte_target=False,
            forced_clinical_ot=clinical_mandatory and is_clinical,
            log_rejection=True,
            payroll_targets=payroll_targets,
            catalog_targets=catalog_targets,
        )
        if violation:
            continue

        shift_hours = template.duration_minutes / 60.0
        bypassed = True
        _apply_assignment_to_state(
            states[chosen.id],
            slot.assignment_date,
            shift_id,
            shift_hours,
            rules=rules,
        )
        states[chosen.id].contract_completion_ot_used = True
        result.assignments.append(
            _planned_assignment_from_mandatory_clinical_pick(
                chosen=chosen,
                provisional=provisional,
                shift_template_id=shift_id,
                assignment_date=slot.assignment_date,
                forced_clinical_ot=is_clinical,
                overtime_compliance_bypassed=bypassed,
            )
        )
        seat_key = (slot.assignment_date, shift_id, slot.required_qual_code)
        fill_counts[seat_key] = fill_counts.get(seat_key, 0) + 1
        if is_smooth_day_balance_pool(slot.role_pool_id):
            filled_smooth_seats.add(
                (slot.assignment_date, shift_id, slot.required_qual_code, slot.seat_index)
            )
        added += 1
        result.overtime_compliance_bypass_count += 1

    return added


def _resolve_morning_after_evening_night_violations(
    assignments: List[PlannedAssignment],
    states: Dict[str, _EmployeeState],
    *,
    employees: Sequence[EmployeeProfile],
    shift_templates: Dict[str, ShiftTemplateInfo],
    shift_required_qualifications: Dict[str, Set[str]],
    expanded_slots: Sequence[ExpandedScheduleSlot],
    rules: JurisdictionRules,
    period_start: date,
    period_end: date,
    availability_blocked: Optional[Mapping[str, Set[date]]],
    qual_codes: Mapping[str, str],
    target_hours_map: Mapping[str, float],
    period_target_hours: Mapping[str, float],
    fill_counts: Dict[Tuple[date, str, Optional[str]], int],
) -> int:
    """Re-home Evening/Night shifts that would force an illegal Morning the next day."""

    resolved = 0
    shift_ids = {
        code: _shift_id_for_code(code, shift_templates)
        for code in ("MORNING", "EVENING", "NIGHT")
    }
    if any(shift_id is None for shift_id in shift_ids.values()):
        return 0

    for _pass in range(8):
        by_employee_day: Dict[Tuple[str, date], Tuple[str, str]] = {}
        for assignment in assignments:
            template = shift_templates[assignment.shift_template_id]
            by_employee_day[(assignment.employee_id, assignment.assignment_date)] = (
                template.code,
                assignment.shift_template_id,
            )

        violations: List[Tuple[str, date, date, str, str]] = []
        for (employee_id, day), (code, shift_id) in by_employee_day.items():
            if code != "MORNING":
                continue
            prior_date = day - timedelta(days=1)
            prior = by_employee_day.get((employee_id, prior_date))
            if prior is None:
                continue
            prior_code, prior_shift_id = prior
            if prior_code in {"EVENING", "NIGHT"}:
                violations.append(
                    (employee_id, prior_date, day, prior_code, prior_shift_id)
                )

        if not violations:
            break

        pass_resolved = 0
        for employee_id, _prior_date, morning_date, _prior_code, _prior_shift_id in violations:
            morning_shift_id: Optional[str] = None
            remove_index: Optional[int] = None
            for index, assignment in enumerate(assignments):
                if (
                    assignment.employee_id != employee_id
                    or assignment.assignment_date != morning_date
                ):
                    continue
                template = shift_templates[assignment.shift_template_id]
                if template.code != "MORNING":
                    continue
                morning_shift_id = assignment.shift_template_id
                remove_index = index
                break
            if remove_index is None or morning_shift_id is None:
                continue

            removed_assignment = assignments[remove_index]
            if getattr(removed_assignment, "master_template_frozen", False):
                continue

            morning_template = shift_templates[morning_shift_id]
            morning_hours = morning_template.duration_minutes / 60.0
            _remove_assignment_from_state(
                states[employee_id],
                morning_date,
                morning_shift_id,
                morning_hours,
            )
            del assignments[remove_index]

            for qual_code in ("MLT", "MLA"):
                seat_key = (morning_date, morning_shift_id, qual_code)
                if fill_counts.get(seat_key, 0) > 0:
                    fill_counts[seat_key] -= 1

            rehomed = False
            for slot in expanded_slots:
                if slot.assignment_date != morning_date or slot.shift_id != morning_shift_id:
                    continue
                if _slot_already_filled(slot, fill_counts):
                    continue
                required = shift_required_qualifications.get(morning_shift_id, set())
                ranked, _, _ = _rank_slot_candidates(
                    employees=employees,
                    required=required,
                    states=states,
                    assignment_date=morning_date,
                    shift_id=morning_shift_id,
                    template=morning_template,
                    shift_templates=shift_templates,
                    rules=rules,
                    period_start=period_start,
                    period_end=period_end,
                    availability_blocked=availability_blocked,
                    prioritize_coverage=True,
                    period_target_hours=period_target_hours,
                    required_qual_code=slot.required_qual_code,
                    qual_codes=qual_codes,
                    employee_target_hours=target_hours_map,
                    role_pool_id=slot.role_pool_id,
                    fill_counts=fill_counts,
                )
                for chosen in ranked:
                    if chosen.id == employee_id:
                        continue
                    _apply_assignment_to_state(
                        states[chosen.id],
                        morning_date,
                        morning_shift_id,
                        morning_hours,
                        rules=rules,
                    )
                    assignments.append(
                        PlannedAssignment(
                            employee_id=chosen.id,
                            shift_template_id=morning_shift_id,
                            assignment_date=morning_date,
                        )
                    )
                    seat_key_count = (morning_date, morning_shift_id, slot.required_qual_code)
                    fill_counts[seat_key_count] = fill_counts.get(seat_key_count, 0) + 1
                    rehomed = True
                    pass_resolved += 1
                    break
                if rehomed:
                    break

            if rehomed:
                resolved += 1

        if pass_resolved == 0:
            break

    return resolved


def _deterministic_resolve_day_night_transitions(
    assignments: List[PlannedAssignment],
    states: Dict[str, _EmployeeState],
    *,
    employees: Sequence[EmployeeProfile],
    shift_templates: Dict[str, ShiftTemplateInfo],
    shift_required_qualifications: Dict[str, Set[str]],
    expanded_slots: Sequence[ExpandedScheduleSlot],
    rules: JurisdictionRules,
    period_start: date,
    period_end: date,
    availability_blocked: Optional[Mapping[str, Set[date]]],
    qual_codes: Mapping[str, str],
    target_hours_map: Mapping[str, float],
    period_target_hours: Mapping[str, float],
    fill_counts: Dict[Tuple[date, str, Optional[str]], int],
    dn_only: bool = False,
) -> None:
    """Iteratively resolve illegal D→N (and optionally N→D) pairs by re-homing the Day shift."""

    from lab_scheduler.engine.demand import _day_night_calendar_band

    for _pass in range(12):
        scan_rows = [
            (assignment.employee_id, assignment.assignment_date, assignment.shift_template_id)
            for assignment in assignments
        ]
        violations: List[Tuple[str, date, date]] = []
        violations.extend(find_day_night_transition_violations(scan_rows, shift_templates))
        if not dn_only:
            violations.extend(find_night_day_transition_violations(scan_rows, shift_templates))
        if not violations:
            return

        pass_resolved = 0
        for employee_id, first_day, second_day in violations:
            day_date: Optional[date] = None
            day_shift_id: Optional[str] = None
            remove_index: Optional[int] = None

            for index, assignment in enumerate(assignments):
                if assignment.employee_id != employee_id:
                    continue
                template = shift_templates[assignment.shift_template_id]
                band = _day_night_calendar_band(template.code)
                if band != "D":
                    continue
                if assignment.assignment_date == first_day:
                    day_date = first_day
                    day_shift_id = assignment.shift_template_id
                    remove_index = index
                    break
                if assignment.assignment_date == second_day:
                    day_date = second_day
                    day_shift_id = assignment.shift_template_id
                    remove_index = index
                    break

            if day_shift_id is None or remove_index is None or day_date is None:
                continue

            removed_assignment = assignments[remove_index]
            if getattr(removed_assignment, "master_template_frozen", False) and not dn_only:
                continue

            template = shift_templates[day_shift_id]
            shift_hours = template.duration_minutes / 60.0
            _remove_assignment_from_state(
                states[employee_id],
                day_date,
                day_shift_id,
                shift_hours,
            )
            del assignments[remove_index]

            for qual_code in ("MLT", "MLA"):
                seat_key = (day_date, day_shift_id, qual_code)
                if fill_counts.get(seat_key, 0) > 0:
                    fill_counts[seat_key] -= 1

            rehomed = False
            for slot in expanded_slots:
                if slot.assignment_date != day_date or slot.shift_id != day_shift_id:
                    continue
                if _slot_already_filled(slot, fill_counts):
                    continue
                required = shift_required_qualifications.get(day_shift_id, set())
                ranked, _, _ = _rank_slot_candidates(
                    employees=employees,
                    required=required,
                    states=states,
                    assignment_date=day_date,
                    shift_id=day_shift_id,
                    template=template,
                    shift_templates=shift_templates,
                    rules=rules,
                    period_start=period_start,
                    period_end=period_end,
                    availability_blocked=availability_blocked,
                    prioritize_coverage=True,
                    period_target_hours=period_target_hours,
                    required_qual_code=slot.required_qual_code,
                    qual_codes=qual_codes,
                    employee_target_hours=target_hours_map,
                    role_pool_id=slot.role_pool_id,
                    fill_counts=fill_counts,
                )
                ranked = [employee for employee in ranked if employee.id != employee_id]
                for chosen in ranked:
                    violation = _would_violate_labor_rules(
                        states[chosen.id],
                        day_date,
                        template,
                        shift_templates,
                        rules,
                        period_start,
                        period_end,
                        availability_blocked,
                    )
                    if violation:
                        continue
                    _apply_assignment_to_state(
                        states[chosen.id],
                        day_date,
                        day_shift_id,
                        shift_hours,
                        rules=rules,
                    )
                    assignments.append(
                        PlannedAssignment(
                            employee_id=chosen.id,
                            shift_template_id=day_shift_id,
                            assignment_date=day_date,
                        )
                    )
                    seat_key_count = (day_date, day_shift_id, slot.required_qual_code)
                    fill_counts[seat_key_count] = fill_counts.get(seat_key_count, 0) + 1
                    rehomed = True
                    pass_resolved += 1
                    break
                if rehomed:
                    break

        if pass_resolved == 0:
            return


BREAKROOM_MASTER_EXPORT_NAME = "breakroom_schedule_period-2026-summer_9.html"


@dataclass(frozen=True, slots=True)
class UnionRiskLine:
    """Fulltime contract line below the Union Risk threshold for the active period."""

    employee_id: str
    employee_name: str
    scheduled_hours: float
    target_hours: float
    deficit_hours: float


def _default_project_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _revert_assignment_from_state(
    state: _EmployeeState,
    assignment_date: date,
    shift_id: str,
    shift_hours: float,
) -> None:
    week_start = workweek_for(assignment_date).start
    state.work_dates.discard(assignment_date)
    state.assignment_records = [
        record
        for record in state.assignment_records
        if not (record[0] == assignment_date and record[1] == shift_id)
    ]
    state.total_hours = max(0.0, state.total_hours - shift_hours)
    prior_week = state.week_hours.get(week_start, 0.0)
    state.week_hours[week_start] = max(0.0, prior_week - shift_hours)


def _would_violate_gap_closure_rules(
    state: _EmployeeState,
    assignment_date: date,
    template: ShiftTemplateInfo,
    shift_templates: Dict[str, ShiftTemplateInfo],
    rules: JurisdictionRules,
    period_start: date,
    period_end: date,
    availability_blocked: Optional[Mapping[str, Set[date]]] = None,
) -> Optional[str]:
    """Hard-lock feasibility for Autonomous Gap Closure (40h/week, 15h turnaround)."""

    shift_hours = template.duration_minutes / 60.0
    violation = _would_violate_labor_rules(
        state,
        assignment_date,
        template,
        shift_templates,
        rules,
        period_start,
        period_end,
        availability_blocked,
        enforce_fte_target=False,
        relax_dn_contract_completion=False,
        forced_clinical_ot=False,
    )
    if violation:
        return violation

    week_start = workweek_for(assignment_date).start
    week_total = state.week_hours.get(week_start, 0.0) + shift_hours
    if week_total > rules.weekly_overtime_threshold_hours + 1e-9:
        return (
            f"would exceed {rules.weekly_overtime_threshold_hours:.0f}h/week hard cap "
            f"({week_total:.1f}h)"
        )

    transitions: List[ShiftTransition] = []
    for day, shift_template_id in state.assignment_records:
        prior_template = shift_templates[shift_template_id]
        start, end = _shift_interval(day, prior_template)
        transitions.append(ShiftTransition(code=prior_template.code, start=start, end=end))
    start, end = _shift_interval(assignment_date, template)
    transitions.append(ShiftTransition(code=template.code, start=start, end=end))
    transitions.sort(key=lambda item: item.start)
    for index in range(1, len(transitions)):
        gap = _hours_between(transitions[index - 1].end, transitions[index].start)
        if gap < 0:
            continue
        if gap < UNION_MIN_TURNAROUND_HOURS - 1e-9:
            return (
                f"would violate 15h turnaround ({gap:.1f}h gap; "
                f"requires {UNION_MIN_TURNAROUND_HOURS:.0f}h)"
            )
    return None


def _identify_union_risk_lines(
    employees: Sequence[EmployeeProfile],
    states: Mapping[str, _EmployeeState],
    *,
    rules: JurisdictionRules,
    weeks_in_period: int,
) -> List[UnionRiskLine]:
    fulltime_target = fulltime_period_contract_hours(
        rules=rules,
        weeks_in_period=weeks_in_period,
    )
    lines: List[UnionRiskLine] = []
    for employee in employees:
        if employee.fte < FULLTIME_FTE_THRESHOLD:
            continue
        state = states.get(employee.id)
        if state is None:
            continue
        if not is_fulltime_contract_deficit(
            employee,
            state.total_hours,
            fulltime_target=fulltime_target,
        ):
            continue
        lines.append(
            UnionRiskLine(
                employee_id=employee.id,
                employee_name=employee.full_name,
                scheduled_hours=state.total_hours,
                target_hours=fulltime_target,
                deficit_hours=round(fulltime_target - state.total_hours, 2),
            )
        )
    return sorted(lines, key=lambda line: line.deficit_hours, reverse=True)


def _collect_unassigned_pool_slots(
    expanded_slots: Sequence[ExpandedScheduleSlot],
    *,
    fill_counts: Mapping[Tuple[date, str, Optional[str]], int],
    shift_templates: Mapping[str, ShiftTemplateInfo],
    filled_smooth_seats: Set[Tuple[date, str, Optional[str], int]],
) -> List[ExpandedScheduleSlot]:
    """Open demand seats available for greedy Union Risk gap closure."""

    pool: List[ExpandedScheduleSlot] = []
    for slot in expanded_slots:
        if _slot_already_filled(slot, fill_counts):
            continue
        if is_smooth_day_balance_pool(slot.role_pool_id):
            smooth_key = (
                slot.assignment_date,
                slot.shift_id,
                slot.required_qual_code,
                slot.seat_index,
            )
            if smooth_key in filled_smooth_seats:
                continue
        pool.append(slot)
    return sorted(
        pool,
        key=lambda slot: (
            0 if shift_templates[slot.shift_id].code == "MORNING" else 1,
            slot.assignment_date,
            slot.shift_id,
            slot.seat_index,
        ),
    )


def _post_assignment_gap_closure_check(
    *,
    employee_id: str,
    employees: Sequence[EmployeeProfile],
    states: Mapping[str, _EmployeeState],
    fill_counts: Mapping[Tuple[date, str, Optional[str]], int],
    shift_templates: Mapping[str, ShiftTemplateInfo],
    expanded_slots: Sequence[ExpandedScheduleSlot],
    period_start: date,
    period_end: date,
) -> bool:
    """Verify a tentative gap-closure assignment preserves immutable clinical floors."""

    if not is_evening_night_clinical_floor_satisfied(
        fill_counts=fill_counts,
        shift_templates=shift_templates,
        period_start=period_start,
        period_end=period_end,
        expanded_slots=expanded_slots,
    ):
        return False

    employee = next((item for item in employees if item.id == employee_id), None)
    state = states.get(employee_id)
    if employee is None or state is None:
        return False
    if employee.fte >= FULLTIME_FTE_THRESHOLD and state.total_hours > state.target_hours + 0.25:
        return False
    return True


def _autonomous_gap_closure(
    result: AutoGenerateResult,
    *,
    rules: JurisdictionRules,
    employees: Sequence[EmployeeProfile],
    shift_templates: Dict[str, ShiftTemplateInfo],
    shift_required_qualifications: Dict[str, Set[str]],
    period_start: date,
    period_end: date,
    weeks_in_period: int,
    employee_target_hours: Optional[Mapping[str, float]],
    availability_blocked: Optional[Mapping[str, Set[date]]],
    qual_codes: Mapping[str, str],
    expanded_slots: Sequence[ExpandedScheduleSlot],
    fill_counts: Dict[Tuple[date, str, Optional[str]], int],
    filled_smooth_seats: Set[Tuple[date, str, Optional[str], int]],
    states: Dict[str, _EmployeeState],
) -> int:
    """
    Sequentially close Union Risk contract deficits using the unassigned pool.

    Each accepted assignment is checked against ComplianceValidator hard locks
    (40h/week, 15h turnaround, immutable Evening/Night floors).
    """

    added = 0
    exhausted_employees: Set[str] = set()

    while True:
        union_risk_lines = _identify_union_risk_lines(
            employees,
            states,
            rules=rules,
            weeks_in_period=weeks_in_period,
        )
        if not union_risk_lines:
            break

        pool = _collect_unassigned_pool_slots(
            expanded_slots,
            fill_counts=fill_counts,
            shift_templates=shift_templates,
            filled_smooth_seats=filled_smooth_seats,
        )
        if not pool:
            break

        progress = False
        for risk_line in union_risk_lines:
            if risk_line.employee_id in exhausted_employees:
                continue

            employee = next(
                (item for item in employees if item.id == risk_line.employee_id),
                None,
            )
            if employee is None:
                exhausted_employees.add(risk_line.employee_id)
                continue

            state = states[risk_line.employee_id]
            assigned_for_line = False
            ranked_pool = sorted(
                pool,
                key=lambda slot: (
                    state.week_hours.get(workweek_for(slot.assignment_date).start, 0.0),
                    0 if shift_templates[slot.shift_id].code == "MORNING" else 1,
                    slot.assignment_date,
                    slot.seat_index,
                ),
            )

            for slot in ranked_pool:
                if _slot_already_filled(slot, fill_counts):
                    continue
                if non_clinical_fill_blocked_until_clinical_floor(
                    slot,
                    fill_counts=fill_counts,
                    expanded_slots=expanded_slots,
                    shift_templates=shift_templates,
                ):
                    continue
                if not _can_assign_clinical_floor_slot(
                    slot,
                    fill_counts,
                    shift_templates=shift_templates,
                    expanded_slots=expanded_slots,
                ):
                    continue

                shift_id = slot.shift_id
                template = shift_templates[shift_id]
                required = shift_required_qualifications.get(shift_id, set())
                if not _is_qualified(employee, required):
                    continue
                if slot.required_qual_code and infer_qual_code(employee, qual_codes=qual_codes) != slot.required_qual_code:
                    continue

                violation = _would_violate_gap_closure_rules(
                    state,
                    slot.assignment_date,
                    template,
                    shift_templates,
                    rules,
                    period_start,
                    period_end,
                    availability_blocked,
                )
                if violation:
                    continue

                shift_hours = template.duration_minutes / 60.0
                assignment = PlannedAssignment(
                    employee_id=employee.id,
                    shift_template_id=shift_id,
                    assignment_date=slot.assignment_date,
                )
                result.assignments.append(assignment)
                _apply_assignment_to_state(
                    state,
                    slot.assignment_date,
                    shift_id,
                    shift_hours,
                    rules=rules,
                )
                seat_key = (slot.assignment_date, shift_id, slot.required_qual_code)
                fill_counts[seat_key] = fill_counts.get(seat_key, 0) + 1
                if is_smooth_day_balance_pool(slot.role_pool_id):
                    filled_smooth_seats.add(
                        (
                            slot.assignment_date,
                            shift_id,
                            slot.required_qual_code,
                            slot.seat_index,
                        )
                    )

                if _post_assignment_gap_closure_check(
                    employee_id=employee.id,
                    employees=employees,
                    states=states,
                    fill_counts=fill_counts,
                    shift_templates=shift_templates,
                    expanded_slots=expanded_slots,
                    period_start=period_start,
                    period_end=period_end,
                ):
                    added += 1
                    progress = True
                    assigned_for_line = True
                    break

                result.assignments.pop()
                _revert_assignment_from_state(
                    state,
                    slot.assignment_date,
                    shift_id,
                    shift_hours,
                )
                fill_counts[seat_key] = max(0, fill_counts.get(seat_key, 0) - 1)
                if is_smooth_day_balance_pool(slot.role_pool_id):
                    filled_smooth_seats.discard(
                        (
                            slot.assignment_date,
                            shift_id,
                            slot.required_qual_code,
                            slot.seat_index,
                        )
                    )

            if not assigned_for_line:
                exhausted_employees.add(risk_line.employee_id)

        if not progress:
            break

    return added


def _write_breakroom_schedule_export(
    result: AutoGenerateResult,
    *,
    employees: Sequence[EmployeeProfile],
    shift_templates: Dict[str, ShiftTemplateInfo],
    period_start: date,
    period_end: date,
    weeks_in_period: int,
    project_root: Optional[Path] = None,
    export_name: str = BREAKROOM_MASTER_EXPORT_NAME,
    aggressive_fill_flags: Optional[Sequence[AggressiveFillFlag]] = None,
    schedule_archetype: str = "STANDARD",
) -> Path:
    from lab_scheduler.scheduling.breakroom_print import generate_breakroom_print_html
    from lab_scheduler.scheduling.portage_template import portage_roster_sort_key
    from lab_scheduler.scheduling.schedule_export import build_schedule_export_rows

    root = project_root or _default_project_root()
    export_path = root / "exports" / export_name
    export_path.parent.mkdir(parents=True, exist_ok=True)

    dates = _daterange(period_start, period_end)
    template_dict = {
        shift_id: {
            "id": shift_id,
            "code": template.code,
            "short": template.code,
            "name": template.name,
        }
        for shift_id, template in shift_templates.items()
    }
    emp_rows = sorted(
        [
            {
                "id": employee.id,
                "full_name": employee.full_name,
                "fte": employee.fte,
                "contract_line_type": employee.contract_line_type or "",
            }
            for employee in employees
        ],
        key=portage_roster_sort_key,
    )
    assignment_rows = [
        {
            "employee_id": assignment.employee_id,
            "assignment_date": assignment.assignment_date,
            "shift_template_id": assignment.shift_template_id,
        }
        for assignment in result.assignments
    ]
    schedule_rows = build_schedule_export_rows(
        emp_rows,
        dates,
        assignment_rows,
        template_dict,
    )
    html = generate_breakroom_print_html(
        facility_name="Northstar Medical Laboratory",
        period_name="Summer 2026 Master Rotation",
        period_start=period_start,
        period_end=period_end,
        week_count=weeks_in_period,
        employees=emp_rows,
        dates=dates,
        schedule_rows=schedule_rows,
        compliance_verified_on=date.today(),
        aggressive_fill_flags=list(aggressive_fill_flags or ()),
        schedule_archetype=schedule_archetype,
    )
    export_path.write_text(html, encoding="utf-8")
    return export_path


def _finalize_deterministic_validation(
    result: AutoGenerateResult,
    *,
    rules: JurisdictionRules,
    employees: Sequence[EmployeeProfile],
    shift_templates: Dict[str, ShiftTemplateInfo],
    shift_required_qualifications: Dict[str, Set[str]],
    period_start: date,
    period_end: date,
    weeks_in_period: int,
    employee_target_hours: Optional[Mapping[str, float]],
    expanded_slots: Sequence[ExpandedScheduleSlot],
    fill_counts: Mapping[Tuple[date, str, Optional[str]], int],
    states: Mapping[str, _EmployeeState],
    require_master_compliance: bool = False,
    coverage_aggressor_mode: bool = False,
    strict_complete_block: bool = True,
) -> None:
    gaps = _collect_clinical_gap_reports(
        expanded_slots=expanded_slots,
        fill_counts=fill_counts,
        shift_templates=shift_templates,
        period_start=period_start,
        period_end=period_end,
    )
    if gaps:
        result.deterministic_status = "FAILURE" if not coverage_aggressor_mode else "AGGRESSOR_GENERATED"
        result.clinical_gap_reports = gaps
        if (
            not coverage_aggressor_mode
            and not getattr(result, "compliance_first", False)
            and strict_complete_block
        ):
            first = gaps[0]
            raise DeterministicScheduleFailure(
                result,
                f"Clinical Gap on {first.assignment_date.isoformat()} "
                f"({first.shift_code}): {first.reason}",
            )

    demand_ok = is_demand_satisfied(
        result.assignments,
        shift_templates=shift_templates,
        period_start=period_start,
        period_end=period_end,
        fill_counts=fill_counts,
        expanded_slots=expanded_slots,
    )
    if (
        not demand_ok
        and not coverage_aggressor_mode
        and not getattr(result, "compliance_first", False)
        and strict_complete_block
    ):
        result.deterministic_status = "FAILURE"
        raise DeterministicScheduleFailure(
            result,
            "Demand matrix not 100% satisfied after Mandatory Injection sequence",
        )

    if coverage_aggressor_mode:
        from lab_scheduler.audit.compliance import (
            ComplianceValidator,
            build_overtime_compliance_bypass_conflicts,
            write_conflict_report,
        )

        result.assignments = _annotate_clinical_floor_stretches(
            result.assignments,
            shift_templates,
        )

        gap_messages = [
            f"{gap.assignment_date.isoformat()} {gap.shift_code}: {gap.reason}"
            for gap in result.clinical_gap_reports
        ]
        result.aggressive_fill_flags = collect_aggressive_fill_flags(
            assignments=result.assignments,
            employees=employees,
            shift_templates=shift_templates,
            rules=rules,
            period_start=period_start,
            period_end=period_end,
            weeks_in_period=weeks_in_period,
            employee_target_hours=employee_target_hours,
            fill_counts=fill_counts,
            expanded_slots=expanded_slots,
            clinical_gap_messages=gap_messages,
            scheduled_shifts_from_assignments=_scheduled_shifts_from_assignments,
        )
        overtime_bypasses = build_overtime_compliance_bypass_conflicts(
            result.assignments,
            employees=employees,
            shift_templates=shift_templates,
        )
        validator = ComplianceValidator()
        validation = validator.validate(
            rules=rules,
            employees=employees,
            assignments=_scheduled_shifts_from_assignments(result.assignments, employees),
            shift_templates=shift_templates,
            period_start=period_start,
            period_end=period_end,
            weeks_in_period=weeks_in_period,
            employee_target_hours=employee_target_hours,
            fill_counts=fill_counts,
            expanded_slots=expanded_slots,
            enforce_clinical_floors=True,
            require_contract_fte=True,
        )
        validation.report_path = write_conflict_report(
            validator.project_root,
            validation,
            period_start=period_start,
            period_end=period_end,
            week_count=weeks_in_period,
            overtime_compliance_bypassed=overtime_bypasses,
        )
        result.compliance_validation = validation
        if validation.report_path is not None:
            result.conflict_report_path = str(validation.report_path)
        result.coverage_aggressor_mode = True
        result.deterministic_status = "AGGRESSOR_GENERATED"
        export_path = _write_breakroom_schedule_export(
            result,
            employees=employees,
            shift_templates=shift_templates,
            period_start=period_start,
            period_end=period_end,
            weeks_in_period=weeks_in_period,
            aggressive_fill_flags=result.aggressive_fill_flags,
            schedule_archetype=result.schedule_archetype,
        )
        result.breakroom_export_path = str(export_path)
        return

    try:
        if (
            require_master_compliance
            and not getattr(result, "compliance_first", False)
            and strict_complete_block
        ):
            _run_master_compliance_gate(
                result,
                rules=rules,
                employees=employees,
                shift_templates=shift_templates,
                period_start=period_start,
                period_end=period_end,
                weeks_in_period=weeks_in_period,
                employee_target_hours=employee_target_hours,
                fill_counts=fill_counts,
                expanded_slots=expanded_slots,
            )
            result.deterministic_status = (
                "PROVISIONAL" if result.requires_provisional_approval else "SUCCESS"
            )
            export_path = _write_breakroom_schedule_export(
                result,
                employees=employees,
                shift_templates=shift_templates,
                period_start=period_start,
                period_end=period_end,
                weeks_in_period=weeks_in_period,
                schedule_archetype=result.schedule_archetype,
            )
            result.breakroom_export_path = str(export_path)
        elif getattr(result, "compliance_first", False):
            result.deterministic_status = "COMPLIANCE_FIRST"
        else:
            result.deterministic_status = "GENERATED"
    except RuntimeError as exc:
        result.deterministic_status = "FAILURE"
        if strict_complete_block:
            raise DeterministicScheduleFailure(
                result,
                f"Master Schedule compliance gate failed: {exc}",
            ) from exc


def _vacant_portage_employee_ids(employees: Sequence[EmployeeProfile]) -> Set[str]:
    return {
        employee.id
        for employee in employees
        if parse_vacant_portage_line(employee.full_name) is not None
    }


def _prepare_vacant_lines_for_cpsat_fill(
    result: AutoGenerateResult,
    states: Dict[str, _EmployeeState],
    *,
    employees: Sequence[EmployeeProfile],
    target_hours_map: Mapping[str, float],
    shift_templates: Dict[str, ShiftTemplateInfo],
    rules: JurisdictionRules,
) -> int:
    """
    Reset vacant-line employee state from their current assignments before CP-SAT.

    Preserves upstream Portage master-rotation preassignments so vacant full-time
    lines keep the same fair rotation as named staff; CP-SAT only fills remaining
    open calendar days up to target_hours.
    """

    vacant_ids = _vacant_portage_employee_ids(employees)
    if not vacant_ids:
        return 0

    for employee in employees:
        if employee.id not in vacant_ids:
            continue
        states[employee.id] = _EmployeeState(
            profile=employee,
            target_hours=target_hours_map[employee.id],
        )

    for assignment in result.assignments:
        if assignment.employee_id not in vacant_ids:
            continue
        template = shift_templates.get(assignment.shift_template_id)
        if template is None:
            continue
        _apply_assignment_to_state(
            states[assignment.employee_id],
            assignment.assignment_date,
            assignment.shift_template_id,
            template.duration_minutes / 60.0,
            rules=rules,
        )
    return 0


def _run_cpsat_vacant_fill_pass(
    *,
    result: AutoGenerateResult,
    states: Dict[str, _EmployeeState],
    rules: JurisdictionRules,
    period_start: date,
    period_end: date,
    weeks_in_period: int,
    employees: Sequence[EmployeeProfile],
    shift_templates: Dict[str, ShiftTemplateInfo],
    target_hours_map: Mapping[str, float],
    availability_blocked: Optional[Mapping[str, Set[date]]],
    qual_codes: Mapping[str, str],
    fairness_weight_scale: float = 1.0,
    time_limit_seconds: float = 0.0,
) -> Tuple[int, List[PlannedAssignment], "CpSatFillResult"]:
    """Fill vacant-line open cells with OR-Tools CP-SAT (no greedy loops)."""

    from lab_scheduler.scheduling.fairness_thresholds import CPSAT_PRIMARY_TIME_LIMIT_SECONDS
    from lab_scheduler.solver.cpsat_fill import CpSatFillResult, solve_vacant_unassigned_slots

    if time_limit_seconds <= 0:
        time_limit_seconds = CPSAT_PRIMARY_TIME_LIMIT_SECONDS

    _prepare_vacant_lines_for_cpsat_fill(
        result,
        states,
        employees=employees,
        target_hours_map=target_hours_map,
        shift_templates=shift_templates,
        rules=rules,
    )

    cpsat_catalog_target_hours = apply_catalog_targets_for_vacant_master_lines(
        employees,
        target_hours_map,
        rules=rules,
        weeks_in_period=weeks_in_period,
        period_start=period_start,
        period_end=period_end,
    )

    fill_result = solve_vacant_unassigned_slots(
        rules=rules,
        period_start=period_start,
        period_end=period_end,
        weeks_in_period=weeks_in_period,
        employees=employees,
        shift_templates=shift_templates,
        fixed_assignments=result.assignments,
        employee_target_hours=target_hours_map,
        catalog_target_hours=cpsat_catalog_target_hours,
        availability_blocked=availability_blocked,
        qual_codes=qual_codes,
        fairness_weight_scale=fairness_weight_scale,
        time_limit_seconds=time_limit_seconds,
        compliance_first=True,
    )
    solver_ok = fill_result.status in {"OPTIMAL", "FEASIBLE"}
    added = 0
    new_assignments: List[PlannedAssignment] = []
    if not solver_ok:
        if fill_result.status:
            result.deterministic_status = fill_result.status
        return added, new_assignments, fill_result
    for assignment in fill_result.assignments:
        template = shift_templates[assignment.shift_template_id]
        state = states[assignment.employee_id]
        _apply_assignment_to_state(
            state,
            assignment.assignment_date,
            assignment.shift_template_id,
            template.duration_minutes / 60.0,
            rules=rules,
        )
        result.assignments.append(assignment)
        new_assignments.append(assignment)
        added += 1
    if fill_result.status:
        result.deterministic_status = fill_result.status
    result.shift_equity_metrics = dict(fill_result.shift_equity_metrics)
    return added, new_assignments, fill_result


def _rollback_cpsat_assignments(
    result: AutoGenerateResult,
    states: Dict[str, _EmployeeState],
    cpsat_assignments: Sequence[PlannedAssignment],
    *,
    employees: Sequence[EmployeeProfile],
    target_hours_map: Mapping[str, float],
    shift_templates: Dict[str, ShiftTemplateInfo],
    rules: JurisdictionRules,
) -> None:
    if not cpsat_assignments:
        return
    remove_keys = {
        (item.employee_id, item.assignment_date, item.shift_template_id)
        for item in cpsat_assignments
    }
    result.assignments = [
        assignment
        for assignment in result.assignments
        if (
            assignment.employee_id,
            assignment.assignment_date,
            assignment.shift_template_id,
        )
        not in remove_keys
    ]
    _prepare_vacant_lines_for_cpsat_fill(
        result,
        states,
        employees=employees,
        target_hours_map=target_hours_map,
        shift_templates=shift_templates,
        rules=rules,
    )


def _shift_templates_for_fairness_report(
    shift_templates: Mapping[str, ShiftTemplateInfo],
) -> Dict[str, Dict[str, object]]:
    return {
        template_id: {
            "code": template.code,
            "name": template.name,
            "start_time": template.start_time,
            "end_time": template.end_time,
            "duration_minutes": template.duration_minutes,
            "crosses_midnight": template.crosses_midnight,
        }
        for template_id, template in shift_templates.items()
    }


def _build_generation_fairness_report(
    *,
    employees: Sequence[EmployeeProfile],
    assignments: Sequence[PlannedAssignment],
    shift_templates: Mapping[str, ShiftTemplateInfo],
    target_hours_map: Mapping[str, float],
    qual_codes: Mapping[str, str],
    period_start: date,
    period_end: date,
    period_name: str = "Schedule Block",
    tenant_name: str = "Facility",
) -> object:
    from lab_scheduler.validation.staff_fairness_report import build_staff_fairness_report

    employee_rows = [
        {
            "id": employee.id,
            "full_name": employee.full_name,
            "fte": employee.fte,
            "contract_line_type": employee.contract_line_type,
        }
        for employee in employees
    ]
    assignment_rows = [
        {
            "employee_id": assignment.employee_id,
            "shift_template_id": assignment.shift_template_id,
            "assignment_date": assignment.assignment_date,
        }
        for assignment in assignments
    ]
    return build_staff_fairness_report(
        tenant_name=tenant_name,
        period_name=period_name,
        period_start=period_start,
        period_end=period_end,
        employees=employee_rows,
        assignments=assignment_rows,
        shift_templates=_shift_templates_for_fairness_report(shift_templates),
        target_hours=dict(target_hours_map),
        qual_lookup=dict(qual_codes),
    )


def _fairness_rerun_warranted(fill_result: object, report: object) -> bool:
    """Return True when a second CP-SAT pass can improve solver-addressable fairness."""

    from lab_scheduler.scheduling.fairness_thresholds import SOLVER_ADDRESSABLE_FAIRNESS_CODES

    evening_slack = int(getattr(fill_result, "evening_cluster_slack_total", 0) or 0)
    post_night_slack = int(getattr(fill_result, "post_night_recovery_slack_total", 0) or 0)
    if evening_slack > 0 or post_night_slack > 0:
        return True

    flags = getattr(report, "flags", ())
    for flag in flags:
        code = getattr(flag, "code", None)
        if code is None and isinstance(flag, dict):
            code = flag.get("code")
        if code in SOLVER_ADDRESSABLE_FAIRNESS_CODES:
            return True
    return False


def _should_run_cpsat_gap_closure(
    *,
    clinical_seats_locked: bool,
    rest_resolved: int,
    coverage_gap_count: int,
) -> bool:
    """Gap-closure CP-SAT only when rest repair opened seats and coverage gaps remain."""

    return clinical_seats_locked and rest_resolved > 0 and coverage_gap_count > 0


def _build_preflight_infeasibility_message(
    *,
    impossible_slots: Set[Tuple[date, str]],
    impossible_tiers: Set[str],
    critical_gaps: Sequence[CriticalClinicalGap],
) -> str:
    parts: list[str] = []
    if impossible_slots:
        parts.append(
            f"Roster preflight: {len(impossible_slots)} shift slot(s) cannot be staffed "
            "with current FTE and qualifications."
        )
    if impossible_tiers:
        parts.append(
            f"{len(impossible_tiers)} roster line(s) lack capacity for contracted coverage."
        )
    for gap in critical_gaps[:3]:
        parts.append(
            f"Clinical floor gap {gap.assignment_date.isoformat()} "
            f"{gap.shift_code} {gap.seat_label}: {gap.reason}."
        )
    if not parts:
        parts.append(
            "Evening/Night clinical floor (2 seats per day) cannot be satisfied before CP-SAT."
        )
    return " ".join(parts)


def _run_cpsat_vacant_fill_with_fairness_rerun(
    *,
    result: AutoGenerateResult,
    states: Dict[str, _EmployeeState],
    rules: JurisdictionRules,
    period_start: date,
    period_end: date,
    weeks_in_period: int,
    employees: Sequence[EmployeeProfile],
    shift_templates: Dict[str, ShiftTemplateInfo],
    target_hours_map: Mapping[str, float],
    availability_blocked: Optional[Mapping[str, Set[date]]],
    qual_codes: Mapping[str, str],
    progress_callback: Optional[Callable[[str], None]] = None,
    enable_fairness_rerun: bool = True,
    fairness_weight_scale: float | None = None,
    fairness_weights: Optional["FairnessWeights"] = None,
) -> int:
    """
    CP-SAT vacant fill with one fairness-weight rerun when pass-1 slack or
    addressable fairness flags warrant a second solve.
    """

    from lab_scheduler.scheduling.fairness_thresholds import (
        CPSAT_FAIRNESS_RERUN_TIME_LIMIT_SECONDS,
        CPSAT_INTERACTIVE_PORTAGE_PRIMARY_TIME_LIMIT_SECONDS,
        CPSAT_PRIMARY_TIME_LIMIT_SECONDS,
        FAIRNESS_RERUN_WEIGHT_SCALE,
    )
    from lab_scheduler.scheduling.equitability_score import FairnessWeights

    weights = fairness_weights or FairnessWeights()
    primary_scale = (
        fairness_weight_scale
        if fairness_weight_scale is not None
        else weights.fairness_weight_scale()
    )
    rerun_scale = primary_scale * FAIRNESS_RERUN_WEIGHT_SCALE

    primary_limit = (
        CPSAT_INTERACTIVE_PORTAGE_PRIMARY_TIME_LIMIT_SECONDS
        if not enable_fairness_rerun
        else CPSAT_PRIMARY_TIME_LIMIT_SECONDS
    )
    if progress_callback is not None:
        progress_callback(f"CP-SAT vacant fill (up to {int(primary_limit)}s)…")

    added, cpsat_assignments, pass1_fill = _run_cpsat_vacant_fill_pass(
        result=result,
        states=states,
        rules=rules,
        period_start=period_start,
        period_end=period_end,
        weeks_in_period=weeks_in_period,
        employees=employees,
        shift_templates=shift_templates,
        target_hours_map=target_hours_map,
        availability_blocked=availability_blocked,
        qual_codes=qual_codes,
        fairness_weight_scale=primary_scale,
        time_limit_seconds=primary_limit,
    )
    report = _build_generation_fairness_report(
        employees=employees,
        assignments=result.assignments,
        shift_templates=shift_templates,
        target_hours_map=target_hours_map,
        qual_codes=qual_codes,
        period_start=period_start,
        period_end=period_end,
    )
    result.staff_fairness_report = report.to_dict()
    result.fairness_rerun_count = 0
    if not enable_fairness_rerun or not _fairness_rerun_warranted(pass1_fill, report):
        return added

    if progress_callback is not None:
        progress_callback("Improving staff fairness…")

    _rollback_cpsat_assignments(
        result,
        states,
        cpsat_assignments,
        employees=employees,
        target_hours_map=target_hours_map,
        shift_templates=shift_templates,
        rules=rules,
    )
    added, _second_pass, _pass2_fill = _run_cpsat_vacant_fill_pass(
        result=result,
        states=states,
        rules=rules,
        period_start=period_start,
        period_end=period_end,
        weeks_in_period=weeks_in_period,
        employees=employees,
        shift_templates=shift_templates,
        target_hours_map=target_hours_map,
        availability_blocked=availability_blocked,
        qual_codes=qual_codes,
        fairness_weight_scale=rerun_scale,
        time_limit_seconds=CPSAT_FAIRNESS_RERUN_TIME_LIMIT_SECONDS,
    )
    report_after = _build_generation_fairness_report(
        employees=employees,
        assignments=result.assignments,
        shift_templates=shift_templates,
        target_hours_map=target_hours_map,
        qual_codes=qual_codes,
        period_start=period_start,
        period_end=period_end,
    )
    result.staff_fairness_report = report_after.to_dict()
    result.fairness_rerun_count = 1
    return added


def _auto_generate_deterministic_first(
    *,
    rules: JurisdictionRules,
    period_start: date,
    period_end: date,
    weeks_in_period: int,
    employees: Sequence[EmployeeProfile],
    shift_templates: Dict[str, ShiftTemplateInfo],
    shift_required_qualifications: Dict[str, Set[str]],
    employee_target_hours: Optional[Mapping[str, float]],
    availability_blocked: Optional[Mapping[str, Set[date]]],
    coverage_targets: Optional[Sequence[CoverageTierTarget]],
    expanded_slots: Sequence[ExpandedScheduleSlot],
    impossible_tiers: Set[str],
    qual_codes: Mapping[str, str],
    target_hours_map: Mapping[str, float],
    period_target_hours: Mapping[str, float],
    require_master_compliance: bool = False,
    pool_manager: Optional[ElasticPoolManager] = None,
    load_reference_hours: Optional[Mapping[str, float]] = None,
    capacity_shortfall: Optional[CapacityShortfallAlert] = None,
    coverage_aggressor_mode: bool = False,
    impossible_seats: Optional[Set[Tuple[date, str, Optional[str], int]]] = None,
    impossible_slots: Optional[Set[Tuple[date, str]]] = None,
    strict_complete_block: bool = True,
    emit_triage: bool = False,
    triage_sink: Optional[GenerationTriageSink] = None,
    weekday_staffing_plan: Optional[WeekdayDailyStaffingPlan] = None,
    progress_callback: Optional[Callable[[str], None]] = None,
    enable_fairness_rerun: bool = True,
    portage_scheduling_policy: Optional["PortageSchedulingPolicy"] = None,
    manager_locked_cells: Optional[Set[Tuple[str, date]]] = None,
    fairness_weights: Optional["FairnessWeights"] = None,
) -> AutoGenerateResult:
    """
    Deterministic-First Mandatory Injection with Elastic Bucket Allocation.

    Anchor & Fill sequence:
      L1: Portage master template stamp (night anchors frozen)
      L2: Core coverage delta (clinical E/N floor)
      L3: Equitable CP-SAT fill (score-weighted, no lottery)
      L4: Seal / persist guardrails (anchors immutable)
    """

    from lab_scheduler.scheduling.equitability_score import FairnessWeights

    result = AutoGenerateResult()
    result.manager_locked_cells = set(manager_locked_cells or ())
    weights = fairness_weights or FairnessWeights()
    result.fairness_weights = {
        "hour_deficit": weights.hour_deficit,
        "consecutive_work_penalty": weights.consecutive_work_penalty,
        "isolated_workday_penalty": weights.isolated_workday_penalty,
        "weekend_share": weights.weekend_share,
    }
    result.slots_total = len(expanded_slots)
    result.coverage_aggressor_mode = coverage_aggressor_mode
    result.capacity_shortfall = capacity_shortfall
    from lab_scheduler.scheduling.portage_equity_policy import (
        PortageSchedulingPolicy,
        resolve_portage_scheduling_policy,
    )

    scheduling_policy = portage_scheduling_policy or resolve_portage_scheduling_policy()
    result.portage_scheduling_policy_id = scheduling_policy.id
    result.compliance_first = scheduling_policy.compliance_first
    pipeline_started = time.perf_counter()
    clinical_lock_started = pipeline_started

    def _emit_progress(label: str, *, phase_key: Optional[str] = None, phase_start: Optional[float] = None) -> None:
        if phase_key is not None and phase_start is not None:
            elapsed_ms = (time.perf_counter() - phase_start) * 1000.0
            result.phase_timing_ms[phase_key] = elapsed_ms
        if progress_callback is None:
            return
        if phase_key is not None and phase_key in result.phase_timing_ms:
            elapsed_s = int(result.phase_timing_ms[phase_key] // 1000)
            progress_callback(f"{label} ({elapsed_s}s)…")
        else:
            progress_callback(label)
    if pool_manager is not None:
        result.elastic_pool_staff_count = pool_manager.staff_count()
        result.elastic_average_load_hours = dict(load_reference_hours or {})
    filled_smooth_seats: Set[Tuple[date, str, Optional[str], int]] = set()
    fulltime_target = fulltime_period_contract_hours(
        rules=rules,
        weeks_in_period=weeks_in_period,
    )

    states: Dict[str, _EmployeeState] = {}
    for emp in employees:
        states[emp.id] = _EmployeeState(profile=emp, target_hours=target_hours_map[emp.id])

    # Phase 2: stamp canonical 8-week master rotation FIRST as immutable constants.
    template_assignments, states = _propagate_portage_template(
        rules=rules,
        period_start=period_start,
        period_end=period_end,
        weeks_in_period=weeks_in_period,
        employees=employees,
        shift_templates=shift_templates,
        employee_target_hours=target_hours_map,
        availability_blocked=availability_blocked,
        initial_states=states,
        locked_clinical_fill_counts=None,
        skip_contract_top_up=True,
        pool_manager=pool_manager,
        weekday_staffing_plan=weekday_staffing_plan,
    )
    result.assignments.extend(template_assignments)
    _register_frozen_master_cells(result)
    template_catalog_targets = apply_catalog_targets_for_vacant_master_lines(
        employees,
        target_hours_map,
        rules=rules,
        weeks_in_period=weeks_in_period,
        period_start=period_start,
        period_end=period_end,
    )
    _rebuild_states_from_assignments(states, result.assignments, shift_templates)
    if not _skip_pre_template_clinical_lockdown(employees):
        _enforce_portage_operational_band_caps(
            result.assignments,
            states,
            employees=employees,
            shift_templates=shift_templates,
            qual_codes=qual_codes,
            period_start=period_start,
            period_end=period_end,
            fulltime_target=fulltime_target,
            catalog_targets=template_catalog_targets,
        )
    fill_counts = _seat_fill_counts(result.assignments, employees, qual_codes)

    floor_assignments: List[PlannedAssignment] = []
    if not _skip_pre_template_clinical_lockdown(employees):
        try:
            floor_assignments = _execute_clinical_safety_first_pass(
                employees=employees,
                states=states,
                expanded_slots=expanded_slots,
                shift_templates=shift_templates,
                shift_required_qualifications=shift_required_qualifications,
                rules=rules,
                period_start=period_start,
                period_end=period_end,
                availability_blocked=availability_blocked,
                qual_codes=qual_codes,
                target_hours_map=target_hours_map,
                period_target_hours=period_target_hours,
                prioritize_coverage=True,
                clinical_mandatory=require_master_compliance or coverage_aggressor_mode,
            )
        except (ClinicalShortageError, ImmediateClinicalFailure) as exc:
            if coverage_aggressor_mode:
                result.clinical_gap_reports.append(
                    ClinicalGapReport(
                        assignment_date=exc.assignment_date,
                        shift_code=getattr(exc, "shift_code", "CLINICAL"),
                        required_seats=CLINICAL_FLOOR.get(getattr(exc, "shift_code", ""), 2),
                        filled_seats=0,
                        reason=exc.reason if hasattr(exc, "reason") else str(exc),
                    )
                )
                floor_assignments = []
            else:
                result.deterministic_status = "FAILURE"
                result.clinical_gap_reports = [
                    ClinicalGapReport(
                        assignment_date=exc.assignment_date,
                        shift_code=getattr(exc, "shift_code", "CLINICAL"),
                        required_seats=CLINICAL_FLOOR.get(getattr(exc, "shift_code", ""), 2),
                        filled_seats=0,
                        reason=exc.reason if hasattr(exc, "reason") else str(exc),
                    )
                ]
                if isinstance(exc, ImmediateClinicalFailure):
                    raise
                raise DeterministicScheduleFailure(
                    result,
                    str(exc),
                ) from exc

    result.assignments.extend(floor_assignments)
    fill_counts = _seat_fill_counts(result.assignments, employees, qual_codes)

    preassigned = _preassign_smooth_balance_slots(
        employees=employees,
        states=states,
        expanded_slots=expanded_slots,
        shift_templates=shift_templates,
        shift_required_qualifications=shift_required_qualifications,
        rules=rules,
        period_start=period_start,
        period_end=period_end,
        availability_blocked=availability_blocked,
        qual_codes=qual_codes,
        target_hours_map=target_hours_map,
        period_target_hours=period_target_hours,
        filled_smooth_seats=filled_smooth_seats,
        weekday_staffing_plan=weekday_staffing_plan,
        frozen_master_cells=result.frozen_master_cells,
    )
    result.assignments.extend(preassigned)
    fill_counts = _seat_fill_counts(result.assignments, employees, qual_codes)

    if not _skip_pre_template_clinical_lockdown(employees):
        prune_weekend_assignments_to_cap(
            result.assignments,
            states=states,
            employees=employees,
            qual_codes=qual_codes,
            shift_templates=shift_templates,
            period_start=period_start,
            period_end=period_end,
        )
        fill_counts = _seat_fill_counts(result.assignments, employees, qual_codes)

        clinical_seats_locked = _extend_evening_night_clinical_lockdown(
            result,
            employees=employees,
            states=states,
            expanded_slots=expanded_slots,
            shift_templates=shift_templates,
            shift_required_qualifications=shift_required_qualifications,
            rules=rules,
            fill_counts=fill_counts,
            availability_blocked=availability_blocked,
            qual_codes=qual_codes,
            period_start=period_start,
            period_end=period_end,
            log_critical_gaps=False,
                payroll_targets=target_hours_map,
        catalog_targets=target_hours_map,
    )
        fill_counts = _seat_fill_counts(result.assignments, employees, qual_codes)
    else:
        clinical_seats_locked = False

    fill_counts = _seat_fill_counts(result.assignments, employees, qual_codes)
    clinical_seats_locked = evening_night_clinical_seats_satisfied(
        fill_counts=fill_counts,
        expanded_slots=expanded_slots,
        shift_templates=shift_templates,
        period_start=period_start,
        period_end=period_end,
    )

    _emit_progress("Clinical lock complete", phase_key="clinical_lock_ms", phase_start=clinical_lock_started)

    from lab_scheduler.scheduling.anchor_fill_compiler import (
        compile_core_coverage_delta,
        compile_equitable_fill,
    )

    post_pass_guard = _post_pass_guard_for_result(
        result,
        employees=employees,
        period_start=period_start,
    )
    compile_core_coverage_delta(
        result,
        states=states,
        employees=employees,
        expanded_slots=expanded_slots,
        shift_templates=shift_templates,
        shift_required_qualifications=shift_required_qualifications,
        rules=rules,
        period_start=period_start,
        period_end=period_end,
        availability_blocked=availability_blocked,
        qual_codes=qual_codes,
        catalog_targets=template_catalog_targets,
        target_hours_map=target_hours_map,
        period_target_hours=period_target_hours,
        filled_smooth_seats=filled_smooth_seats,
        post_pass_guard=post_pass_guard,
    )
    cpsat_started = time.perf_counter()

    result.gap_closure_assignments_count = compile_equitable_fill(
        result,
        states=states,
        rules=rules,
        period_start=period_start,
        period_end=period_end,
        weeks_in_period=weeks_in_period,
        employees=employees,
        shift_templates=shift_templates,
        target_hours_map=target_hours_map,
        availability_blocked=availability_blocked,
        qual_codes=qual_codes,
        fairness_weights=weights,
        enable_fairness_rerun=enable_fairness_rerun,
        progress_callback=progress_callback,
    )
    cpsat_catalog_targets = apply_catalog_targets_for_vacant_master_lines(
        employees,
        target_hours_map,
        rules=rules,
        weeks_in_period=weeks_in_period,
        period_start=period_start,
        period_end=period_end,
    )
    _rebuild_states_from_assignments(states, result.assignments, shift_templates)
    if not result.compliance_first:
        _enforce_portage_operational_band_caps(
            result.assignments,
            states,
            employees=employees,
            shift_templates=shift_templates,
            qual_codes=qual_codes,
            period_start=period_start,
            period_end=period_end,
            fulltime_target=fulltime_target,
            catalog_targets=cpsat_catalog_targets,
        )
    fill_counts = _seat_fill_counts(result.assignments, employees, qual_codes)
    post_pass_guard = _post_pass_guard_for_result(
        result,
        employees=employees,
        period_start=period_start,
    )

    deduped_gap_count, clinical_seats_locked = _deduped_coverage_gate_snapshot(
        result.assignments,
        employees=employees,
        expanded_slots=expanded_slots,
        shift_templates=shift_templates,
        qual_codes=qual_codes,
        period_start=period_start,
        period_end=period_end,
    )
    skip_post_cpsat_healing = should_bypass_post_cpsat_healing(
        coverage_gap_count=deduped_gap_count,
        clinical_seats_locked=clinical_seats_locked,
        compliance_first=result.compliance_first,
    )
    result.post_cpsat_healing_skipped = skip_post_cpsat_healing
    _update_slot_fill_metrics(
        result,
        expanded_slots=expanded_slots,
        fill_counts=fill_counts,
        shift_templates=shift_templates,
    )
    if not clinical_seats_locked and not skip_post_cpsat_healing:
        _record_critical_clinical_gaps(
            result,
            collect_critical_clinical_gaps(
                fill_counts=fill_counts,
                expanded_slots=expanded_slots,
                shift_templates=shift_templates,
            ),
            emit_summary=True,
        )

    _emit_progress("CP-SAT fill complete", phase_key="cpsat_primary_ms", phase_start=cpsat_started)

    rest_resolved = 0
    if not skip_post_cpsat_healing:
        healing_started = time.perf_counter()
        morning_locked = _clinical_floor_lock_pass(
            employees=employees,
            states=states,
            expanded_slots=expanded_slots,
            shift_templates=shift_templates,
            shift_required_qualifications=shift_required_qualifications,
            rules=rules,
            period_start=period_start,
            period_end=period_end,
            availability_blocked=availability_blocked,
            qual_codes=qual_codes,
            target_hours_map=target_hours_map,
            period_target_hours=period_target_hours,
            fill_counts=fill_counts,
            filled_smooth_seats=filled_smooth_seats,
            prioritize_coverage=True,
            shift_codes=("MORNING",),
            allow_forced_clinical_ot=True,
            single_pass=True,
            clinical_mandatory=require_master_compliance or coverage_aggressor_mode,
            pool_manager=pool_manager,
            post_pass_guard=post_pass_guard,
            guard_assignments=result.assignments,
        )
        result.assignments.extend(morning_locked)
        fill_counts = _seat_fill_counts(result.assignments, employees, qual_codes)
        _trim_weekend_morning_overfill(
            result.assignments,
            states,
            employees=employees,
            shift_templates=shift_templates,
            qual_codes=qual_codes,
            period_start=period_start,
            period_end=period_end,
        )
        fill_counts = _seat_fill_counts(result.assignments, employees, qual_codes)
        clinical_seats_locked = evening_night_clinical_seats_satisfied(
            fill_counts=fill_counts,
            expanded_slots=expanded_slots,
            shift_templates=shift_templates,
            period_start=period_start,
            period_end=period_end,
        )

        _extend_evening_night_clinical_lockdown(
            result,
            employees=employees,
            states=states,
            expanded_slots=expanded_slots,
            shift_templates=shift_templates,
            shift_required_qualifications=shift_required_qualifications,
            rules=rules,
            fill_counts=fill_counts,
            availability_blocked=availability_blocked,
            qual_codes=qual_codes,
            period_start=period_start,
            period_end=period_end,
            log_critical_gaps=False,
            post_pass_guard=post_pass_guard,
                payroll_targets=target_hours_map,
        catalog_targets=target_hours_map,
    )
        fill_counts = _seat_fill_counts(result.assignments, employees, qual_codes)

        _trim_clinical_band_overfill(
            result.assignments,
            states,
            employees=employees,
            shift_templates=shift_templates,
            qual_codes=qual_codes,
            period_start=period_start,
            period_end=period_end,
            fulltime_target=fulltime_target,
        )
        fill_counts = _seat_fill_counts(result.assignments, employees, qual_codes)

        rest_resolved = _resolve_morning_after_evening_night_violations(
            result.assignments,
            states,
            employees=employees,
            shift_templates=shift_templates,
            shift_required_qualifications=shift_required_qualifications,
            expanded_slots=expanded_slots,
            rules=rules,
            period_start=period_start,
            period_end=period_end,
            availability_blocked=availability_blocked,
            qual_codes=qual_codes,
            target_hours_map=target_hours_map,
            period_target_hours=period_target_hours,
            fill_counts=fill_counts,
        )
        fill_counts = _seat_fill_counts(result.assignments, employees, qual_codes)

        _deterministic_resolve_day_night_transitions(
            result.assignments,
            states,
            employees=employees,
            shift_templates=shift_templates,
            shift_required_qualifications=shift_required_qualifications,
            expanded_slots=expanded_slots,
            rules=rules,
            period_start=period_start,
            period_end=period_end,
            availability_blocked=availability_blocked,
            qual_codes=qual_codes,
            target_hours_map=target_hours_map,
            period_target_hours=period_target_hours,
            fill_counts=fill_counts,
        )
        fill_counts = _seat_fill_counts(result.assignments, employees, qual_codes)
        prune_weekend_assignments_to_cap(
            result.assignments,
            states=states,
            employees=employees,
            qual_codes=qual_codes,
            shift_templates=shift_templates,
            period_start=period_start,
            period_end=period_end,
        )
        fill_counts = _seat_fill_counts(result.assignments, employees, qual_codes)
        result.phase_timing_ms["post_cpsat_healing_ms"] = (
            time.perf_counter() - healing_started
        ) * 1000.0
    else:
        result.phase_timing_ms["post_cpsat_healing_ms"] = 0.0

    if not skip_post_cpsat_healing:
        _trim_weekend_morning_overfill(
            result.assignments,
            states,
            employees=employees,
            shift_templates=shift_templates,
            qual_codes=qual_codes,
            period_start=period_start,
            period_end=period_end,
        )
        prune_weekend_assignments_to_cap(
            result.assignments,
            states=states,
            employees=employees,
            qual_codes=qual_codes,
            shift_templates=shift_templates,
            period_start=period_start,
            period_end=period_end,
        )
        fill_counts = _seat_fill_counts(result.assignments, employees, qual_codes)

    _update_slot_fill_metrics(
        result,
        expanded_slots=expanded_slots,
        fill_counts=fill_counts,
        shift_templates=shift_templates,
    )
    if (
        not skip_post_cpsat_healing
        and _should_run_cpsat_gap_closure(
            clinical_seats_locked=clinical_seats_locked,
            rest_resolved=rest_resolved,
            coverage_gap_count=result.coverage_gap_count,
        )
    ):
        from lab_scheduler.scheduling.fairness_thresholds import CPSAT_GAP_CLOSURE_TIME_LIMIT_SECONDS

        if progress_callback is not None:
            progress_callback(
                f"Gap-closure CP-SAT ({result.coverage_gap_count} gap(s), "
                f"up to {int(CPSAT_GAP_CLOSURE_TIME_LIMIT_SECONDS)}s)…"
            )
        added, _gap_assignments, _gap_fill = _run_cpsat_vacant_fill_pass(
            result=result,
            states=states,
            rules=rules,
            period_start=period_start,
            period_end=period_end,
            weeks_in_period=weeks_in_period,
            employees=employees,
            shift_templates=shift_templates,
            target_hours_map=target_hours_map,
            availability_blocked=availability_blocked,
            qual_codes=qual_codes,
            time_limit_seconds=CPSAT_GAP_CLOSURE_TIME_LIMIT_SECONDS,
        )
        result.gap_closure_assignments_count += added
        fill_counts = _seat_fill_counts(result.assignments, employees, qual_codes)

    aggressor_started = time.perf_counter()
    if coverage_aggressor_mode and not skip_post_cpsat_healing:
        if clinical_seats_locked:
            clinical_seats_locked = evening_night_clinical_seats_satisfied(
                fill_counts=fill_counts,
                expanded_slots=expanded_slots,
                shift_templates=shift_templates,
                period_start=period_start,
                period_end=period_end,
            )
            aggressor_added = _run_coverage_aggressor_protocol(
                result,
                employees=employees,
                states=states,
                expanded_slots=expanded_slots,
                shift_templates=shift_templates,
                shift_required_qualifications=shift_required_qualifications,
                rules=rules,
                availability_blocked=availability_blocked,
                qual_codes=qual_codes,
                fill_counts=fill_counts,
                filled_smooth_seats=filled_smooth_seats,
                weeks_in_period=weeks_in_period,
                period_start=period_start,
                period_end=period_end,
                allow_contract_and_even_phases=clinical_seats_locked,
                post_pass_guard=post_pass_guard,
            )
            if aggressor_added:
                fill_counts = _seat_fill_counts(result.assignments, employees, qual_codes)

        _trim_clinical_band_overfill(
            result.assignments,
            states,
            employees=employees,
            shift_templates=shift_templates,
            qual_codes=qual_codes,
            period_start=period_start,
            period_end=period_end,
            fulltime_target=fulltime_target,
        )
        fill_counts = _seat_fill_counts(result.assignments, employees, qual_codes)
        _extend_evening_night_clinical_lockdown(
            result,
            employees=employees,
            states=states,
            expanded_slots=expanded_slots,
            shift_templates=shift_templates,
            shift_required_qualifications=shift_required_qualifications,
            rules=rules,
            fill_counts=fill_counts,
            availability_blocked=availability_blocked,
            qual_codes=qual_codes,
            period_start=period_start,
            period_end=period_end,
            log_critical_gaps=False,
            post_pass_guard=post_pass_guard,
                payroll_targets=target_hours_map,
        catalog_targets=target_hours_map,
    )
        fill_counts = _seat_fill_counts(result.assignments, employees, qual_codes)
        prune_weekend_assignments_to_cap(
            result.assignments,
            states=states,
            employees=employees,
            qual_codes=qual_codes,
            shift_templates=shift_templates,
            period_start=period_start,
            period_end=period_end,
        )
        fill_counts = _seat_fill_counts(result.assignments, employees, qual_codes)

        try:
            _enforce_weekend_clinical_floor(
                result.assignments,
                employees=employees,
                qual_codes=qual_codes,
                shift_templates=shift_templates,
                period_start=period_start,
                period_end=period_end,
                states=states,
            )
            fill_counts = _seat_fill_counts(result.assignments, employees, qual_codes)
        except ImmediateClinicalFailure as exc:
            if coverage_aggressor_mode:
                result.clinical_gap_reports.append(
                    ClinicalGapReport(
                        assignment_date=exc.assignment_date,
                        shift_code=exc.shift_code,
                        required_seats=1,
                        filled_seats=0,
                        reason=exc.reason,
                    )
                )
            else:
                result.deterministic_status = "FAILURE"
                result.clinical_gap_reports = [
                    ClinicalGapReport(
                        assignment_date=exc.assignment_date,
                        shift_code=exc.shift_code,
                        required_seats=1,
                        filled_seats=0,
                        reason=exc.reason,
                    )
                ]
                raise

    if coverage_aggressor_mode:
        _emit_progress("Coverage aggressor complete", phase_key="aggressor_ms", phase_start=aggressor_started)
    else:
        result.phase_timing_ms["aggressor_ms"] = 0.0

    _finalize_deterministic_validation(
        result,
        rules=rules,
        employees=employees,
        shift_templates=shift_templates,
        shift_required_qualifications=shift_required_qualifications,
        period_start=period_start,
        period_end=period_end,
        weeks_in_period=weeks_in_period,
        employee_target_hours=employee_target_hours,
        expanded_slots=expanded_slots,
        fill_counts=fill_counts,
        states=states,
        require_master_compliance=require_master_compliance,
        coverage_aggressor_mode=coverage_aggressor_mode,
        strict_complete_block=strict_complete_block,
    )

    _update_slot_fill_metrics(
        result,
        expanded_slots=expanded_slots,
        fill_counts=fill_counts,
        shift_templates=shift_templates,
    )

    if coverage_targets is not None:
        employee_hours = {emp_id: state.total_hours for emp_id, state in states.items()}
        tier_results = evaluate_coverage_tier_results(
            targets=coverage_targets,
            employee_hours=employee_hours,
            rules=rules,
            weeks_in_period=weeks_in_period,
            impossible_tier_ids=impossible_tiers,
            shift_templates=shift_templates,
            period_start=period_start,
            period_end=period_end,
        )
        result.coverage_tier_results = list(tier_results)
        result.coverage_complete = is_schedule_coverage_complete(
            unfilled_coverage_gaps=result.coverage_gap_count,
            tier_results=tier_results,
        )

    if strict_complete_block and not coverage_aggressor_mode and not result.compliance_first:
        _fail_on_remaining_open_slots(
            expanded_slots=expanded_slots,
            fill_counts=fill_counts,
            filled_smooth_seats=filled_smooth_seats,
            impossible_seats=impossible_seats or set(),
            impossible_slots=impossible_slots or set(),
            employees=employees,
            shift_templates=shift_templates,
            shift_required_qualifications=shift_required_qualifications,
            qual_codes=qual_codes,
            prioritize_coverage=True,
            strict_complete_block=strict_complete_block,
            emit_triage=emit_triage,
            triage_sink=triage_sink,
        )
    if triage_sink is not None:
        result.triage_list = list(triage_sink.entries)

    result.phase_timing_ms["total_ms"] = (time.perf_counter() - pipeline_started) * 1000.0
    _emit_progress(
        f"Generate complete ({int(result.phase_timing_ms['total_ms'] // 1000)}s total)",
        phase_key="total_ms",
        phase_start=pipeline_started,
    )

    _finalize_portage_schedule(
        result,
        employees=employees,
        states=states,
        expanded_slots=expanded_slots,
        shift_templates=shift_templates,
        shift_required_qualifications=shift_required_qualifications,
        rules=rules,
        period_start=period_start,
        period_end=period_end,
        weeks_in_period=weeks_in_period,
        availability_blocked=availability_blocked,
        qual_codes=qual_codes,
        target_hours_map=target_hours_map,
        period_target_hours=period_target_hours,
        fulltime_target=fulltime_target,
        filled_smooth_seats=filled_smooth_seats,
    )
    fill_counts = _seat_fill_counts(result.assignments, employees, qual_codes)

    from lab_scheduler.scheduling.auto_pilot import dedupe_planned_assignments
    from lab_scheduler.scheduling.schedule_tallies import shift_band_from_template_code

    template_bands = {
        template_id: shift_band_from_template_code(info.code)
        for template_id, info in shift_templates.items()
    }
    result.assignments = dedupe_planned_assignments(
        result.assignments,
        template_id_to_band=template_bands,
    )
    _rebuild_states_from_assignments(states, result.assignments, shift_templates)

    if result.compliance_first:
        cpsat_catalog_targets = apply_catalog_targets_for_vacant_master_lines(
            employees,
            target_hours_map,
            rules=rules,
            weeks_in_period=weeks_in_period,
            period_start=period_start,
            period_end=period_end,
        )
        _trim_catalog_contract_surplus(
            result.assignments,
            states,
            employees=employees,
            shift_templates=shift_templates,
            catalog_targets=cpsat_catalog_targets,
            period_start=period_start,
            period_end=period_end,
            allow_trim_frozen=True,
            payroll_targets=target_hours_map,
        )
        _rebuild_states_from_assignments(states, result.assignments, shift_templates)
        _compliance_safe_vacant_finalize(
            result,
            employees=employees,
            states=states,
            expanded_slots=expanded_slots,
            shift_templates=shift_templates,
            shift_required_qualifications=shift_required_qualifications,
            rules=rules,
            period_start=period_start,
            period_end=period_end,
            weeks_in_period=weeks_in_period,
            availability_blocked=availability_blocked,
            qual_codes=qual_codes,
            catalog_targets=cpsat_catalog_targets,
            payroll_targets=target_hours_map,
            fulltime_target=fulltime_target,
        )
        for _ in range(12):
            if not _trim_clinical_band_overfill(
                result.assignments,
                states,
                employees=employees,
                shift_templates=shift_templates,
                qual_codes=qual_codes,
                period_start=period_start,
                period_end=period_end,
                fulltime_target=fulltime_target,
                catalog_targets=cpsat_catalog_targets,
                allow_trim_frozen=True,
            ):
                break
        _trim_weekend_morning_overfill(
            result.assignments,
            states,
            employees=employees,
            shift_templates=shift_templates,
            qual_codes=qual_codes,
            period_start=period_start,
            period_end=period_end,
        )
        trim_weekend_daily_qual_over_cap(
            result.assignments,
            states=states,
            employees=employees,
            qual_codes=qual_codes,
            shift_templates=shift_templates,
            period_start=period_start,
            period_end=period_end,
        )
        _trim_portage_day_shift_overfill(
            result.assignments,
            states,
            employees=employees,
            shift_templates=shift_templates,
            qual_codes=qual_codes,
            rules=rules,
            period_start=period_start,
            period_end=period_end,
            availability_blocked=availability_blocked,
            catalog_targets=cpsat_catalog_targets,
        )
        _trim_catalog_contract_surplus(
            result.assignments,
            states,
            employees=employees,
            shift_templates=shift_templates,
            catalog_targets=cpsat_catalog_targets,
            period_start=period_start,
            period_end=period_end,
            allow_trim_frozen=True,
            tolerance=0.0,
        )
        _rebuild_states_from_assignments(states, result.assignments, shift_templates)
        _trim_catalog_contract_surplus(
            result.assignments,
            states,
            employees=employees,
            shift_templates=shift_templates,
            catalog_targets=cpsat_catalog_targets,
            period_start=period_start,
            period_end=period_end,
            allow_trim_frozen=True,
            payroll_targets=target_hours_map,
        )
        _rebuild_states_from_assignments(states, result.assignments, shift_templates)
        if scheduling_policy.id == "clinical_and_hours_first":
            _clinical_first_finalize(
                result,
                states=states,
                employees=employees,
                expanded_slots=expanded_slots,
                shift_templates=shift_templates,
                shift_required_qualifications=shift_required_qualifications,
                rules=rules,
                period_start=period_start,
                period_end=period_end,
                weeks_in_period=weeks_in_period,
                availability_blocked=availability_blocked,
                qual_codes=qual_codes,
                catalog_targets=cpsat_catalog_targets,
                fulltime_target=fulltime_target,
                target_hours_map=target_hours_map,
                period_target_hours=period_target_hours,
                filled_smooth_seats=filled_smooth_seats,
                scheduling_policy=scheduling_policy,
            )
        _seal_portage_generate_result(
            result,
            states=states,
            employees=employees,
            expanded_slots=expanded_slots,
            shift_templates=shift_templates,
            shift_required_qualifications=shift_required_qualifications,
            rules=rules,
            period_start=period_start,
            period_end=period_end,
            weeks_in_period=weeks_in_period,
            availability_blocked=availability_blocked,
            qual_codes=qual_codes,
            catalog_targets=cpsat_catalog_targets,
            target_hours_map=target_hours_map,
            period_target_hours=period_target_hours,
            filled_smooth_seats=filled_smooth_seats,
            coverage_targets=coverage_targets,
            impossible_tier_ids=impossible_tiers,
        )
        return result

    prune_weekend_assignments_to_cap(
        result.assignments,
        states=states,
        employees=employees,
        qual_codes=qual_codes,
        shift_templates=shift_templates,
        period_start=period_start,
        period_end=period_end,
    )

    catalog_targets = apply_catalog_targets_for_vacant_master_lines(
        employees,
        target_hours_map,
        rules=rules,
        weeks_in_period=weeks_in_period,
        period_start=period_start,
        period_end=period_end,
    )
    _rebuild_states_from_assignments(states, result.assignments, shift_templates)
    for _ in range(24):
        if _all_at_contract_finalize_targets(
            employees, states, catalog_targets, target_hours_map
        ):
            break
        rebalanced = _rebalance_catalog_contract_hours(
            employees=employees,
            states=states,
            assignments=result.assignments,
            shift_templates=shift_templates,
            shift_required_qualifications=shift_required_qualifications,
            rules=rules,
            period_start=period_start,
            period_end=period_end,
            availability_blocked=availability_blocked,
            qual_codes=qual_codes,
            catalog_targets=catalog_targets,
        )
        topped_up = _catalog_contract_top_up_pass(
            employees=employees,
            states=states,
            assignments=result.assignments,
            shift_templates=shift_templates,
            rules=rules,
            period_start=period_start,
            period_end=period_end,
            availability_blocked=availability_blocked,
            qual_codes=qual_codes,
            catalog_targets=catalog_targets,
            payroll_targets=target_hours_map,
        )
        if not rebalanced and not topped_up:
            break
        _trim_clinical_band_overfill(
            result.assignments,
            states,
            employees=employees,
            shift_templates=shift_templates,
            qual_codes=qual_codes,
            period_start=period_start,
            period_end=period_end,
            fulltime_target=fulltime_target,
            catalog_targets=catalog_targets,
        )

    _break_portage_work_streaks(
        result.assignments,
        states,
        employees=employees,
        shift_templates=shift_templates,
        shift_required_qualifications=shift_required_qualifications,
        rules=rules,
        period_start=period_start,
        period_end=period_end,
        availability_blocked=availability_blocked,
        qual_codes=qual_codes,
    )

    from lab_scheduler.scheduling.night_streak_corrector import correct_portage_night_streaks

    night_correction = correct_portage_night_streaks(
        result.assignments,
        employees=employees,
        shift_templates=shift_templates,
        shift_required_qualifications=shift_required_qualifications,
        rules=rules,
        period_start=period_start,
        period_end=period_end,
        weeks_in_period=weeks_in_period,
        employee_target_hours=catalog_targets,
        availability_blocked=availability_blocked,
    )
    if night_correction.swaps_applied:
        _rebuild_states_from_assignments(states, result.assignments, shift_templates)

    for _ in range(6):
        _break_portage_work_streaks(
            result.assignments,
            states,
            employees=employees,
            shift_templates=shift_templates,
            shift_required_qualifications=shift_required_qualifications,
            rules=rules,
            period_start=period_start,
            period_end=period_end,
            availability_blocked=availability_blocked,
            qual_codes=qual_codes,
            max_passes=48,
        )
        night_correction = correct_portage_night_streaks(
            result.assignments,
            employees=employees,
            shift_templates=shift_templates,
            shift_required_qualifications=shift_required_qualifications,
            rules=rules,
            period_start=period_start,
            period_end=period_end,
            weeks_in_period=weeks_in_period,
            employee_target_hours=catalog_targets,
            availability_blocked=availability_blocked,
        )
        if not night_correction.swaps_applied:
            break
        _rebuild_states_from_assignments(states, result.assignments, shift_templates)

    for _ in range(12):
        if _all_at_contract_finalize_targets(
            employees, states, catalog_targets, target_hours_map
        ):
            break
        topped_up = _catalog_contract_top_up_pass(
            employees=employees,
            states=states,
            assignments=result.assignments,
            shift_templates=shift_templates,
            rules=rules,
            period_start=period_start,
            period_end=period_end,
            availability_blocked=availability_blocked,
            qual_codes=qual_codes,
            catalog_targets=catalog_targets,
            payroll_targets=target_hours_map,
        )
        if not topped_up:
            break
        _trim_clinical_band_overfill(
            result.assignments,
            states,
            employees=employees,
            shift_templates=shift_templates,
            qual_codes=qual_codes,
            period_start=period_start,
            period_end=period_end,
            fulltime_target=fulltime_target,
            catalog_targets=catalog_targets,
        )

    for _ in range(2):
        _portage_scrub_topup_and_rebuild(
            result,
            states,
            employees=employees,
            expanded_slots=expanded_slots,
            shift_templates=shift_templates,
            shift_required_qualifications=shift_required_qualifications,
            rules=rules,
            period_start=period_start,
            period_end=period_end,
            availability_blocked=availability_blocked,
            qual_codes=qual_codes,
            catalog_targets=catalog_targets,
            payroll_targets=target_hours_map,
            refill_clinical_en=True,
        )
        for _ in range(8):
            if _all_at_contract_finalize_targets(
            employees, states, catalog_targets, target_hours_map
        ):
                break
            rebalanced = _rebalance_catalog_contract_hours(
                employees=employees,
                states=states,
                assignments=result.assignments,
                shift_templates=shift_templates,
                shift_required_qualifications=shift_required_qualifications,
                rules=rules,
                period_start=period_start,
                period_end=period_end,
                availability_blocked=availability_blocked,
                qual_codes=qual_codes,
                catalog_targets=catalog_targets,
            )
            topped_up = _catalog_contract_top_up_pass(
                employees=employees,
                states=states,
                assignments=result.assignments,
                shift_templates=shift_templates,
                rules=rules,
                period_start=period_start,
                period_end=period_end,
                availability_blocked=availability_blocked,
                qual_codes=qual_codes,
                catalog_targets=catalog_targets,
                payroll_targets=target_hours_map,
            )
            if not rebalanced and not topped_up:
                break
        _break_portage_work_streaks(
            result.assignments,
            states,
            employees=employees,
            shift_templates=shift_templates,
            shift_required_qualifications=shift_required_qualifications,
            rules=rules,
            period_start=period_start,
            period_end=period_end,
            availability_blocked=availability_blocked,
            qual_codes=qual_codes,
            max_passes=48,
        )
        correct_portage_night_streaks(
            result.assignments,
            employees=employees,
            shift_templates=shift_templates,
            shift_required_qualifications=shift_required_qualifications,
            rules=rules,
            period_start=period_start,
            period_end=period_end,
            weeks_in_period=weeks_in_period,
            employee_target_hours=catalog_targets,
            availability_blocked=availability_blocked,
        )
        _rebuild_states_from_assignments(states, result.assignments, shift_templates)
        _apply_portage_weekend_pairing_policy(
            result.assignments,
            states,
            employees=employees,
            shift_templates=shift_templates,
            rules=rules,
            period_start=period_start,
            period_end=period_end,
            availability_blocked=availability_blocked,
            qual_codes=qual_codes,
            catalog_targets=catalog_targets,
        )

    _trim_portage_day_shift_overfill(
        result.assignments,
        states,
        employees=employees,
        shift_templates=shift_templates,
        qual_codes=qual_codes,
        rules=rules,
        period_start=period_start,
        period_end=period_end,
        availability_blocked=availability_blocked,
        catalog_targets=catalog_targets,
    )
    for _ in range(8):
        if _all_at_contract_finalize_targets(
            employees, states, catalog_targets, target_hours_map
        ):
            break
        topped_up = _catalog_contract_top_up_pass(
            employees=employees,
            states=states,
            assignments=result.assignments,
            shift_templates=shift_templates,
            rules=rules,
            period_start=period_start,
            period_end=period_end,
            availability_blocked=availability_blocked,
            qual_codes=qual_codes,
            catalog_targets=catalog_targets,
            payroll_targets=target_hours_map,
        )
        if not topped_up:
            break
        _trim_clinical_band_overfill(
            result.assignments,
            states,
            employees=employees,
            shift_templates=shift_templates,
            qual_codes=qual_codes,
            period_start=period_start,
            period_end=period_end,
            fulltime_target=fulltime_target,
            catalog_targets=catalog_targets,
        )
        _trim_portage_day_shift_overfill(
            result.assignments,
            states,
            employees=employees,
            shift_templates=shift_templates,
            qual_codes=qual_codes,
            rules=rules,
            period_start=period_start,
            period_end=period_end,
            availability_blocked=availability_blocked,
            catalog_targets=catalog_targets,
        )

    if scheduling_policy.id != "clinical_and_hours_first":
        for _ in range(2):
            _apply_portage_weekend_pairing_policy(
                result.assignments,
                states,
                employees=employees,
                shift_templates=shift_templates,
                rules=rules,
                period_start=period_start,
                period_end=period_end,
                availability_blocked=availability_blocked,
                qual_codes=qual_codes,
                catalog_targets=catalog_targets,
            )
            fill_counts = _seat_fill_counts(result.assignments, employees, qual_codes)
            _extend_evening_night_clinical_lockdown(
                result,
                employees=employees,
                states=states,
                expanded_slots=expanded_slots,
                shift_templates=shift_templates,
                shift_required_qualifications=shift_required_qualifications,
                rules=rules,
                fill_counts=fill_counts,
                availability_blocked=availability_blocked,
                qual_codes=qual_codes,
                period_start=period_start,
                period_end=period_end,
                log_critical_gaps=False,
                post_pass_guard=post_pass_guard,
                max_rounds=8,
                    payroll_targets=target_hours_map,
        catalog_targets=catalog_targets,
    )
            prune_weekend_assignments_to_cap(
                result.assignments,
                states=states,
                employees=employees,
                qual_codes=qual_codes,
                shift_templates=shift_templates,
                period_start=period_start,
                period_end=period_end,
            )
            _rebuild_states_from_assignments(states, result.assignments, shift_templates)
        _apply_portage_weekend_pairing_policy(
            result.assignments,
            states,
            employees=employees,
            shift_templates=shift_templates,
            rules=rules,
            period_start=period_start,
            period_end=period_end,
            availability_blocked=availability_blocked,
            qual_codes=qual_codes,
            catalog_targets=catalog_targets,
        )
    _trim_portage_day_shift_overfill(
        result.assignments,
        states,
        employees=employees,
        shift_templates=shift_templates,
        qual_codes=qual_codes,
        rules=rules,
        period_start=period_start,
        period_end=period_end,
        availability_blocked=availability_blocked,
        catalog_targets=catalog_targets,
    )
    _rebuild_states_from_assignments(states, result.assignments, shift_templates)
    trim_weekend_daily_qual_over_cap(
        result.assignments,
        states=states,
        employees=employees,
        qual_codes=qual_codes,
        shift_templates=shift_templates,
        period_start=period_start,
        period_end=period_end,
    )
    _rebuild_states_from_assignments(states, result.assignments, shift_templates)

    if scheduling_policy.id == "clinical_and_hours_first":
        _clinical_first_finalize(
            result,
            states=states,
            employees=employees,
            expanded_slots=expanded_slots,
            shift_templates=shift_templates,
            shift_required_qualifications=shift_required_qualifications,
            rules=rules,
            period_start=period_start,
            period_end=period_end,
            weeks_in_period=weeks_in_period,
            availability_blocked=availability_blocked,
            qual_codes=qual_codes,
            catalog_targets=catalog_targets,
            fulltime_target=fulltime_target,
            target_hours_map=target_hours_map,
            period_target_hours=period_target_hours,
            filled_smooth_seats=filled_smooth_seats,
            scheduling_policy=scheduling_policy,
        )

    _seal_portage_generate_result(
        result,
        states=states,
        employees=employees,
        expanded_slots=expanded_slots,
        shift_templates=shift_templates,
        shift_required_qualifications=shift_required_qualifications,
        rules=rules,
        period_start=period_start,
        period_end=period_end,
        weeks_in_period=weeks_in_period,
        availability_blocked=availability_blocked,
        qual_codes=qual_codes,
        catalog_targets=catalog_targets,
        target_hours_map=target_hours_map,
        period_target_hours=period_target_hours,
        filled_smooth_seats=filled_smooth_seats,
        coverage_targets=coverage_targets,
        impossible_tier_ids=impossible_tiers,
    )

    return result


def _generate_standard_schedule(
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
    progress_callback: Optional[Callable[[str], None]] = None,
    enable_fairness_rerun: bool = True,
    portage_scheduling_policy: Optional["PortageSchedulingPolicy"] = None,
    manager_locked_cells: Optional[Set[Tuple[str, date]]] = None,
    fairness_weights: Optional["FairnessWeights"] = None,
) -> AutoGenerateResult:
    """
    Standard ~8h (Portage 7.75h effective) schedule generator. Portage deployments use
    Deterministic-First Mandatory Injection
    (hard clinical floors → template → single-pass contract/pool fill → validation).
    When ``coverage_aggressor_mode`` is True, compliance hard-stop is disabled and
    broken rules are flagged in AGGRESSIVE_FILL_FLAGS on export.
    Non-Portage paths retain legacy coverage balancing.
    """

    result = AutoGenerateResult()
    result.manager_locked_cells = set(manager_locked_cells or ())
    if not employees or not shift_templates:
        return result

    triage_sink = GenerationTriageSink() if emit_triage else None
    strict_raise = strict_complete_block and not emit_triage

    prioritize_coverage = coverage_targets is not None and len(coverage_targets) > 0
    active_demands: Optional[Sequence[ShiftConcurrentDemand]] = concurrent_demands
    if active_demands is None and prioritize_coverage:
        active_demands = portage_concurrent_demands()

    from lab_scheduler.engine.demand import (
        _uses_portage_concurrent_demands,
        filter_portage_operational_shift_templates,
    )

    if active_demands and _uses_portage_concurrent_demands(active_demands):
        shift_templates = filter_portage_operational_shift_templates(shift_templates)
        shift_required_qualifications = {
            shift_id: shift_required_qualifications[shift_id]
            for shift_id in shift_templates
            if shift_id in shift_required_qualifications
        }

    qual_codes = build_qual_code_lookup(employees, shift_required_qualifications)

    impossible_slots: Set[Tuple[date, str]] = set()
    impossible_seats: Set[Tuple[date, str, Optional[str], int]] = set()
    impossible_tiers: Set[str] = set()
    if active_demands:
        impossible_seats, impossible_tiers = assess_concurrent_capacity_shortfall(
            employees=employees,
            shift_templates=shift_templates,
            shift_required_qualifications=shift_required_qualifications,
            period_start=period_start,
            period_end=period_end,
            weeks_in_period=weeks_in_period,
            rules=rules,
            concurrent_demands=active_demands,
            qual_codes=qual_codes,
        )
        impossible_slots = {(d, sid) for d, sid, _qual, _seat in impossible_seats}
    else:
        impossible_slots, impossible_tiers = assess_impossible_coverage_slots(
            employees=employees,
            shift_templates=shift_templates,
            shift_required_qualifications=shift_required_qualifications,
            period_start=period_start,
            period_end=period_end,
            weeks_in_period=weeks_in_period,
            rules=rules,
        )

    states: Dict[str, _EmployeeState] = {}
    target_hours_map = build_solver_target_hours_map(
        employees,
        rules=rules,
        weeks_in_period=weeks_in_period,
        employee_target_hours=employee_target_hours,
    )
    # Payroll (320h FTE) drives hour balance; catalog map is rotation/alt/weekend only.
    catalog_targets = apply_catalog_targets_for_vacant_master_lines(
        employees,
        target_hours_map,
        rules=rules,
        weeks_in_period=weeks_in_period,
        period_start=period_start,
        period_end=period_end,
    )
    for emp in employees:
        states[emp.id] = _EmployeeState(profile=emp, target_hours=target_hours_map[emp.id])

    expanded_slots: List[ExpandedScheduleSlot] = []
    if prioritize_coverage and active_demands:
        balance_plan = AutonomousDemandBalancer(
            period_start=period_start,
            period_end=period_end,
            shift_templates=shift_templates,
            concurrent_demands=active_demands,
            employees=employees,
            rules=rules,
            weeks_in_period=weeks_in_period,
        ).reconcile()
        expanded_slots = expand_schedule_slots(
            period_start=period_start,
            period_end=period_end,
            shift_templates=shift_templates,
            concurrent_demands=active_demands,
            supplemental_balance_slots=balance_plan.balance_slots,
        )
    else:
        expanded_slots = expand_schedule_slots(
            period_start=period_start,
            period_end=period_end,
            shift_templates=shift_templates,
            concurrent_demands=active_demands,
            employees=employees if employee_target_hours else None,
            rules=rules,
            weeks_in_period=weeks_in_period,
        )

    weekday_staffing_plan = compute_weekday_daily_staffing_plan(
        employees,
        period_start=period_start,
        period_end=period_end,
        standard_weekly_hours=rules.standard_hours_per_week_at_1_0_fte,
        weeks_in_period=weeks_in_period,
        shift_hours=morning_shift_hours(shift_templates),
    )

    target_hours_map = {
        emp_id: state.target_hours for emp_id, state in states.items()
    }

    period_target_hours: Dict[str, float] = {}
    if prioritize_coverage and coverage_targets is not None:
        period_target_hours = compute_period_target_hours_map(
            coverage_targets,
            shift_templates=shift_templates,
            period_start=period_start,
            period_end=period_end,
        )

    pool_manager: Optional[ElasticPoolManager] = None
    load_reference_hours: Dict[str, float] = {}
    capacity_shortfall: Optional[CapacityShortfallAlert] = None
    elastic_portage = (
        prioritize_coverage
        and _portage_clinical_safety_first_enabled(employees, active_demands)
    )
    if elastic_portage:
        pool_manager = ElasticPoolManager.from_employees(employees, qual_codes=qual_codes)
        load_reference_hours = pool_manager.load_reference_hours_map(
            rules=rules,
            weeks_in_period=weeks_in_period,
        )
        target_hours_map = build_elastic_target_hours_map(
            pool_manager,
            rules=rules,
            weeks_in_period=weeks_in_period,
        )
        catalog_targets = apply_catalog_targets_for_vacant_master_lines(
            employees,
            target_hours_map,
            rules=rules,
            weeks_in_period=weeks_in_period,
            period_start=period_start,
            period_end=period_end,
        )
        period_target_hours = dict(load_reference_hours)
        for emp in employees:
            states[emp.id] = _EmployeeState(profile=emp, target_hours=target_hours_map[emp.id])
        capacity_shortfall = assess_elastic_capacity_shortfall(
            pool_manager,
            expanded_slots,
            shift_templates,
            rules=rules,
            weeks_in_period=weeks_in_period,
        )

    if elastic_portage:
        return _auto_generate_deterministic_first(
            rules=rules,
            period_start=period_start,
            period_end=period_end,
            weeks_in_period=weeks_in_period,
            employees=employees,
            shift_templates=shift_templates,
            shift_required_qualifications=shift_required_qualifications,
            employee_target_hours=employee_target_hours,
            availability_blocked=availability_blocked,
            coverage_targets=coverage_targets,
            expanded_slots=expanded_slots,
            impossible_tiers=impossible_tiers,
            qual_codes=qual_codes,
            target_hours_map=target_hours_map,
            period_target_hours=period_target_hours,
            require_master_compliance=require_master_compliance,
            pool_manager=pool_manager,
            load_reference_hours=load_reference_hours,
            capacity_shortfall=capacity_shortfall,
            coverage_aggressor_mode=coverage_aggressor_mode,
            impossible_seats=impossible_seats,
            impossible_slots=impossible_slots,
            strict_complete_block=strict_complete_block,
            emit_triage=emit_triage,
            triage_sink=triage_sink,
            weekday_staffing_plan=weekday_staffing_plan,
            progress_callback=progress_callback,
            enable_fairness_rerun=enable_fairness_rerun,
            portage_scheduling_policy=portage_scheduling_policy,
            manager_locked_cells=manager_locked_cells,
            fairness_weights=fairness_weights,
        )
    elif prioritize_coverage:
        filled_smooth_seats = set()
        template_assignments, states = _propagate_portage_template(
            rules=rules,
            period_start=period_start,
            period_end=period_end,
            weeks_in_period=weeks_in_period,
            employees=employees,
            shift_templates=shift_templates,
            employee_target_hours=target_hours_map,
            availability_blocked=availability_blocked,
            weekday_staffing_plan=weekday_staffing_plan,
        )
        _rebalance_weekday_morning_assignments(
            template_assignments,
            states=states,
            shift_templates=shift_templates,
            rules=rules,
            period_start=period_start,
            period_end=period_end,
            availability_blocked=availability_blocked,
            weekday_staffing_plan=weekday_staffing_plan,
        )
        result.assignments.extend(template_assignments)
        template_catalog_targets = apply_catalog_targets_for_vacant_master_lines(
            employees,
            target_hours_map,
            rules=rules,
            weeks_in_period=weeks_in_period,
            period_start=period_start,
            period_end=period_end,
        )
        _rebuild_states_from_assignments(states, result.assignments, shift_templates)
        fulltime_target_early = fulltime_period_contract_hours(
            rules=rules,
            weeks_in_period=weeks_in_period,
        )
        _enforce_portage_operational_band_caps(
            result.assignments,
            states,
            employees=employees,
            shift_templates=shift_templates,
            qual_codes=qual_codes,
            period_start=period_start,
            period_end=period_end,
            fulltime_target=fulltime_target_early,
            catalog_targets=template_catalog_targets,
        )
    else:
        filled_smooth_seats = set()

    result.slots_total = len(expanded_slots)

    def eligible_count(slot: ExpandedScheduleSlot) -> int:
        required = shift_required_qualifications.get(slot.shift_id, set())
        return sum(
            1
            for emp in employees
            if _is_qualified(emp, required)
            and employee_matches_seat_qual(
                emp,
                slot.required_qual_code,
                qual_codes=qual_codes,
                shift_required_qualification_ids=required or None,
            )
        )

    fill_counts = _seat_fill_counts(result.assignments, employees, qual_codes)
    result.slots_filled = sum(
        1 for slot in expanded_slots if _slot_already_filled(slot, fill_counts)
    )

    expanded_slots.sort(
        key=lambda slot: (
            clinical_demand_slot_sort_key(
                slot,
                shift_templates=shift_templates,
                fill_counts=fill_counts,
                expanded_slots=expanded_slots,
            ),
            eligible_count(slot),
        )
    )

    fulltime_target = fulltime_period_contract_hours(
        rules=rules,
        weeks_in_period=weeks_in_period,
    )

    if prioritize_coverage and _portage_clinical_safety_first_enabled(employees, active_demands):
        for _clinical_attempt in range(8):
            if is_evening_night_clinical_floor_satisfied(
                fill_counts=fill_counts,
                shift_templates=shift_templates,
                period_start=period_start,
                period_end=period_end,
                expanded_slots=expanded_slots,
            ):
                break
            locked = _clinical_floor_lock_pass(
                employees=employees,
                states=states,
                expanded_slots=expanded_slots,
                shift_templates=shift_templates,
                shift_required_qualifications=shift_required_qualifications,
                rules=rules,
                period_start=period_start,
                period_end=period_end,
                availability_blocked=availability_blocked,
                qual_codes=qual_codes,
                target_hours_map=target_hours_map,
                period_target_hours=period_target_hours,
                fill_counts=fill_counts,
                filled_smooth_seats=filled_smooth_seats,
                prioritize_coverage=prioritize_coverage,
                shift_codes=("EVENING", "NIGHT"),
                allow_forced_clinical_ot=True,
            )
            result.assignments.extend(locked)
            fill_counts = _seat_fill_counts(result.assignments, employees, qual_codes)
            if not locked:
                break

        hard_demand_met = is_evening_night_clinical_floor_satisfied(
            fill_counts=fill_counts,
            shift_templates=shift_templates,
            period_start=period_start,
            period_end=period_end,
            expanded_slots=expanded_slots,
        )
    elif prioritize_coverage:
        hard_demand_met = True
    else:
        hard_demand_met = True

    if prioritize_coverage and hard_demand_met:
        contract_first = _mandatory_fulltime_contract_pass(
            employees=employees,
            states=states,
            expanded_slots=expanded_slots,
            shift_templates=shift_templates,
            shift_required_qualifications=shift_required_qualifications,
            rules=rules,
            period_start=period_start,
            period_end=period_end,
            weeks_in_period=weeks_in_period,
            availability_blocked=availability_blocked,
            qual_codes=qual_codes,
            fill_counts=fill_counts,
            filled_smooth_seats=filled_smooth_seats,
            prioritize_coverage=prioritize_coverage,
        )
        result.assignments.extend(contract_first)
        result.slots_filled = sum(
            1 for slot in expanded_slots if _slot_already_filled(slot, fill_counts)
        )

    if prioritize_coverage and hard_demand_met:
        for _contract_round in range(8):
            if _all_fulltime_at_contract_target(
                employees,
                states,
                fulltime_target=fulltime_target,
            ):
                break
            round_progress = False
            for slot in expanded_slots:
                if shift_templates[slot.shift_id].code in {"NIGHT", "EVENING"}:
                    continue
                if is_smooth_day_balance_pool(slot.role_pool_id):
                    smooth_key = (
                        slot.assignment_date,
                        slot.shift_id,
                        slot.required_qual_code,
                        slot.seat_index,
                    )
                    if smooth_key in filled_smooth_seats:
                        continue

                if prioritize_coverage and _slot_already_filled(slot, fill_counts):
                    continue

                mandatory = _pick_mandatory_fulltime_candidate(
                    employees=employees,
                    states=states,
                    slot=slot,
                    shift_templates=shift_templates,
                    shift_required_qualifications=shift_required_qualifications,
                    rules=rules,
                    period_start=period_start,
                    period_end=period_end,
                    availability_blocked=availability_blocked,
                    qual_codes=qual_codes,
                    fulltime_target=fulltime_target,
                )
                if mandatory is None:
                    continue

                shift_id = slot.shift_id
                template = shift_templates[shift_id]
                shift_hours = template.duration_minutes / 60.0
                _apply_assignment_to_state(
                    states[mandatory.id],
                    slot.assignment_date,
                    shift_id,
                    shift_hours,
                )
                result.assignments.append(
                    PlannedAssignment(
                        employee_id=mandatory.id,
                        shift_template_id=shift_id,
                        assignment_date=slot.assignment_date,
                    )
                )
                result.slots_filled += 1
                seat_key = (
                    slot.assignment_date,
                    shift_id,
                    slot.required_qual_code,
                    slot.seat_index,
                )
                if is_smooth_day_balance_pool(slot.role_pool_id):
                    filled_smooth_seats.add(seat_key)
                seat_key_count = (slot.assignment_date, shift_id, slot.required_qual_code)
                fill_counts[seat_key_count] = fill_counts.get(seat_key_count, 0) + 1
                round_progress = True

            if not round_progress:
                break

    if prioritize_coverage and hard_demand_met:
        for _rebalance_round in range(6):
            rebalanced = _reassign_parttime_shifts_to_fulltime_contract(
                employees=employees,
                states=states,
                assignments=result.assignments,
                shift_templates=shift_templates,
                shift_required_qualifications=shift_required_qualifications,
                rules=rules,
                period_start=period_start,
                period_end=period_end,
                availability_blocked=availability_blocked,
                qual_codes=qual_codes,
                fulltime_target=fulltime_target,
            )
            rebalanced += _reassign_loaded_fulltime_shifts_to_contract_deficit(
                employees=employees,
                states=states,
                assignments=result.assignments,
                shift_templates=shift_templates,
                shift_required_qualifications=shift_required_qualifications,
                rules=rules,
                period_start=period_start,
                period_end=period_end,
                availability_blocked=availability_blocked,
                qual_codes=qual_codes,
                fulltime_target=fulltime_target,
            )
            if rebalanced == 0:
                break

            extra_contract = _mandatory_fulltime_contract_pass(
                employees=employees,
                states=states,
                expanded_slots=expanded_slots,
                shift_templates=shift_templates,
                shift_required_qualifications=shift_required_qualifications,
                rules=rules,
                period_start=period_start,
                period_end=period_end,
                weeks_in_period=weeks_in_period,
                availability_blocked=availability_blocked,
                qual_codes=qual_codes,
                fill_counts=fill_counts,
                filled_smooth_seats=filled_smooth_seats,
                prioritize_coverage=prioritize_coverage,
            )
            result.assignments.extend(extra_contract)

            for _contract_round in range(4):
                if _all_fulltime_at_contract_target(
                    employees,
                    states,
                    fulltime_target=fulltime_target,
                ):
                    break
                round_progress = False
                for slot in expanded_slots:
                    if shift_templates[slot.shift_id].code in {"NIGHT", "EVENING"}:
                        continue
                    if is_smooth_day_balance_pool(slot.role_pool_id):
                        smooth_key = (
                            slot.assignment_date,
                            slot.shift_id,
                            slot.required_qual_code,
                            slot.seat_index,
                        )
                        if smooth_key in filled_smooth_seats:
                            continue

                    if prioritize_coverage and _slot_already_filled(slot, fill_counts):
                        continue

                    mandatory = _pick_mandatory_fulltime_candidate(
                        employees=employees,
                        states=states,
                        slot=slot,
                        shift_templates=shift_templates,
                        shift_required_qualifications=shift_required_qualifications,
                        rules=rules,
                        period_start=period_start,
                        period_end=period_end,
                        availability_blocked=availability_blocked,
                        qual_codes=qual_codes,
                        fulltime_target=fulltime_target,
                    )
                    if mandatory is None:
                        continue

                    shift_id = slot.shift_id
                    template = shift_templates[shift_id]
                    shift_hours = template.duration_minutes / 60.0
                    _apply_assignment_to_state(
                        states[mandatory.id],
                        slot.assignment_date,
                        shift_id,
                        shift_hours,
                    )
                    result.assignments.append(
                        PlannedAssignment(
                            employee_id=mandatory.id,
                            shift_template_id=shift_id,
                            assignment_date=slot.assignment_date,
                        )
                    )
                    result.slots_filled += 1
                    seat_key = (
                        slot.assignment_date,
                        shift_id,
                        slot.required_qual_code,
                        slot.seat_index,
                    )
                    if is_smooth_day_balance_pool(slot.role_pool_id):
                        filled_smooth_seats.add(seat_key)
                    seat_key_count = (slot.assignment_date, shift_id, slot.required_qual_code)
                    fill_counts[seat_key_count] = fill_counts.get(seat_key_count, 0) + 1
                    round_progress = True

                if not round_progress:
                    break

            if _all_fulltime_at_contract_target(
                employees,
                states,
                fulltime_target=fulltime_target,
            ):
                break

    fill_counts = _seat_fill_counts(result.assignments, employees, qual_codes)
    result.slots_filled = sum(
        1 for slot in expanded_slots if _slot_already_filled(slot, fill_counts)
    )

    contract_complete = _all_fulltime_at_contract_target(
        employees,
        states,
        fulltime_target=fulltime_target,
    )
    hard_demand_met = is_demand_satisfied(
        result.assignments,
        shift_templates=shift_templates,
        period_start=period_start,
        period_end=period_end,
        fill_counts=fill_counts,
        expanded_slots=expanded_slots,
    )

    if prioritize_coverage and hard_demand_met and not contract_complete:
        for _final_pass in range(12):
            if _all_fulltime_at_contract_target(
                employees,
                states,
                fulltime_target=fulltime_target,
            ):
                break
            dn_extra = _backfill_dn_contract_pass(
                employees=employees,
                states=states,
                expanded_slots=expanded_slots,
                shift_templates=shift_templates,
                shift_required_qualifications=shift_required_qualifications,
                rules=rules,
                period_start=period_start,
                period_end=period_end,
                availability_blocked=availability_blocked,
                qual_codes=qual_codes,
                fill_counts=fill_counts,
                fulltime_target=fulltime_target,
                relax_labor_rules=True,
            )
            result.assignments.extend(dn_extra)
            fill_counts = _seat_fill_counts(result.assignments, employees, qual_codes)
            extra_mandatory = _mandatory_fulltime_contract_pass(
                employees=employees,
                states=states,
                expanded_slots=expanded_slots,
                shift_templates=shift_templates,
                shift_required_qualifications=shift_required_qualifications,
                rules=rules,
                period_start=period_start,
                period_end=period_end,
                weeks_in_period=weeks_in_period,
                availability_blocked=availability_blocked,
                qual_codes=qual_codes,
                fill_counts=fill_counts,
                filled_smooth_seats=filled_smooth_seats,
                prioritize_coverage=prioritize_coverage,
                relax_dn_contract_completion=True,
            )
            result.assignments.extend(extra_mandatory)
            fill_counts = _seat_fill_counts(result.assignments, employees, qual_codes)
            repaired = _tail_repair_fulltime_contract(
                employees=employees,
                states=states,
                assignments=result.assignments,
                expanded_slots=expanded_slots,
                shift_templates=shift_templates,
                shift_required_qualifications=shift_required_qualifications,
                rules=rules,
                period_start=period_start,
                period_end=period_end,
                availability_blocked=availability_blocked,
                qual_codes=qual_codes,
                fill_counts=fill_counts,
                filled_smooth_seats=filled_smooth_seats,
                prioritize_coverage=prioritize_coverage,
                fulltime_target=fulltime_target,
            )
            if len(dn_extra) == 0 and len(extra_mandatory) == 0 and repaired == 0:
                break

        contract_complete = _all_fulltime_at_contract_target(
            employees,
            states,
            fulltime_target=fulltime_target,
        )

    equity_allowed = not prioritize_coverage or (contract_complete and hard_demand_met)

    if equity_allowed:
        for slot in expanded_slots:
            if is_smooth_day_balance_pool(slot.role_pool_id):
                smooth_key = (
                    slot.assignment_date,
                    slot.shift_id,
                    slot.required_qual_code,
                    slot.seat_index,
                )
                if smooth_key in filled_smooth_seats:
                    continue

            if prioritize_coverage and _slot_already_filled(slot, fill_counts):
                continue

            if not _can_assign_clinical_floor_slot(
                slot,
                fill_counts,
                shift_templates=shift_templates,
                expanded_slots=expanded_slots,
            ):
                continue

            assignment_date = slot.assignment_date
            shift_id = slot.shift_id
            template = shift_templates[shift_id]
            required = shift_required_qualifications.get(shift_id, set())
            slot_key = (assignment_date, shift_id)
            seat_key = (assignment_date, shift_id, slot.required_qual_code, slot.seat_index)
            slot_is_impossible = seat_key in impossible_seats or slot_key in impossible_slots

            ranked, ineligible_reasons, eligible_ids = _rank_slot_candidates(
                employees=employees,
                required=required,
                states=states,
                assignment_date=assignment_date,
                shift_id=shift_id,
                template=template,
                shift_templates=shift_templates,
                rules=rules,
                period_start=period_start,
                period_end=period_end,
                availability_blocked=availability_blocked,
                prioritize_coverage=prioritize_coverage,
                period_target_hours=period_target_hours,
                required_qual_code=slot.required_qual_code,
                qual_codes=qual_codes,
                employee_target_hours=target_hours_map,
                role_pool_id=slot.role_pool_id,
                fill_counts=fill_counts,
            )

            if not ranked:
                qualified_names = [
                    emp.full_name
                    for emp in employees
                    if _is_qualified(emp, required)
                    and employee_matches_seat_qual(
                        emp,
                        slot.required_qual_code,
                        qual_codes=qual_codes,
                        shift_required_qualification_ids=required or None,
                    )
                ]
                handle_unfillable_slot(
                    triage_sink=triage_sink,
                    strict_raise=strict_raise,
                    assignment_date=assignment_date,
                    shift_code=template.code,
                    shift_id=shift_id,
                    role_pool_id=slot.role_pool_id,
                    seat_index=slot.seat_index,
                    required_qual_code=slot.required_qual_code,
                    shift_hours=template.duration_minutes / 60.0,
                    slot_is_impossible=slot_is_impossible,
                    qualified_staff_exist=bool(qualified_names),
                    constraint_summary=(
                        _summarize_constraint_blocks(ineligible_reasons)
                        if qualified_names
                        else None
                    ),
                    ineligible_reasons=ineligible_reasons,
                )
                continue

            chosen = ranked[0]
            qualified_profiles = [
                emp
                for emp in employees
                if _is_qualified(emp, required)
                and employee_matches_seat_qual(
                    emp,
                    slot.required_qual_code,
                    qual_codes=qual_codes,
                    shift_required_qualification_ids=required or None,
                )
            ]
            bypass = evaluate_seniority_bypass(
                qualified_profiles=qualified_profiles,
                eligible_ids=eligible_ids,
                selected=chosen,
                ineligible_reasons=ineligible_reasons,
            )
            if bypass is not None:
                result.seniority_bypasses.append(
                    SeniorityBypassEvent(
                        assignment_date=assignment_date,
                        shift_template_id=shift_id,
                        shift_code=template.code,
                        selected_employee_id=chosen.id,
                        selected_employee_name=chosen.full_name,
                        bypass=bypass,
                    )
                )

            chosen_id = chosen.id
            shift_hours = template.duration_minutes / 60.0
            _apply_assignment_to_state(
                states[chosen_id],
                assignment_date,
                shift_id,
                shift_hours,
            )

            result.assignments.append(
                PlannedAssignment(
                    employee_id=chosen_id,
                    shift_template_id=shift_id,
                    assignment_date=assignment_date,
                )
            )
            result.slots_filled += 1
            if is_smooth_day_balance_pool(slot.role_pool_id):
                filled_smooth_seats.add(
                    (assignment_date, shift_id, slot.required_qual_code, slot.seat_index)
                )
            seat_key_count = (assignment_date, shift_id, slot.required_qual_code)
            fill_counts[seat_key_count] = fill_counts.get(seat_key_count, 0) + 1

    if prioritize_coverage:
        fill_counts = _seat_fill_counts(result.assignments, employees, qual_codes)
        for _tail_pass in range(12):
            if _all_fulltime_at_contract_target(
                employees,
                states,
                fulltime_target=fulltime_target,
            ):
                break
            repaired = _tail_repair_fulltime_contract(
                employees=employees,
                states=states,
                assignments=result.assignments,
                expanded_slots=expanded_slots,
                shift_templates=shift_templates,
                shift_required_qualifications=shift_required_qualifications,
                rules=rules,
                period_start=period_start,
                period_end=period_end,
                availability_blocked=availability_blocked,
                qual_codes=qual_codes,
                fill_counts=fill_counts,
                filled_smooth_seats=filled_smooth_seats,
                prioritize_coverage=prioritize_coverage,
                fulltime_target=fulltime_target,
            )
            if repaired == 0:
                break
            fill_counts = _seat_fill_counts(result.assignments, employees, qual_codes)

        if _portage_clinical_safety_first_enabled(employees, active_demands):
            fill_counts = _seat_fill_counts(result.assignments, employees, qual_codes)
            for shift_codes in (("EVENING", "NIGHT"), ("MORNING",)):
                for _ in range(8):
                    if all(
                        clinical_floor_filled_for_day(
                            assignment_date,
                            shift_code,
                            fill_counts=fill_counts,
                            expanded_slots=expanded_slots,
                            shift_templates=shift_templates,
                        )
                        == CLINICAL_FLOOR[shift_code]
                        for assignment_date in _daterange(period_start, period_end)
                        for shift_code in shift_codes
                    ):
                        break
                    locked = _clinical_floor_lock_pass(
                        employees=employees,
                        states=states,
                        expanded_slots=expanded_slots,
                        shift_templates=shift_templates,
                        shift_required_qualifications=shift_required_qualifications,
                        rules=rules,
                        period_start=period_start,
                        period_end=period_end,
                        availability_blocked=availability_blocked,
                        qual_codes=qual_codes,
                        target_hours_map=target_hours_map,
                        period_target_hours=period_target_hours,
                        fill_counts=fill_counts,
                        filled_smooth_seats=filled_smooth_seats,
                        prioritize_coverage=prioritize_coverage,
                        shift_codes=shift_codes,
                        allow_forced_clinical_ot=True,
                    )
                    result.assignments.extend(locked)
                    fill_counts = _seat_fill_counts(result.assignments, employees, qual_codes)
                    if not locked:
                        break
            _trim_clinical_band_overfill(
                result.assignments,
                states,
                employees=employees,
                shift_templates=shift_templates,
                qual_codes=qual_codes,
                period_start=period_start,
                period_end=period_end,
                fulltime_target=fulltime_target,
            )
            fill_counts = _seat_fill_counts(result.assignments, employees, qual_codes)
            for _contract_close in range(16):
                if _all_fulltime_at_contract_target(
                    employees,
                    states,
                    fulltime_target=fulltime_target,
                ):
                    break
                dn_backfill = _backfill_dn_contract_pass(
                    employees=employees,
                    states=states,
                    expanded_slots=expanded_slots,
                    shift_templates=shift_templates,
                    shift_required_qualifications=shift_required_qualifications,
                    rules=rules,
                    period_start=period_start,
                    period_end=period_end,
                    availability_blocked=availability_blocked,
                    qual_codes=qual_codes,
                    fill_counts=fill_counts,
                    fulltime_target=fulltime_target,
                )
                contract_close = _mandatory_fulltime_contract_pass(
                    employees=employees,
                    states=states,
                    expanded_slots=expanded_slots,
                    shift_templates=shift_templates,
                    shift_required_qualifications=shift_required_qualifications,
                    rules=rules,
                    period_start=period_start,
                    period_end=period_end,
                    weeks_in_period=weeks_in_period,
                    availability_blocked=availability_blocked,
                    qual_codes=qual_codes,
                    fill_counts=fill_counts,
                    filled_smooth_seats=filled_smooth_seats,
                    prioritize_coverage=prioritize_coverage,
                )
                result.assignments.extend(dn_backfill)
                result.assignments.extend(contract_close)
                fill_counts = _seat_fill_counts(result.assignments, employees, qual_codes)
                if not dn_backfill and not contract_close:
                    break
            _trim_clinical_band_overfill(
                result.assignments,
                states,
                employees=employees,
                shift_templates=shift_templates,
                qual_codes=qual_codes,
                period_start=period_start,
                period_end=period_end,
                fulltime_target=fulltime_target,
            )
            fill_counts = _seat_fill_counts(result.assignments, employees, qual_codes)

        _update_slot_fill_metrics(
            result,
            expanded_slots=expanded_slots,
            fill_counts=fill_counts,
            shift_templates=shift_templates,
        )
        _fail_on_remaining_open_slots(
            expanded_slots=expanded_slots,
            fill_counts=fill_counts,
            filled_smooth_seats=filled_smooth_seats,
            impossible_seats=impossible_seats,
            impossible_slots=impossible_slots,
            employees=employees,
            shift_templates=shift_templates,
            shift_required_qualifications=shift_required_qualifications,
            qual_codes=qual_codes,
            prioritize_coverage=prioritize_coverage,
            strict_complete_block=strict_complete_block,
            emit_triage=emit_triage,
            triage_sink=triage_sink,
        )

    if prioritize_coverage:
        employee_hours = {emp_id: state.total_hours for emp_id, state in states.items()}
        tier_results = evaluate_coverage_tier_results(
            targets=coverage_targets,
            employee_hours=employee_hours,
            rules=rules,
            weeks_in_period=weeks_in_period,
            impossible_tier_ids=impossible_tiers,
            shift_templates=shift_templates,
            period_start=period_start,
            period_end=period_end,
        )
        result.coverage_tier_results = list(tier_results)
        result.coverage_complete = is_schedule_coverage_complete(
            unfilled_coverage_gaps=result.coverage_gap_count,
            tier_results=tier_results,
        )

    if triage_sink is not None:
        result.triage_list = list(triage_sink.entries)

    return result


def auto_generate_schedule(
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
    archetype: str = "STANDARD",
    progress_callback: Optional[Callable[[str], None]] = None,
    enable_fairness_rerun: bool = True,
    portage_scheduling_policy: Optional["PortageSchedulingPolicy"] = None,
    manager_locked_cells: Optional[Set[Tuple[str, date]]] = None,
    fairness_weights: Optional["FairnessWeights"] = None,
) -> AutoGenerateResult:
    """
    Route schedule generation to the strategy for ``archetype`` (``STANDARD`` or
    ``TWELVE_HOUR``). Defaults to the existing Portage master rotation path.
    """
    from lab_scheduler.scheduling.strategies import generate_schedule_for_archetype

    return generate_schedule_for_archetype(
        archetype,
        rules=rules,
        period_start=period_start,
        period_end=period_end,
        weeks_in_period=weeks_in_period,
        employees=employees,
        shift_templates=shift_templates,
        shift_required_qualifications=shift_required_qualifications,
        employee_target_hours=employee_target_hours,
        availability_blocked=availability_blocked,
        coverage_targets=coverage_targets,
        concurrent_demands=concurrent_demands,
        require_master_compliance=require_master_compliance,
        coverage_aggressor_mode=coverage_aggressor_mode,
        strict_complete_block=strict_complete_block,
        emit_triage=emit_triage,
        progress_callback=progress_callback,
        enable_fairness_rerun=enable_fairness_rerun,
        portage_scheduling_policy=portage_scheduling_policy,
        manager_locked_cells=manager_locked_cells,
        fairness_weights=fairness_weights,
    )


def _scheduled_shifts_from_result(
    result: AutoGenerateResult,
    employees: Sequence[EmployeeProfile],
) -> List[ScheduledShift]:
    return _scheduled_shifts_from_assignments(result.assignments, employees)


def _scheduled_shifts_from_assignments(
    assignments: Sequence[PlannedAssignment],
    employees: Sequence[EmployeeProfile],
) -> List[ScheduledShift]:
    emp_lookup = {employee.id: employee.full_name for employee in employees}
    return [
        ScheduledShift(
            employee_id=assignment.employee_id,
            employee_name=emp_lookup.get(assignment.employee_id, assignment.employee_id),
            assignment_date=assignment.assignment_date,
            shift_template_id=assignment.shift_template_id,
            approved_stretch=assignment.approved_stretch,
            clinical_floor_stretch=assignment.clinical_floor_stretch,
            contract_line_exception=assignment.contract_line_exception,
            contract_line_exception_message=assignment.contract_line_exception_message,
        )
        for assignment in assignments
    ]


def _run_master_compliance_gate(
    result: AutoGenerateResult,
    *,
    rules: JurisdictionRules,
    employees: Sequence[EmployeeProfile],
    shift_templates: Dict[str, ShiftTemplateInfo],
    period_start: date,
    period_end: date,
    weeks_in_period: int,
    employee_target_hours: Optional[Mapping[str, float]] = None,
    fill_counts: Optional[Mapping[Tuple[date, str, Optional[str]], int]] = None,
    expanded_slots: Optional[Sequence[ExpandedScheduleSlot]] = None,
) -> "ComplianceValidationResult":
    """Hard-stop gate: Master Schedule output requires 100% compliance audit pass."""

    from lab_scheduler.audit.compliance import (
        ComplianceValidator,
        build_overtime_compliance_bypass_conflicts,
    )

    result.assignments = _annotate_clinical_floor_stretches(
        result.assignments,
        shift_templates,
    )
    overtime_bypasses = build_overtime_compliance_bypass_conflicts(
        result.assignments,
        employees=employees,
        shift_templates=shift_templates,
    )
    validator = ComplianceValidator()
    validation = validator.validate_or_abort(
        rules=rules,
        employees=employees,
        assignments=_scheduled_shifts_from_result(result, employees),
        shift_templates=shift_templates,
        period_start=period_start,
        period_end=period_end,
        weeks_in_period=weeks_in_period,
        employee_target_hours=employee_target_hours,
        fill_counts=fill_counts,
        expanded_slots=expanded_slots,
        enforce_clinical_floors=True,
        require_contract_fte=True,
        overtime_compliance_bypassed=overtime_bypasses,
        log_rejections=True,
    )
    result.compliance_validation = validation
    contract_line_rows = _contract_line_provisionals_from_planned(
        result.assignments,
        employees=employees,
        shift_templates=shift_templates,
    )
    merged: Dict[Tuple[str, date, str], ProvisionalAssignment] = {
        item.assignment_key(): item for item in contract_line_rows
    }
    for item in validation.provisional_assignments:
        merged[item.assignment_key()] = item
    result.provisional_assignments = list(merged.values())
    if result.provisional_assignments:
        result.schedule_status = "PROVISIONAL"
    if validation.report_path is not None:
        result.conflict_report_path = str(validation.report_path)
    if validation.passed:
        return validation

    summary = ", ".join(validation.manager_summary[:5])
    if validation.conflict_count > len(validation.manager_summary[:5]):
        summary = f"{summary}, ..."
    raise RuntimeError(
        f"ComplianceValidator returned {validation.pass_rate_pct:.0f}% PASS "
        f"({validation.conflict_count} conflict(s): {summary})"
    )


def validate_generated_schedule(
    result: AutoGenerateResult,
    *,
    rules: JurisdictionRules,
    employees: Sequence[EmployeeProfile],
    shift_templates: Dict[str, ShiftTemplateInfo],
    period_start: date,
    period_end: date,
    weeks_in_period: int,
    employee_target_hours: Optional[Mapping[str, float]] = None,
    fill_counts: Optional[Mapping[Tuple[date, str, Optional[str]], int]] = None,
    expanded_slots: Optional[Sequence[ExpandedScheduleSlot]] = None,
    master_schedule: bool = False,
) -> None:
    """Post-check schedule output (raises if validation fails)."""

    if master_schedule:
        _run_master_compliance_gate(
            result,
            rules=rules,
            employees=employees,
            shift_templates=shift_templates,
            period_start=period_start,
            period_end=period_end,
            weeks_in_period=weeks_in_period,
            employee_target_hours=employee_target_hours,
            fill_counts=fill_counts,
            expanded_slots=expanded_slots,
        )
        return

    scheduled = _scheduled_shifts_from_result(result, employees)
    employee_dicts = [
        {"id": employee.id, "full_name": employee.full_name, "fte": employee.fte}
        for employee in employees
    ]
    report = evaluate_schedule(
        rules,
        employees=employee_dicts,
        assignments=scheduled,
        shift_templates=shift_templates,
        period_start=period_start,
        period_end=period_end,
        weeks_in_period=weeks_in_period,
        employee_target_hours=employee_target_hours,
    )
    errors = [violation for violation in report.violations if violation.severity == "error"]
    if errors:
        msgs = "; ".join(violation.message for violation in errors[:3])
        raise RuntimeError(f"Generated schedule failed compliance validation: {msgs}")


def _build_employee_state(
    profile: EmployeeProfile,
    assignments: Sequence[ScheduledShift],
    shift_templates: Dict[str, ShiftTemplateInfo],
    rules: JurisdictionRules,
    weeks_in_period: int,
    target_hours_override: Optional[float] = None,
) -> _EmployeeState:
    target = (
        target_hours_override
        if target_hours_override is not None
        else rules.standard_hours_per_week_at_1_0_fte * profile.fte * weeks_in_period
    )
    state = _EmployeeState(profile=profile, target_hours=target)
    for a in assignments:
        if a.employee_id != profile.id:
            continue
        tmpl = shift_templates.get(a.shift_template_id)
        if tmpl is None:
            continue
        hours = tmpl.duration_minutes / 60.0
        state.work_dates.add(a.assignment_date)
        state.assignment_records.append((a.assignment_date, a.shift_template_id))
        state.total_hours += hours
        week_start = workweek_for(a.assignment_date).start
        state.week_hours[week_start] = state.week_hours.get(week_start, 0.0) + hours
    return state


def validate_assignment_change(
    *,
    rules: JurisdictionRules,
    period_start: date,
    period_end: date,
    weeks_in_period: int,
    employee: EmployeeProfile,
    all_assignments: Sequence[ScheduledShift],
    shift_templates: Dict[str, ShiftTemplateInfo],
    shift_required_qualifications: Dict[str, Set[str]],
    assignment_date: date,
    new_shift_template_id: Optional[str],
    employee_target_hours: Optional[Mapping[str, float]] = None,
    availability_blocked: Optional[Mapping[str, Set[date]]] = None,
    enforce_fte_target: bool = True,
    approved_stretch: bool = False,
    role_pool_id: Optional[str] = None,
) -> Optional[str]:
    """Return an error message if the proposed assignment is not allowed."""

    if new_shift_template_id is None:
        return None

    if availability_blocked and assignment_date in availability_blocked.get(employee.id, set()):
        return "Employee has approved time off on this date."

    required = shift_required_qualifications.get(new_shift_template_id, set())
    if not _is_qualified(employee, required):
        return "Employee lacks the required qualification for this shift."

    template = shift_templates.get(new_shift_template_id)
    if template is None:
        return "Unknown shift type."

    emp_qual = infer_qual_code(employee)
    line_assessment = assess_clinical_floor_contract_line(
        contract_line_type=employee.contract_line_type,
        shift_code=template.code,
        qual_code=emp_qual,
        role_pool_id=role_pool_id,
    )
    if line_assessment.hard_rejection:
        return line_assessment.violation_message

    others = [
        a
        for a in all_assignments
        if a.employee_id == employee.id and a.assignment_date != assignment_date
    ]
    target_override = (
        float(employee_target_hours[employee.id])
        if employee_target_hours and employee.id in employee_target_hours
        else None
    )
    state = _build_employee_state(
        employee, others, shift_templates, rules, weeks_in_period, target_override
    )
    return _would_violate_labor_rules(
        state,
        assignment_date,
        template,
        shift_templates,
        rules,
        period_start,
        period_end,
        availability_blocked,
        enforce_fte_target=enforce_fte_target,
        approved_stretch=approved_stretch,
    )


def is_operational_shift_template(
    template: ShiftTemplateInfo,
    *,
    schedule_archetype: str = "STANDARD",
) -> bool:
    """Return False for templates that are not staffed coverage seats for the archetype.

    Synthetic FTE top-up rows never count. In the TWELVE_HOUR (7-on/7-off) archetype the
    lab runs two 12-hour seats — Day and Night only — so the Evening template is not a
    coverage requirement and must not register as an unfilled gap.
    """

    code = str(template.code or "").strip().upper()
    if code.startswith("TOPUP"):
        return False
    template_id = str(template.id or "")
    if "twelve-hour-fte-topup" in template_id:
        return False
    normalized_archetype = str(schedule_archetype or "STANDARD").strip().upper().replace("-", "_")
    if normalized_archetype in {"TWELVE_HOUR", "TWELVEHOUR", "12H", "7ON7OFF"} and code in {
        "EVENING",
        "E",
    }:
        return False
    return True


def list_open_shift_slots(
    *,
    period_start: date,
    period_end: date,
    shift_templates: Dict[str, ShiftTemplateInfo],
    assignments: Sequence[ScheduledShift],
    schedule_archetype: str = "STANDARD",
) -> List[UnfilledSlot]:
    """Slots with no coverage: one worker required per operational shift template per day."""

    covered = {(a.assignment_date, a.shift_template_id) for a in assignments}
    open_slots: List[UnfilledSlot] = []
    for d in _daterange(period_start, period_end):
        for shift_id, tmpl in shift_templates.items():
            if not is_operational_shift_template(tmpl, schedule_archetype=schedule_archetype):
                continue
            if (d, shift_id) in covered:
                continue
            open_slots.append(
                UnfilledSlot(
                    assignment_date=d,
                    shift_template_id=shift_id,
                    shift_code=tmpl.code,
                    reason="No coverage scheduled",
                )
            )
    return open_slots


def suggest_employees_for_slot(
    *,
    rules: JurisdictionRules,
    period_start: date,
    period_end: date,
    weeks_in_period: int,
    employees: Sequence[EmployeeProfile],
    all_assignments: Sequence[ScheduledShift],
    shift_templates: Dict[str, ShiftTemplateInfo],
    shift_required_qualifications: Dict[str, Set[str]],
    slot_date: date,
    shift_template_id: str,
    limit: int = 3,
    employee_target_hours: Optional[Mapping[str, float]] = None,
    availability_blocked: Optional[Mapping[str, Set[date]]] = None,
) -> List[SlotSuggestion]:
    template = shift_templates.get(shift_template_id)
    if template is None:
        return []

    required = shift_required_qualifications.get(shift_template_id, set())
    ranked: List[SlotSuggestion] = []
    qualified_profiles = [emp for emp in employees if _is_qualified(emp, required)]
    eligible_ids: Set[str] = set()
    ineligible_reasons: Dict[str, str] = {}
    eligible_profiles: list[EmployeeProfile] = []

    for emp in qualified_profiles:
        emp_qual = infer_qual_code(emp)
        line_violation = validate_contract_line_eligibility(
            emp.contract_line_type,
            template.code,
            qual_code=emp_qual,
        )
        if line_violation:
            log_assignment_rejection(emp.id, slot_date, line_violation)
            ineligible_reasons[emp.id] = line_violation
            continue

        others = [
            a
            for a in all_assignments
            if a.employee_id == emp.id and a.assignment_date != slot_date
        ]
        target_override = (
            float(employee_target_hours[emp.id])
            if employee_target_hours and emp.id in employee_target_hours
            else None
        )
        state = _build_employee_state(
            emp, others, shift_templates, rules, weeks_in_period, target_override
        )
        violation = _would_violate_labor_rules(
            state,
            slot_date,
            template,
            shift_templates,
            rules,
            period_start,
            period_end,
            availability_blocked,
            log_rejection=True,
        )
        if violation:
            ineligible_reasons[emp.id] = violation
            continue
        eligible_ids.add(emp.id)
        eligible_profiles.append(emp)

    for emp in rank_profiles_cba(eligible_profiles):
        bypass = evaluate_seniority_bypass(
            qualified_profiles=qualified_profiles,
            eligible_ids=eligible_ids,
            selected=emp,
            ineligible_reasons=ineligible_reasons,
        )
        ranked.append(
            SlotSuggestion(
                employee_id=emp.id,
                employee_name=emp.full_name,
                score=emp.seniority_hours,
                seniority_bypass=bypass is not None,
                seniority_bypass_justification=bypass.justification if bypass else None,
                requires_seniority_justification=(
                    bypass.requires_manual_justification if bypass else False
                ),
            )
        )

    return ranked[:limit]


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def persist_schedule_to_database(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    schedule_period_id: str,
    assignments: Sequence[PlannedAssignment],
    replace_existing: bool = True,
) -> int:
    """
    Write planned assignments through SQLite (qualification triggers remain active).
    Returns number of rows inserted.
    """

    conn.execute("PRAGMA foreign_keys = ON;")
    now = _utc_now_iso()

    if replace_existing:
        conn.execute(
            """
            DELETE FROM shift_assignments
            WHERE tenant_id = ? AND schedule_period_id = ?
            """,
            (tenant_id, schedule_period_id),
        )

    inserted = 0
    for a in assignments:
        conn.execute(
            """
            INSERT INTO shift_assignments (
              id, tenant_id, schedule_period_id, employee_id,
              shift_template_id, assignment_date, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"asg-{uuid.uuid4().hex[:12]}",
                tenant_id,
                schedule_period_id,
                a.employee_id,
                a.shift_template_id,
                a.assignment_date.isoformat(),
                now,
                now,
            ),
        )
        inserted += 1

    conn.commit()
    return inserted
