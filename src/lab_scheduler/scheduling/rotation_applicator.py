"""Apply planned rotation shifts to the schedule grid."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import List, Mapping, Optional, Sequence, Set, Tuple

import pandas as pd

from lab_scheduler.compliance.engine import ShiftTemplateInfo
from lab_scheduler.compliance.jurisdictions import JurisdictionRules
from lab_scheduler.scheduling.profiles import EmployeeProfile
from lab_scheduler.scheduling.rotation_planner import PlannedShift, RotationReasonCode
from lab_scheduler.scheduling.rotation_spec import FT_DE_EVENING_BLOCK_STREAK_DAYS
from lab_scheduler.scheduling.weekend_placement_rules import (
    get_grid_token,
    is_editable_cell,
    is_empty_grid_token,
    operational_alt_band_cap_per_qual,
    set_grid_token,
    daily_band_qual_count,
)
from lab_scheduler.engine.demand import infer_qual_code


@dataclass(slots=True)
class PlacementConflict:
    employee_id: str
    day: date
    band: str
    reason: str


@dataclass(slots=True)
class ApplyResult:
    cells_changed: int
    conflicts: List[PlacementConflict] = field(default_factory=list)


def _passes_weekday_qual_cap(
    frame: pd.DataFrame,
    row_lookup: Mapping[str, int],
    employees_by_id: Mapping[str, EmployeeProfile],
    qual_codes: Mapping[str, str],
    *,
    employee_id: str,
    day: date,
    band: str,
) -> bool:
    if day.weekday() >= 5 or band not in {"E", "N"}:
        return True
    employee = employees_by_id.get(employee_id)
    if employee is None:
        return False
    qual = infer_qual_code(employee, qual_codes=qual_codes)
    cap = operational_alt_band_cap_per_qual(band)
    counts = daily_band_qual_count(
        frame, row_lookup, employees_by_id, qual_codes, day, band
    )
    row_idx = row_lookup.get(employee_id)
    if row_idx is None:
        return False
    existing = get_grid_token(frame, row_idx, day)
    if existing == band:
        return True
    return counts.get(qual, 0) < cap


def _would_violate_work_cap_for_block(
    frame: pd.DataFrame,
    row_idx: int,
    block_days: Sequence[date],
    dates: Sequence[date],
    *,
    max_consecutive: int,
) -> bool:
    from lab_scheduler.compliance.engine import _consecutive_work_day_streaks

    work_days = {
        day
        for day in dates
        if get_grid_token(frame, row_idx, day) in {"D", "E", "N"}
    }
    work_days.update(block_days)
    simulated = sorted(work_days)
    for _start, _end, length in _consecutive_work_day_streaks(simulated):
        if length > max_consecutive:
            return True
    return False


def apply_planned_shifts(
    frame: pd.DataFrame,
    planned: Sequence[PlannedShift],
    *,
    row_lookup: Mapping[str, int],
    employees_by_id: Mapping[str, EmployeeProfile],
    qual_codes: Mapping[str, str],
    dates: Sequence[date],
    locked_cells: Set[Tuple[str, date]],
    blocked_map: Mapping[str, Mapping[date, str]],
    validate_labor: Optional[object] = None,
) -> ApplyResult:
    """Place planned shifts; respect locks, qual caps, and 7-day D/E block streak rule."""
    changed = 0
    conflicts: List[PlacementConflict] = []
    block_days_by_employee: dict[str, list[date]] = {}
    for shift in planned:
        if shift.reason_code == RotationReasonCode.DE_EVENING_BLOCK_WEEK:
            block_days_by_employee.setdefault(shift.employee_id, []).append(shift.day)

    for employee_id, days in block_days_by_employee.items():
        row_idx = row_lookup.get(employee_id)
        if row_idx is None:
            continue
        if _would_violate_work_cap_for_block(
            frame,
            row_idx,
            sorted(days),
            dates,
            max_consecutive=FT_DE_EVENING_BLOCK_STREAK_DAYS,
        ):
            conflicts.append(
                PlacementConflict(
                    employee_id=employee_id,
                    day=days[0],
                    band="E",
                    reason="seven_day_block_exceeds_streak_cap",
                )
            )

    placed_block_employees: Set[str] = {
        eid
        for eid, days in block_days_by_employee.items()
        if not any(c.employee_id == eid for c in conflicts)
    }

    for shift in planned:
        if (
            shift.reason_code == RotationReasonCode.DE_EVENING_BLOCK_WEEK
            and shift.employee_id not in placed_block_employees
        ):
            continue
        row_idx = row_lookup.get(shift.employee_id)
        if row_idx is None:
            continue
        if not is_editable_cell(
            shift.employee_id,
            shift.day,
            locked_cells=locked_cells,
            blocked_map=blocked_map,
        ):
            conflicts.append(
                PlacementConflict(
                    shift.employee_id,
                    shift.day,
                    shift.band,
                    "locked_or_blocked",
                )
            )
            continue
        token = get_grid_token(frame, row_idx, shift.day)
        if token == shift.band:
            continue
        if token not in {"", "D"}:
            conflicts.append(
                PlacementConflict(
                    shift.employee_id,
                    shift.day,
                    shift.band,
                    f"occupied_by_{token}",
                )
            )
            continue
        if not _passes_weekday_qual_cap(
            frame,
            row_lookup,
            employees_by_id,
            qual_codes,
            employee_id=shift.employee_id,
            day=shift.day,
            band=shift.band,
        ):
            conflicts.append(
                PlacementConflict(
                    shift.employee_id,
                    shift.day,
                    shift.band,
                    "qual_cap",
                )
            )
            continue
        if validate_labor is not None and shift.reason_code not in {
            RotationReasonCode.DE_EVENING_BLOCK_WEEK,
            RotationReasonCode.DE_EVENING_EXTRA_WEEKDAY,
        }:
            if not validate_labor(
                employee_id=shift.employee_id,
                day=shift.day,
                band=shift.band,
            ):
                conflicts.append(
                    PlacementConflict(
                        shift.employee_id,
                        shift.day,
                        shift.band,
                        "labor_validation",
                    )
                )
                continue
        if token == "D":
            if set_grid_token(frame, row_idx, shift.day, shift.band):
                changed += 1
            continue
        if is_empty_grid_token(frame.at[row_idx, shift.day.isoformat()]):
            if set_grid_token(frame, row_idx, shift.day, shift.band):
                changed += 1
    return ApplyResult(cells_changed=changed, conflicts=conflicts)
