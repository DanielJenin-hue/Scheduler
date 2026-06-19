"""Schedule generation strategies keyed by staffing archetype."""

from __future__ import annotations

from enum import Enum
from datetime import date
from typing import TYPE_CHECKING, Callable, Mapping, Optional, Sequence, Set, Tuple

from lab_scheduler.compliance.engine import ShiftTemplateInfo
from lab_scheduler.compliance.jurisdictions import JurisdictionRules
from lab_scheduler.engine.constraints import CoverageTierTarget
from lab_scheduler.engine.demand import ShiftConcurrentDemand

if TYPE_CHECKING:
    from lab_scheduler.scheduling.auto_generate import AutoGenerateResult, EmployeeProfile

__all__ = [
    "ScheduleArchetype",
    "generate_schedule_for_archetype",
    "normalize_archetype",
    "schedule_archetype_display_label",
]


class ScheduleArchetype(str, Enum):
    STANDARD = "STANDARD"
    TWELVE_HOUR = "TWELVE_HOUR"


def normalize_archetype(archetype: ScheduleArchetype | str) -> ScheduleArchetype:
    if isinstance(archetype, ScheduleArchetype):
        return archetype
    normalized = str(archetype).strip().upper().replace("-", "_")
    aliases = {
        "STANDARD": ScheduleArchetype.STANDARD,
        "TWELVE_HOUR": ScheduleArchetype.TWELVE_HOUR,
        "TWELVEHOUR": ScheduleArchetype.TWELVE_HOUR,
        "12H": ScheduleArchetype.TWELVE_HOUR,
        "7ON7OFF": ScheduleArchetype.TWELVE_HOUR,
    }
    try:
        return aliases[normalized]
    except KeyError as exc:
        valid = ", ".join(member.value for member in ScheduleArchetype)
        raise ValueError(f"Unknown schedule archetype {archetype!r}. Expected one of: {valid}.") from exc


def schedule_archetype_display_label(archetype: ScheduleArchetype | str) -> str:
    if normalize_archetype(archetype) is ScheduleArchetype.TWELVE_HOUR:
        return "7-on/7-off"
    return "Regular"


def generate_schedule_for_archetype(
    archetype: ScheduleArchetype | str,
    *,
    rules: JurisdictionRules,
    period_start,
    period_end,
    weeks_in_period: int,
    employees: Sequence[EmployeeProfile],
    shift_templates: Mapping[str, ShiftTemplateInfo],
    shift_required_qualifications: Mapping[str, Set[str]],
    employee_target_hours: Optional[Mapping[str, float]] = None,
    availability_blocked: Optional[Mapping[str, Set]] = None,
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
    strategy = _resolve_strategy(normalize_archetype(archetype))
    return strategy(
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


def _resolve_strategy(archetype: ScheduleArchetype) -> Callable[..., AutoGenerateResult]:
    if archetype is ScheduleArchetype.TWELVE_HOUR:
        from lab_scheduler.scheduling.strategies.twelve_hour_7on7off_strategy import generate_schedule

        return generate_schedule
    from lab_scheduler.scheduling.strategies.standard_strategy import generate_schedule

    return generate_schedule
