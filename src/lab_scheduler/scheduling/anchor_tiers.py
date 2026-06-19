"""Anchor tier model for Anchor & Fill compilation (Layer 1 protection)."""

from __future__ import annotations

from datetime import date
from enum import IntEnum
from typing import Mapping, Optional, Sequence, Set

from lab_scheduler.compliance.engine import ShiftTemplateInfo
from lab_scheduler.scheduling.portage_template import (
    FULLTIME_FTE_THRESHOLD,
    parse_vacant_portage_line,
    vacant_master_rotation_fte,
    vacant_master_scheduled_shift_code,
)
from lab_scheduler.scheduling.profiles import EmployeeProfile
from lab_scheduler.scheduling.schedule_tallies import shift_band_from_template_code

CellKey = tuple[str, date]


class AnchorTier(IntEnum):
    SOFT = 0
    CATALOG_DAY = 1
    MANAGER_LOCK = 2
    NIGHT_ANCHOR = 3


def anchor_tier_for_cell(
    employee: EmployeeProfile,
    assignment_date: date,
    period_start: date,
    *,
    manager_locked_cells: Set[CellKey] | None = None,
    assignments: Optional[Sequence[object]] = None,
    shift_templates: Optional[Mapping[str, ShiftTemplateInfo]] = None,
) -> AnchorTier:
    """Resolve protection tier for one employee/day cell."""

    if parse_vacant_portage_line(employee.full_name) is None:
        return AnchorTier.SOFT

    contract = (employee.contract_line_type or "").upper()
    rotation_fte = vacant_master_rotation_fte(employee)
    expected_code = vacant_master_scheduled_shift_code(
        employee,
        assignment_date,
        period_start,
    )

    if (
        contract == "D/N"
        and rotation_fte is not None
        and rotation_fte >= FULLTIME_FTE_THRESHOLD
        and expected_code == "NIGHT"
    ):
        return AnchorTier.NIGHT_ANCHOR

    if manager_locked_cells and (employee.id, assignment_date) in manager_locked_cells:
        if assignments is not None and shift_templates is not None:
            for assignment in assignments:
                if getattr(assignment, "employee_id", "") != employee.id:
                    continue
                if getattr(assignment, "assignment_date", None) != assignment_date:
                    continue
                template = shift_templates.get(getattr(assignment, "shift_template_id", ""))
                if template is None:
                    continue
                if shift_band_from_template_code(template.code) in {"D", "E", "N"}:
                    return AnchorTier.MANAGER_LOCK
        else:
            return AnchorTier.MANAGER_LOCK

    if (
        expected_code == "MORNING"
        and assignment_date.weekday() < 5
        and rotation_fte is not None
        and rotation_fte >= FULLTIME_FTE_THRESHOLD
    ):
        return AnchorTier.CATALOG_DAY

    return AnchorTier.SOFT


def blocks_modification(
    tier: AnchorTier,
    *,
    minimum_tier: AnchorTier = AnchorTier.NIGHT_ANCHOR,
) -> bool:
    return tier >= minimum_tier


def is_night_anchor_cell(
    employee: EmployeeProfile,
    assignment_date: date,
    period_start: date,
) -> bool:
    return (
        anchor_tier_for_cell(employee, assignment_date, period_start)
        == AnchorTier.NIGHT_ANCHOR
    )


def merge_night_anchor_fixed_bands(
    fixed: dict[tuple[str, date], str],
    *,
    employees: Sequence[EmployeeProfile],
    period_start: date,
    period_end: date,
) -> dict[tuple[str, date], str]:
    """Extend CP-SAT fixed map so Layer-1 night anchors are never reassigned."""

    from datetime import timedelta

    from lab_scheduler.scheduling.portage_template import vacant_master_scheduled_shift_code

    merged = dict(fixed)
    day = period_start
    while day <= period_end:
        for employee in employees:
            if anchor_tier_for_cell(employee, day, period_start) != AnchorTier.NIGHT_ANCHOR:
                continue
            key = (employee.id, day)
            if key in merged:
                continue
            expected = vacant_master_scheduled_shift_code(employee, day, period_start)
            if expected == "NIGHT":
                merged[key] = "N"
            elif expected == "MORNING":
                merged[key] = "D"
        day += timedelta(days=1)
    return merged
