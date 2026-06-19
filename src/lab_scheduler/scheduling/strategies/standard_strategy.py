"""Standard ~8h (Portage 7.75h effective) master rotation schedule generation."""

from __future__ import annotations

from datetime import date
from typing import Callable, Dict, Mapping, Optional, Sequence, Set, Tuple

from lab_scheduler.compliance.engine import ShiftTemplateInfo
from lab_scheduler.compliance.jurisdictions import JurisdictionRules
from lab_scheduler.scheduling.auto_generate import AutoGenerateResult, EmployeeProfile
from lab_scheduler.engine.constraints import CoverageTierTarget
from lab_scheduler.engine.demand import ShiftConcurrentDemand

__all__ = ["generate_schedule"]


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
    progress_callback: Optional[Callable[[str], None]] = None,
    enable_fairness_rerun: bool = True,
    portage_scheduling_policy: Optional["PortageSchedulingPolicy"] = None,
    manager_locked_cells: Optional[Set[Tuple[str, date]]] = None,
    fairness_weights: Optional["FairnessWeights"] = None,
) -> AutoGenerateResult:
    from lab_scheduler.scheduling.auto_generate import _generate_standard_schedule

    return _generate_standard_schedule(
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
