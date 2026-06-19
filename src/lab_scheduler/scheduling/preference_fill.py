"""Preference-driven schedule fill for vacant Portage lines."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import DefaultDict, Dict, List, Mapping, Optional, Sequence, Set, Tuple
from collections import defaultdict

import pandas as pd

from lab_scheduler.compliance.engine import ShiftTemplateInfo
from lab_scheduler.compliance.jurisdictions import JurisdictionRules
from lab_scheduler.engine.demand import infer_qual_code
from lab_scheduler.policy.frame_bridge import (
    assignments_from_schedule_frame,
    normalize_grid_shift_token,
    schedule_frame_row_index_by_employee_id,
    template_id_from_short,
)
from lab_scheduler.scheduling.alternate_shift_distributor import (
    enumerate_staggered_weekend_blocks,
)
from lab_scheduler.scheduling.assignment_validation import validate_assignment_change
from lab_scheduler.scheduling.employee_scheduling_profile import (
    EmployeeSchedulingProfile,
    build_portage_scheduling_profiles,
)
from lab_scheduler.scheduling.portage_equity_targets import (
    PORTAGE_DN_FT_NIGHT_SHIFT_TARGET,
    portage_alt_shift_target_for_employee,
    portage_contract_shift_count,
    portage_is_fulltime_catalog_hours,
)
from lab_scheduler.scheduling.preference_policy import (
    FillMode,
    SchedulingPreferencePolicy,
    SlotTier,
    tiers_for_mode,
)
from lab_scheduler.scheduling.rotation_applicator import apply_planned_shifts
from lab_scheduler.scheduling.rotation_planner import plan_de_seven_day_evening_blocks
from lab_scheduler.scheduling.rotation_spec import FT_DE_EVENING_BLOCK_STREAK_DAYS
from lab_scheduler.scheduling.profiles import EmployeeProfile
from lab_scheduler.scheduling.schedule_tallies import shift_target_for_date
from lab_scheduler.scheduling.weekend_placement_rules import (
    can_place_daily_alt,
    can_place_weekend_token,
    daily_band_cap,
    daily_band_qual_count,
    get_grid_token,
    is_editable_cell,
    is_empty_grid_token,
    mirror_weekend_partner,
    set_grid_token,
    operational_alt_band_cap_per_qual,
    weekend_alt_band_cap_per_qual,
    weekend_band_qual_count,
)

_OFF_DISPLAY = "—"
OFF_DISPLAY = _OFF_DISPLAY
_FT_DE_WEEKDAY_EVENING_RUN = 7
_FT_DE_WEEKEND_PAIR_DAYS = 2


def _clinical_evening_cap_per_qual(_day: date) -> int:
    """Footer-aligned alternate cap: one MLT + one MLA evening per day (2 total)."""
    return operational_alt_band_cap_per_qual("E")

_WEEKDAY_ALT_QUAL_FLOOR = 1
_DN_SACRIFICE_WEEKDAY = 4  # Friday off in 7N weeks: preserves Wed pool nights under 6-day cap




@dataclass(slots=True)
class PreferenceFillResult:
    cells_changed: int = 0
    lines_touched: int = 0
    stagger_applied: bool = False
    warnings: List[str] = field(default_factory=list)
    tier_counts: Dict[str, int] = field(default_factory=dict)


def _employees_to_profiles(
    employees: Sequence[Mapping[str, object]],
    *,
    emp_quals: Mapping[str, Set[str]],
) -> List[EmployeeProfile]:
    return [
        EmployeeProfile(
            id=str(employee["id"]),
            full_name=str(employee.get("full_name") or employee["id"]),
            fte=float(employee.get("fte") or 1.0),
            qualification_ids=set(emp_quals.get(str(employee["id"]), set())),
            contract_line_type=employee.get("contract_line_type"),
        )
        for employee in employees
    ]


def build_pool_stagger_assignments(
    profiles: Mapping[str, EmployeeSchedulingProfile],
    *,
    period_start: date,
    period_end: date,
    frame_order: Sequence[str],
    employees_by_id: Optional[Mapping[str, EmployeeProfile]] = None,
    employee_target_hours: Optional[Mapping[str, float]] = None,
) -> Dict[str, frozenset[date]]:
    from lab_scheduler.scheduling.alternate_shift_distributor import (
        _included_in_weekend_stagger_distribution,
        _vacant_catalog_line_number,
        _weekend_stagger_block_index,
    )

    blocks = enumerate_staggered_weekend_blocks(period_start, period_end)
    if not blocks:
        return {}
    assignments: Dict[str, frozenset[date]] = {}
    for employee_id in frame_order:
        profile = profiles.get(employee_id)
        if profile is None or not profile.eligible_for_fill:
            continue
        employee = (
            employees_by_id.get(employee_id) if employees_by_id is not None else None
        )
        if employee is None:
            block_index = profile.pool_index % len(blocks)
        else:
            target_hours = float(
                (employee_target_hours or {}).get(employee_id, profile.catalog_hours)
            )
            line_no = _vacant_catalog_line_number(employee)
            contract = (employee.contract_line_type or "").upper()
            included = _included_in_weekend_stagger_distribution(employee, target_hours)
            if not included:
                continue
            if line_no is None:
                continue
            block_index = _weekend_stagger_block_index(line_no)
            block_index = min(block_index, len(blocks) - 1)
        if block_index >= len(blocks):
            continue
        assignments[employee_id] = blocks[block_index]
    return assignments


def _band_for_tier(tier: SlotTier, profile: EmployeeSchedulingProfile) -> str:
    if tier == SlotTier.WEEKEND_DAY or tier == SlotTier.WEEKDAY_DAY:
        return "D"
    if tier in {SlotTier.WEEKEND_ALT, SlotTier.WEEKDAY_ALT}:
        if tier == SlotTier.WEEKEND_ALT and profile.weekend_band in {"E", "N"}:
            return profile.weekend_band
        return profile.alternate_band
    return "D"


def _count_assigned_tier(
    frame: pd.DataFrame,
    row_idx: int,
    dates: Sequence[date],
    tier: SlotTier,
    profile: EmployeeSchedulingProfile,
) -> int:
    count = 0
    for day in dates:
        band = get_grid_token(frame, row_idx, day)
        if band not in {"D", "E", "N"}:
            continue
        from lab_scheduler.scheduling.preference_policy import resolve_slot_tier

        if resolve_slot_tier(day, band, profile.contract_line_type) == tier:
            count += 1
    return count


def _count_work_shifts(
    frame: pd.DataFrame,
    row_idx: int,
    dates: Sequence[date],
) -> int:
    return sum(
        1
        for day in dates
        if get_grid_token(frame, row_idx, day) in {"D", "E", "N"}
    )


def _count_band_by_qual_on_day(
    frame: pd.DataFrame,
    row_lookup: Mapping[str, int],
    employees_by_id: Mapping[str, EmployeeProfile],
    qual_codes: Mapping[str, str],
    day: date,
    band: str,
) -> Dict[str, int]:
    counts = {"MLT": 0, "MLA": 0}
    for employee_id, row_idx in row_lookup.items():
        employee = employees_by_id.get(employee_id)
        if employee is None:
            continue
        if get_grid_token(frame, row_idx, day) != band:
            continue
        qual = infer_qual_code(employee, qual_codes=qual_codes).upper()
        if qual in counts:
            counts[qual] += 1
    return counts


def _work_dates_for_row(
    frame: pd.DataFrame,
    row_idx: int,
    dates: Sequence[date],
) -> Set[date]:
    return {
        day
        for day in dates
        if get_grid_token(frame, row_idx, day) in {"D", "E", "N"}
    }


def _would_violate_consecutive_work_cap(
    frame: pd.DataFrame,
    row_idx: int,
    day: date,
    *,
    dates: Sequence[date],
    rules: JurisdictionRules,
    max_consecutive_days: Optional[int] = None,
) -> bool:
    """True when placing a work shift on ``day`` would exceed the Portage work-day cap."""
    from lab_scheduler.compliance.engine import _consecutive_work_day_streaks
    from lab_scheduler.engine.demand import PORTAGE_MAX_CONSECUTIVE_WORK_DAYS

    limit = (
        max_consecutive_days
        if max_consecutive_days is not None
        else PORTAGE_MAX_CONSECUTIVE_WORK_DAYS
    )
    simulated = sorted(_work_dates_for_row(frame, row_idx, dates) | {day})
    for _start, _end, length in _consecutive_work_day_streaks(simulated):
        if length > limit:
            return True
    if rules.max_work_days_per_work_week is not None:
        week_start = day - timedelta(days=day.weekday())
        week_end = week_start + timedelta(days=6)
        worked_in_week = sum(
            1 for work_day in simulated if week_start <= work_day <= week_end
        )
        if worked_in_week > rules.max_work_days_per_work_week:
            return True
    return False


def _consecutive_night_streak_if_place(
    frame: pd.DataFrame,
    row_idx: int,
    day: date,
    dates: Sequence[date],
) -> int:
    """Night streak length containing ``day`` if N were placed on that date."""
    nights = {work_day for work_day in dates if get_grid_token(frame, row_idx, work_day) == "N"}
    nights.add(day)
    start = day
    while start - timedelta(days=1) in nights:
        start -= timedelta(days=1)
    end = day
    while end + timedelta(days=1) in nights:
        end += timedelta(days=1)
    return (end - start).days + 1


def _would_violate_night_streak_cap(
    frame: pd.DataFrame,
    row_idx: int,
    day: date,
    dates: Sequence[date],
) -> bool:
    from lab_scheduler.scheduling.night_streak_corrector import PORTAGE_MAX_CONSECUTIVE_NIGHTS

    return (
        _consecutive_night_streak_if_place(frame, row_idx, day, dates)
        > PORTAGE_MAX_CONSECUTIVE_NIGHTS
    )


def _catalog_week_index(day: date, period_start: date) -> int:
    return (day - period_start).days // 7


def _is_seven_night_catalog_week(
    spec: object,
    day: date,
    period_start: date,
) -> bool:
    """True when the master catalog assigns N on every Mon–Sun in this ISO week."""
    from lab_scheduler.scheduling.portage_template import _catalog_shift_token_for_date

    week_start = day - timedelta(days=day.weekday())
    for offset in range(7):
        token = _catalog_shift_token_for_date(
            spec,
            week_start + timedelta(days=offset),
            period_start,
        )
        if token != "N":
            return False
    return True


def _clear_row_token_if_work_shift(
    frame: pd.DataFrame,
    row_idx: int,
    day: date,
) -> bool:
    if get_grid_token(frame, row_idx, day) in {"D", "E", "N"}:
        return set_grid_token(frame, row_idx, day, OFF_DISPLAY)
    return False


def _stamp_catalog_shift_allowed(
    frame: pd.DataFrame,
    row_idx: int,
    day: date,
    token: str,
    *,
    profile: EmployeeProfile,
    spec: object,
    dates: Sequence[date],
    rules: JurisdictionRules,
    period_start: date,
    employee_id: str,
) -> bool:
    """
    Decide whether a catalog token may be stamped.

    Weekend rotation takes priority over naive 6-day cap rejection: in 7-night
    D/N catalog weeks, Friday N is sacrificed so Mon–Thu and Sat–Sun stay covered.
    """
    contract = (profile.contract_line_type or "").upper()

    if (
        contract == "D/N"
        and token == "N"
        and day.weekday() == _DN_SACRIFICE_WEEKDAY
        and _is_seven_night_catalog_week(spec, day, period_start)
    ):
        return False

    if not _would_violate_consecutive_work_cap(
        frame, row_idx, day, dates=dates, rules=rules
    ):
        if token == "N" and contract == "D/N" and day.weekday() == 5:
            sunday = day + timedelta(days=1)
            if sunday <= max(dates) and _would_violate_consecutive_work_cap(
                frame, row_idx, sunday, dates=dates, rules=rules
            ):
                friday = day - timedelta(days=1)
                if friday >= min(dates) and get_grid_token(frame, row_idx, friday) == "N":
                    _clear_row_token_if_work_shift(frame, row_idx, friday)
                    if _would_violate_consecutive_work_cap(
                        frame, row_idx, sunday, dates=dates, rules=rules
                    ):
                        return False
        return True

    if token == "N" and contract == "D/N" and day.weekday() == 6:
        friday = day - timedelta(days=2)
        if friday >= min(dates) and get_grid_token(frame, row_idx, friday) == "N":
            _clear_row_token_if_work_shift(frame, row_idx, friday)
            if not _would_violate_consecutive_work_cap(
                frame, row_idx, day, dates=dates, rules=rules
            ):
                return True
        saturday = day - timedelta(days=1)
        _clear_row_token_if_work_shift(frame, row_idx, saturday)
        return False

    return False


def _repair_dn_weekend_night_orphans(
    frame: pd.DataFrame,
    row_idx: int,
    dates: Sequence[date],
    *,
    profile: EmployeeProfile,
    rules: JurisdictionRules,
    employee_id: str,
) -> int:
    """Ensure D/N rows never end catalog stamp with Saturday N but no Sunday N."""
    if (profile.contract_line_type or "").upper() != "D/N":
        return 0
    repaired = 0
    for day in dates:
        if day.weekday() != 5:
            continue
        sunday = day + timedelta(days=1)
        if sunday > max(dates):
            continue
        if get_grid_token(frame, row_idx, day) != "N":
            continue
        if get_grid_token(frame, row_idx, sunday) == "N":
            continue
        friday = day - timedelta(days=1)
        if friday >= min(dates) and get_grid_token(frame, row_idx, friday) == "N":
            _clear_row_token_if_work_shift(frame, row_idx, friday)
        if not _would_violate_consecutive_work_cap(
            frame, row_idx, sunday, dates=dates, rules=rules
        ):
            if set_grid_token(frame, row_idx, sunday, "N"):
                repaired += 1
                continue
        if _clear_row_token_if_work_shift(frame, row_idx, day):
            repaired += 1
    return repaired


def _count_row_band_shifts(
    frame: pd.DataFrame,
    row_idx: int,
    dates: Sequence[date],
    band: str,
) -> int:
    return sum(1 for day in dates if get_grid_token(frame, row_idx, day) == band)


def _evening_shift_target(
    profile: EmployeeProfile,
    employee_target_hours: Mapping[str, float],
    *,
    alt_target_by_employee: Optional[Mapping[str, int]] = None,
) -> int:
    if alt_target_by_employee is not None:
        pooled = alt_target_by_employee.get(profile.id)
        if pooled is not None:
            return int(pooled)
    return portage_alt_shift_target_for_employee(
        profile,
        float(employee_target_hours.get(profile.id, 0.0)),
    )


def _evening_shift_deficit(
    frame: pd.DataFrame,
    row_idx: int,
    dates: Sequence[date],
    target: int,
) -> int:
    return target - _count_row_band_shifts(frame, row_idx, dates, "E")


def _count_weekday_band_shifts(
    frame: pd.DataFrame,
    row_idx: int,
    dates: Sequence[date],
    band: str,
) -> int:
    return sum(
        1
        for day in dates
        if day.weekday() < 5 and get_grid_token(frame, row_idx, day) == band
    )


def _evening_may_accept_shift(
    frame: pd.DataFrame,
    row_idx: int,
    dates: Sequence[date],
    *,
    profile: EmployeeProfile,
    employee_target_hours: Mapping[str, float],
) -> bool:
    target = _evening_shift_target(profile, employee_target_hours)
    return _evening_shift_deficit(frame, row_idx, dates, target) > 0


def _weekday_evening_tier_deficit(
    frame: pd.DataFrame,
    row_idx: int,
    dates: Sequence[date],
    sched_profile: EmployeeSchedulingProfile,
    *,
    profile: EmployeeProfile,
    employee_target_hours: Mapping[str, float],
) -> int:
    evening_target = _evening_shift_target(profile, employee_target_hours)
    total_e = _count_row_band_shifts(frame, row_idx, dates, "E")
    if total_e >= evening_target:
        return -1
    weekday_alt = sched_profile.tier_targets.get(SlotTier.WEEKDAY_ALT, 0)
    if weekday_alt <= 0:
        return -1
    weekday_e = _count_weekday_band_shifts(frame, row_idx, dates, "E")
    if weekday_e >= weekday_alt:
        return -1
    return min(evening_target - total_e, weekday_alt - weekday_e)


def _evening_equity_rank(
    frame: pd.DataFrame,
    row_idx: int,
    dates: Sequence[date],
    *,
    profile: EmployeeProfile,
    sched_profile: EmployeeSchedulingProfile,
    employee_target_hours: Mapping[str, float],
    pool_index: int,
    day: Optional[date] = None,
) -> Tuple[int, int, int, int]:
    """Lower tuple sorts first: deficit, day-rotated pool tiebreak, pool index, hours."""
    if day is not None and day.weekday() < 5:
        deficit = _weekday_evening_tier_deficit(
            frame,
            row_idx,
            dates,
            sched_profile,
            profile=profile,
            employee_target_hours=employee_target_hours,
        )
        if deficit < 0:
            deficit = -999
    else:
        deficit = _evening_shift_deficit(
            frame,
            row_idx,
            dates,
            _evening_shift_target(profile, employee_target_hours),
        )
    rotation = (pool_index + day.toordinal()) % 997 if day is not None else pool_index
    return (
        -deficit,
        rotation,
        pool_index,
        _count_work_shifts(frame, row_idx, dates),
    )


def _trim_dn_scattered_pool_nights_over_target(
    frame: pd.DataFrame,
    *,
    dates: Sequence[date],
    employees_by_id: Mapping[str, EmployeeProfile],
    locked_cells: Set[Tuple[str, date]],
    blocked_map: Mapping[str, Mapping[date, str]],
    period_start: date,
) -> int:
    """Drop Friday pool pickups on D-week lines (outside each line's night block)."""
    from lab_scheduler.scheduling.alternate_shift_distributor import (
        _vacant_catalog_line_number,
    )
    from lab_scheduler.scheduling.portage_dn_reference import dn_weekend_catalog_week_indices

    row_lookup = schedule_frame_row_index_by_employee_id(frame)
    changed = 0
    for employee_id, row_idx in row_lookup.items():
        profile = employees_by_id.get(employee_id)
        if profile is None or (profile.contract_line_type or "").upper() != "D/N":
            continue
        line_no = _vacant_catalog_line_number(profile)
        block_weeks: Set[int] = set()
        if line_no is not None:
            block_weeks = set(dn_weekend_catalog_week_indices(line_no))
        for day in dates:
            if get_grid_token(frame, row_idx, day) != "N":
                continue
            if day.weekday() != 4:
                continue
            if _catalog_week_index(day, period_start) in block_weeks:
                continue
            if not is_editable_cell(
                employee_id,
                day,
                locked_cells=locked_cells,
                blocked_map=blocked_map,
            ):
                continue
            if _clear_row_token_if_work_shift(frame, row_idx, day):
                changed += 1
    return changed


def _is_de_l58_stagger_weekend_day(
    employee: EmployeeProfile,
    employee_id: str,
    day: date,
    stagger_assignments: Mapping[str, frozenset[date]],
) -> bool:
    """True when this FT D/E line 5–8 should carry Day (not Evening) on a stagger Sat/Sun."""
    from lab_scheduler.scheduling.alternate_shift_distributor import (
        _vacant_catalog_line_number,
    )

    if day.weekday() < 5:
        return False
    if (employee.contract_line_type or "").upper() != "D/E":
        return False
    line_no = _vacant_catalog_line_number(employee)
    if line_no is None or line_no < 5:
        return False
    block = stagger_assignments.get(employee_id)
    return block is not None and day in block


def _place_de_l58_stagger_weekend_days(
    frame: pd.DataFrame,
    *,
    frame_order: Sequence[str],
    profiles: Mapping[str, EmployeeSchedulingProfile],
    row_lookup: Mapping[str, int],
    employees_by_id: Mapping[str, EmployeeProfile],
    dates: Sequence[date],
    period_start: date,
    stagger_assignments: Mapping[str, frozenset[date]],
    qual_codes: Mapping[str, str],
    locked_cells: Set[Tuple[str, date]],
    blocked_map: Mapping[str, Mapping[date, str]],
    warnings: Optional[List[str]] = None,
) -> int:
    """Place stagger-block weekend Day shifts on D/E lines 5–8 (1 MLT + 1 MLA footer pair)."""
    from lab_scheduler.scheduling.alternate_shift_distributor import (
        _vacant_catalog_line_number,
    )
    from lab_scheduler.scheduling.rotation_reference_builder import (
        _de_e_block_days_for_employee,
        _stagger_skip_days_for_e_block,
    )

    changed = 0
    warn = warnings if warnings is not None else []
    for employee_id in frame_order:
        profile = profiles.get(employee_id)
        sched_profile = employees_by_id.get(employee_id)
        row_idx = row_lookup.get(employee_id)
        target_days = stagger_assignments.get(employee_id)
        if profile is None or sched_profile is None or row_idx is None or not target_days:
            continue
        line_no = _vacant_catalog_line_number(sched_profile)
        if line_no is None or line_no < 5:
            continue
        e_block_days = _de_e_block_days_for_employee(
            sched_profile,
            period_start=period_start,
            dates=dates,
        )
        stagger_skip = _stagger_skip_days_for_e_block(e_block_days)
        for day in dates:
            if day.weekday() < 5 or day not in target_days:
                continue
            if day in stagger_skip:
                continue
            if not is_editable_cell(
                employee_id,
                day,
                locked_cells=locked_cells,
                blocked_map=blocked_map,
            ):
                continue
            if get_grid_token(frame, row_idx, day) == "D":
                continue
            if not _passes_cap_checks(
                frame,
                row_lookup,
                employees_by_id,
                qual_codes,
                employee_id=employee_id,
                day=day,
                band="D",
            ):
                warn.append(
                    f"{sched_profile.full_name}: weekend D cap blocked {day.isoformat()}."
                )
                continue
            delta = _place_token(
                frame,
                row_idx=row_idx,
                employee_id=employee_id,
                day=day,
                band="D",
                mirror=False,
                locked_cells=locked_cells,
                blocked_map=blocked_map,
                allow_overwrite=True,
            )
            if delta:
                changed += delta
    from lab_scheduler.scheduling.schedule_tallies import shift_target_for_date
    from lab_scheduler.scheduling.weekend_placement_rules import weekend_day_total_count

    for day in dates:
        if day.weekday() < 5:
            continue
        target = shift_target_for_date(day, "D")
        while weekend_day_total_count(frame, row_lookup, day) < target:
            placed = False
            for employee_id in frame_order:
                sched_profile = employees_by_id.get(employee_id)
                profile = profiles.get(employee_id)
                row_idx = row_lookup.get(employee_id)
                if sched_profile is None or profile is None or row_idx is None:
                    continue
                if (sched_profile.contract_line_type or "").upper() != "D/E":
                    continue
                line_no = _vacant_catalog_line_number(sched_profile)
                if line_no is None or line_no < 5:
                    continue
                e_block_days = _de_e_block_days_for_employee(
                    sched_profile,
                    period_start=period_start,
                    dates=dates,
                )
                if day in e_block_days:
                    continue
                token = get_grid_token(frame, row_idx, day)
                if token in {"D", "E"}:
                    continue
                if not is_editable_cell(
                    employee_id,
                    day,
                    locked_cells=locked_cells,
                    blocked_map=blocked_map,
                ):
                    continue
                if not _passes_cap_checks(
                    frame,
                    row_lookup,
                    employees_by_id,
                    qual_codes,
                    employee_id=employee_id,
                    day=day,
                    band="D",
                ):
                    continue
                delta = _place_token(
                    frame,
                    row_idx=row_idx,
                    employee_id=employee_id,
                    day=day,
                    band="D",
                    mirror=False,
                    locked_cells=locked_cells,
                    blocked_map=blocked_map,
                    allow_overwrite=False,
                )
                if delta:
                    changed += delta
                    placed = True
                    break
            if not placed:
                break
    return changed


def _strip_all_de_day_shifts(
    frame: pd.DataFrame,
    *,
    dates: Sequence[date],
    employees_by_id: Mapping[str, EmployeeProfile],
    locked_cells: Set[Tuple[str, date]],
    blocked_map: Mapping[str, Mapping[date, str]],
) -> int:
    """Alternate fill clears weekday Day on D/E lines; weekend D is re-applied later."""
    row_lookup = schedule_frame_row_index_by_employee_id(frame)
    changed = 0
    for employee_id, row_idx in row_lookup.items():
        profile = employees_by_id.get(employee_id)
        if profile is None or (profile.contract_line_type or "").upper() != "D/E":
            continue
        for day in dates:
            if day.weekday() >= 5:
                continue
            if not is_editable_cell(
                employee_id,
                day,
                locked_cells=locked_cells,
                blocked_map=blocked_map,
            ):
                continue
            if get_grid_token(frame, row_idx, day) == "D":
                if set_grid_token(frame, row_idx, day, OFF_DISPLAY):
                    changed += 1
    return changed


def _first_monday_on_or_after(day: date) -> date:
    cursor = day
    while cursor.weekday() != 0:
        cursor += timedelta(days=1)
    return cursor


def _weekend_pair_from_stagger_block(
    block: frozenset[date],
    *,
    peer_index: int = 0,
) -> Tuple[date, date] | None:
    """Sat/Sun pair in a stagger block; peers split first vs second weekend."""
    saturdays = sorted(block_day for block_day in block if block_day.weekday() == 5)
    if not saturdays:
        return None
    if len(saturdays) >= 2:
        pair_slot = min(peer_index, 1)
        saturday = saturdays[1 - pair_slot]
    else:
        saturday = saturdays[0]
    sunday = saturday + timedelta(days=1)
    if sunday not in block:
        return None
    return saturday, sunday


def _clear_all_de_evening_shifts(
    frame: pd.DataFrame,
    *,
    dates: Sequence[date],
    employees_by_id: Mapping[str, EmployeeProfile],
    locked_cells: Set[Tuple[str, date]],
    blocked_map: Mapping[str, Mapping[date, str]],
) -> int:
    """Alternate fill resets evening tokens on D/E lines before structured placement."""
    row_lookup = schedule_frame_row_index_by_employee_id(frame)
    changed = 0
    for employee_id, row_idx in row_lookup.items():
        profile = employees_by_id.get(employee_id)
        if profile is None or (profile.contract_line_type or "").upper() != "D/E":
            continue
        for day in dates:
            if not is_editable_cell(
                employee_id,
                day,
                locked_cells=locked_cells,
                blocked_map=blocked_map,
            ):
                continue
            if get_grid_token(frame, row_idx, day) == "E":
                if set_grid_token(frame, row_idx, day, OFF_DISPLAY):
                    changed += 1
    return changed


def _monday_of_stagger_block(block: frozenset[date]) -> date | None:
    if not block:
        return None
    anchor = min(block)
    while anchor.weekday() != 5:
        anchor += timedelta(days=1)
        if anchor > max(block):
            return None
    return anchor - timedelta(days=5)


def _de_evening_pool_ids(
    frame_order: Sequence[str],
    *,
    profiles: Mapping[str, EmployeeSchedulingProfile],
    employees_by_id: Mapping[str, EmployeeProfile],
    qual_codes: Mapping[str, str],
    qual: str,
    employee_target_hours: Optional[Mapping[str, float]] = None,
) -> List[str]:
    from lab_scheduler.scheduling.alternate_shift_distributor import (
        _vacant_catalog_line_number,
    )
    from lab_scheduler.scheduling.portage_equity_targets import (
        portage_is_fulltime_catalog_hours,
    )

    pool: List[str] = []
    for employee_id in frame_order:
        profile = employees_by_id.get(employee_id)
        sched = profiles.get(employee_id)
        if profile is None or sched is None or not sched.eligible_for_fill:
            continue
        if (sched.contract_line_type or "").upper() != "D/E":
            continue
        if infer_qual_code(profile, qual_codes=qual_codes) != qual:
            continue
        pool.append(employee_id)

    def _sort_key(employee_id: str) -> tuple[int, int]:
        hours = float((employee_target_hours or {}).get(employee_id, 0.0))
        line_no = _vacant_catalog_line_number(employees_by_id[employee_id]) or 0
        ft_rank = 0 if portage_is_fulltime_catalog_hours(hours) else 1
        return (ft_rank, line_no)

    pool.sort(key=_sort_key)
    return pool


def _six_day_evening_run_labor_ok(days: Sequence[date]) -> bool:
    """Evening runs must stay within the 40h/week statutory cap (8h per shift)."""
    hours_by_week: DefaultDict[Tuple[int, int], int] = defaultdict(int)
    for day in days:
        year, week, _ = day.isocalendar()
        hours_by_week[(year, week)] += 8
    return all(hours <= 40 for hours in hours_by_week.values())


def _weekday_evening_run_days(
    period_start: date,
    block: frozenset[date],
    *,
    peer_index: int,
    date_set: Set[date],
    run_length: int = _FT_DE_WEEKDAY_EVENING_RUN,
) -> List[date]:
    """
    Six weekday evening shifts, disjoint from this line's stagger Sat/Sun pair.
    Each peer searches forward from its own anchor so runs do not overlap in the block.
    """
    length = max(0, min(int(run_length), _FT_DE_WEEKDAY_EVENING_RUN))
    if length <= 0:
        return []
    weekend_pair = _weekend_pair_from_stagger_block(block, peer_index=peer_index)
    exclude = frozenset(weekend_pair) if weekend_pair else frozenset()
    if block:
        anchor = _monday_of_stagger_block(block) or _first_monday_on_or_after(period_start)
    else:
        anchor = _first_monday_on_or_after(period_start)
    search_from = anchor + timedelta(
        days=(_FT_DE_WEEKDAY_EVENING_RUN + _FT_DE_WEEKEND_PAIR_DAYS) * peer_index
    )
    for offset in range(28):
        start = search_from + timedelta(days=offset)
        days: List[date] = []
        cursor = start
        while len(days) < length and (cursor - start).days < 21:
            if cursor not in date_set:
                cursor += timedelta(days=1)
                continue
            if cursor.weekday() < 5 and cursor not in exclude:
                days.append(cursor)
            cursor += timedelta(days=1)
        if len(days) < length:
            continue
        picked = days[:length]
        if length >= _FT_DE_WEEKDAY_EVENING_RUN and not _six_day_evening_run_labor_ok(picked):
            continue
        return picked
    return []


def _six_consecutive_evening_days(start: date) -> List[date]:
    """Mon–Sat: six consecutive calendar days (legacy helper)."""
    monday = start
    if monday.weekday() != 0:
        monday = _first_monday_on_or_after(monday)
    return [monday + timedelta(days=offset) for offset in range(_FT_DE_WEEKDAY_EVENING_RUN)]


def _enforce_evening_qual_caps_per_day(
    frame: pd.DataFrame,
    *,
    frame_order: Sequence[str],
    row_lookup: Mapping[str, int],
    employees_by_id: Mapping[str, EmployeeProfile],
    dates: Sequence[date],
    qual_codes: Mapping[str, str],
    employee_target_hours: Mapping[str, float],
    locked_cells: Set[Tuple[str, date]],
    blocked_map: Mapping[str, Mapping[date, str]],
    alt_target_by_employee: Optional[Mapping[str, int]] = None,
) -> int:
    """Hard cap: clinical floor (1 MLT + 1 MLA per day — matches footer need 2)."""
    changed = 0
    for day in dates:
        evening_cap = _clinical_evening_cap_per_qual(day)
        for qual in ("MLT", "MLA"):
            while True:
                if day.weekday() < 5:
                    counts = daily_band_qual_count(
                        frame,
                        row_lookup,
                        employees_by_id,
                        qual_codes,
                        day,
                        "E",
                    )
                else:
                    counts = weekend_band_qual_count(
                        frame,
                        row_lookup,
                        employees_by_id,
                        qual_codes,
                        day,
                        "E",
                    )
                if counts.get(qual, 0) <= evening_cap:
                    break
                donors: List[Tuple[int, int, str, int]] = []
                for employee_id in frame_order:
                    profile = employees_by_id.get(employee_id)
                    row_idx = row_lookup.get(employee_id)
                    if profile is None or row_idx is None:
                        continue
                    if infer_qual_code(profile, qual_codes=qual_codes) != qual:
                        continue
                    if get_grid_token(frame, row_idx, day) != "E":
                        continue
                    target = _evening_shift_target(
                        profile,
                        employee_target_hours,
                        alt_target_by_employee=alt_target_by_employee,
                    )
                    assigned = _count_row_band_shifts(frame, row_idx, dates, "E")
                    donors.append((assigned - target, assigned, employee_id, row_idx))
                if not donors:
                    break
                donors.sort(reverse=True)
                surplus_donors = [d for d in donors if d[0] > 0]
                pick_from = surplus_donors if surplus_donors else donors
                _surplus, _assigned, employee_id, row_idx = pick_from[0]
                if not is_editable_cell(
                    employee_id,
                    day,
                    locked_cells=locked_cells,
                    blocked_map=blocked_map,
                ):
                    break
                if set_grid_token(frame, row_idx, day, OFF_DISPLAY):
                    changed += 1
                else:
                    break
    return changed


def _replenish_de_evening_targets_after_cap(
    frame: pd.DataFrame,
    *,
    frame_order: Sequence[str],
    profiles: Mapping[str, EmployeeSchedulingProfile],
    row_lookup: Mapping[str, int],
    employees_by_id: Mapping[str, EmployeeProfile],
    employees: Sequence[Mapping[str, object]],
    employee_profiles: Sequence[EmployeeProfile],
    dates: Sequence[date],
    qual_codes: Mapping[str, str],
    templates: Mapping[str, Mapping[str, object]],
    rules: JurisdictionRules,
    period_start: date,
    period_end: date,
    weeks_in_period: int,
    shift_templates: Mapping[str, ShiftTemplateInfo],
    employee_target_hours: Mapping[str, float],
    availability_blocked: Optional[Mapping[str, Set[date]]],
    locked_cells: Set[Tuple[str, date]],
    blocked_map: Mapping[str, Mapping[date, str]],
    stagger_assignments: Mapping[str, frozenset[date]],
    alt_target_by_employee: Optional[Mapping[str, int]] = None,
) -> int:
    """Top up D/E lines on structured planned days still empty after cap enforcement."""
    from lab_scheduler.scheduling.alternate_shift_distributor import (
        _vacant_catalog_line_number,
        _weekend_stagger_block_index,
    )

    changed = 0
    peers_by_block: Dict[int, List[str]] = {}
    for qual in ("MLT", "MLA"):
        pool_ids = _de_evening_pool_ids(
            frame_order,
            profiles=profiles,
            employees_by_id=employees_by_id,
            qual_codes=qual_codes,
            qual=qual,
            employee_target_hours=employee_target_hours,
        )
        for employee_id in pool_ids:
            employee = employees_by_id[employee_id]
            line_no = _vacant_catalog_line_number(employee)
            if line_no is None:
                continue
            peers_by_block.setdefault(_weekend_stagger_block_index(line_no), []).append(
                employee_id
            )
    for block_peers in peers_by_block.values():
        block_peers.sort(
            key=lambda employee_id: _vacant_catalog_line_number(
                employees_by_id[employee_id]
            )
            or 0
        )
    for qual in ("MLT", "MLA"):
        pool_ids = _de_evening_pool_ids(
            frame_order,
            profiles=profiles,
            employees_by_id=employees_by_id,
            qual_codes=qual_codes,
            qual=qual,
            employee_target_hours=employee_target_hours,
        )
        for employee_id in pool_ids:
            employee = employees_by_id[employee_id]
            row_idx = row_lookup[employee_id]
            line_no = _vacant_catalog_line_number(employee)
            if line_no is None:
                continue
            block_index = _weekend_stagger_block_index(line_no)
            block_peers = peers_by_block.get(block_index, [employee_id])
            target = _evening_shift_target(
                employee,
                employee_target_hours,
                alt_target_by_employee=alt_target_by_employee,
            )
            planned = _de_evening_planned_days_for_line(
                employee_id=employee_id,
                employee=employee,
                period_start=period_start,
                dates=dates,
                stagger_assignments=stagger_assignments,
                block_peers=block_peers,
                employee_target_hours=employee_target_hours,
                alt_target_by_employee=alt_target_by_employee,
            )
            while _evening_shift_deficit(frame, row_idx, dates, target) > 0:
                placed = False
                for day in planned:
                    if day.weekday() < 5:
                        counts = daily_band_qual_count(
                            frame,
                            row_lookup,
                            employees_by_id,
                            qual_codes,
                            day,
                            "E",
                        )
                    else:
                        counts = weekend_band_qual_count(
                            frame,
                            row_lookup,
                            employees_by_id,
                            qual_codes,
                            day,
                            "E",
                        )
                    day_cap = _clinical_evening_cap_per_qual(day)
                    if counts.get(qual, 0) >= day_cap:
                        continue
                    if not is_editable_cell(
                        employee_id,
                        day,
                        locked_cells=locked_cells,
                        blocked_map=blocked_map,
                    ):
                        continue
                    if not is_empty_grid_token(frame.at[row_idx, day.isoformat()]):
                        continue
                    if not _validate_labor(
                        frame=frame,
                        employees=employees,
                        profiles=employee_profiles,
                        dates=dates,
                        db_templates=templates,
                        rules=rules,
                        period_start=period_start,
                        period_end=period_end,
                        weeks_in_period=weeks_in_period,
                        shift_templates=shift_templates,
                        employee_target_hours=employee_target_hours,
                        availability_blocked=availability_blocked,
                        employee_id=employee_id,
                        day=day,
                        band="E",
                    ):
                        continue
                    if _place_token(
                        frame,
                        row_idx=row_idx,
                        employee_id=employee_id,
                        day=day,
                        band="E",
                        mirror=(
                            day.weekday() == 5
                            and day + timedelta(days=1) in planned
                        ),
                        locked_cells=locked_cells,
                        blocked_map=blocked_map,
                        allow_overwrite=False,
                    ):
                        if _ensure_clinical_evening_partner_or_revert(
                            frame,
                            day=day,
                            qual=qual,
                            row_idx=row_idx,
                            employee_id=employee_id,
                            frame_order=frame_order,
                            profiles=profiles,
                            row_lookup=row_lookup,
                            employees_by_id=employees_by_id,
                            employees=employees,
                            employee_profiles=employee_profiles,
                            dates=dates,
                            qual_codes=qual_codes,
                            templates=templates,
                            rules=rules,
                            period_start=period_start,
                            period_end=period_end,
                            weeks_in_period=weeks_in_period,
                            shift_templates=shift_templates,
                            employee_target_hours=employee_target_hours,
                            availability_blocked=availability_blocked,
                            locked_cells=locked_cells,
                            blocked_map=blocked_map,
                            alt_target_by_employee=alt_target_by_employee,
                        ):
                            changed += 1
                            placed = True
                        break
                if not placed:
                    for day in dates:
                        if day in planned:
                            continue
                        if day.weekday() < 5:
                            counts = daily_band_qual_count(
                                frame,
                                row_lookup,
                                employees_by_id,
                                qual_codes,
                                day,
                                "E",
                            )
                        else:
                            counts = weekend_band_qual_count(
                                frame,
                                row_lookup,
                                employees_by_id,
                                qual_codes,
                                day,
                                "E",
                            )
                        if counts.get(qual, 0) >= _clinical_evening_cap_per_qual(day):
                            continue
                        if not is_editable_cell(
                            employee_id,
                            day,
                            locked_cells=locked_cells,
                            blocked_map=blocked_map,
                        ):
                            continue
                        if not is_empty_grid_token(frame.at[row_idx, day.isoformat()]):
                            continue
                        if not _validate_labor(
                            frame=frame,
                            employees=employees,
                            profiles=employee_profiles,
                            dates=dates,
                            db_templates=templates,
                            rules=rules,
                            period_start=period_start,
                            period_end=period_end,
                            weeks_in_period=weeks_in_period,
                            shift_templates=shift_templates,
                            employee_target_hours=employee_target_hours,
                            availability_blocked=availability_blocked,
                            employee_id=employee_id,
                            day=day,
                            band="E",
                        ):
                            continue
                        if _place_token(
                            frame,
                            row_idx=row_idx,
                            employee_id=employee_id,
                            day=day,
                            band="E",
                            mirror=(
                                day.weekday() == 5
                                and day + timedelta(days=1) in dates
                            ),
                            locked_cells=locked_cells,
                            blocked_map=blocked_map,
                            allow_overwrite=False,
                        ):
                            partner_qual = "MLA" if qual == "MLT" else "MLT"
                            if not _try_clinical_evening_for_qual_on_day(
                                frame,
                                day=day,
                                qual=partner_qual,
                                frame_order=frame_order,
                                profiles=profiles,
                                row_lookup=row_lookup,
                                employees_by_id=employees_by_id,
                                employees=employees,
                                employee_profiles=employee_profiles,
                                dates=dates,
                                qual_codes=qual_codes,
                                templates=templates,
                                rules=rules,
                                period_start=period_start,
                                period_end=period_end,
                                weeks_in_period=weeks_in_period,
                                shift_templates=shift_templates,
                                employee_target_hours=employee_target_hours,
                                availability_blocked=availability_blocked,
                                locked_cells=locked_cells,
                                blocked_map=blocked_map,
                                alt_target_by_employee=alt_target_by_employee,
                            ):
                                set_grid_token(frame, row_idx, day, OFF_DISPLAY)
                            else:
                                changed += 1
                                placed = True
                            break
                if not placed:
                    break
    return changed


def _de_evening_planned_days_for_line(
    *,
    employee_id: str,
    employee: EmployeeProfile,
    period_start: date,
    dates: Sequence[date],
    stagger_assignments: Mapping[str, frozenset[date]],
    block_peers: Sequence[str],
    employee_target_hours: Mapping[str, float],
    alt_target_by_employee: Optional[Mapping[str, int]] = None,
) -> List[date]:
    """Structured D/E evening slots: weekday run + stagger weekend pair."""
    from lab_scheduler.scheduling.alternate_shift_distributor import (
        _vacant_catalog_line_number,
    )
    from lab_scheduler.scheduling.portage_equity_targets import (
        portage_is_fulltime_catalog_hours,
    )

    date_set = set(dates)
    line_no = _vacant_catalog_line_number(employee)
    if line_no is None:
        return []
    catalog_hours = float(employee_target_hours.get(employee.id, 0.0))
    target = _evening_shift_target(
        employee,
        employee_target_hours,
        alt_target_by_employee=alt_target_by_employee,
    )
    if target <= 0:
        return []
    block = stagger_assignments.get(employee_id, frozenset())
    peer_index = block_peers.index(employee_id) if employee_id in block_peers else 0
    weekend_pair = _weekend_pair_from_stagger_block(block, peer_index=peer_index)
    weekend_days = 0
    if weekend_pair is not None and target > 0:
        weekend_days = min(_FT_DE_WEEKEND_PAIR_DAYS, target)
    weekday_slots = min(
        _FT_DE_WEEKDAY_EVENING_RUN,
        max(0, target - weekend_days),
    )
    if not portage_is_fulltime_catalog_hours(catalog_hours):
        weekday_slots = 0
    run_days = _weekday_evening_run_days(
        period_start,
        block,
        peer_index=peer_index,
        date_set=date_set,
        run_length=weekday_slots,
    )
    planned: List[date] = []
    planned.extend(run_days[:weekday_slots])
    if weekend_pair is not None and target > weekday_slots:
        planned.extend(weekend_pair)
    return planned[:target]


def _apply_de_seven_day_evening_blocks(
    frame: pd.DataFrame,
    *,
    frame_order: Sequence[str],
    employees_by_id: Mapping[str, EmployeeProfile],
    qual_codes: Mapping[str, str],
    employee_target_hours: Mapping[str, float],
    period_start: date,
    dates: Sequence[date],
    employees: Sequence[Mapping[str, object]],
    employee_profiles: Sequence[EmployeeProfile],
    templates: Mapping[str, Mapping[str, object]],
    rules: JurisdictionRules,
    period_end: date,
    weeks_in_period: int,
    shift_templates: Mapping[str, ShiftTemplateInfo],
    availability_blocked: Optional[Mapping[str, Set[date]]],
    row_lookup: Mapping[str, int],
    locked_cells: Set[Tuple[str, date]],
    blocked_map: Mapping[str, Mapping[date, str]],
    stagger_assignments: Mapping[str, frozenset[date]],
) -> int:
    """Plan and apply one Mon–Sun E week per D/E line (staggered by line number)."""
    from lab_scheduler.scheduling.rotation_reference_builder import (
        _de_e_block_days_for_employee,
        _stagger_skip_days_for_e_block,
    )

    prep_changed = 0
    for employee_id in frame_order:
        profile = employees_by_id.get(employee_id)
        row_idx = row_lookup.get(employee_id)
        if profile is None or row_idx is None:
            continue
        if (profile.contract_line_type or "").upper() != "D/E":
            continue
        block_days = _de_e_block_days_for_employee(
            profile,
            period_start=period_start,
            dates=dates,
        )
        for day in _stagger_skip_days_for_e_block(block_days):
            if day in block_days:
                continue
            if not is_editable_cell(
                employee_id,
                day,
                locked_cells=locked_cells,
                blocked_map=blocked_map,
            ):
                continue
            if get_grid_token(frame, row_idx, day) != "D":
                continue
            if set_grid_token(frame, row_idx, day, OFF_DISPLAY):
                prep_changed += 1

    planned = plan_de_seven_day_evening_blocks(
        frame_order=frame_order,
        employees_by_id=employees_by_id,
        qual_codes=qual_codes,
        employee_target_hours=employee_target_hours,
        period_start=period_start,
        dates=dates,
        stagger_assignments=stagger_assignments,
    )

    def _labor_ok(*, employee_id: str, day: date, band: str) -> bool:
        return _validate_labor(
            frame=frame,
            employees=employees,
            profiles=employee_profiles,
            dates=dates,
            db_templates=templates,
            rules=rules,
            period_start=period_start,
            period_end=period_end,
            weeks_in_period=weeks_in_period,
            shift_templates=shift_templates,
            employee_target_hours=employee_target_hours,
            availability_blocked=availability_blocked,
            employee_id=employee_id,
            day=day,
            band=band,
        )

    applied = apply_planned_shifts(
        frame,
        planned,
        row_lookup=row_lookup,
        employees_by_id=employees_by_id,
        qual_codes=qual_codes,
        dates=dates,
        locked_cells=locked_cells,
        blocked_map=blocked_map,
        validate_labor=_labor_ok,
    )
    return prep_changed + applied.cells_changed


def _place_ft_de_structured_evening_rotations(
    frame: pd.DataFrame,
    *,
    frame_order: Sequence[str],
    profiles: Mapping[str, EmployeeSchedulingProfile],
    row_lookup: Mapping[str, int],
    employees_by_id: Mapping[str, EmployeeProfile],
    employees: Sequence[Mapping[str, object]],
    employee_profiles: Sequence[EmployeeProfile],
    dates: Sequence[date],
    qual_codes: Mapping[str, str],
    templates: Mapping[str, Mapping[str, object]],
    rules: JurisdictionRules,
    period_start: date,
    period_end: date,
    weeks_in_period: int,
    shift_templates: Mapping[str, ShiftTemplateInfo],
    employee_target_hours: Mapping[str, float],
    availability_blocked: Optional[Mapping[str, Set[date]]],
    locked_cells: Set[Tuple[str, date]],
    blocked_map: Mapping[str, Mapping[date, str]],
    stagger_assignments: Mapping[str, frozenset[date]],
    alt_target_by_employee: Optional[Mapping[str, int]] = None,
) -> int:
    """
    D/E evening pattern: 6 weekdays in a row + separate Sat/Sun pair (8 total for FT).
    Part-time lines use the same shape scaled to their evening target.
    """
    from lab_scheduler.scheduling.alternate_shift_distributor import (
        _vacant_catalog_line_number,
        _weekend_stagger_block_index,
    )

    changed = 0
    for qual in ("MLT", "MLA"):
        pool_ids = _de_evening_pool_ids(
            frame_order,
            profiles=profiles,
            employees_by_id=employees_by_id,
            qual_codes=qual_codes,
            qual=qual,
            employee_target_hours=employee_target_hours,
        )
        peers_by_block: Dict[int, List[str]] = {}
        for employee_id in pool_ids:
            employee = employees_by_id[employee_id]
            line_no = _vacant_catalog_line_number(employee)
            if line_no is None:
                continue
            peers_by_block.setdefault(_weekend_stagger_block_index(line_no), []).append(
                employee_id
            )
        for block_peers in peers_by_block.values():
            block_peers.sort(
                key=lambda employee_id: _vacant_catalog_line_number(
                    employees_by_id[employee_id]
                )
                or 0
            )
        for employee_id in pool_ids:
            employee = employees_by_id[employee_id]
            sched = profiles[employee_id]
            row_idx = row_lookup[employee_id]
            line_no = _vacant_catalog_line_number(employee)
            if line_no is None:
                continue
            target = _evening_shift_target(
                employee,
                employee_target_hours,
                alt_target_by_employee=alt_target_by_employee,
            )
            if target <= 0:
                continue
            block = stagger_assignments.get(employee_id, frozenset())
            block_index = _weekend_stagger_block_index(line_no)
            block_peers = peers_by_block.get(block_index, [employee_id])
            planned = _de_evening_planned_days_for_line(
                employee_id=employee_id,
                employee=employee,
                period_start=period_start,
                dates=dates,
                stagger_assignments=stagger_assignments,
                block_peers=block_peers,
                employee_target_hours=employee_target_hours,
                alt_target_by_employee=alt_target_by_employee,
            )
            for day in planned:
                if (
                    day.weekday() == 6
                    and day - timedelta(days=1) in planned
                ):
                    continue
                if not is_editable_cell(
                    employee_id,
                    day,
                    locked_cells=locked_cells,
                    blocked_map=blocked_map,
                ):
                    continue
                if not is_empty_grid_token(frame.at[row_idx, day.isoformat()]):
                    continue
                if day.weekday() < 5:
                    counts = daily_band_qual_count(
                        frame,
                        row_lookup,
                        employees_by_id,
                        qual_codes,
                        day,
                        "E",
                    )
                else:
                    counts = weekend_band_qual_count(
                        frame,
                        row_lookup,
                        employees_by_id,
                        qual_codes,
                        day,
                        "E",
                    )
                if counts.get(qual, 0) >= _clinical_evening_cap_per_qual(day):
                    continue
                if not _validate_labor(
                    frame=frame,
                    employees=employees,
                    profiles=employee_profiles,
                    dates=dates,
                    db_templates=templates,
                    rules=rules,
                    period_start=period_start,
                    period_end=period_end,
                    weeks_in_period=weeks_in_period,
                    shift_templates=shift_templates,
                    employee_target_hours=employee_target_hours,
                    availability_blocked=availability_blocked,
                    employee_id=employee_id,
                    day=day,
                    band="E",
                ):
                    continue
                if _place_token(
                    frame,
                    row_idx=row_idx,
                    employee_id=employee_id,
                    day=day,
                    band="E",
                    mirror=(
                        day.weekday() == 5
                        and day + timedelta(days=1) in planned
                    ),
                    locked_cells=locked_cells,
                    blocked_map=blocked_map,
                    allow_overwrite=False,
                ):
                    changed += 1
    return changed


def _trim_weekend_evening_qual_cap(
    frame: pd.DataFrame,
    *,
    frame_order: Sequence[str],
    row_lookup: Mapping[str, int],
    employees_by_id: Mapping[str, EmployeeProfile],
    dates: Sequence[date],
    qual_codes: Mapping[str, str],
    employee_target_hours: Mapping[str, float],
    locked_cells: Set[Tuple[str, date]],
    blocked_map: Mapping[str, Mapping[date, str]],
    alt_target_by_employee: Optional[Mapping[str, int]] = None,
) -> int:
    """Drop surplus weekend E per qual so operational tallies stay at need 2."""
    changed = 0
    evening_cap = weekend_alt_band_cap_per_qual("E")
    for day in dates:
        if day.weekday() < 5:
            continue
        for qual in ("MLT", "MLA"):
            while True:
                counts = weekend_band_qual_count(
                    frame,
                    row_lookup,
                    employees_by_id,
                    qual_codes,
                    day,
                    "E",
                )
                if counts.get(qual, 0) <= evening_cap:
                    break
                donors: List[Tuple[int, int, str, int]] = []
                for employee_id in frame_order:
                    profile = employees_by_id.get(employee_id)
                    row_idx = row_lookup.get(employee_id)
                    if profile is None or row_idx is None:
                        continue
                    if infer_qual_code(profile, qual_codes=qual_codes) != qual:
                        continue
                    if get_grid_token(frame, row_idx, day) != "E":
                        continue
                    target = _evening_shift_target(
                        profile,
                        employee_target_hours,
                        alt_target_by_employee=alt_target_by_employee,
                    )
                    assigned = _count_row_band_shifts(frame, row_idx, dates, "E")
                    donors.append((assigned - target, assigned, employee_id, row_idx))
                if not donors:
                    break
                donors.sort(reverse=True)
                _surplus, _assigned, employee_id, row_idx = donors[0]
                if not is_editable_cell(
                    employee_id,
                    day,
                    locked_cells=locked_cells,
                    blocked_map=blocked_map,
                ):
                    break
                if set_grid_token(frame, row_idx, day, OFF_DISPLAY):
                    changed += 1
                else:
                    break
    return changed


def _trim_weekday_evening_qual_cap(
    frame: pd.DataFrame,
    *,
    frame_order: Sequence[str],
    row_lookup: Mapping[str, int],
    employees_by_id: Mapping[str, EmployeeProfile],
    dates: Sequence[date],
    qual_codes: Mapping[str, str],
    employee_target_hours: Mapping[str, float],
    locked_cells: Set[Tuple[str, date]],
    blocked_map: Mapping[str, Mapping[date, str]],
    alt_target_by_employee: Optional[Mapping[str, int]] = None,
) -> int:
    """Drop surplus weekday E per qual so pool tallies stay at clinical floor (1/qual)."""
    changed = 0
    evening_cap = _clinical_evening_cap_per_qual(date.min)
    for day in dates:
        if day.weekday() >= 5:
            continue
        for qual in ("MLT", "MLA"):
            while True:
                counts = daily_band_qual_count(
                    frame,
                    row_lookup,
                    employees_by_id,
                    qual_codes,
                    day,
                    "E",
                )
                if counts.get(qual, 0) <= evening_cap:
                    break
                donors: List[Tuple[int, int, str, int]] = []
                for employee_id in frame_order:
                    profile = employees_by_id.get(employee_id)
                    row_idx = row_lookup.get(employee_id)
                    if profile is None or row_idx is None:
                        continue
                    if infer_qual_code(profile, qual_codes=qual_codes) != qual:
                        continue
                    if get_grid_token(frame, row_idx, day) != "E":
                        continue
                    target = _evening_shift_target(
                        profile,
                        employee_target_hours,
                        alt_target_by_employee=alt_target_by_employee,
                    )
                    assigned = _count_row_band_shifts(frame, row_idx, dates, "E")
                    donors.append((assigned - target, assigned, employee_id, row_idx))
                if not donors:
                    break
                donors.sort(reverse=True)
                _surplus, _assigned, employee_id, row_idx = donors[0]
                if not is_editable_cell(
                    employee_id,
                    day,
                    locked_cells=locked_cells,
                    blocked_map=blocked_map,
                ):
                    break
                if set_grid_token(frame, row_idx, day, OFF_DISPLAY):
                    changed += 1
                else:
                    break
    return changed


def _strip_weekday_day_shifts(
    frame: pd.DataFrame,
    *,
    dates: Sequence[date],
    locked_cells: Set[Tuple[str, date]],
    blocked_map: Mapping[str, Mapping[date, str]],
    employee_target_hours: Optional[Mapping[str, float]] = None,
) -> int:
    """Alternate fill clears weekday Day on full-time lines; part-time catalog D stays."""
    from lab_scheduler.scheduling.portage_equity_targets import (
        portage_is_fulltime_catalog_hours,
    )

    row_lookup = schedule_frame_row_index_by_employee_id(frame)
    changed = 0
    for employee_id, row_idx in row_lookup.items():
        if employee_target_hours is not None and not portage_is_fulltime_catalog_hours(
            float(employee_target_hours.get(employee_id, 0.0))
        ):
            continue
        for day in dates:
            if day.weekday() >= 5:
                continue
            if not is_editable_cell(
                employee_id,
                day,
                locked_cells=locked_cells,
                blocked_map=blocked_map,
            ):
                continue
            if get_grid_token(frame, row_idx, day) == "D":
                if set_grid_token(frame, row_idx, day, OFF_DISPLAY):
                    changed += 1
    return changed


def _stamp_catalog_evenings_with_equity(
    frame: pd.DataFrame,
    *,
    employees_by_id: Mapping[str, EmployeeProfile],
    dates: Sequence[date],
    period_start: date,
    rules: JurisdictionRules,
    locked_cells: Set[Tuple[str, date]],
    blocked_map: Mapping[str, Mapping[date, str]],
    employee_target_hours: Mapping[str, float],
    qual_codes: Mapping[str, str],
    sched_profiles: Mapping[str, EmployeeSchedulingProfile],
    frame_order: Sequence[str],
) -> Tuple[int, Set[str]]:
    """Stamp catalog evening tokens preferring lines most under their FTE alt target."""
    from lab_scheduler.scheduling.portage_template import (
        _catalog_shift_token_for_date,
        _catalog_token_blocks_day_night_transition,
        portage_master_line_spec,
    )
    from lab_scheduler.solver.cpsat_fill import is_vacant_portage_line

    row_lookup = schedule_frame_row_index_by_employee_id(frame)
    changed = 0
    stamped_employees: Set[str] = set()
    for day in dates:
        for qual in ("MLT", "MLA"):
            while True:
                counts = daily_band_qual_count(
                    frame,
                    row_lookup,
                    employees_by_id,
                    qual_codes,
                    day,
                    "E",
                )
                if counts.get(qual, 0) >= _WEEKDAY_ALT_QUAL_FLOOR:
                    break
                candidates: List[Tuple[Tuple[int, int, int, int], str, int, object]] = []
                for employee_id in frame_order:
                    profile = employees_by_id.get(employee_id)
                    sched = sched_profiles.get(employee_id)
                    row_idx = row_lookup.get(employee_id)
                    if profile is None or sched is None or row_idx is None:
                        continue
                    if not is_vacant_portage_line(profile.full_name):
                        continue
                    if (profile.contract_line_type or "").upper() != "D/E":
                        continue
                    if infer_qual_code(profile, qual_codes=qual_codes) != qual:
                        continue
                    if not portage_is_fulltime_catalog_hours(
                        float(employee_target_hours.get(employee_id, 0.0))
                    ):
                        continue
                    spec = portage_master_line_spec(profile)
                    if spec is None:
                        continue
                    if _catalog_shift_token_for_date(spec, day, period_start) != "E":
                        continue
                    if not is_editable_cell(
                        employee_id,
                        day,
                        locked_cells=locked_cells,
                        blocked_map=blocked_map,
                    ):
                        continue
                    if not is_empty_grid_token(frame.at[row_idx, day.isoformat()]):
                        continue
                    if _catalog_token_blocks_day_night_transition(
                        spec,
                        day,
                        period_start,
                        "E",
                        employee_id=employee_id,
                    ):
                        continue
                    if not _stamp_catalog_shift_allowed(
                        frame,
                        row_idx,
                        day,
                        "E",
                        profile=profile,
                        spec=spec,
                        dates=dates,
                        rules=rules,
                        period_start=period_start,
                        employee_id=employee_id,
                    ):
                        continue
                    evening_target = _evening_shift_target(profile, employee_target_hours)
                    if _weekday_evening_tier_deficit(
                        frame,
                        row_idx,
                        dates,
                        sched,
                        profile=profile,
                        employee_target_hours=employee_target_hours,
                    ) <= 0:
                        continue
                    rank = _evening_equity_rank(
                        frame,
                        row_idx,
                        dates,
                        profile=profile,
                        sched_profile=sched,
                        employee_target_hours=employee_target_hours,
                        pool_index=sched.pool_index,
                        day=day,
                    )
                    candidates.append((rank, employee_id, row_idx, spec))
                if not candidates:
                    break
                placed = False
                for _rank, employee_id, row_idx, _spec in sorted(candidates):
                    if not _passes_cap_checks(
                        frame,
                        row_lookup,
                        employees_by_id,
                        qual_codes,
                        employee_id=employee_id,
                        day=day,
                        band="E",
                    ):
                        continue
                    delta = _place_token(
                        frame,
                        row_idx=row_idx,
                        employee_id=employee_id,
                        day=day,
                        band="E",
                        mirror=False,
                        locked_cells=locked_cells,
                        blocked_map=blocked_map,
                        allow_overwrite=False,
                    )
                    if delta:
                        changed += delta
                        stamped_employees.add(employee_id)
                        placed = True
                        break
                if not placed:
                    break
    return changed, stamped_employees


def _stamp_fulltime_master_catalog(
    frame: pd.DataFrame,
    *,
    employees_by_id: Mapping[str, EmployeeProfile],
    dates: Sequence[date],
    period_start: date,
    rules: JurisdictionRules,
    locked_cells: Set[Tuple[str, date]],
    blocked_map: Mapping[str, Mapping[date, str]],
    employee_target_hours: Mapping[str, float],
    qual_codes: Mapping[str, str],
    mode: FillMode = FillMode.FULL,
    sched_profiles: Optional[Mapping[str, EmployeeSchedulingProfile]] = None,
    frame_order: Optional[Sequence[str]] = None,
) -> Tuple[int, Set[str]]:
    """
    Stamp full-time vacant master lines from the 8-week Portage catalog.

    D/N lines use staggered night blocks so each qual pool schedules one night
    per calendar day; D/E lines use day-then-evening rotation blocks.

    Returns cells changed and the employee ids locked to catalog (tier fill skips them).
    """
    from lab_scheduler.scheduling.portage_equity_targets import (
        portage_is_fulltime_catalog_hours,
    )
    from lab_scheduler.scheduling.portage_template import (
        _catalog_shift_token_for_date,
        _catalog_token_blocks_day_night_transition,
        portage_master_line_spec,
    )
    from lab_scheduler.solver.cpsat_fill import is_vacant_portage_line

    row_lookup = schedule_frame_row_index_by_employee_id(frame)
    changed = 0
    stamped_employees: Set[str] = set()
    for employee_id in frame["employee_id"].astype(str):
        profile = employees_by_id.get(employee_id)
        if profile is None or not is_vacant_portage_line(profile.full_name):
            continue
        spec = portage_master_line_spec(profile)
        if spec is None:
            continue
        target_hours = float(employee_target_hours.get(employee_id, 0.0))
        is_fulltime = portage_is_fulltime_catalog_hours(target_hours)
        row_idx = row_lookup.get(employee_id)
        if row_idx is None:
            continue
        payroll_cap = portage_contract_shift_count(target_hours)
        for day in dates:
            if _count_work_shifts(frame, row_idx, dates) >= payroll_cap:
                break
            if not is_editable_cell(
                employee_id,
                day,
                locked_cells=locked_cells,
                blocked_map=blocked_map,
            ):
                continue
            if not is_empty_grid_token(frame.at[row_idx, day.isoformat()]):
                continue
            token = _catalog_shift_token_for_date(spec, day, period_start)
            if token not in {"D", "E", "N"}:
                continue
            if mode == FillMode.ALTERNATE_SHIFTS and (
                profile.contract_line_type or ""
            ).upper() == "D/E":
                if token == "D":
                    continue
                if token == "E":
                    continue
                if day.weekday() >= 5:
                    continue
            elif mode == FillMode.ALTERNATE_SHIFTS and is_fulltime:
                contract = (profile.contract_line_type or "").upper()
                if contract == "D/N":
                    if day.weekday() < 5 and token == "D":
                        continue
                    if token == "E":
                        continue
                else:
                    if day.weekday() < 5 and token == "D":
                        continue
                    if token == "E":
                        continue
                    if day.weekday() >= 5:
                        continue
            if token in {"E", "N"} and is_fulltime:
                qual = infer_qual_code(profile, qual_codes=qual_codes)
                counts = daily_band_qual_count(
                    frame,
                    row_lookup,
                    employees_by_id,
                    qual_codes,
                    day,
                    str(token),
                )
                if counts.get(qual, 0) >= _WEEKDAY_ALT_QUAL_FLOOR:
                    continue
            if token == "E":
                if not _evening_may_accept_shift(
                    frame,
                    row_idx,
                    dates,
                    profile=profile,
                    employee_target_hours=employee_target_hours,
                ):
                    continue
                if not is_fulltime and day.weekday() < 5:
                    qual = infer_qual_code(profile, qual_codes=qual_codes)
                    counts = daily_band_qual_count(
                        frame,
                        row_lookup,
                        employees_by_id,
                        qual_codes,
                        day,
                        "E",
                    )
                    if counts.get(qual, 0) >= _WEEKDAY_ALT_QUAL_FLOOR:
                        continue
            if _catalog_token_blocks_day_night_transition(
                spec,
                day,
                period_start,
                token,
                employee_id=employee_id,
            ):
                continue
            if not _stamp_catalog_shift_allowed(
                frame,
                row_idx,
                day,
                str(token),
                profile=profile,
                spec=spec,
                dates=dates,
                rules=rules,
                period_start=period_start,
                employee_id=employee_id,
            ):
                continue
            delta = _place_token(
                frame,
                row_idx=row_idx,
                employee_id=employee_id,
                day=day,
                band=str(token),
                mirror=False,
                locked_cells=locked_cells,
                blocked_map=blocked_map,
                allow_overwrite=False,
            )
            if delta:
                changed += delta
                stamped_employees.add(employee_id)
        changed += _repair_dn_weekend_night_orphans(
            frame,
            row_idx,
            dates,
            profile=profile,
            rules=rules,
            employee_id=employee_id,
        )
        if is_fulltime and _count_work_shifts(frame, row_idx, dates) > 0:
            stamped_employees.add(employee_id)
    if mode == FillMode.FULL and sched_profiles is not None:
        order = frame_order or [
            str(employee_id) for employee_id in frame["employee_id"].astype(str)
        ]
        evening_changed, evening_locked = _stamp_catalog_evenings_with_equity(
            frame,
            employees_by_id=employees_by_id,
            dates=dates,
            period_start=period_start,
            rules=rules,
            locked_cells=locked_cells,
            blocked_map=blocked_map,
            employee_target_hours=employee_target_hours,
            qual_codes=qual_codes,
            sched_profiles=sched_profiles,
            frame_order=order,
        )
        changed += evening_changed
        stamped_employees |= evening_locked
    return changed, stamped_employees


def _clear_preceding_weekday_ds_for_pool_night(
    frame: pd.DataFrame,
    row_idx: int,
    day: date,
) -> int:
    """Drop earlier same-week Day tokens so a pool-gap Night can satisfy the 6-day cap."""
    cleared = 0
    week_start = day - timedelta(days=day.weekday())
    probe = day - timedelta(days=1)
    while probe >= week_start and cleared < 2:
        if get_grid_token(frame, row_idx, probe) == "D":
            if _clear_row_token_if_work_shift(frame, row_idx, probe):
                cleared += 1
        probe -= timedelta(days=1)
    return cleared


def _clear_neighbor_days_for_pool_night(
    frame: pd.DataFrame,
    row_idx: int,
    day: date,
    dates: Sequence[date],
) -> int:
    """Clear adjacent catalog D cells that block pool-gap Night placement."""
    cleared = 0
    for offset in (-1, 1):
        neighbor = day + timedelta(days=offset)
        if neighbor < min(dates) or neighbor > max(dates):
            continue
        if get_grid_token(frame, row_idx, neighbor) == "D":
            if _clear_row_token_if_work_shift(frame, row_idx, neighbor):
                cleared += 1
    return cleared


def _validate_pool_gap_night_labor(
    frame: pd.DataFrame,
    row_idx: int,
    *,
    profile: EmployeeProfile,
    day: date,
    dates: Sequence[date],
    rules: JurisdictionRules,
    availability_blocked: Optional[Mapping[str, Set[date]]],
) -> bool:
    """Pool-gap Night checks: contract line, local streak, D→N, availability."""
    from lab_scheduler.engine.demand import infer_qual_code
    from lab_scheduler.scheduling.clinical_seats import assess_clinical_floor_contract_line

    if availability_blocked and day in availability_blocked.get(profile.id, set()):
        return False
    assessment = assess_clinical_floor_contract_line(
        contract_line_type=profile.contract_line_type,
        shift_code="NIGHT",
        qual_code=infer_qual_code(profile),
        role_pool_id=None,
    )
    if assessment.hard_rejection:
        return False
    if _would_violate_night_streak_cap(frame, row_idx, day, dates):
        return False
    prior = day - timedelta(days=1)
    if prior >= min(dates) and get_grid_token(frame, row_idx, prior) == "D":
        return False
    return True


def _try_place_pool_gap_night(
    frame: pd.DataFrame,
    *,
    row_idx: int,
    employee_id: str,
    day: date,
    dates: Sequence[date],
    employees: Sequence[Mapping[str, object]],
    employee_profiles: Sequence[EmployeeProfile],
    templates: Mapping[str, Mapping[str, object]],
    rules: JurisdictionRules,
    period_start: date,
    period_end: date,
    weeks_in_period: int,
    shift_templates: Mapping[str, ShiftTemplateInfo],
    employee_target_hours: Mapping[str, float],
    availability_blocked: Optional[Mapping[str, Set[date]]],
    locked_cells: Set[Tuple[str, date]],
    blocked_map: Mapping[str, Mapping[date, str]],
    pool_emergency: bool = False,
) -> int:
    """Place pool-gap Night, clearing blocking neighbor D cells when needed."""
    for _attempt in range(4):
        if not _would_violate_consecutive_work_cap(
            frame, row_idx, day, dates=dates, rules=rules
        ):
            break
        if pool_emergency:
            break
        relieved = _clear_preceding_weekday_ds_for_pool_night(frame, row_idx, day)
        if relieved == 0 and not _clear_neighbor_days_for_pool_night(
            frame, row_idx, day, dates
        ):
            return 0
    else:
        if not pool_emergency:
            return 0
    employee = next(
        (item for item in employee_profiles if item.id == employee_id),
        None,
    )
    if employee is None:
        return 0
    if not pool_emergency and not _validate_pool_gap_night_labor(
        frame,
        row_idx,
        profile=employee,
        day=day,
        dates=dates,
        rules=rules,
        availability_blocked=availability_blocked,
    ):
        if _clear_neighbor_days_for_pool_night(frame, row_idx, day, dates):
            return _try_place_pool_gap_night(
                frame,
                row_idx=row_idx,
                employee_id=employee_id,
                day=day,
                dates=dates,
                employees=employees,
                employee_profiles=employee_profiles,
                templates=templates,
                rules=rules,
                period_start=period_start,
                period_end=period_end,
                weeks_in_period=weeks_in_period,
                shift_templates=shift_templates,
                employee_target_hours=employee_target_hours,
                availability_blocked=availability_blocked,
                locked_cells=locked_cells,
                blocked_map=blocked_map,
                pool_emergency=pool_emergency,
            )
        return 0
    return _place_token(
        frame,
        row_idx=row_idx,
        employee_id=employee_id,
        day=day,
        band="N",
        mirror=False,
        locked_cells=locked_cells,
        blocked_map=blocked_map,
        allow_overwrite=True,
    )


def _stamp_de_weekday_days_from_catalog(
    frame: pd.DataFrame,
    *,
    employees_by_id: Mapping[str, EmployeeProfile],
    dates: Sequence[date],
    period_start: date,
    rules: JurisdictionRules,
    locked_cells: Set[Tuple[str, date]],
    blocked_map: Mapping[str, Mapping[date, str]],
    employee_target_hours: Mapping[str, float],
    weekday_day_targets: Mapping[date, int] | None = None,
) -> int:
    """Fill weekday Day tokens on part-time D/E lines through payroll cap."""
    from lab_scheduler.scheduling.portage_equity_targets import (
        portage_is_fulltime_catalog_hours,
    )
    from lab_scheduler.scheduling.portage_template import portage_master_line_spec
    from lab_scheduler.scheduling.rotation_reference_builder import (
        _de_e_block_days_for_employee,
        _employee_weekday_d_in_week,
        _rest_day_after_e_block,
        _weekday_day_counts,
        _would_placing_day_violate_work_cap,
        compute_de_weekday_day_targets,
    )
    from lab_scheduler.solver.cpsat_fill import is_vacant_portage_line

    row_lookup = schedule_frame_row_index_by_employee_id(frame)
    weekdays = [day for day in dates if day.weekday() < 5]
    pt_employee_ids = [
        employee_id
        for employee_id, row_idx in row_lookup.items()
        if (profile := employees_by_id.get(employee_id)) is not None
        and is_vacant_portage_line(profile.full_name)
        and (profile.contract_line_type or "").upper() == "D/E"
        and not portage_is_fulltime_catalog_hours(
            float(employee_target_hours.get(employee_id, 0.0))
        )
        and portage_master_line_spec(profile) is not None
    ]
    if weekday_day_targets is None:
        weekday_day_targets = compute_de_weekday_day_targets(
            frame,
            row_lookup=row_lookup,
            employees_by_id=employees_by_id,
            dates=dates,
            period_start=period_start,
            employee_target_hours=employee_target_hours,
            ft_deficits={},
        )
    changed = 0
    while pt_employee_ids:
        day_counts = _weekday_day_counts(frame, row_lookup, weekdays)
        progressed = False
        for employee_id in pt_employee_ids:
            profile = employees_by_id.get(employee_id)
            row_idx = row_lookup[employee_id]
            if profile is None:
                continue
            spec = portage_master_line_spec(profile)
            if spec is None:
                continue
            payroll_cap = portage_contract_shift_count(
                float(employee_target_hours.get(employee_id, 0.0))
            )
            if _count_work_shifts(frame, row_idx, dates) >= payroll_cap:
                continue
            e_block_days = _de_e_block_days_for_employee(
                profile,
                period_start=period_start,
                dates=dates,
            )
            rest_days = _rest_day_after_e_block(e_block_days)
            candidate_days = sorted(
                (
                    day
                    for day in weekdays
                    if day not in e_block_days
                    and day not in rest_days
                    and is_editable_cell(
                        employee_id,
                        day,
                        locked_cells=locked_cells,
                        blocked_map=blocked_map,
                    )
                    and is_empty_grid_token(frame.at[row_idx, day.isoformat()])
                    and not _would_placing_day_violate_work_cap(
                        frame, row_idx, day, dates=dates, rules=rules
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
                progressed = True
                day_counts[day] += 1
        if not progressed:
            break
        pt_employee_ids = [
            employee_id
            for employee_id in pt_employee_ids
            if _count_work_shifts(frame, row_lookup[employee_id], dates)
            < portage_contract_shift_count(
                float(employee_target_hours.get(employee_id, 0.0))
            )
        ]
    return changed


def _cover_dn_pool_night_gaps(
    frame: pd.DataFrame,
    *,
    frame_order: Sequence[str],
    profiles: Mapping[str, EmployeeSchedulingProfile],
    row_lookup: Mapping[str, int],
    employees_by_id: Mapping[str, EmployeeProfile],
    employees: Sequence[Mapping[str, object]],
    employee_profiles: Sequence[EmployeeProfile],
    dates: Sequence[date],
    qual_codes: Mapping[str, str],
    templates: Mapping[str, Mapping[str, object]],
    rules: JurisdictionRules,
    period_start: date,
    period_end: date,
    weeks_in_period: int,
    shift_templates: Mapping[str, ShiftTemplateInfo],
    employee_target_hours: Mapping[str, float],
    availability_blocked: Optional[Mapping[str, Set[date]]],
    locked_cells: Set[Tuple[str, date]],
    blocked_map: Mapping[str, Mapping[date, str]],
    allow_emergency_override: bool = False,
) -> int:
    """
    Cover weekday pool night gaps left by catalog sacrifice (6-day / night-streak caps).

    When the active D/N night-block line cannot work N, borrow from another D/N line
    in the same qual pool (typically a line on its day-block week).
    """
    changed = 0
    for day in dates:
        if day.weekday() >= 5:
            continue
        for qual in ("MLT", "MLA"):
            counts = daily_band_qual_count(
                frame,
                row_lookup,
                employees_by_id,
                qual_codes,
                day,
                "N",
            )
            if counts.get(qual, 0) >= _WEEKDAY_ALT_QUAL_FLOOR:
                continue
            pool_emergency = (
                allow_emergency_override
                and counts.get(qual, 0) < _WEEKDAY_ALT_QUAL_FLOOR
            )
            night_cap = PORTAGE_DN_FT_NIGHT_SHIFT_TARGET + (
                1 if pool_emergency else 0
            )
            candidates: List[Tuple[int, int, int, str]] = []
            for employee_id in frame_order:
                sched_profile = profiles.get(employee_id)
                employee = employees_by_id.get(employee_id)
                row_idx = row_lookup.get(employee_id)
                if (
                    sched_profile is None
                    or employee is None
                    or row_idx is None
                    or not sched_profile.eligible_for_fill
                ):
                    continue
                if (employee.contract_line_type or "").upper() != "D/N":
                    continue
                if infer_qual_code(employee, qual_codes=qual_codes) != qual:
                    continue
                if not is_editable_cell(
                    employee_id,
                    day,
                    locked_cells=locked_cells,
                    blocked_map=blocked_map,
                ):
                    continue
                existing = get_grid_token(frame, row_idx, day)
                if existing == "N":
                    continue
                if existing == "E":
                    continue
                if existing not in {"", OFF_DISPLAY, "D"} and not is_empty_grid_token(existing):
                    continue
                if _would_violate_consecutive_work_cap(
                    frame, row_idx, day, dates=dates, rules=rules
                ):
                    if not pool_emergency:
                        continue
                if _would_violate_night_streak_cap(frame, row_idx, day, dates):
                    if not pool_emergency:
                        continue
                night_count = _count_row_band_shifts(frame, row_idx, dates, "N")
                if night_count >= night_cap:
                    continue
                rank = 0 if is_empty_grid_token(existing) else 1
                candidates.append((night_count, rank, sched_profile.pool_index, employee_id))
            placed = False
            for _nights, _rank, _pool, employee_id in sorted(candidates):
                row_idx = row_lookup[employee_id]
                if not _passes_cap_checks(
                    frame,
                    row_lookup,
                    employees_by_id,
                    qual_codes,
                    employee_id=employee_id,
                    day=day,
                    band="N",
                ):
                    continue
                delta = _try_place_pool_gap_night(
                    frame,
                    row_idx=row_idx,
                    employee_id=employee_id,
                    day=day,
                    dates=dates,
                    employees=employees,
                    employee_profiles=employee_profiles,
                    templates=templates,
                    rules=rules,
                    period_start=period_start,
                    period_end=period_end,
                    weeks_in_period=weeks_in_period,
                    shift_templates=shift_templates,
                    employee_target_hours=employee_target_hours,
                    availability_blocked=availability_blocked,
                    locked_cells=locked_cells,
                    blocked_map=blocked_map,
                    pool_emergency=pool_emergency,
                )
                if delta:
                    changed += delta
                    placed = True
                    break
    return changed


def _passes_cap_checks(
    frame: pd.DataFrame,
    row_lookup: Mapping[str, int],
    employees_by_id: Mapping[str, EmployeeProfile],
    qual_codes: Mapping[str, str],
    *,
    employee_id: str,
    day: date,
    band: str,
) -> bool:
    if day.weekday() >= 5:
        return can_place_weekend_token(
            frame,
            row_lookup,
            employees_by_id,
            qual_codes,
            employee_id=employee_id,
            day=day,
            token=band,
        )
    if band in {"E", "N"}:
        employee = employees_by_id.get(employee_id)
        if employee is None:
            return False
        qual = infer_qual_code(employee, qual_codes=qual_codes)
        cap = operational_alt_band_cap_per_qual(band)
        counts = daily_band_qual_count(
            frame,
            row_lookup,
            employees_by_id,
            qual_codes,
            day,
            band,
        )
        return counts.get(qual, 0) < cap
    return True


def _fill_weekday_alt_clinical_gaps(
    frame: pd.DataFrame,
    *,
    frame_order: Sequence[str],
    profiles: Mapping[str, EmployeeSchedulingProfile],
    row_lookup: Mapping[str, int],
    employees_by_id: Mapping[str, EmployeeProfile],
    employees: Sequence[Mapping[str, object]],
    employee_profiles: Sequence[EmployeeProfile],
    dates: Sequence[date],
    qual_codes: Mapping[str, str],
    templates: Mapping[str, Mapping[str, object]],
    rules: JurisdictionRules,
    period_start: date,
    period_end: date,
    weeks_in_period: int,
    shift_templates: Mapping[str, ShiftTemplateInfo],
    employee_target_hours: Mapping[str, float],
    availability_blocked: Optional[Mapping[str, Set[date]]],
    payroll_limit: Mapping[str, int],
    locked_cells: Set[Tuple[str, date]],
    blocked_map: Mapping[str, Mapping[date, str]],
    catalog_locked: Set[str],
) -> int:
    """Place weekday E/N until each qual reaches the daily clinical floor (2/qual)."""
    changed = 0
    for day in dates:
        if day.weekday() >= 5:
            continue
        for band in ("E", "N"):
            for qual in ("MLT", "MLA"):
                while True:
                    counts = daily_band_qual_count(
                        frame,
                        row_lookup,
                        employees_by_id,
                        qual_codes,
                        day,
                        band,
                    )
                    if counts.get(qual, 0) >= _WEEKDAY_ALT_QUAL_FLOOR:
                        break
                    candidates: List[Tuple[int, int, str]] = []
                    for employee_id in frame_order:
                        profile = profiles.get(employee_id)
                        row_idx = row_lookup.get(employee_id)
                        if profile is None or row_idx is None or not profile.eligible_for_fill:
                            continue
                        if profile.alternate_band != band:
                            continue
                        employee = employees_by_id.get(employee_id)
                        if employee is None:
                            continue
                        if infer_qual_code(employee, qual_codes=qual_codes) != qual:
                            continue
                        if not is_editable_cell(
                            employee_id,
                            day,
                            locked_cells=locked_cells,
                            blocked_map=blocked_map,
                        ):
                            continue
                        if not is_empty_grid_token(frame.at[row_idx, day.isoformat()]):
                            continue
                        if _count_work_shifts(frame, row_idx, dates) >= payroll_limit.get(
                            employee_id, 0
                        ):
                            continue
                        if band == "E":
                            tier_deficit = _weekday_evening_tier_deficit(
                                frame,
                                row_idx,
                                dates,
                                profile,
                                profile=employee,
                                employee_target_hours=employee_target_hours,
                            )
                            if tier_deficit <= 0:
                                continue
                            rank = _evening_equity_rank(
                                frame,
                                row_idx,
                                dates,
                                profile=employee,
                                sched_profile=profile,
                                employee_target_hours=employee_target_hours,
                                pool_index=profile.pool_index,
                                day=day,
                            )
                            candidates.append((rank, employee_id))
                        else:
                            candidates.append(
                                (
                                    (profile.pool_index, _count_work_shifts(frame, row_idx, dates)),
                                    employee_id,
                                )
                            )
                    placed = False
                    if candidates:
                        for _rank, employee_id in sorted(candidates):
                            row_idx = row_lookup[employee_id]
                            if not _passes_cap_checks(
                                frame,
                                row_lookup,
                                employees_by_id,
                                qual_codes,
                                employee_id=employee_id,
                                day=day,
                                band=band,
                            ):
                                continue
                            if not _validate_labor(
                                frame=frame,
                                employees=employees,
                                profiles=employee_profiles,
                                dates=dates,
                                db_templates=templates,
                                rules=rules,
                                period_start=period_start,
                                period_end=period_end,
                                weeks_in_period=weeks_in_period,
                                shift_templates=shift_templates,
                                employee_target_hours=employee_target_hours,
                                availability_blocked=availability_blocked,
                                employee_id=employee_id,
                                day=day,
                                band=band,
                            ):
                                continue
                            if _place_token(
                                frame,
                                row_idx=row_idx,
                                employee_id=employee_id,
                                day=day,
                                band=band,
                                mirror=False,
                                locked_cells=locked_cells,
                                blocked_map=blocked_map,
                                allow_overwrite=False,
                            ):
                                changed += 1
                                placed = True
                                break
                    if not placed and band == "E":
                        emergency_ids: List[Tuple[Tuple[int, int, int], str]] = []
                        for eid in frame_order:
                            profile = profiles.get(eid)
                            row_idx = row_lookup.get(eid)
                            employee = employees_by_id.get(eid)
                            if profile is None or row_idx is None or employee is None:
                                continue
                            if not profile.eligible_for_fill or profile.alternate_band != band:
                                continue
                            if infer_qual_code(employee, qual_codes=qual_codes) != qual:
                                continue
                            if not is_editable_cell(
                                eid, day, locked_cells=locked_cells, blocked_map=blocked_map
                            ):
                                continue
                            if not is_empty_grid_token(frame.at[row_idx, day.isoformat()]):
                                continue
                            if _count_work_shifts(frame, row_idx, dates) >= payroll_limit.get(
                                eid, 0
                            ):
                                continue
                            if not _evening_may_accept_shift(
                                frame,
                                row_idx,
                                dates,
                                profile=employee,
                                employee_target_hours=employee_target_hours,
                            ):
                                continue
                            weekday_e = _count_weekday_band_shifts(frame, row_idx, dates, "E")
                            rank = _evening_equity_rank(
                                frame,
                                row_idx,
                                dates,
                                profile=employee,
                                sched_profile=profile,
                                employee_target_hours=employee_target_hours,
                                pool_index=profile.pool_index,
                                day=day,
                            )
                            emergency_ids.append((rank, eid))
                        for _rank, employee_id in sorted(emergency_ids):
                            row_idx = row_lookup[employee_id]
                            if not _passes_cap_checks(
                                frame,
                                row_lookup,
                                employees_by_id,
                                qual_codes,
                                employee_id=employee_id,
                                day=day,
                                band=band,
                            ):
                                continue
                            if not _validate_labor(
                                frame=frame,
                                employees=employees,
                                profiles=employee_profiles,
                                dates=dates,
                                db_templates=templates,
                                rules=rules,
                                period_start=period_start,
                                period_end=period_end,
                                weeks_in_period=weeks_in_period,
                                shift_templates=shift_templates,
                                employee_target_hours=employee_target_hours,
                                availability_blocked=availability_blocked,
                                employee_id=employee_id,
                                day=day,
                                band=band,
                            ):
                                continue
                            if _place_token(
                                frame,
                                row_idx=row_idx,
                                employee_id=employee_id,
                                day=day,
                                band=band,
                                mirror=False,
                                locked_cells=locked_cells,
                                blocked_map=blocked_map,
                                allow_overwrite=False,
                            ):
                                changed += 1
                                placed = True
                                break
                        if not placed:
                            floor_ids: List[Tuple[int, str]] = []
                            for eid in frame_order:
                                profile = profiles.get(eid)
                                row_idx = row_lookup.get(eid)
                                employee = employees_by_id.get(eid)
                                if profile is None or row_idx is None or employee is None:
                                    continue
                                if not profile.eligible_for_fill or profile.alternate_band != band:
                                    continue
                                if infer_qual_code(employee, qual_codes=qual_codes) != qual:
                                    continue
                                if not is_editable_cell(
                                    eid, day, locked_cells=locked_cells, blocked_map=blocked_map
                                ):
                                    continue
                                if not is_empty_grid_token(frame.at[row_idx, day.isoformat()]):
                                    continue
                                if _count_work_shifts(frame, row_idx, dates) >= payroll_limit.get(
                                    eid, 0
                                ):
                                    continue
                                floor_ids.append(
                                    (
                                        _count_weekday_band_shifts(frame, row_idx, dates, "E"),
                                        eid,
                                    )
                                )
                            for _wk_e, employee_id in sorted(floor_ids):
                                row_idx = row_lookup[employee_id]
                                if not _passes_cap_checks(
                                    frame,
                                    row_lookup,
                                    employees_by_id,
                                    qual_codes,
                                    employee_id=employee_id,
                                    day=day,
                                    band=band,
                                ):
                                    continue
                                if not _validate_labor(
                                    frame=frame,
                                    employees=employees,
                                    profiles=employee_profiles,
                                    dates=dates,
                                    db_templates=templates,
                                    rules=rules,
                                    period_start=period_start,
                                    period_end=period_end,
                                    weeks_in_period=weeks_in_period,
                                    shift_templates=shift_templates,
                                    employee_target_hours=employee_target_hours,
                                    availability_blocked=availability_blocked,
                                    employee_id=employee_id,
                                    day=day,
                                    band=band,
                                ):
                                    continue
                                if _place_token(
                                    frame,
                                    row_idx=row_idx,
                                    employee_id=employee_id,
                                    day=day,
                                    band=band,
                                    mirror=False,
                                    locked_cells=locked_cells,
                                    blocked_map=blocked_map,
                                    allow_overwrite=False,
                                ):
                                    changed += 1
                                    placed = True
                                    break
                    if not placed:
                        break
    return changed


def _trim_over_target_evening_shifts(
    frame: pd.DataFrame,
    *,
    frame_order: Sequence[str],
    profiles: Mapping[str, EmployeeSchedulingProfile],
    row_lookup: Mapping[str, int],
    employees_by_id: Mapping[str, EmployeeProfile],
    dates: Sequence[date],
    period_start: date,
    qual_codes: Mapping[str, str],
    employee_target_hours: Mapping[str, float],
    locked_cells: Set[Tuple[str, date]],
    blocked_map: Mapping[str, Mapping[date, str]],
) -> int:
    """Drop surplus E on D/E lines above hours-weighted evening targets and daily MLT caps."""
    from lab_scheduler.scheduling.rotation_reference_builder import (
        _de_e_block_days_for_employee,
    )
    from lab_scheduler.solver.cpsat_fill import is_vacant_portage_line

    changed = 0
    e_block_by_employee: Dict[str, Set[date]] = {}
    for employee_id in frame_order:
        profile = employees_by_id.get(employee_id)
        if profile is None or not is_vacant_portage_line(profile.full_name):
            continue
        if (profile.contract_line_type or "").upper() != "D/E":
            continue
        e_block_by_employee[employee_id] = _de_e_block_days_for_employee(
            profile,
            period_start=period_start,
            dates=dates,
        )

    def _clear_e(employee_id: str, row_idx: int, day: date) -> bool:
        nonlocal changed
        if day in e_block_by_employee.get(employee_id, set()):
            return False
        if not is_editable_cell(
            employee_id, day, locked_cells=locked_cells, blocked_map=blocked_map
        ):
            return False
        if get_grid_token(frame, row_idx, day) != "E":
            return False
        employee = employees_by_id.get(employee_id)
        if employee is not None:
            qual = infer_qual_code(employee, qual_codes=qual_codes)
            counts = daily_band_qual_count(
                frame,
                row_lookup,
                employees_by_id,
                qual_codes,
                day,
                "E",
            )
            if counts.get(qual, 0) <= 1:
                return False
        if set_grid_token(frame, row_idx, day, OFF_DISPLAY):
            changed += 1
            return True
        return False

    for employee_id in frame_order:
        profile = employees_by_id.get(employee_id)
        row_idx = row_lookup.get(employee_id)
        if profile is None or row_idx is None or not is_vacant_portage_line(profile.full_name):
            continue
        if (profile.contract_line_type or "").upper() != "D/E":
            continue
        target = _evening_shift_target(profile, employee_target_hours)
        surplus = _count_row_band_shifts(frame, row_idx, dates, "E") - target
        if surplus <= 0:
            continue
        block_days = e_block_by_employee.get(employee_id, set())
        day_order = [day for day in dates if day not in block_days and day.weekday() < 5] + [
            day for day in dates if day not in block_days and day.weekday() >= 5
        ]
        for day in day_order:
            if surplus <= 0:
                break
            if _clear_e(employee_id, row_idx, day):
                surplus -= 1

    for day in dates:
        while True:
            over_cap = False
            for qual in ("MLT", "MLA"):
                counts = daily_band_qual_count(
                    frame,
                    row_lookup,
                    employees_by_id,
                    qual_codes,
                    day,
                    "E",
                )
                if counts.get(qual, 0) <= _clinical_evening_cap_per_qual(day):
                    continue
                over_cap = True
                donors: List[Tuple[int, str, int]] = []
                for employee_id in frame_order:
                    profile = employees_by_id.get(employee_id)
                    row_idx = row_lookup.get(employee_id)
                    if profile is None or row_idx is None:
                        continue
                    if infer_qual_code(profile, qual_codes=qual_codes) != qual:
                        continue
                    if get_grid_token(frame, row_idx, day) != "E":
                        continue
                    target = _evening_shift_target(profile, employee_target_hours)
                    assigned = _count_row_band_shifts(frame, row_idx, dates, "E")
                    donors.append((assigned - target, employee_id, row_idx))
                if not donors:
                    break
                donors.sort(reverse=True)
                _surplus, employee_id, row_idx = donors[0]
                if not _clear_e(employee_id, row_idx, day):
                    break
            if not over_cap:
                break
    return changed


def _trim_weekend_d_to_footer_target(
    frame: pd.DataFrame,
    *,
    row_lookup: Mapping[str, int],
    employees_by_id: Mapping[str, EmployeeProfile],
    dates: Sequence[date],
    frame_order: Sequence[str],
    stagger_assignments: Mapping[str, frozenset[date]],
    locked_cells: Set[Tuple[str, date]],
    blocked_map: Mapping[str, Mapping[date, str]],
) -> int:
    """Drop excess Sat/Sun Day shifts so footer matches ``shift_target_for_date`` (2 total)."""
    from lab_scheduler.scheduling.alternate_shift_distributor import (
        _vacant_catalog_line_number,
    )
    from lab_scheduler.scheduling.schedule_tallies import shift_target_for_date
    from lab_scheduler.scheduling.weekend_placement_rules import weekend_day_total_count
    from lab_scheduler.solver.cpsat_fill import is_vacant_portage_line

    def _removal_rank(employee_id: str, day: date) -> tuple[int, int]:
        profile = employees_by_id.get(employee_id)
        if profile is None:
            return (2, 999)
        line_no = _vacant_catalog_line_number(profile) or 999
        contract = (profile.contract_line_type or "").upper()
        block = stagger_assignments.get(employee_id)
        on_stagger = block is not None and day in block
        if contract == "D/E" and line_no <= 4:
            return (0, line_no)
        if contract == "D/E" and line_no >= 5 and on_stagger:
            return (3, line_no)
        if contract == "D/E" and line_no >= 5:
            return (2, line_no)
        return (1, line_no)

    changed = 0
    for day in dates:
        if day.weekday() < 5:
            continue
        target = shift_target_for_date(day, "D")
        while weekend_day_total_count(frame, row_lookup, day) > target:
            candidates: List[Tuple[tuple[int, int], str]] = []
            for employee_id in frame_order:
                profile = employees_by_id.get(employee_id)
                row_idx = row_lookup.get(employee_id)
                if profile is None or row_idx is None:
                    continue
                if not is_vacant_portage_line(profile.full_name):
                    continue
                if get_grid_token(frame, row_idx, day) != "D":
                    continue
                if not is_editable_cell(
                    employee_id,
                    day,
                    locked_cells=locked_cells,
                    blocked_map=blocked_map,
                ):
                    continue
                candidates.append((_removal_rank(employee_id, day), employee_id))
            if not candidates:
                break
            _, employee_id = sorted(candidates)[0]
            row_idx = row_lookup[employee_id]
            if set_grid_token(frame, row_idx, day, OFF_DISPLAY):
                changed += 1
            else:
                break
    return changed


def _trim_payroll_overflow_shifts(
    frame: pd.DataFrame,
    *,
    frame_order: Sequence[str],
    row_lookup: Mapping[str, int],
    employees_by_id: Mapping[str, EmployeeProfile],
    dates: Sequence[date],
    payroll_limit: Mapping[str, int],
    locked_cells: Set[Tuple[str, date]],
    blocked_map: Mapping[str, Mapping[date, str]],
) -> int:
    """Drop weekday Day shifts when a line exceeds its catalog shift payroll cap."""
    from lab_scheduler.solver.cpsat_fill import is_vacant_portage_line

    changed = 0
    for employee_id in frame_order:
        profile = employees_by_id.get(employee_id)
        row_idx = row_lookup.get(employee_id)
        if profile is None or row_idx is None or not is_vacant_portage_line(profile.full_name):
            continue
        cap = payroll_limit.get(employee_id, 0)
        if cap <= 0:
            continue
        surplus = _count_work_shifts(frame, row_idx, dates) - cap
        if surplus <= 0:
            continue
        for day in reversed([day for day in dates if day.weekday() < 5]):
            if surplus <= 0:
                break
            if not is_editable_cell(
                employee_id, day, locked_cells=locked_cells, blocked_map=blocked_map
            ):
                continue
            if get_grid_token(frame, row_idx, day) != "D":
                continue
            if set_grid_token(frame, row_idx, day, OFF_DISPLAY):
                changed += 1
                surplus -= 1
    return changed


def _ft_de_pool_ids(
    frame_order: Sequence[str],
    *,
    profiles: Mapping[str, EmployeeSchedulingProfile],
    employees_by_id: Mapping[str, EmployeeProfile],
    qual_codes: Mapping[str, str],
    employee_target_hours: Mapping[str, float],
    qual: str,
) -> List[str]:
    from lab_scheduler.scheduling.portage_equity_targets import (
        portage_is_fulltime_catalog_hours,
    )

    pool: List[str] = []
    for employee_id in frame_order:
        profile = employees_by_id.get(employee_id)
        sched = profiles.get(employee_id)
        if profile is None or sched is None or not sched.eligible_for_fill:
            continue
        if (sched.contract_line_type or "").upper() != "D/E":
            continue
        if infer_qual_code(profile, qual_codes=qual_codes) != qual:
            continue
        if not portage_is_fulltime_catalog_hours(
            float(employee_target_hours.get(employee_id, 0.0))
        ):
            continue
        pool.append(employee_id)
    return pool


def _weekday_evening_orphan_qual(
    frame: pd.DataFrame,
    *,
    row_lookup: Mapping[str, int],
    employees_by_id: Mapping[str, EmployeeProfile],
    qual_codes: Mapping[str, str],
    day: date,
) -> Optional[str]:
    """Return missing qual when exactly one of MLT/MLA is present on a weekday."""
    if day.weekday() >= 5:
        return None
    counts = daily_band_qual_count(
        frame, row_lookup, employees_by_id, qual_codes, day, "E"
    )
    mlt = counts.get("MLT", 0)
    mla = counts.get("MLA", 0)
    if mlt == 1 and mla == 0:
        return "MLA"
    if mla == 1 and mlt == 0:
        return "MLT"
    return None


def _ensure_clinical_evening_partner_or_revert(
    frame: pd.DataFrame,
    *,
    day: date,
    qual: str,
    row_idx: int,
    employee_id: str,
    frame_order: Sequence[str],
    profiles: Mapping[str, EmployeeSchedulingProfile],
    row_lookup: Mapping[str, int],
    employees_by_id: Mapping[str, EmployeeProfile],
    employees: Sequence[Mapping[str, object]],
    employee_profiles: Sequence[EmployeeProfile],
    dates: Sequence[date],
    qual_codes: Mapping[str, str],
    templates: Mapping[str, Mapping[str, object]],
    rules: JurisdictionRules,
    period_start: date,
    period_end: date,
    weeks_in_period: int,
    shift_templates: Mapping[str, ShiftTemplateInfo],
    employee_target_hours: Mapping[str, float],
    availability_blocked: Optional[Mapping[str, Set[date]]],
    locked_cells: Set[Tuple[str, date]],
    blocked_map: Mapping[str, Mapping[date, str]],
    alt_target_by_employee: Optional[Mapping[str, int]] = None,
) -> bool:
    """Keep a newly placed evening only when the partner qual can also cover the day."""
    partner_qual = "MLA" if qual == "MLT" else "MLT"
    partner_pool = _de_evening_pool_ids(
        frame_order,
        profiles=profiles,
        employees_by_id=employees_by_id,
        qual_codes=qual_codes,
        qual=partner_qual,
        employee_target_hours=employee_target_hours,
    )
    if not partner_pool:
        return True
    if _try_clinical_evening_for_qual_on_day(
        frame,
        day=day,
        qual=partner_qual,
        frame_order=frame_order,
        profiles=profiles,
        row_lookup=row_lookup,
        employees_by_id=employees_by_id,
        employees=employees,
        employee_profiles=employee_profiles,
        dates=dates,
        qual_codes=qual_codes,
        templates=templates,
        rules=rules,
        period_start=period_start,
        period_end=period_end,
        weeks_in_period=weeks_in_period,
        shift_templates=shift_templates,
        employee_target_hours=employee_target_hours,
        availability_blocked=availability_blocked,
        locked_cells=locked_cells,
        blocked_map=blocked_map,
        alt_target_by_employee=alt_target_by_employee,
    ):
        return True
    set_grid_token(frame, row_idx, day, OFF_DISPLAY)
    return False


def _evening_orphan_slot_ready(
    frame: pd.DataFrame,
    *,
    row_lookup: Mapping[str, int],
    employees_by_id: Mapping[str, EmployeeProfile],
    qual_codes: Mapping[str, str],
    employee_id: str,
    day: date,
    locked_cells: Set[Tuple[str, date]],
    blocked_map: Mapping[str, Mapping[date, str]],
) -> bool:
    row_idx = row_lookup[employee_id]
    token = get_grid_token(frame, row_idx, day)
    if token == "E":
        return False
    if token not in {"", "D"}:
        return False
    if not is_editable_cell(
        employee_id, day, locked_cells=locked_cells, blocked_map=blocked_map
    ):
        return False
    return _passes_cap_checks(
        frame,
        row_lookup,
        employees_by_id,
        qual_codes,
        employee_id=employee_id,
        day=day,
        band="E",
    )


def _place_evening_orphan_slot(
    frame: pd.DataFrame,
    *,
    row_idx: int,
    employee_id: str,
    employee: EmployeeProfile,
    day: date,
    dates: Sequence[date],
    employee_target_hours: Mapping[str, float],
    alt_target_by_employee: Optional[Mapping[str, int]] = None,
    employees: Sequence[Mapping[str, object]],
    employee_profiles: Sequence[EmployeeProfile],
    templates: Mapping[str, Mapping[str, object]],
    rules: JurisdictionRules,
    period_start: date,
    period_end: date,
    weeks_in_period: int,
    shift_templates: Mapping[str, ShiftTemplateInfo],
    availability_blocked: Optional[Mapping[str, Set[date]]],
    locked_cells: Set[Tuple[str, date]],
    blocked_map: Mapping[str, Mapping[date, str]],
) -> bool:
    token = get_grid_token(frame, row_idx, day)
    if token == "D":
        target = _evening_shift_target(
            employee,
            employee_target_hours,
            alt_target_by_employee=alt_target_by_employee,
        )
        if _evening_shift_deficit(frame, row_idx, dates, target) <= 0:
            return False
        return set_grid_token(frame, row_idx, day, "E")
    if not _validate_labor(
        frame=frame,
        employees=employees,
        profiles=employee_profiles,
        dates=dates,
        db_templates=templates,
        rules=rules,
        period_start=period_start,
        period_end=period_end,
        weeks_in_period=weeks_in_period,
        shift_templates=shift_templates,
        employee_target_hours=employee_target_hours,
        availability_blocked=availability_blocked,
        employee_id=employee_id,
        day=day,
        band="E",
    ):
        return False
    return _place_token(
        frame,
        row_idx=row_idx,
        employee_id=employee_id,
        day=day,
        band="E",
        mirror=False,
        locked_cells=locked_cells,
        blocked_map=blocked_map,
        allow_overwrite=False,
    )


def _pair_solo_evening_orphan_weekdays(
    frame: pd.DataFrame,
    *,
    frame_order: Sequence[str],
    profiles: Mapping[str, EmployeeSchedulingProfile],
    row_lookup: Mapping[str, int],
    employees_by_id: Mapping[str, EmployeeProfile],
    employees: Sequence[Mapping[str, object]],
    employee_profiles: Sequence[EmployeeProfile],
    dates: Sequence[date],
    qual_codes: Mapping[str, str],
    templates: Mapping[str, Mapping[str, object]],
    rules: JurisdictionRules,
    period_start: date,
    period_end: date,
    weeks_in_period: int,
    shift_templates: Mapping[str, ShiftTemplateInfo],
    employee_target_hours: Mapping[str, float],
    availability_blocked: Optional[Mapping[str, Set[date]]],
    locked_cells: Set[Tuple[str, date]],
    blocked_map: Mapping[str, Mapping[date, str]],
    alt_target_by_employee: Optional[Mapping[str, int]] = None,
) -> int:
    """Top up missing qual on weekday orphan evenings (deficit peers only; never clears solo E)."""
    changed = 0
    for day in dates:
        if day.weekday() >= 5:
            continue
        missing = _weekday_evening_orphan_qual(
            frame,
            row_lookup=row_lookup,
            employees_by_id=employees_by_id,
            qual_codes=qual_codes,
            day=day,
        )
        if missing is None:
            continue

        if _try_clinical_evening_for_qual_on_day(
            frame,
            day=day,
            qual=missing,
            frame_order=frame_order,
            profiles=profiles,
            row_lookup=row_lookup,
            employees_by_id=employees_by_id,
            employees=employees,
            employee_profiles=employee_profiles,
            dates=dates,
            qual_codes=qual_codes,
            templates=templates,
            rules=rules,
            period_start=period_start,
            period_end=period_end,
            weeks_in_period=weeks_in_period,
            shift_templates=shift_templates,
            employee_target_hours=employee_target_hours,
            availability_blocked=availability_blocked,
            locked_cells=locked_cells,
            blocked_map=blocked_map,
            alt_target_by_employee=alt_target_by_employee,
        ):
            changed += 1
            continue
    return changed


def _try_clinical_evening_for_qual_on_day(
    frame: pd.DataFrame,
    *,
    day: date,
    qual: str,
    frame_order: Sequence[str],
    profiles: Mapping[str, EmployeeSchedulingProfile],
    row_lookup: Mapping[str, int],
    employees_by_id: Mapping[str, EmployeeProfile],
    employees: Sequence[Mapping[str, object]],
    employee_profiles: Sequence[EmployeeProfile],
    dates: Sequence[date],
    qual_codes: Mapping[str, str],
    templates: Mapping[str, Mapping[str, object]],
    rules: JurisdictionRules,
    period_start: date,
    period_end: date,
    weeks_in_period: int,
    shift_templates: Mapping[str, ShiftTemplateInfo],
    employee_target_hours: Mapping[str, float],
    availability_blocked: Optional[Mapping[str, Set[date]]],
    locked_cells: Set[Tuple[str, date]],
    blocked_map: Mapping[str, Mapping[date, str]],
    alt_target_by_employee: Optional[Mapping[str, int]] = None,
    allow_catalog_d_swap: bool = False,
    skip_labor_for_footer: bool = False,
    stagger_assignments: Optional[Mapping[str, frozenset[date]]] = None,
) -> bool:
    """Place one evening shift for qual on day if below clinical floor."""
    if day.weekday() < 5:
        counts = daily_band_qual_count(
            frame, row_lookup, employees_by_id, qual_codes, day, "E"
        )
    else:
        counts = weekend_band_qual_count(
            frame, row_lookup, employees_by_id, qual_codes, day, "E"
        )
    if counts.get(qual, 0) >= _clinical_evening_cap_per_qual(day):
        return True
    candidates: List[Tuple[int, Tuple[int, int, int, int], str, int, str]] = []
    for employee_id in _de_evening_pool_ids(
        frame_order,
        profiles=profiles,
        employees_by_id=employees_by_id,
        qual_codes=qual_codes,
        qual=qual,
        employee_target_hours=employee_target_hours,
    ):
        employee = employees_by_id[employee_id]
        sched = profiles[employee_id]
        row_idx = row_lookup[employee_id]
        if stagger_assignments and _is_de_l58_stagger_weekend_day(
            employee, employee_id, day, stagger_assignments
        ):
            continue
        target = _evening_shift_target(
            employee,
            employee_target_hours,
            alt_target_by_employee=alt_target_by_employee,
        )
        deficit = _evening_shift_deficit(frame, row_idx, dates, target)
        existing = get_grid_token(frame, row_idx, day)
        if deficit <= 0 and not (allow_catalog_d_swap and existing == "D"):
            continue
        if not is_editable_cell(
            employee_id, day, locked_cells=locked_cells, blocked_map=blocked_map
        ):
            continue
        if existing not in {"", "D"}:
            continue
        rank = _evening_equity_rank(
            frame,
            row_idx,
            dates,
            profile=employee,
            sched_profile=sched,
            employee_target_hours=employee_target_hours,
            pool_index=sched.pool_index,
            day=day,
        )
        candidates.append((max(deficit, 0), rank, employee_id, row_idx, existing))
    for _deficit, _rank, employee_id, row_idx, existing in sorted(
        candidates, key=lambda item: (-item[0], item[1])
    ):
        if not _passes_cap_checks(
            frame,
            row_lookup,
            employees_by_id,
            qual_codes,
            employee_id=employee_id,
            day=day,
            band="E",
        ):
            continue
        if existing == "D":
            if stagger_assignments and _is_de_l58_stagger_weekend_day(
                employees_by_id[employee_id], employee_id, day, stagger_assignments
            ):
                continue
            if set_grid_token(frame, row_idx, day, "E"):
                return True
            continue
        if not skip_labor_for_footer and not _validate_labor(
            frame=frame,
            employees=employees,
            profiles=employee_profiles,
            dates=dates,
            db_templates=templates,
            rules=rules,
            period_start=period_start,
            period_end=period_end,
            weeks_in_period=weeks_in_period,
            shift_templates=shift_templates,
            employee_target_hours=employee_target_hours,
            availability_blocked=availability_blocked,
            employee_id=employee_id,
            day=day,
            band="E",
        ):
            continue
        mirror = False
        if _place_token(
            frame,
            row_idx=row_idx,
            employee_id=employee_id,
            day=day,
            band="E",
            mirror=mirror,
            locked_cells=locked_cells,
            blocked_map=blocked_map,
            allow_overwrite=False,
        ):
            return True
    return False


def _fill_alternate_evening_clinical_floor(
    frame: pd.DataFrame,
    *,
    frame_order: Sequence[str],
    profiles: Mapping[str, EmployeeSchedulingProfile],
    row_lookup: Mapping[str, int],
    employees_by_id: Mapping[str, EmployeeProfile],
    employees: Sequence[Mapping[str, object]],
    employee_profiles: Sequence[EmployeeProfile],
    dates: Sequence[date],
    qual_codes: Mapping[str, str],
    templates: Mapping[str, Mapping[str, object]],
    rules: JurisdictionRules,
    period_start: date,
    period_end: date,
    weeks_in_period: int,
    shift_templates: Mapping[str, ShiftTemplateInfo],
    employee_target_hours: Mapping[str, float],
    availability_blocked: Optional[Mapping[str, Set[date]]],
    locked_cells: Set[Tuple[str, date]],
    blocked_map: Mapping[str, Mapping[date, str]],
    alt_target_by_employee: Optional[Mapping[str, int]] = None,
    stagger_assignments: Optional[Mapping[str, frozenset[date]]] = None,
) -> int:
    """Top up missing MLT or MLA evening on days below the clinical floor (footer 2/2)."""
    changed = 0
    for day in dates:
        for qual in ("MLT", "MLA"):
            while True:
                if day.weekday() < 5:
                    counts = daily_band_qual_count(
                        frame,
                        row_lookup,
                        employees_by_id,
                        qual_codes,
                        day,
                        "E",
                    )
                else:
                    counts = weekend_band_qual_count(
                        frame,
                        row_lookup,
                        employees_by_id,
                        qual_codes,
                        day,
                        "E",
                    )
                if counts.get(qual, 0) >= _clinical_evening_cap_per_qual(day):
                    break
                placed = _try_clinical_evening_for_qual_on_day(
                    frame,
                    day=day,
                    qual=qual,
                    frame_order=frame_order,
                    profiles=profiles,
                    row_lookup=row_lookup,
                    employees_by_id=employees_by_id,
                    employees=employees,
                    employee_profiles=employee_profiles,
                    dates=dates,
                    qual_codes=qual_codes,
                    templates=templates,
                    rules=rules,
                    period_start=period_start,
                    period_end=period_end,
                    weeks_in_period=weeks_in_period,
                    shift_templates=shift_templates,
                    employee_target_hours=employee_target_hours,
                    availability_blocked=availability_blocked,
                    locked_cells=locked_cells,
                    blocked_map=blocked_map,
                    alt_target_by_employee=alt_target_by_employee,
                    stagger_assignments=stagger_assignments,
                ) or _try_clinical_evening_for_qual_on_day(
                    frame,
                    day=day,
                    qual=qual,
                    frame_order=frame_order,
                    profiles=profiles,
                    row_lookup=row_lookup,
                    employees_by_id=employees_by_id,
                    employees=employees,
                    employee_profiles=employee_profiles,
                    dates=dates,
                    qual_codes=qual_codes,
                    templates=templates,
                    rules=rules,
                    period_start=period_start,
                    period_end=period_end,
                    weeks_in_period=weeks_in_period,
                    shift_templates=shift_templates,
                    employee_target_hours=employee_target_hours,
                    availability_blocked=availability_blocked,
                    locked_cells=locked_cells,
                    blocked_map=blocked_map,
                    alt_target_by_employee=alt_target_by_employee,
                    skip_labor_for_footer=True,
                    stagger_assignments=stagger_assignments,
                )
                if placed:
                    changed += 1
                else:
                    break
    return changed


def _rebalance_evening_surplus_to_deficit_peers(
    frame: pd.DataFrame,
    *,
    frame_order: Sequence[str],
    profiles: Mapping[str, EmployeeSchedulingProfile],
    row_lookup: Mapping[str, int],
    employees_by_id: Mapping[str, EmployeeProfile],
    employees: Sequence[Mapping[str, object]],
    employee_profiles: Sequence[EmployeeProfile],
    dates: Sequence[date],
    qual_codes: Mapping[str, str],
    templates: Mapping[str, Mapping[str, object]],
    rules: JurisdictionRules,
    period_start: date,
    period_end: date,
    weeks_in_period: int,
    shift_templates: Mapping[str, ShiftTemplateInfo],
    employee_target_hours: Mapping[str, float],
    availability_blocked: Optional[Mapping[str, Set[date]]],
    locked_cells: Set[Tuple[str, date]],
    blocked_map: Mapping[str, Mapping[date, str]],
    alt_target_by_employee: Optional[Mapping[str, int]] = None,
) -> int:
    """Move surplus evening shifts to under-target peers on the same day (footer-neutral)."""
    changed = 0
    for qual in ("MLT", "MLA"):
        pool_ids = _de_evening_pool_ids(
            frame_order,
            profiles=profiles,
            employees_by_id=employees_by_id,
            qual_codes=qual_codes,
            qual=qual,
            employee_target_hours=employee_target_hours,
        )
        surplus_ids: List[str] = []
        deficit_ids: List[str] = []
        for employee_id in pool_ids:
            employee = employees_by_id[employee_id]
            target = _evening_shift_target(
                employee,
                employee_target_hours,
                alt_target_by_employee=alt_target_by_employee,
            )
            row_idx = row_lookup[employee_id]
            assigned = _count_row_band_shifts(frame, row_idx, dates, "E")
            if assigned > target:
                surplus_ids.append(employee_id)
            elif assigned < target:
                deficit_ids.append(employee_id)
        if not surplus_ids or not deficit_ids:
            continue
        for day in dates:
            for surplus_id in list(surplus_ids):
                surplus_row = row_lookup[surplus_id]
                if get_grid_token(frame, surplus_row, day) != "E":
                    continue
                for deficit_id in list(deficit_ids):
                    deficit_row = row_lookup[deficit_id]
                    existing = get_grid_token(frame, deficit_row, day)
                    if existing not in {"", "D"}:
                        continue
                    if not is_editable_cell(
                        surplus_id,
                        day,
                        locked_cells=locked_cells,
                        blocked_map=blocked_map,
                    ) or not is_editable_cell(
                        deficit_id,
                        day,
                        locked_cells=locked_cells,
                        blocked_map=blocked_map,
                    ):
                        continue
                    if not _passes_cap_checks(
                        frame,
                        row_lookup,
                        employees_by_id,
                        qual_codes,
                        employee_id=deficit_id,
                        day=day,
                        band="E",
                    ):
                        continue
                    if existing == "D":
                        if not set_grid_token(frame, surplus_row, day, OFF_DISPLAY):
                            continue
                        if not set_grid_token(frame, deficit_row, day, "E"):
                            set_grid_token(frame, surplus_row, day, "E")
                            continue
                    elif not _validate_labor(
                        frame=frame,
                        employees=employees,
                        profiles=employee_profiles,
                        dates=dates,
                        db_templates=templates,
                        rules=rules,
                        period_start=period_start,
                        period_end=period_end,
                        weeks_in_period=weeks_in_period,
                        shift_templates=shift_templates,
                        employee_target_hours=employee_target_hours,
                        availability_blocked=availability_blocked,
                        employee_id=deficit_id,
                        day=day,
                        band="E",
                    ):
                        continue
                    elif not _place_token(
                        frame,
                        row_idx=deficit_row,
                        employee_id=deficit_id,
                        day=day,
                        band="E",
                        mirror=False,
                        locked_cells=locked_cells,
                        blocked_map=blocked_map,
                        allow_overwrite=False,
                    ):
                        continue
                    else:
                        if not set_grid_token(frame, surplus_row, day, OFF_DISPLAY):
                            set_grid_token(frame, deficit_row, day, OFF_DISPLAY)
                            continue
                    changed += 2
                    surplus_ids.remove(surplus_id)
                    deficit_ids.remove(deficit_id)
                    break
                if surplus_id not in surplus_ids:
                    break
    return changed


def _fill_ft_de_evening_deficits_to_target(
    frame: pd.DataFrame,
    *,
    frame_order: Sequence[str],
    profiles: Mapping[str, EmployeeSchedulingProfile],
    row_lookup: Mapping[str, int],
    employees_by_id: Mapping[str, EmployeeProfile],
    employees: Sequence[Mapping[str, object]],
    employee_profiles: Sequence[EmployeeProfile],
    dates: Sequence[date],
    qual_codes: Mapping[str, str],
    templates: Mapping[str, Mapping[str, object]],
    rules: JurisdictionRules,
    period_start: date,
    period_end: date,
    weeks_in_period: int,
    shift_templates: Mapping[str, ShiftTemplateInfo],
    employee_target_hours: Mapping[str, float],
    availability_blocked: Optional[Mapping[str, Set[date]]],
    locked_cells: Set[Tuple[str, date]],
    blocked_map: Mapping[str, Mapping[date, str]],
    alt_target_by_employee: Optional[Mapping[str, int]] = None,
) -> int:
    """Place weekday E on full-time D/E lines still under the pool evening target."""
    changed = 0
    for qual in ("MLT", "MLA"):
        pool_ids = _ft_de_pool_ids(
            frame_order,
            profiles=profiles,
            employees_by_id=employees_by_id,
            qual_codes=qual_codes,
            employee_target_hours=employee_target_hours,
            qual=qual,
        )
        if not pool_ids:
            continue
        for _ in range(len(dates) * len(pool_ids)):
            ranked: List[Tuple[Tuple[int, int, int, int], str, int]] = []
            for employee_id in pool_ids:
                employee = employees_by_id[employee_id]
                sched = profiles[employee_id]
                row_idx = row_lookup[employee_id]
                target = _evening_shift_target(
                    employee,
                    employee_target_hours,
                    alt_target_by_employee=alt_target_by_employee,
                )
                deficit = _evening_shift_deficit(frame, row_idx, dates, target)
                if deficit <= 0:
                    continue
                rank = _evening_equity_rank(
                    frame,
                    row_idx,
                    dates,
                    profile=employee,
                    sched_profile=sched,
                    employee_target_hours=employee_target_hours,
                    pool_index=sched.pool_index,
                )
                ranked.append((rank, employee_id, row_idx))
            if not ranked:
                break
            ranked.sort(key=lambda item: item[0])
            placed = False
            for day in dates:
                if day.weekday() < 5:
                    counts = daily_band_qual_count(
                        frame,
                        row_lookup,
                        employees_by_id,
                        qual_codes,
                        day,
                        "E",
                    )
                    if counts.get(qual, 0) >= _WEEKDAY_ALT_QUAL_FLOOR:
                        continue
                for _rank, employee_id, row_idx in ranked:
                    employee = employees_by_id[employee_id]
                    target = _evening_shift_target(
                        employee,
                        employee_target_hours,
                        alt_target_by_employee=alt_target_by_employee,
                    )
                    if _evening_shift_deficit(frame, row_idx, dates, target) <= 0:
                        continue
                    if not is_editable_cell(
                        employee_id,
                        day,
                        locked_cells=locked_cells,
                        blocked_map=blocked_map,
                    ):
                        continue
                    if not is_empty_grid_token(frame.at[row_idx, day.isoformat()]):
                        continue
                    if not _passes_cap_checks(
                        frame,
                        row_lookup,
                        employees_by_id,
                        qual_codes,
                        employee_id=employee_id,
                        day=day,
                        band="E",
                    ):
                        continue
                    if not _validate_labor(
                        frame=frame,
                        employees=employees,
                        profiles=employee_profiles,
                        dates=dates,
                        db_templates=templates,
                        rules=rules,
                        period_start=period_start,
                        period_end=period_end,
                        weeks_in_period=weeks_in_period,
                        shift_templates=shift_templates,
                        employee_target_hours=employee_target_hours,
                        availability_blocked=availability_blocked,
                        employee_id=employee_id,
                        day=day,
                        band="E",
                    ):
                        continue
                    if _place_token(
                        frame,
                        row_idx=row_idx,
                        employee_id=employee_id,
                        day=day,
                        band="E",
                        mirror=False,
                        locked_cells=locked_cells,
                        blocked_map=blocked_map,
                        allow_overwrite=False,
                    ):
                        changed += 1
                        placed = True
                        break
                if placed:
                    break
            if not placed:
                break
    return changed


def _promote_ft_de_day_shifts_to_evening(
    frame: pd.DataFrame,
    *,
    frame_order: Sequence[str],
    profiles: Mapping[str, EmployeeSchedulingProfile],
    row_lookup: Mapping[str, int],
    employees_by_id: Mapping[str, EmployeeProfile],
    employees: Sequence[Mapping[str, object]],
    employee_profiles: Sequence[EmployeeProfile],
    dates: Sequence[date],
    qual_codes: Mapping[str, str],
    templates: Mapping[str, Mapping[str, object]],
    rules: JurisdictionRules,
    period_start: date,
    period_end: date,
    weeks_in_period: int,
    shift_templates: Mapping[str, ShiftTemplateInfo],
    employee_target_hours: Mapping[str, float],
    availability_blocked: Optional[Mapping[str, Set[date]]],
    locked_cells: Set[Tuple[str, date]],
    blocked_map: Mapping[str, Mapping[date, str]],
    alt_target_by_employee: Optional[Mapping[str, int]] = None,
) -> int:
    """Convert a line's own D shifts to E when still under the full-time evening target."""
    changed = 0
    for qual in ("MLT", "MLA"):
        pool_ids = _ft_de_pool_ids(
            frame_order,
            profiles=profiles,
            employees_by_id=employees_by_id,
            qual_codes=qual_codes,
            employee_target_hours=employee_target_hours,
            qual=qual,
        )
        for employee_id in pool_ids:
            employee = employees_by_id[employee_id]
            row_idx = row_lookup[employee_id]
            target = _evening_shift_target(
                employee,
                employee_target_hours,
                alt_target_by_employee=alt_target_by_employee,
            )
            while _evening_shift_deficit(frame, row_idx, dates, target) > 0:
                promoted = False
                for day in dates:
                    if get_grid_token(frame, row_idx, day) != "D":
                        continue
                    if not is_editable_cell(
                        employee_id,
                        day,
                        locked_cells=locked_cells,
                        blocked_map=blocked_map,
                    ):
                        continue
                    if day.weekday() < 5:
                        counts = daily_band_qual_count(
                            frame,
                            row_lookup,
                            employees_by_id,
                            qual_codes,
                            day,
                            "E",
                        )
                        if counts.get(qual, 0) >= _WEEKDAY_ALT_QUAL_FLOOR:
                            continue
                    else:
                        counts = weekend_band_qual_count(
                            frame,
                            row_lookup,
                            employees_by_id,
                            qual_codes,
                            day,
                            "E",
                        )
                        if counts.get(qual, 0) >= weekend_alt_band_cap_per_qual("E"):
                            continue
                    if not _validate_labor(
                        frame=frame,
                        employees=employees,
                        profiles=employee_profiles,
                        dates=dates,
                        db_templates=templates,
                        rules=rules,
                        period_start=period_start,
                        period_end=period_end,
                        weeks_in_period=weeks_in_period,
                        shift_templates=shift_templates,
                        employee_target_hours=employee_target_hours,
                        availability_blocked=availability_blocked,
                        employee_id=employee_id,
                        day=day,
                        band="E",
                    ):
                        continue
                    if set_grid_token(frame, row_idx, day, "E"):
                        changed += 1
                        promoted = True
                        break
                if not promoted:
                    break
    return changed


def _rebalance_evening_equity(
    frame: pd.DataFrame,
    *,
    frame_order: Sequence[str],
    profiles: Mapping[str, EmployeeSchedulingProfile],
    row_lookup: Mapping[str, int],
    employees_by_id: Mapping[str, EmployeeProfile],
    employees: Sequence[Mapping[str, object]],
    employee_profiles: Sequence[EmployeeProfile],
    dates: Sequence[date],
    qual_codes: Mapping[str, str],
    templates: Mapping[str, Mapping[str, object]],
    rules: JurisdictionRules,
    period_start: date,
    period_end: date,
    weeks_in_period: int,
    shift_templates: Mapping[str, ShiftTemplateInfo],
    employee_target_hours: Mapping[str, float],
    availability_blocked: Optional[Mapping[str, Set[date]]],
    locked_cells: Set[Tuple[str, date]],
    blocked_map: Mapping[str, Mapping[date, str]],
    alt_target_by_employee: Optional[Mapping[str, int]] = None,
) -> int:
    """Same-day E swaps among full-time D/E peers to equalize total evening counts."""
    changed = 0
    for qual in ("MLT", "MLA"):
        pool_ids = _ft_de_pool_ids(
            frame_order,
            profiles=profiles,
            employees_by_id=employees_by_id,
            qual_codes=qual_codes,
            employee_target_hours=employee_target_hours,
            qual=qual,
        )
        if len(pool_ids) < 2:
            continue
        max_passes = len(pool_ids) * len(dates)
        pass_count = 0
        improved = True
        while improved and pass_count < max_passes:
            pass_count += 1
            improved = False
            deficits: List[Tuple[int, str]] = []
            surpluses: List[Tuple[int, str]] = []
            counts_by_employee: Dict[str, int] = {}
            for employee_id in pool_ids:
                employee = employees_by_id[employee_id]
                row_idx = row_lookup[employee_id]
                target = _evening_shift_target(
                    employee,
                    employee_target_hours,
                    alt_target_by_employee=alt_target_by_employee,
                )
                assigned = _count_row_band_shifts(frame, row_idx, dates, "E")
                counts_by_employee[employee_id] = assigned
                delta = target - assigned
                if delta > 0:
                    deficits.append((delta, employee_id))
                elif delta < 0:
                    surpluses.append((-delta, employee_id))
            deficits.sort(reverse=True)
            surpluses.sort(reverse=True)
            donor_order: List[str] = [employee_id for _score, employee_id in surpluses]
            if not donor_order and counts_by_employee:
                donor_order = sorted(
                    pool_ids,
                    key=lambda employee_id: counts_by_employee[employee_id],
                    reverse=True,
                )
            receiver_order: List[str] = [employee_id for _score, employee_id in deficits]
            if not receiver_order and counts_by_employee:
                receiver_order = sorted(
                    pool_ids,
                    key=lambda employee_id: counts_by_employee[employee_id],
                )
            if not donor_order or not receiver_order:
                break
            pool_spread = max(counts_by_employee.values()) - min(
                counts_by_employee.values()
            )
            all_at_target = all(
                counts_by_employee[employee_id]
                >= _evening_shift_target(
                    employees_by_id[employee_id],
                    employee_target_hours,
                    alt_target_by_employee=alt_target_by_employee,
                )
                for employee_id in pool_ids
            )
            if pool_spread == 0 or all_at_target:
                break
            for receiver_id in receiver_order:
                receiver_idx = row_lookup[receiver_id]
                receiver = employees_by_id[receiver_id]
                recv_target = _evening_shift_target(
                    receiver,
                    employee_target_hours,
                    alt_target_by_employee=alt_target_by_employee,
                )
                if counts_by_employee[receiver_id] >= recv_target:
                    continue
                for donor_id in donor_order:
                    if donor_id == receiver_id:
                        continue
                    if counts_by_employee[donor_id] <= counts_by_employee[receiver_id]:
                        continue
                    donor_idx = row_lookup[donor_id]
                    for day in dates:
                        if get_grid_token(frame, donor_idx, day) != "E":
                            continue
                        if not is_empty_grid_token(
                            frame.at[receiver_idx, day.isoformat()]
                        ):
                            continue
                        if not is_editable_cell(
                            donor_id,
                            day,
                            locked_cells=locked_cells,
                            blocked_map=blocked_map,
                        ):
                            continue
                        if not is_editable_cell(
                            receiver_id,
                            day,
                            locked_cells=locked_cells,
                            blocked_map=blocked_map,
                        ):
                            continue
                        if not _clear_row_token_if_work_shift(frame, donor_idx, day):
                            continue
                        if not _validate_labor(
                            frame=frame,
                            employees=employees,
                            profiles=employee_profiles,
                            dates=dates,
                            db_templates=templates,
                            rules=rules,
                            period_start=period_start,
                            period_end=period_end,
                            weeks_in_period=weeks_in_period,
                            shift_templates=shift_templates,
                            employee_target_hours=employee_target_hours,
                            availability_blocked=availability_blocked,
                            employee_id=receiver_id,
                            day=day,
                            band="E",
                        ):
                            set_grid_token(frame, donor_idx, day, "E")
                            continue
                        if set_grid_token(frame, receiver_idx, day, "E"):
                            changed += 2
                            improved = True
                            counts_by_employee[donor_id] -= 1
                            counts_by_employee[receiver_id] += 1
                            break
                    if improved:
                        break
                if improved:
                    break
    return changed


def _validate_labor(
    *,
    frame: pd.DataFrame,
    employees: Sequence[Mapping[str, object]],
    profiles: Sequence[EmployeeProfile],
    dates: Sequence[date],
    db_templates: Mapping[str, Mapping[str, object]],
    rules: JurisdictionRules,
    period_start: date,
    period_end: date,
    weeks_in_period: int,
    shift_templates: Mapping[str, ShiftTemplateInfo],
    employee_target_hours: Mapping[str, float],
    availability_blocked: Optional[Mapping[str, Set[date]]],
    employee_id: str,
    day: date,
    band: str,
) -> bool:
    template_id = template_id_from_short(db_templates, band)
    if template_id is None:
        return False
    employee = next((item for item in profiles if item.id == employee_id), None)
    if employee is None:
        return False
    assignments = assignments_from_schedule_frame(
        frame,
        employees=employees,
        dates=dates,
        templates=db_templates,
    )
    error = validate_assignment_change(
        rules=rules,
        period_start=period_start,
        period_end=period_end,
        weeks_in_period=weeks_in_period,
        employee=employee,
        all_assignments=assignments,
        shift_templates=dict(shift_templates),
        shift_required_qualifications={},
        assignment_date=day,
        new_shift_template_id=template_id,
        employee_target_hours=employee_target_hours,
        availability_blocked=availability_blocked,
        enforce_fte_target=False,
    )
    return error is None


def _place_token(
    frame: pd.DataFrame,
    *,
    row_idx: int,
    employee_id: str,
    day: date,
    band: str,
    mirror: bool,
    locked_cells: Set[Tuple[str, date]],
    blocked_map: Mapping[str, Mapping[date, str]],
    allow_overwrite: bool,
) -> int:
    changed = 0
    days = [day]
    if mirror:
        partner = mirror_weekend_partner(day)
        if partner is not None:
            days.append(partner)
    for target_day in days:
        if not is_editable_cell(
            employee_id,
            target_day,
            locked_cells=locked_cells,
            blocked_map=blocked_map,
        ):
            continue
        existing = get_grid_token(frame, row_idx, target_day)
        if not allow_overwrite and not is_empty_grid_token(existing):
            continue
        if existing == band:
            continue
        if set_grid_token(frame, row_idx, target_day, band):
            changed += 1
    return changed


def _run_weekend_stagger_slice(
    frame: pd.DataFrame,
    *,
    profiles: Mapping[str, EmployeeSchedulingProfile],
    employee_profiles: Sequence[EmployeeProfile],
    employees_by_id: Mapping[str, EmployeeProfile],
    dates: Sequence[date],
    period_start: date,
    stagger_assignments: Mapping[str, frozenset[date]],
    qual_codes: Mapping[str, str],
    locked_cells: Set[Tuple[str, date]],
    blocked_map: Mapping[str, Mapping[date, str]],
    warnings: List[str],
    mode: FillMode = FillMode.FULL,
) -> int:
    row_lookup = schedule_frame_row_index_by_employee_id(frame)
    changed = 0
    touched: Set[str] = set()

    def _placement_order(employee_id: str) -> Tuple[int, int]:
        profile = profiles.get(employee_id)
        if profile is None:
            return (9, 999)
        pass_rank = 0 if profile.weekend_band in {"E", "N"} else 1
        return (pass_rank, profile.pool_index)

    for employee_id in sorted(stagger_assignments, key=_placement_order):
        profile = profiles.get(employee_id)
        sched_profile = employees_by_id.get(employee_id)
        row_idx = row_lookup.get(employee_id)
        target_days = stagger_assignments.get(employee_id)
        if profile is None or sched_profile is None or row_idx is None or not target_days:
            continue
        if (
            mode == FillMode.ALTERNATE_SHIFTS
            and (sched_profile.contract_line_type or "").upper() == "D/E"
        ):
            from lab_scheduler.scheduling.alternate_shift_distributor import (
                _vacant_catalog_line_number,
            )
            from lab_scheduler.scheduling.rotation_reference_builder import (
                _de_e_block_days_for_employee,
                _stagger_skip_days_for_e_block,
            )

            line_no = _vacant_catalog_line_number(sched_profile)
            e_block_days = _de_e_block_days_for_employee(
                sched_profile,
                period_start=period_start,
                dates=dates,
            )
            stagger_skip = _stagger_skip_days_for_e_block(e_block_days)
            for day in dates:
                if day.weekday() < 5:
                    continue
                if day in stagger_skip:
                    if (
                        line_no is not None
                        and line_no >= 5
                        and day in target_days
                        and get_grid_token(frame, row_idx, day) == "D"
                        and is_editable_cell(
                            employee_id,
                            day,
                            locked_cells=locked_cells,
                            blocked_map=blocked_map,
                        )
                        and set_grid_token(frame, row_idx, day, OFF_DISPLAY)
                    ):
                        changed += 1
                        touched.add(employee_id)
                    continue
                if not is_editable_cell(
                    employee_id,
                    day,
                    locked_cells=locked_cells,
                    blocked_map=blocked_map,
                ):
                    continue
                if line_no is not None and line_no >= 5 and day in target_days:
                    if get_grid_token(frame, row_idx, day) == "D":
                        continue
                    if not _passes_cap_checks(
                        frame,
                        row_lookup,
                        employees_by_id,
                        qual_codes,
                        employee_id=employee_id,
                        day=day,
                        band="D",
                    ):
                        warnings.append(
                            f"{sched_profile.full_name}: weekend D cap blocked {day.isoformat()}."
                        )
                        continue
                    delta = _place_token(
                        frame,
                        row_idx=row_idx,
                        employee_id=employee_id,
                        day=day,
                        band="D",
                        mirror=False,
                        locked_cells=locked_cells,
                        blocked_map=blocked_map,
                        allow_overwrite=True,
                    )
                    if delta:
                        changed += delta
                        touched.add(employee_id)
                    continue
                if line_no is not None and line_no <= 4:
                    if get_grid_token(frame, row_idx, day) == "D":
                        if set_grid_token(frame, row_idx, day, OFF_DISPLAY):
                            changed += 1
                            touched.add(employee_id)
                    continue
                if day in target_days:
                    continue
                if get_grid_token(frame, row_idx, day) in {"D", "E", "N"}:
                    if set_grid_token(frame, row_idx, day, OFF_DISPLAY):
                        changed += 1
                        touched.add(employee_id)
            continue
        band = profile.weekend_band
        for day in dates:
            if day.weekday() < 5:
                continue
            if not is_editable_cell(
                employee_id,
                day,
                locked_cells=locked_cells,
                blocked_map=blocked_map,
            ):
                continue
            if day in target_days:
                if not _passes_cap_checks(
                    frame,
                    row_lookup,
                    employees_by_id,
                    qual_codes,
                    employee_id=employee_id,
                    day=day,
                    band=band,
                ):
                    warnings.append(
                        f"{sched_profile.full_name}: weekend cap blocked {day.isoformat()}."
                    )
                    continue
                delta = _place_token(
                    frame,
                    row_idx=row_idx,
                    employee_id=employee_id,
                    day=day,
                    band=band,
                    mirror=False,
                    locked_cells=locked_cells,
                    blocked_map=blocked_map,
                    allow_overwrite=True,
                )
                if delta:
                    changed += delta
                    touched.add(employee_id)
            elif get_grid_token(frame, row_idx, day) in {"D", "E", "N"}:
                if set_grid_token(frame, row_idx, day, OFF_DISPLAY):
                    changed += 1
                    touched.add(employee_id)
    return changed


def fill_schedule_by_preferences(
    draft: pd.DataFrame,
    *,
    employees: Sequence[Mapping[str, object]],
    dates: Sequence[date],
    period_start: date,
    period_end: date,
    weeks_in_period: int,
    rules: JurisdictionRules,
    templates: Mapping[str, Mapping[str, object]],
    shift_templates: Mapping[str, ShiftTemplateInfo],
    locked_cells: Set[Tuple[str, date]],
    blocked_map: Mapping[str, Mapping[date, str]],
    emp_quals: Mapping[str, Set[str]],
    qual_codes: Mapping[str, str],
    employee_target_hours: Mapping[str, float],
    policy: SchedulingPreferencePolicy,
    profiles: Optional[Mapping[str, EmployeeSchedulingProfile]] = None,
    availability_blocked: Optional[Mapping[str, Set[date]]] = None,
    mode: FillMode = FillMode.FULL,
) -> Tuple[pd.DataFrame, PreferenceFillResult]:
    working = draft.copy(deep=True)
    result = PreferenceFillResult()
    employee_profiles = _employees_to_profiles(employees, emp_quals=emp_quals)
    if profiles is None:
        profiles = build_portage_scheduling_profiles(
            working,
            employee_profiles,
            employee_target_hours=employee_target_hours,
            qual_codes=qual_codes,
        )
    employees_by_id = {profile.id: profile for profile in employee_profiles}
    row_lookup = schedule_frame_row_index_by_employee_id(working)
    frame_order = [
        str(employee_id)
        for employee_id in working["employee_id"].astype(str)
        if str(employee_id)
    ]
    stagger_assignments = build_pool_stagger_assignments(
        profiles,
        period_start=period_start,
        period_end=period_end,
        frame_order=frame_order,
        employees_by_id=employees_by_id,
        employee_target_hours=employee_target_hours,
    )
    result.stagger_applied = bool(stagger_assignments)

    if mode == FillMode.WEEKEND_STAGGER_SLICE:
        result.cells_changed = _run_weekend_stagger_slice(
            working,
            profiles=profiles,
            employee_profiles=employee_profiles,
            employees_by_id=employees_by_id,
            dates=dates,
            period_start=period_start,
            stagger_assignments=stagger_assignments,
            qual_codes=qual_codes,
            locked_cells=locked_cells,
            blocked_map=blocked_map,
            warnings=result.warnings,
        )
        result.lines_touched = len(
            {
                employee_id
                for employee_id in stagger_assignments
                if row_lookup.get(employee_id) is not None
            }
        )
        return working, result

    touched: Set[str] = set()
    tier_order = tiers_for_mode(mode)
    payroll_limit = {
        employee_id: portage_contract_shift_count(
            float(employee_target_hours.get(employee_id, 0.0))
        )
        for employee_id in profiles
    }
    from lab_scheduler.scheduling.portage_equity_targets import (
        build_vacant_line_alt_target_map,
    )

    alt_target_by_employee = build_vacant_line_alt_target_map(
        employee_profiles,
        employee_target_hours,
        qual_codes,
    )

    catalog_locked: Set[str] = set()
    if mode in {FillMode.FULL, FillMode.ALTERNATE_SHIFTS}:
        catalog_stamped, catalog_locked = _stamp_fulltime_master_catalog(
            working,
            employees_by_id=employees_by_id,
            dates=dates,
            period_start=period_start,
            rules=rules,
            locked_cells=locked_cells,
            blocked_map=blocked_map,
            employee_target_hours=employee_target_hours,
            qual_codes=qual_codes,
            mode=mode,
            sched_profiles=profiles,
            frame_order=frame_order,
        )
        if catalog_stamped:
            result.cells_changed += catalog_stamped
            result.tier_counts["master_catalog"] = catalog_stamped

    if mode == FillMode.ALTERNATE_SHIFTS:
        stripped_de_days = _strip_all_de_day_shifts(
            working,
            dates=dates,
            employees_by_id=employees_by_id,
            locked_cells=locked_cells,
            blocked_map=blocked_map,
        )
        if stripped_de_days:
            result.cells_changed += stripped_de_days
            result.tier_counts["de_day_strip"] = stripped_de_days
        cleared_evenings = _clear_all_de_evening_shifts(
            working,
            dates=dates,
            employees_by_id=employees_by_id,
            locked_cells=locked_cells,
            blocked_map=blocked_map,
        )
        if cleared_evenings:
            result.cells_changed += cleared_evenings
            result.tier_counts["de_evening_clear"] = cleared_evenings

    if mode == FillMode.ALTERNATE_SHIFTS:
        weekend_changed = _run_weekend_stagger_slice(
            working,
            profiles=profiles,
            employee_profiles=employee_profiles,
            employees_by_id=employees_by_id,
            dates=dates,
            period_start=period_start,
            stagger_assignments=stagger_assignments,
            qual_codes=qual_codes,
            locked_cells=locked_cells,
            blocked_map=blocked_map,
            warnings=result.warnings,
            mode=mode,
        )
        if weekend_changed:
            result.cells_changed += weekend_changed
            result.tier_counts["weekend_stagger"] = weekend_changed
        touched.update(
            employee_id
            for employee_id in stagger_assignments
            if row_lookup.get(employee_id) is not None
        )
        structured = _apply_de_seven_day_evening_blocks(
            working,
            frame_order=frame_order,
            employees_by_id=employees_by_id,
            qual_codes=qual_codes,
            employee_target_hours=employee_target_hours,
            period_start=period_start,
            dates=dates,
            employees=employees,
            employee_profiles=employee_profiles,
            templates=templates,
            rules=rules,
            period_end=period_end,
            weeks_in_period=weeks_in_period,
            shift_templates=shift_templates,
            availability_blocked=availability_blocked,
            row_lookup=row_lookup,
            locked_cells=locked_cells,
            blocked_map=blocked_map,
            stagger_assignments=stagger_assignments,
        )
        if structured:
            result.cells_changed += structured
            result.tier_counts["seven_day_evening_blocks"] = structured

    def _eligible_for_tier_fill(employee_id: str) -> bool:
        return employee_id not in catalog_locked

    def _try_candidate(
        employee_id: str,
        day: date,
        band: str,
        tier: SlotTier,
    ) -> bool:
        if not _eligible_for_tier_fill(employee_id):
            return False
        profile = profiles.get(employee_id)
        row_idx = row_lookup.get(employee_id)
        if profile is None or row_idx is None or not profile.eligible_for_fill:
            return False
        if not is_editable_cell(
            employee_id,
            day,
            locked_cells=locked_cells,
            blocked_map=blocked_map,
        ):
            return False
        if not is_empty_grid_token(working.at[row_idx, day.isoformat()]):
            return False
        if tier in {SlotTier.WEEKEND_ALT, SlotTier.WEEKEND_DAY}:
            block = stagger_assignments.get(employee_id)
            if block is not None and day not in block:
                return False
        if not _passes_cap_checks(
            working,
            row_lookup,
            employees_by_id,
            qual_codes,
            employee_id=employee_id,
            day=day,
            band=band,
        ):
            return False
        if not _validate_labor(
            frame=working,
            employees=employees,
            profiles=employee_profiles,
            dates=dates,
            db_templates=templates,
            rules=rules,
            period_start=period_start,
            period_end=period_end,
            weeks_in_period=weeks_in_period,
            shift_templates=shift_templates,
            employee_target_hours=employee_target_hours,
            availability_blocked=availability_blocked,
            employee_id=employee_id,
            day=day,
            band=band,
        ):
            return False
        mirror = day.weekday() >= 5
        if mirror:
            partner = mirror_weekend_partner(day)
            if partner is not None and is_editable_cell(
                employee_id,
                partner,
                locked_cells=locked_cells,
                blocked_map=blocked_map,
            ) and is_empty_grid_token(working.at[row_idx, partner.isoformat()]):
                if not _validate_labor(
                    frame=working,
                    employees=employees,
                    profiles=employee_profiles,
                    dates=dates,
                    db_templates=templates,
                    rules=rules,
                    period_start=period_start,
                    period_end=period_end,
                    weeks_in_period=weeks_in_period,
                    shift_templates=shift_templates,
                    employee_target_hours=employee_target_hours,
                    availability_blocked=availability_blocked,
                    employee_id=employee_id,
                    day=partner,
                    band=band,
                ):
                    mirror = False
        delta = _place_token(
            working,
            row_idx=row_idx,
            employee_id=employee_id,
            day=day,
            band=band,
            mirror=mirror,
            locked_cells=locked_cells,
            blocked_map=blocked_map,
            allow_overwrite=False,
        )
        if delta:
            result.cells_changed += delta
            touched.add(employee_id)
            tier_key = tier.value
            result.tier_counts[tier_key] = result.tier_counts.get(tier_key, 0) + delta
            return True
        return False

    for tier in tier_order:
        candidates: List[Tuple[int, int, int, str, date, str]] = []
        for employee_id in frame_order:
            profile = profiles.get(employee_id)
            row_idx = row_lookup.get(employee_id)
            if profile is None or row_idx is None or not profile.eligible_for_fill:
                continue
            if not _eligible_for_tier_fill(employee_id):
                continue
            if mode == FillMode.ALTERNATE_SHIFTS and (
                profile.contract_line_type or ""
            ).upper() == "D/E":
                continue
            target = profile.tier_targets.get(tier, 0)
            if target <= 0:
                continue
            assigned = _count_assigned_tier(
                working, row_idx, dates, tier, profile
            )
            if assigned >= target:
                continue
            payroll_cap = payroll_limit.get(employee_id, 0)
            if _count_work_shifts(working, row_idx, dates) >= payroll_cap:
                continue
            band = _band_for_tier(tier, profile)
            for day in dates:
                from lab_scheduler.scheduling.preference_policy import resolve_slot_tier

                if resolve_slot_tier(day, band, profile.contract_line_type) != tier:
                    continue
                if tier in {SlotTier.WEEKEND_ALT, SlotTier.WEEKEND_DAY}:
                    block = stagger_assignments.get(employee_id)
                    if block is not None and day not in block:
                        continue
                if not is_editable_cell(
                    employee_id,
                    day,
                    locked_cells=locked_cells,
                    blocked_map=blocked_map,
                ):
                    continue
                if not is_empty_grid_token(working.at[row_idx, day.isoformat()]):
                    continue
                tier_deficit = max(0, target - assigned)
                candidates.append(
                    (
                        -tier_deficit,
                        profile.pool_index,
                        day.toordinal(),
                        employee_id,
                        day,
                        band,
                    )
                )
        for _deficit, _pool_idx, _ordinal, employee_id, day, band in sorted(candidates):
            profile = profiles[employee_id]
            row_idx = row_lookup[employee_id]
            target = profile.tier_targets.get(tier, 0)
            assigned = _count_assigned_tier(
                working, row_idx, dates, tier, profile
            )
            if assigned >= target:
                continue
            payroll_cap = payroll_limit.get(employee_id, 0)
            if _count_work_shifts(working, row_idx, dates) >= payroll_cap:
                continue
            _try_candidate(employee_id, day, band, tier)

    if mode == FillMode.FULL:
        gap_changed = _fill_weekday_alt_clinical_gaps(
            working,
            frame_order=frame_order,
            profiles=profiles,
            row_lookup=row_lookup,
            employees_by_id=employees_by_id,
            employees=employees,
            employee_profiles=employee_profiles,
            dates=dates,
            qual_codes=qual_codes,
            templates=templates,
            rules=rules,
            period_start=period_start,
            period_end=period_end,
            weeks_in_period=weeks_in_period,
            shift_templates=shift_templates,
            employee_target_hours=employee_target_hours,
            availability_blocked=availability_blocked,
            payroll_limit=payroll_limit,
            locked_cells=locked_cells,
            blocked_map=blocked_map,
            catalog_locked=catalog_locked,
        )
        if gap_changed:
            result.cells_changed += gap_changed
            result.tier_counts["weekday_alt_gaps"] = gap_changed

    if mode in {FillMode.FULL, FillMode.ALTERNATE_SHIFTS}:
        pool_night_changed = _cover_dn_pool_night_gaps(
            working,
            frame_order=frame_order,
            profiles=profiles,
            row_lookup=row_lookup,
            employees_by_id=employees_by_id,
            employees=employees,
            employee_profiles=employee_profiles,
            dates=dates,
            qual_codes=qual_codes,
            templates=templates,
            rules=rules,
            period_start=period_start,
            period_end=period_end,
            weeks_in_period=weeks_in_period,
            shift_templates=shift_templates,
            employee_target_hours=employee_target_hours,
            availability_blocked=availability_blocked,
            locked_cells=locked_cells,
            blocked_map=blocked_map,
            allow_emergency_override=(mode == FillMode.ALTERNATE_SHIFTS),
        )
        if pool_night_changed:
            result.cells_changed += pool_night_changed
            result.tier_counts["pool_night_gaps"] = pool_night_changed
        trimmed = _trim_dn_scattered_pool_nights_over_target(
            working,
            dates=dates,
            employees_by_id=employees_by_id,
            locked_cells=locked_cells,
            blocked_map=blocked_map,
            period_start=period_start,
        )
        if trimmed:
            result.cells_changed += trimmed
            result.tier_counts["dn_night_trim"] = trimmed
        pool_repass = _cover_dn_pool_night_gaps(
            working,
            frame_order=frame_order,
            profiles=profiles,
            row_lookup=row_lookup,
            employees_by_id=employees_by_id,
            employees=employees,
            employee_profiles=employee_profiles,
            dates=dates,
            qual_codes=qual_codes,
            templates=templates,
            rules=rules,
            period_start=period_start,
            period_end=period_end,
            weeks_in_period=weeks_in_period,
            shift_templates=shift_templates,
            employee_target_hours=employee_target_hours,
            availability_blocked=availability_blocked,
            locked_cells=locked_cells,
            blocked_map=blocked_map,
            allow_emergency_override=(mode == FillMode.ALTERNATE_SHIFTS),
        )
        if pool_repass:
            result.cells_changed += pool_repass
            result.tier_counts["pool_night_gaps"] = (
                result.tier_counts.get("pool_night_gaps", 0) + pool_repass
            )

    if mode == FillMode.ALTERNATE_SHIFTS:
        from lab_scheduler.scheduling.rotation_reference_builder import (
            collect_de_ft_weekday_deficits,
            collect_dn_ft_weekday_deficits,
            compute_de_weekday_day_targets,
            fill_de_ft_weekday_days_balanced,
            fill_dn_ft_weekday_days_balanced,
        )

        pool_index_by_employee = {
            employee_id: profiles[employee_id].pool_index
            for employee_id in profiles
            if employee_id in profiles
        }
        ft_weekday_deficits = collect_de_ft_weekday_deficits(
            working,
            frame_order=frame_order,
            row_lookup=row_lookup,
            employees_by_id=employees_by_id,
            dates=dates,
            employee_target_hours=employee_target_hours,
        )
        dn_ft_weekday_deficits = collect_dn_ft_weekday_deficits(
            working,
            frame_order=frame_order,
            row_lookup=row_lookup,
            employees_by_id=employees_by_id,
            dates=dates,
            employee_target_hours=employee_target_hours,
        )
        weekday_day_targets = compute_de_weekday_day_targets(
            working,
            row_lookup=row_lookup,
            employees_by_id=employees_by_id,
            dates=dates,
            period_start=period_start,
            employee_target_hours=employee_target_hours,
            ft_deficits=ft_weekday_deficits,
            dn_ft_deficits=dn_ft_weekday_deficits,
        )
        balanced_days = fill_de_ft_weekday_days_balanced(
            working,
            frame_order=frame_order,
            row_lookup=row_lookup,
            employees_by_id=employees_by_id,
            dates=dates,
            period_start=period_start,
            rules=rules,
            locked_cells=locked_cells,
            blocked_map=blocked_map,
            employee_target_hours=employee_target_hours,
            pool_index_by_employee=pool_index_by_employee,
            weekday_day_targets=weekday_day_targets,
        )
        if balanced_days:
            result.cells_changed += balanced_days
            result.tier_counts["de_weekday_day_balanced"] = balanced_days
        balanced_dn_days = fill_dn_ft_weekday_days_balanced(
            working,
            frame_order=frame_order,
            row_lookup=row_lookup,
            employees_by_id=employees_by_id,
            dates=dates,
            rules=rules,
            locked_cells=locked_cells,
            blocked_map=blocked_map,
            employee_target_hours=employee_target_hours,
            weekday_day_targets=weekday_day_targets,
        )
        if balanced_dn_days:
            result.cells_changed += balanced_dn_days
            result.tier_counts["dn_weekday_day_balanced"] = balanced_dn_days
        stamped_de_days = _stamp_de_weekday_days_from_catalog(
            working,
            employees_by_id=employees_by_id,
            dates=dates,
            period_start=period_start,
            rules=rules,
            locked_cells=locked_cells,
            blocked_map=blocked_map,
            employee_target_hours=employee_target_hours,
            weekday_day_targets=weekday_day_targets,
        )
        if stamped_de_days:
            result.cells_changed += stamped_de_days
            result.tier_counts["de_weekday_day_stamp"] = stamped_de_days
        trimmed_evenings = _trim_over_target_evening_shifts(
            working,
            frame_order=frame_order,
            profiles=profiles,
            row_lookup=row_lookup,
            employees_by_id=employees_by_id,
            dates=dates,
            period_start=period_start,
            qual_codes=qual_codes,
            employee_target_hours=employee_target_hours,
            locked_cells=locked_cells,
            blocked_map=blocked_map,
        )
        if trimmed_evenings:
            result.cells_changed += trimmed_evenings
            result.tier_counts["evening_trim"] = trimmed_evenings
        clinical_floor = _fill_alternate_evening_clinical_floor(
            working,
            frame_order=frame_order,
            profiles=profiles,
            row_lookup=row_lookup,
            employees_by_id=employees_by_id,
            employees=employees,
            employee_profiles=employee_profiles,
            dates=dates,
            qual_codes=qual_codes,
            templates=templates,
            rules=rules,
            period_start=period_start,
            period_end=period_end,
            weeks_in_period=weeks_in_period,
            shift_templates=shift_templates,
            employee_target_hours=employee_target_hours,
            availability_blocked=availability_blocked,
            locked_cells=locked_cells,
            blocked_map=blocked_map,
            alt_target_by_employee=alt_target_by_employee,
            stagger_assignments=stagger_assignments,
        )
        if clinical_floor:
            result.cells_changed += clinical_floor
            result.tier_counts["evening_clinical_floor"] = clinical_floor
        weekend_d_reapply = _place_de_l58_stagger_weekend_days(
            working,
            frame_order=frame_order,
            profiles=profiles,
            row_lookup=row_lookup,
            employees_by_id=employees_by_id,
            dates=dates,
            period_start=period_start,
            stagger_assignments=stagger_assignments,
            qual_codes=qual_codes,
            locked_cells=locked_cells,
            blocked_map=blocked_map,
            warnings=result.warnings,
        )
        if weekend_d_reapply:
            result.cells_changed += weekend_d_reapply
            result.tier_counts["de_stagger_weekend_d"] = weekend_d_reapply
        post_clinical_trim = _trim_over_target_evening_shifts(
            working,
            frame_order=frame_order,
            profiles=profiles,
            row_lookup=row_lookup,
            employees_by_id=employees_by_id,
            dates=dates,
            period_start=period_start,
            qual_codes=qual_codes,
            employee_target_hours=employee_target_hours,
            locked_cells=locked_cells,
            blocked_map=blocked_map,
        )
        if post_clinical_trim:
            result.cells_changed += post_clinical_trim
            result.tier_counts["evening_trim"] = (
                result.tier_counts.get("evening_trim", 0) + post_clinical_trim
            )
        trimmed_payroll = _trim_payroll_overflow_shifts(
            working,
            frame_order=frame_order,
            row_lookup=row_lookup,
            employees_by_id=employees_by_id,
            dates=dates,
            payroll_limit=payroll_limit,
            locked_cells=locked_cells,
            blocked_map=blocked_map,
        )
        if trimmed_payroll:
            result.cells_changed += trimmed_payroll
            result.tier_counts["payroll_trim"] = trimmed_payroll
        trimmed_weekend_d = _trim_weekend_d_to_footer_target(
            working,
            row_lookup=row_lookup,
            employees_by_id=employees_by_id,
            dates=dates,
            frame_order=frame_order,
            stagger_assignments=stagger_assignments,
            locked_cells=locked_cells,
            blocked_map=blocked_map,
        )
        if trimmed_weekend_d:
            result.cells_changed += trimmed_weekend_d
            result.tier_counts["weekend_d_trim"] = trimmed_weekend_d
        from lab_scheduler.scheduling.rotation_reference_builder import (
            rebalance_weekday_day_shifts,
        )

        rebalanced = rebalance_weekday_day_shifts(
            working,
            frame_order=frame_order,
            row_lookup=row_lookup,
            employees_by_id=employees_by_id,
            dates=dates,
            period_start=period_start,
            rules=rules,
            locked_cells=locked_cells,
            blocked_map=blocked_map,
            employee_target_hours=employee_target_hours,
        )
        if rebalanced:
            result.cells_changed += rebalanced
            result.tier_counts["de_weekday_day_rebalance"] = rebalanced

    result.lines_touched = len(touched)
    return working, result
