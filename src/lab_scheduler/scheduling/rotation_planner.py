"""Pure planner for Portage DE 7-day evening blocks."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from enum import Enum
from typing import Dict, List, Mapping, Optional, Sequence, Set

from lab_scheduler.engine.demand import infer_qual_code
from lab_scheduler.scheduling.portage_equity_targets import portage_is_fulltime_catalog_hours
from lab_scheduler.scheduling.profiles import EmployeeProfile
from lab_scheduler.scheduling.rotation_spec import DEFAULT_EVENING_BLOCK, EveningBlockSpec

# Part-time D/E lines that receive the same staggered Mon–Sun E block as FT lines.
_PT_DE_SEVEN_DAY_BLOCK_LINES: Dict[str, Set[int]] = {
    "MLA": {7, 8},
}


class RotationReasonCode(str, Enum):
    DE_EVENING_BLOCK_WEEK = "de_evening_block_week"
    DE_EVENING_EXTRA_WEEKDAY = "de_evening_extra_weekday"


@dataclass(frozen=True, slots=True)
class PlannedShift:
    employee_id: str
    day: date
    band: str
    reason_code: RotationReasonCode
    reason_detail: Dict[str, object] = field(default_factory=dict)


def _vacant_catalog_line_number(employee: EmployeeProfile) -> Optional[int]:
    from lab_scheduler.scheduling.alternate_shift_distributor import (
        _vacant_catalog_line_number as _line_no,
    )

    return _line_no(employee)


def _ft_de_pool_ids(
    frame_order: Sequence[str],
    *,
    employees_by_id: Mapping[str, EmployeeProfile],
    qual_codes: Mapping[str, str],
    qual: str,
    employee_target_hours: Mapping[str, float],
) -> List[str]:
    pool: List[str] = []
    for employee_id in frame_order:
        employee = employees_by_id.get(employee_id)
        if employee is None:
            continue
        if (employee.contract_line_type or "").upper() != "D/E":
            continue
        if infer_qual_code(employee, qual_codes=qual_codes) != qual:
            continue
        hours = float(employee_target_hours.get(employee_id, 0.0))
        if not portage_is_fulltime_catalog_hours(hours):
            continue
        pool.append(employee_id)
    pool.sort(key=lambda eid: _vacant_catalog_line_number(employees_by_id[eid]) or 0)
    return pool


def _pt_de_seven_day_block_pool_ids(
    frame_order: Sequence[str],
    *,
    employees_by_id: Mapping[str, EmployeeProfile],
    qual_codes: Mapping[str, str],
    qual: str,
    employee_target_hours: Mapping[str, float],
) -> List[str]:
    allowed_lines = _PT_DE_SEVEN_DAY_BLOCK_LINES.get(qual, set())
    if not allowed_lines:
        return []
    pool: List[str] = []
    for employee_id in frame_order:
        employee = employees_by_id.get(employee_id)
        if employee is None:
            continue
        if (employee.contract_line_type or "").upper() != "D/E":
            continue
        if infer_qual_code(employee, qual_codes=qual_codes) != qual:
            continue
        hours = float(employee_target_hours.get(employee_id, 0.0))
        if portage_is_fulltime_catalog_hours(hours):
            continue
        line_no = _vacant_catalog_line_number(employee)
        if line_no is None or line_no not in allowed_lines:
            continue
        pool.append(employee_id)
    pool.sort(key=lambda eid: _vacant_catalog_line_number(employees_by_id[eid]) or 0)
    return pool


def _week_monday(period_start: date, week_index: int) -> date:
    return period_start + timedelta(weeks=week_index)


def _seven_day_block_days(
    period_start: date,
    week_index: int,
    date_set: Set[date],
) -> List[date]:
    monday = _week_monday(period_start, week_index)
    return [
        day
        for offset in range(7)
        if (day := monday + timedelta(days=offset)) in date_set
    ]


def _block_evening_day_cap(
    employee_id: str,
    *,
    employees_by_id: Mapping[str, EmployeeProfile],
    employee_target_hours: Mapping[str, float],
) -> int:
    """Max evening shifts to plan from the Mon–Sun block for one D/E line."""
    from lab_scheduler.scheduling.portage_equity_targets import (
        portage_hours_weighted_de_alt_target,
    )

    hours = float(employee_target_hours.get(employee_id, 0.0))
    return portage_hours_weighted_de_alt_target(hours)


def _append_seven_day_blocks(
    planned: List[PlannedShift],
    *,
    pool: Sequence[str],
    employees_by_id: Mapping[str, EmployeeProfile],
    employee_target_hours: Mapping[str, float],
    period_start: date,
    date_set: Set[date],
    weeks_in_period: int,
    qual: str,
    peer_block_days_for_eighth: Set[date],
    stagger_assignments: Mapping[str, frozenset[date]],
    plan_eighth: bool,
    append_blocks: bool = True,
) -> Set[date]:
    """Plan Mon–Sun E blocks for ``pool``; optionally add one 8th weekday E per line."""
    block_days_by_employee: Dict[str, Set[date]] = {}
    meta_by_employee: Dict[str, tuple[int, int]] = {}
    for employee_id in pool:
        employee = employees_by_id[employee_id]
        line_no = _vacant_catalog_line_number(employee)
        if line_no is None:
            continue
        week_index = (line_no - 1) % weeks_in_period
        block_days = _seven_day_block_days(period_start, week_index, date_set)
        block_days_by_employee[employee_id] = set(block_days)
        meta_by_employee[employee_id] = (line_no, week_index)

    all_block_days: Set[date] = set()
    for days in block_days_by_employee.values():
        all_block_days.update(days)

    used_eighth_days: Set[date] = set()
    for employee_id in pool:
        if employee_id not in block_days_by_employee:
            continue
        line_no, week_index = meta_by_employee[employee_id]
        block_day_set = block_days_by_employee[employee_id]
        if append_blocks:
            day_cap = _block_evening_day_cap(
                employee_id,
                employees_by_id=employees_by_id,
                employee_target_hours=employee_target_hours,
            )
            for day in sorted(block_day_set)[:day_cap]:
                planned.append(
                    PlannedShift(
                        employee_id=employee_id,
                        day=day,
                        band="E",
                        reason_code=RotationReasonCode.DE_EVENING_BLOCK_WEEK,
                        reason_detail={
                            "line_no": line_no,
                            "week_index": week_index,
                            "qual": qual,
                        },
                    )
                )
        if not plan_eighth:
            continue
        target = _block_evening_day_cap(
            employee_id,
            employees_by_id=employees_by_id,
            employee_target_hours=employee_target_hours,
        )
        if target <= len(block_day_set):
            continue
        other_block_days = peer_block_days_for_eighth - block_day_set
        eighth = _plan_eighth_evening_day(
            line_no=line_no,
            dates=sorted(date_set),
            block_days=block_day_set,
            other_peer_block_days=other_block_days,
            stagger_block=stagger_assignments.get(employee_id, frozenset()),
            used_eighth_days=used_eighth_days,
        )
        if eighth is not None:
            used_eighth_days.add(eighth)
            planned.append(
                PlannedShift(
                    employee_id=employee_id,
                    day=eighth,
                    band="E",
                    reason_code=RotationReasonCode.DE_EVENING_EXTRA_WEEKDAY,
                    reason_detail={"line_no": line_no, "qual": qual},
                )
            )
    return all_block_days


def plan_de_seven_day_evening_blocks(
    *,
    frame_order: Sequence[str],
    employees_by_id: Mapping[str, EmployeeProfile],
    qual_codes: Mapping[str, str],
    employee_target_hours: Mapping[str, float],
    period_start: date,
    dates: Sequence[date],
    stagger_assignments: Optional[Mapping[str, frozenset[date]]] = None,
    spec: EveningBlockSpec = DEFAULT_EVENING_BLOCK,
) -> List[PlannedShift]:
    """One Mon–Sun E week per D/E line (FT + selected PT), staggered by line number."""
    date_set = set(dates)
    weeks_in_period = max(1, (max(dates) - period_start).days // 7 + 1)
    planned: List[PlannedShift] = []
    stagger_assignments = stagger_assignments or {}

    for qual in ("MLT", "MLA"):
        ft_pool = _ft_de_pool_ids(
            frame_order,
            employees_by_id=employees_by_id,
            qual_codes=qual_codes,
            qual=qual,
            employee_target_hours=employee_target_hours,
        )
        pt_pool = _pt_de_seven_day_block_pool_ids(
            frame_order,
            employees_by_id=employees_by_id,
            qual_codes=qual_codes,
            qual=qual,
            employee_target_hours=employee_target_hours,
        )
        if not ft_pool and not pt_pool:
            continue
        ft_block_days = _append_seven_day_blocks(
            planned,
            pool=ft_pool,
            employees_by_id=employees_by_id,
            employee_target_hours=employee_target_hours,
            period_start=period_start,
            date_set=date_set,
            weeks_in_period=weeks_in_period,
            qual=qual,
            peer_block_days_for_eighth=set(),
            stagger_assignments=stagger_assignments,
            plan_eighth=False,
        )
        pt_block_days: Set[date] = set()
        if pt_pool:
            pt_block_days = _append_seven_day_blocks(
                planned,
                pool=pt_pool,
                employees_by_id=employees_by_id,
                employee_target_hours=employee_target_hours,
                period_start=period_start,
                date_set=date_set,
                weeks_in_period=weeks_in_period,
                qual=qual,
                peer_block_days_for_eighth=set(),
                stagger_assignments=stagger_assignments,
                plan_eighth=False,
            )
        _append_seven_day_blocks(
            planned,
            pool=ft_pool,
            employees_by_id=employees_by_id,
            employee_target_hours=employee_target_hours,
            period_start=period_start,
            date_set=date_set,
            weeks_in_period=weeks_in_period,
            qual=qual,
            peer_block_days_for_eighth=ft_block_days | pt_block_days,
            stagger_assignments=stagger_assignments,
            plan_eighth=True,
            append_blocks=False,
        )
    return planned


def _plan_eighth_evening_day(
    *,
    line_no: int,
    dates: Sequence[date],
    block_days: Set[date],
    other_peer_block_days: Set[date],
    stagger_block: frozenset[date],
    used_eighth_days: Set[date],
) -> Optional[date]:
    """8th E on a day outside own block, outside peers' blocks, and unique within qual pool."""
    candidates: List[date] = []
    if line_no <= 4:
        for day in sorted(stagger_block):
            if day.weekday() < 5:
                continue
            if day in block_days or day in other_peer_block_days:
                continue
            if day in used_eighth_days:
                continue
            candidates.append(day)
    for day in dates:
        if day.weekday() >= 5:
            continue
        if day in block_days or day in other_peer_block_days:
            continue
        if day in used_eighth_days:
            continue
        candidates.append(day)
    return candidates[0] if candidates else None
