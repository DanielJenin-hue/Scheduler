"""Reference Portage DE rotation: staggered 7+1 E blocks and balanced weekday Day fill."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Dict, List, Mapping, Sequence, Set, Tuple

import pandas as pd

from lab_scheduler.compliance.jurisdictions import JurisdictionRules
from lab_scheduler.scheduling.portage_equity_targets import (
    PORTAGE_DN_FT_NIGHT_SHIFT_TARGET,
    PORTAGE_DN_FT_PERIOD_WORK_SHIFTS,
    portage_contract_shift_count,
    portage_is_fulltime_catalog_hours,
)
from lab_scheduler.scheduling.profiles import EmployeeProfile
from lab_scheduler.scheduling.schedule_tallies import WEEKDAY_DAY_BALANCE_TOLERANCE
from lab_scheduler.scheduling.weekend_placement_rules import (
    OFF_DISPLAY,
    get_grid_token,
    is_editable_cell,
    is_empty_grid_token,
    set_grid_token,
)
from lab_scheduler.solver.cpsat_fill import is_vacant_portage_line


def _weekday_day_count(
    frame: pd.DataFrame,
    row_lookup: Mapping[str, int],
    day: date,
) -> int:
    return sum(
        1
        for row_idx in row_lookup.values()
        if get_grid_token(frame, row_idx, day) == "D"
    )


def _weekday_day_counts(
    frame: pd.DataFrame,
    row_lookup: Mapping[str, int],
    weekdays: Sequence[date],
) -> Dict[date, int]:
    return {day: _weekday_day_count(frame, row_lookup, day) for day in weekdays}


def _de_e_block_days_for_employee(
    employee: EmployeeProfile,
    *,
    period_start: date,
    dates: Sequence[date],
) -> Set[date]:
    """Mon–Sun E-block week for this FT D/E line (matches rotation_planner)."""
    from lab_scheduler.scheduling.alternate_shift_distributor import (
        _vacant_catalog_line_number,
    )

    line_no = _vacant_catalog_line_number(employee)
    if line_no is None:
        return set()
    weeks_in_period = max(1, (max(dates) - period_start).days // 7 + 1)
    week_index = (line_no - 1) % weeks_in_period
    monday = period_start + timedelta(weeks=week_index)
    date_set = set(dates)
    return {
        monday + timedelta(days=offset)
        for offset in range(7)
        if (monday + timedelta(days=offset)) in date_set
    }


def _rest_day_after_e_block(block_days: Set[date]) -> Set[date]:
    """Skip the weekday immediately after a 7-day E block (8th consecutive work day)."""
    if not block_days:
        return set()
    return {max(block_days) + timedelta(days=1)}


def _stagger_skip_days_for_e_block(block_days: Set[date]) -> Set[date]:
    """Block week plus Sun before Mon block start (avoids 8-day streak into the block)."""
    if not block_days:
        return set()
    skip = set(block_days)
    block_monday = min(block_days)
    if block_monday.weekday() == 0:
        skip.add(block_monday - timedelta(days=1))
    return skip


def _would_placing_day_violate_work_cap(
    frame: pd.DataFrame,
    row_idx: int,
    day: date,
    *,
    dates: Sequence[date],
    rules: JurisdictionRules,
) -> bool:
    """
    True when placing a work shift on ``day`` would exceed the Portage cap.

    Unlike the global row check, only the streak *containing* ``day`` is
    evaluated so an existing 7-day E block (streak exception) does not block
    unrelated weekday Day placements after the mandatory rest day.
    """
    from lab_scheduler.compliance.engine import _consecutive_work_day_streaks
    from lab_scheduler.engine.demand import PORTAGE_MAX_CONSECUTIVE_WORK_DAYS
    from lab_scheduler.scheduling.preference_fill import _work_dates_for_row

    simulated = sorted(_work_dates_for_row(frame, row_idx, dates) | {day})
    for start, end, length in _consecutive_work_day_streaks(simulated):
        if start <= day <= end:
            if length > PORTAGE_MAX_CONSECUTIVE_WORK_DAYS:
                return True
            break
    if rules.max_work_days_per_work_week is not None:
        week_start = day - timedelta(days=day.weekday())
        week_end = week_start + timedelta(days=6)
        worked_in_week = sum(
            1 for work_day in simulated if week_start <= work_day <= week_end
        )
        if worked_in_week > rules.max_work_days_per_work_week:
            return True
    return False


def _even_weekday_day_targets(
    weekdays: Sequence[date],
    total_shifts: int,
) -> Dict[date, int]:
    if not weekdays or total_shifts <= 0:
        return {day: 0 for day in weekdays}
    base = total_shifts // len(weekdays)
    remainder = total_shifts % len(weekdays)
    return {
        day: base + (1 if index < remainder else 0)
        for index, day in enumerate(weekdays)
    }


def collect_de_ft_weekday_deficits(
    frame: pd.DataFrame,
    *,
    frame_order: Sequence[str],
    row_lookup: Mapping[str, int],
    employees_by_id: Mapping[str, EmployeeProfile],
    dates: Sequence[date],
    employee_target_hours: Mapping[str, float],
) -> Dict[str, int]:
    """Payroll shift deficits for full-time D/E lines before weekday Day fill."""
    from lab_scheduler.scheduling.preference_fill import _count_work_shifts

    deficits: Dict[str, int] = {}
    for employee_id in frame_order:
        profile = employees_by_id.get(employee_id)
        row_idx = row_lookup.get(employee_id)
        if profile is None or row_idx is None:
            continue
        if not is_vacant_portage_line(profile.full_name):
            continue
        if (profile.contract_line_type or "").upper() != "D/E":
            continue
        hours = float(employee_target_hours.get(employee_id, 0.0))
        if not portage_is_fulltime_catalog_hours(hours):
            continue
        cap = portage_contract_shift_count(hours)
        deficit = cap - _count_work_shifts(frame, row_idx, dates)
        if deficit > 0:
            deficits[employee_id] = deficit
    return deficits


def collect_dn_ft_weekday_deficits(
    frame: pd.DataFrame,
    *,
    frame_order: Sequence[str],
    row_lookup: Mapping[str, int],
    employees_by_id: Mapping[str, EmployeeProfile],
    dates: Sequence[date],
    employee_target_hours: Mapping[str, float],
) -> Dict[str, int]:
    """Payroll shift deficits for full-time D/N lines before weekday Day fill."""
    from lab_scheduler.scheduling.preference_fill import _count_work_shifts

    deficits: Dict[str, int] = {}
    for employee_id in frame_order:
        profile = employees_by_id.get(employee_id)
        row_idx = row_lookup.get(employee_id)
        if profile is None or row_idx is None:
            continue
        if not is_vacant_portage_line(profile.full_name):
            continue
        if (profile.contract_line_type or "").upper() != "D/N":
            continue
        hours = float(employee_target_hours.get(employee_id, 0.0))
        if not portage_is_fulltime_catalog_hours(hours):
            continue
        cap = portage_contract_shift_count(hours)
        deficit = cap - _count_work_shifts(frame, row_idx, dates)
        if deficit > 0:
            deficits[employee_id] = deficit
    return deficits


def _placing_dn_day_creates_adjacency(
    frame: pd.DataFrame,
    row_idx: int,
    day: date,
    dates: Sequence[date],
) -> bool:
    """True when placing D on ``day`` would create an illegal D→N handoff."""
    date_set = set(dates)
    next_day = day + timedelta(days=1)
    if next_day in date_set and get_grid_token(frame, row_idx, next_day) == "N":
        return True
    return False


def _ft_dn_day_eligible_for_d(
    frame: pd.DataFrame,
    *,
    employee_id: str,
    row_idx: int,
    day: date,
    locked_cells: Set[Tuple[str, date]],
    blocked_map: Mapping[str, Mapping[date, str]],
    dates: Sequence[date],
    rules: JurisdictionRules,
) -> bool:
    if day.weekday() >= 5:
        return False
    if get_grid_token(frame, row_idx, day) == "N":
        return False
    if not is_editable_cell(
        employee_id,
        day,
        locked_cells=locked_cells,
        blocked_map=blocked_map,
    ):
        return False
    if not is_empty_grid_token(frame.at[row_idx, day.isoformat()]):
        return False
    if _placing_dn_day_creates_adjacency(frame, row_idx, day, dates):
        return False
    return not _would_placing_day_violate_work_cap(
        frame, row_idx, day, dates=dates, rules=rules
    )


def dn_ft_weekday_day_target() -> int:
    """Weekday Day count per FT D/N line when nights meet catalog target."""
    return PORTAGE_DN_FT_PERIOD_WORK_SHIFTS - PORTAGE_DN_FT_NIGHT_SHIFT_TARGET


def compute_de_weekday_day_targets(
    frame: pd.DataFrame,
    *,
    row_lookup: Mapping[str, int],
    employees_by_id: Mapping[str, EmployeeProfile],
    dates: Sequence[date],
    period_start: date,
    employee_target_hours: Mapping[str, float],
    ft_deficits: Mapping[str, int],
    dn_ft_deficits: Mapping[str, int] | None = None,
) -> Dict[date, int]:
    """Even per-weekday D targets from current grid + remaining FT/PT payroll fill."""
    from lab_scheduler.scheduling.portage_template import portage_master_line_spec
    from lab_scheduler.scheduling.preference_fill import _count_work_shifts

    weekdays = [day for day in dates if day.weekday() < 5]
    current = sum(_weekday_day_counts(frame, row_lookup, weekdays).values())
    pt_remaining = 0
    for employee_id, profile in employees_by_id.items():
        if profile is None or not is_vacant_portage_line(profile.full_name):
            continue
        if (profile.contract_line_type or "").upper() != "D/E":
            continue
        if portage_is_fulltime_catalog_hours(
            float(employee_target_hours.get(employee_id, 0.0))
        ):
            continue
        if portage_master_line_spec(profile) is None:
            continue
        row_idx = row_lookup.get(employee_id)
        if row_idx is None:
            continue
        cap = portage_contract_shift_count(
            float(employee_target_hours.get(employee_id, 0.0))
        )
        pt_remaining += max(0, cap - _count_work_shifts(frame, row_idx, dates))
    dn_remaining = sum((dn_ft_deficits or {}).values())
    total = current + sum(ft_deficits.values()) + pt_remaining + dn_remaining
    return _even_weekday_day_targets(weekdays, total)


def _employee_weekday_d_in_week(
    frame: pd.DataFrame,
    row_idx: int,
    day: date,
) -> int:
    week_start = day - timedelta(days=day.weekday())
    week_end = week_start + timedelta(days=4)
    count = 0
    cursor = week_start
    while cursor <= week_end:
        if get_grid_token(frame, row_idx, cursor) == "D":
            count += 1
        cursor += timedelta(days=1)
    return count


def _ft_de_day_eligible_for_d(
    frame: pd.DataFrame,
    *,
    employee_id: str,
    row_idx: int,
    day: date,
    block_days: Set[date],
    rest_days: Set[date],
    locked_cells: Set[Tuple[str, date]],
    blocked_map: Mapping[str, Mapping[date, str]],
    dates: Sequence[date],
    rules: JurisdictionRules,
) -> bool:
    if day in block_days or day in rest_days:
        return False
    if not is_editable_cell(
        employee_id,
        day,
        locked_cells=locked_cells,
        blocked_map=blocked_map,
    ):
        return False
    if not is_empty_grid_token(frame.at[row_idx, day.isoformat()]):
        return False
    return not _would_placing_day_violate_work_cap(
        frame, row_idx, day, dates=dates, rules=rules
    )


def rebalance_weekday_day_shifts(
    frame: pd.DataFrame,
    *,
    frame_order: Sequence[str],
    row_lookup: Mapping[str, int],
    employees_by_id: Mapping[str, EmployeeProfile],
    dates: Sequence[date],
    period_start: date,
    rules: JurisdictionRules,
    locked_cells: Set[Tuple[str, date]],
    blocked_map: Mapping[str, Mapping[date, str]],
    employee_target_hours: Mapping[str, float],
    only_fulltime: bool | None = None,
) -> int:
    """Move weekday D shifts from heavy days to light days within ±1 tolerance."""
    weekdays = [day for day in dates if day.weekday() < 5]
    if not weekdays:
        return 0

    e_block_by_employee: Dict[str, Set[date]] = {}
    dn_ft_employees: Set[str] = set()
    for employee_id in frame_order:
        profile = employees_by_id.get(employee_id)
        if profile is None:
            continue
        if not is_vacant_portage_line(profile.full_name):
            continue
        contract = (profile.contract_line_type or "").upper()
        hours = float(employee_target_hours.get(employee_id, 0.0))
        is_ft = portage_is_fulltime_catalog_hours(hours)
        if only_fulltime is True and not is_ft:
            continue
        if only_fulltime is False and is_ft:
            continue
        if contract == "D/E":
            e_block_by_employee[employee_id] = _de_e_block_days_for_employee(
                profile, period_start=period_start, dates=dates
            )
        elif contract == "D/N" and is_ft:
            dn_ft_employees.add(employee_id)

    rebalance_pool = set(e_block_by_employee) | dn_ft_employees

    changed = 0
    for _ in range(len(weekdays) * len(frame_order)):
        counts = _weekday_day_counts(frame, row_lookup, weekdays)
        lo = min(counts.values())
        hi = max(counts.values())
        if hi - lo <= WEEKDAY_DAY_BALANCE_TOLERANCE:
            break
        heavy = max(weekdays, key=lambda day: counts[day])
        light = min(weekdays, key=lambda day: counts[day])
        moved = False
        for employee_id in frame_order:
            profile = employees_by_id.get(employee_id)
            row_idx = row_lookup.get(employee_id)
            if profile is None or row_idx is None:
                continue
            if employee_id not in rebalance_pool:
                continue
            if get_grid_token(frame, row_idx, heavy) != "D":
                continue
            hours = float(employee_target_hours.get(employee_id, 0.0))
            is_ft = portage_is_fulltime_catalog_hours(hours)
            contract = (profile.contract_line_type or "").upper()
            block_days = e_block_by_employee.get(employee_id, set()) if is_ft else set()
            rest_days = _rest_day_after_e_block(block_days) if contract == "D/E" and is_ft else set()
            if not is_editable_cell(
                employee_id,
                heavy,
                locked_cells=locked_cells,
                blocked_map=blocked_map,
            ):
                continue
            if contract == "D/N":
                light_ok = _ft_dn_day_eligible_for_d(
                    frame,
                    employee_id=employee_id,
                    row_idx=row_idx,
                    day=light,
                    locked_cells=locked_cells,
                    blocked_map=blocked_map,
                    dates=dates,
                    rules=rules,
                )
            else:
                light_ok = _ft_de_day_eligible_for_d(
                    frame,
                    employee_id=employee_id,
                    row_idx=row_idx,
                    day=light,
                    block_days=block_days,
                    rest_days=rest_days,
                    locked_cells=locked_cells,
                    blocked_map=blocked_map,
                    dates=dates,
                    rules=rules,
                )
            if not light_ok:
                continue
            if not set_grid_token(frame, row_idx, heavy, OFF_DISPLAY):
                continue
            if _would_placing_day_violate_work_cap(
                frame, row_idx, light, dates=dates, rules=rules
            ):
                set_grid_token(frame, row_idx, heavy, "D")
                continue
            if set_grid_token(frame, row_idx, light, "D"):
                changed += 1
                moved = True
                break
            set_grid_token(frame, row_idx, heavy, "D")
        if not moved:
            break
    return changed


def fill_de_ft_weekday_days_balanced(
    frame: pd.DataFrame,
    *,
    frame_order: Sequence[str],
    row_lookup: Mapping[str, int],
    employees_by_id: Mapping[str, EmployeeProfile],
    dates: Sequence[date],
    period_start: date,
    rules: JurisdictionRules,
    locked_cells: Set[Tuple[str, date]],
    blocked_map: Mapping[str, Mapping[date, str]],
    employee_target_hours: Mapping[str, float],
    pool_index_by_employee: Mapping[str, int],
    weekday_day_targets: Mapping[date, int] | None = None,
) -> int:
    """
    Fill remaining weekday Day shifts on full-time D/E lines.

    Uses load-balanced placement (lowest lab-wide D count first) so weekday
    footer totals stay even — matching the manual reference grid instead of
    stamping every catalog-D day onto every line.
    """
    weekdays = [day for day in dates if day.weekday() < 5]
    deficits = collect_de_ft_weekday_deficits(
        frame,
        frame_order=frame_order,
        row_lookup=row_lookup,
        employees_by_id=employees_by_id,
        dates=dates,
        employee_target_hours=employee_target_hours,
    )
    e_block_by_employee: Dict[str, Set[date]] = {}
    for employee_id in deficits:
        profile = employees_by_id.get(employee_id)
        if profile is None:
            continue
        e_block_by_employee[employee_id] = _de_e_block_days_for_employee(
            profile, period_start=period_start, dates=dates
        )

    if weekday_day_targets is None:
        weekday_day_targets = compute_de_weekday_day_targets(
            frame,
            row_lookup=row_lookup,
            employees_by_id=employees_by_id,
            dates=dates,
            period_start=period_start,
            employee_target_hours=employee_target_hours,
            ft_deficits=deficits,
        )

    changed = 0
    while deficits:
        day_counts = _weekday_day_counts(frame, row_lookup, weekdays)
        progressed = False
        for employee_id in frame_order:
            deficit = deficits.get(employee_id, 0)
            if deficit <= 0:
                continue
            row_idx = row_lookup[employee_id]
            block_days = e_block_by_employee.get(employee_id, set())
            rest_days = _rest_day_after_e_block(block_days)
            candidate_days = sorted(
                (
                    day
                    for day in weekdays
                    if _ft_de_day_eligible_for_d(
                        frame,
                        employee_id=employee_id,
                        row_idx=row_idx,
                        day=day,
                        block_days=block_days,
                        rest_days=rest_days,
                        locked_cells=locked_cells,
                        blocked_map=blocked_map,
                        dates=dates,
                        rules=rules,
                    )
                ),
                key=lambda day: (
                    day_counts[day] - weekday_day_targets.get(day, day_counts[day]),
                    day_counts[day],
                    _employee_weekday_d_in_week(frame, row_idx, day),
                    day.toordinal(),
                ),
            )
            if not candidate_days:
                continue
            below_target = [
                day
                for day in candidate_days
                if day_counts[day] < weekday_day_targets.get(day, day_counts[day] + 1)
            ]
            day = (below_target or candidate_days)[0]
            if set_grid_token(frame, row_idx, day, "D"):
                changed += 1
                deficits[employee_id] -= 1
                if deficits[employee_id] <= 0:
                    del deficits[employee_id]
                day_counts[day] += 1
                progressed = True
        if not progressed:
            break
    return changed


def fill_dn_ft_weekday_days_balanced(
    frame: pd.DataFrame,
    *,
    frame_order: Sequence[str],
    row_lookup: Mapping[str, int],
    employees_by_id: Mapping[str, EmployeeProfile],
    dates: Sequence[date],
    rules: JurisdictionRules,
    locked_cells: Set[Tuple[str, date]],
    blocked_map: Mapping[str, Mapping[date, str]],
    employee_target_hours: Mapping[str, float],
    weekday_day_targets: Mapping[date, int] | None = None,
) -> int:
    """
    Fill remaining weekday Day shifts on full-time D/N lines.

    Nights are stamped from the master catalog first; this pass fills day-band
    weeks on empty weekday cells while respecting D→N adjacency and payroll cap.
    """
    weekdays = [day for day in dates if day.weekday() < 5]
    deficits = collect_dn_ft_weekday_deficits(
        frame,
        frame_order=frame_order,
        row_lookup=row_lookup,
        employees_by_id=employees_by_id,
        dates=dates,
        employee_target_hours=employee_target_hours,
    )
    if weekday_day_targets is None:
        weekday_day_targets = compute_de_weekday_day_targets(
            frame,
            row_lookup=row_lookup,
            employees_by_id=employees_by_id,
            dates=dates,
            period_start=dates[0],
            employee_target_hours=employee_target_hours,
            ft_deficits={},
            dn_ft_deficits=deficits,
        )

    changed = 0
    while deficits:
        day_counts = _weekday_day_counts(frame, row_lookup, weekdays)
        progressed = False
        for employee_id in frame_order:
            deficit = deficits.get(employee_id, 0)
            if deficit <= 0:
                continue
            row_idx = row_lookup[employee_id]
            candidate_days = sorted(
                (
                    day
                    for day in weekdays
                    if _ft_dn_day_eligible_for_d(
                        frame,
                        employee_id=employee_id,
                        row_idx=row_idx,
                        day=day,
                        locked_cells=locked_cells,
                        blocked_map=blocked_map,
                        dates=dates,
                        rules=rules,
                    )
                ),
                key=lambda day: (
                    day_counts[day] - weekday_day_targets.get(day, day_counts[day]),
                    day_counts[day],
                    _employee_weekday_d_in_week(frame, row_idx, day),
                    day.toordinal(),
                ),
            )
            if not candidate_days:
                continue
            below_target = [
                day
                for day in candidate_days
                if day_counts[day] < weekday_day_targets.get(day, day_counts[day] + 1)
            ]
            day = (below_target or candidate_days)[0]
            if set_grid_token(frame, row_idx, day, "D"):
                changed += 1
                deficits[employee_id] -= 1
                if deficits[employee_id] <= 0:
                    del deficits[employee_id]
                day_counts[day] += 1
                progressed = True
        if not progressed:
            break
    return changed
