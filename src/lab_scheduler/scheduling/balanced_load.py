from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Optional, Sequence, Tuple

from lab_scheduler.compliance.engine import ShiftTemplateInfo
from lab_scheduler.compliance.jurisdictions import JurisdictionRules
from lab_scheduler.engine.demand import CLINICAL_FLOOR, ExpandedScheduleSlot
from lab_scheduler.scheduling.pool_manager import ElasticPoolManager


CAPACITY_SHORTFALL_MESSAGE = (
    "Staffing level insufficient to meet floor requirements with even distribution."
)


@dataclass(frozen=True, slots=True)
class CapacityShortfallAlert:
    message: str
    total_demand_hours: float
    total_pool_capacity_hours: float
    deficit_hours: float
    role_deficits: Mapping[str, float]

    def manager_summary(self) -> str:
        return (
            f"{self.message} "
            f"Demand {self.total_demand_hours:.1f}h exceeds pool capacity "
            f"{self.total_pool_capacity_hours:.1f}h "
            f"(shortfall {self.deficit_hours:.1f}h)."
        )


def balanced_load_rank_key(
    total_hours: float,
    pool_average_hours: float,
) -> Tuple[float, float]:
    """
    Ascending sort prefers the most underloaded staff relative to the pool average.

    Minimizes hour variance by penalizing deviation from the shared average load.
    """

    average = max(float(pool_average_hours), 1e-6)
    deviation = total_hours - average
    variance_penalty = deviation * deviation
    return (total_hours, variance_penalty)


def hour_variance(hours: Sequence[float]) -> float:
    if len(hours) < 2:
        return 0.0
    average = sum(hours) / len(hours)
    return sum((value - average) ** 2 for value in hours) / len(hours)


def _slot_demand_hours(
    slots: Sequence[ExpandedScheduleSlot],
    shift_templates: Mapping[str, ShiftTemplateInfo],
) -> float:
    total = 0.0
    for slot in slots:
        template = shift_templates[slot.shift_id]
        total += template.duration_minutes / 60.0
    return round(total, 2)


def _clinical_floor_demand_hours(
    slots: Sequence[ExpandedScheduleSlot],
    shift_templates: Mapping[str, ShiftTemplateInfo],
) -> float:
    """Minimum hours tied to clinical floor seats (E/N/M clinical pools)."""

    total = 0.0
    for slot in slots:
        template = shift_templates[slot.shift_id]
        if template.code not in CLINICAL_FLOOR:
            continue
        total += template.duration_minutes / 60.0
    return round(total, 2)


def assess_elastic_capacity_shortfall(
    pool_manager: ElasticPoolManager,
    expanded_slots: Sequence[ExpandedScheduleSlot],
    shift_templates: Mapping[str, ShiftTemplateInfo],
    *,
    rules: JurisdictionRules,
    weeks_in_period: int,
) -> Optional[CapacityShortfallAlert]:
    """
    Compare roster payroll supply to expanded shift demand.

    Returns a non-fatal alert when the pool cannot cover demand with even distribution.
    """

    total_demand = _slot_demand_hours(expanded_slots, shift_templates)
    total_capacity = pool_manager.total_capacity_hours(
        rules=rules,
        weeks_in_period=weeks_in_period,
    )
    role_deficits: dict[str, float] = {}

    for role in ("MLT", "MLA"):
        role_slots = [
            slot
            for slot in expanded_slots
            if (slot.required_qual_code or "").upper() == role
        ]
        if not role_slots:
            continue
        role_demand = _slot_demand_hours(role_slots, shift_templates)
        role_capacity = pool_manager.role_capacity_hours(
            role,
            rules=rules,
            weeks_in_period=weeks_in_period,
        )
        if role_capacity + 1e-9 < role_demand:
            role_deficits[role] = round(role_demand - role_capacity, 2)

    clinical_demand = _clinical_floor_demand_hours(expanded_slots, shift_templates)
    if total_capacity + 1e-9 < total_demand or role_deficits:
        deficit = max(total_demand - total_capacity, 0.0)
        if role_deficits and deficit < 0.01:
            deficit = max(role_deficits.values())
        return CapacityShortfallAlert(
            message=CAPACITY_SHORTFALL_MESSAGE,
            total_demand_hours=total_demand,
            total_pool_capacity_hours=total_capacity,
            deficit_hours=round(deficit, 2),
            role_deficits=role_deficits,
        )

    if clinical_demand > total_capacity * 0.85:
        return CapacityShortfallAlert(
            message=CAPACITY_SHORTFALL_MESSAGE,
            total_demand_hours=total_demand,
            total_pool_capacity_hours=total_capacity,
            deficit_hours=round(max(clinical_demand - total_capacity, 0.0), 2),
            role_deficits=role_deficits,
        )
    return None


def balanced_deficit_hours(
    *,
    employee_id: str,
    total_hours: float,
    load_reference_hours: Mapping[str, float],
    fulltime_target: float,
    use_elastic: bool,
) -> float:
    if use_elastic and employee_id in load_reference_hours:
        return float(load_reference_hours[employee_id]) - total_hours
    return fulltime_target - total_hours
