"""Anchor & Fill layer compilers (L2 coverage delta, L3 equitable fill hooks)."""

from __future__ import annotations

from datetime import date
from typing import Dict, Mapping, Optional, Sequence, Set, Tuple

from lab_scheduler.compliance.engine import ShiftTemplateInfo
from lab_scheduler.compliance.jurisdictions import JurisdictionRules
from lab_scheduler.engine.demand import ExpandedScheduleSlot
from lab_scheduler.scheduling.equitability_score import FairnessWeights
from lab_scheduler.scheduling.profiles import EmployeeProfile


def compile_core_coverage_delta(
    result: object,
    *,
    states: Dict[str, object],
    employees: Sequence[EmployeeProfile],
    expanded_slots: Sequence[ExpandedScheduleSlot],
    shift_templates: Dict[str, ShiftTemplateInfo],
    shift_required_qualifications: Mapping[str, Set[str]],
    rules: JurisdictionRules,
    period_start: date,
    period_end: date,
    availability_blocked: Optional[Mapping[str, Set[date]]],
    qual_codes: Mapping[str, str],
    catalog_targets: Mapping[str, float],
    target_hours_map: Mapping[str, float],
    period_target_hours: Mapping[str, float],
    filled_smooth_seats: Set[Tuple[date, str, Optional[str], int]],
    post_pass_guard: Optional[object] = None,
) -> bool:
    """
    Layer 2: close clinical E/N floor gaps without mutating night anchors.
    """

    from lab_scheduler.scheduling.auto_generate import (
        _extend_evening_night_clinical_lockdown,
        _heal_required_coverage_after_catalog_trim,
        _rebuild_states_from_assignments,
        _seat_fill_counts,
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
        filled_smooth_seats=filled_smooth_seats,
        post_pass_guard=post_pass_guard,
    )
    fill_counts = _seat_fill_counts(result.assignments, employees, qual_codes)
    locked = _extend_evening_night_clinical_lockdown(
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
    _rebuild_states_from_assignments(states, result.assignments, shift_templates)
    return bool(locked)


def compile_equitable_fill(
    result: object,
    *,
    states: Dict[str, object],
    rules: JurisdictionRules,
    period_start: date,
    period_end: date,
    weeks_in_period: int,
    employees: Sequence[EmployeeProfile],
    shift_templates: Dict[str, ShiftTemplateInfo],
    target_hours_map: Mapping[str, float],
    availability_blocked: Optional[Mapping[str, Set[date]]],
    qual_codes: Mapping[str, str],
    fairness_weights: FairnessWeights | None = None,
    enable_fairness_rerun: bool = True,
    progress_callback: Optional[object] = None,
) -> int:
    """Layer 3: CP-SAT vacant fill with equitability weight scaling."""

    from lab_scheduler.scheduling.auto_generate import (
        _run_cpsat_vacant_fill_with_fairness_rerun,
    )

    weights = fairness_weights or FairnessWeights()
    return _run_cpsat_vacant_fill_with_fairness_rerun(
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
        progress_callback=progress_callback,
        enable_fairness_rerun=enable_fairness_rerun,
        fairness_weight_scale=weights.fairness_weight_scale(),
        fairness_weights=weights,
    )


def recompile_layer3_from_draft(
    draft_frame: object,
    *,
    rules: JurisdictionRules,
    period_start: date,
    period_end: date,
    weeks_in_period: int,
    employees: Sequence[EmployeeProfile],
    shift_templates: Dict[str, ShiftTemplateInfo],
    db_templates: Mapping[str, Mapping[str, object]],
    dates: Sequence[date],
    target_hours_map: Mapping[str, float],
    availability_blocked: Optional[Mapping[str, Set[date]]],
    qual_codes: Mapping[str, str],
    manager_locked_cells: Set[Tuple[str, date]] | None = None,
    fairness_weights: FairnessWeights | None = None,
) -> Tuple[int, list]:
    """
    Partial Layer-3 recompile: fill soft vacant slots around manager locks without
    touching night anchors.
    """

    from lab_scheduler.policy.frame_bridge import assignments_from_schedule_frame
    from lab_scheduler.scheduling.auto_generate import (
        AutoGenerateResult,
        PlannedAssignment,
        _EmployeeState,
        _rebuild_states_from_assignments,
    )

    scheduled = assignments_from_schedule_frame(
        draft_frame,
        employees=employees,
        dates=dates,
        templates=db_templates,
    )
    assignments = [
        PlannedAssignment(
            employee_id=row.employee_id,
            shift_template_id=row.shift_template_id,
            assignment_date=row.assignment_date,
        )
        for row in scheduled
    ]
    result = AutoGenerateResult(
        assignments=assignments,
        manager_locked_cells=set(manager_locked_cells or ()),
    )
    states = {
        employee.id: _EmployeeState(
            profile=employee,
            target_hours=float(target_hours_map.get(employee.id, 0.0)),
        )
        for employee in employees
    }
    _rebuild_states_from_assignments(states, result.assignments, shift_templates)
    added = compile_equitable_fill(
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
        fairness_weights=fairness_weights,
        enable_fairness_rerun=False,
    )
    return added, list(result.assignments)
