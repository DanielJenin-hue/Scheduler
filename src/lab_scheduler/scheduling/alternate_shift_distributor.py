"""Manual alternate-shift redistribution for full-time Portage vacant lines."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

import pandas as pd

from lab_scheduler.compliance.engine import ShiftTemplateInfo
from lab_scheduler.compliance.jurisdictions import JurisdictionRules
from lab_scheduler.models.employee import normalize_contract_line_type
from lab_scheduler.policy.frame_bridge import (
    assignments_from_schedule_frame,
    normalize_grid_shift_token,
    schedule_frame_row_index_by_employee_id,
)
from lab_scheduler.scheduling.portage_equity_targets import (
    portage_alt_shift_target_for_employee,
    portage_is_fulltime_catalog_hours,
)
from lab_scheduler.scheduling.profiles import EmployeeProfile
from lab_scheduler.scheduling.schedule_tallies import shift_band_from_template_code
from lab_scheduler.scheduling.weekend_placement_rules import (
    OFF_DISPLAY,
    can_place_daily_alt as _can_place_daily_alt,
    can_place_weekend_token as _can_place_weekend_token,
    get_grid_token as _get_token,
    is_editable_cell as _is_editable,
    set_grid_token as _set_token,
)

FT_WEEKEND_SHIFT_DAYS = 4


@dataclass(slots=True)
class DistributeAltResult:
    cells_changed: int = 0
    lines_touched: int = 0
    pool_summaries: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    alt_spread_before: Dict[str, Tuple[int, int]] = field(default_factory=dict)
    alt_spread_after: Dict[str, Tuple[int, int]] = field(default_factory=dict)


def _daterange(start: date, end: date) -> Iterable[date]:
    current = start
    while current <= end:
        yield current
        current += timedelta(days=1)


def alternate_band_for_contract_line(contract_line_type: object) -> str:
    contract = normalize_contract_line_type(str(contract_line_type or "")) or "D/E"
    return "N" if contract == "D/N" else "E"


def weekend_band_for_contract_line(contract_line_type: object) -> str:
    """Weekend shifts use the alternate band (E on D/E, N on D/N)."""

    return alternate_band_for_contract_line(contract_line_type)


def _vacant_catalog_line_number(profile: EmployeeProfile) -> Optional[int]:
    from lab_scheduler.scheduling.portage_template import parse_vacant_portage_line

    parsed = parse_vacant_portage_line(profile.full_name)
    if parsed is None:
        return None
    return int(parsed[2])


def _weekend_stagger_block_index(line_no: int) -> int:
    """Lines 1–4 and 5–8 share the same W1–2 … W7–8 calendar blocks."""

    if line_no <= 4:
        return line_no - 1
    return line_no - 5


def weekend_shift_token_for_employee(employee: EmployeeProfile) -> str:
    """D/E lines 1–4 → E, 5–8 → D on weekends; D/N → N."""

    contract = normalize_contract_line_type(str(employee.contract_line_type or "")) or "D/E"
    if contract == "D/N":
        return "N"
    line_no = _vacant_catalog_line_number(employee)
    if line_no is not None and line_no >= 5:
        return "D"
    return "E"


def _included_in_weekend_stagger_distribution(
    employee: EmployeeProfile,
    target_hours: float,
) -> bool:
    """FT lines plus D/E lines 5–9 (weekend day-shift tier and light PT)."""

    if portage_is_fulltime_catalog_hours(float(target_hours)):
        return True
    line_no = _vacant_catalog_line_number(employee)
    contract = normalize_contract_line_type(str(employee.contract_line_type or "")) or "D/E"
    return contract == "D/E" and line_no is not None and 5 <= line_no <= 9


def _weekend_stagger_members_in_frame_order(
    frame: pd.DataFrame,
    member_ids: Sequence[str],
    *,
    employees_by_id: Mapping[str, EmployeeProfile],
    employee_target_hours: Mapping[str, float],
) -> List[str]:
    member_set = set(member_ids)
    ordered: List[str] = []
    for employee_id in frame["employee_id"].astype(str):
        if employee_id not in member_set or employee_id in ordered:
            continue
        employee = employees_by_id.get(employee_id)
        if employee is None:
            continue
        target_hours = float(employee_target_hours.get(employee_id, 0.0))
        if not _included_in_weekend_stagger_distribution(employee, target_hours):
            continue
        ordered.append(employee_id)
    return ordered


def _weekend_block_member_order(
    frame: pd.DataFrame,
    member_ids: Sequence[str],
    *,
    employees_by_id: Mapping[str, EmployeeProfile],
    employee_target_hours: Mapping[str, float],
) -> List[str]:
    """Order weekend-stagger lines within one contract pool by vacant line number."""

    from lab_scheduler.scheduling.portage_template import parse_vacant_portage_line

    ft_members = _weekend_stagger_members_in_frame_order(
        frame,
        member_ids,
        employees_by_id=employees_by_id,
        employee_target_hours=employee_target_hours,
    )
    frame_index = {
        employee_id: index
        for index, employee_id in enumerate(frame["employee_id"].astype(str))
    }

    def _line_sort_key(employee_id: str) -> Tuple[int, int]:
        profile = employees_by_id.get(employee_id)
        if profile is None:
            return 999, frame_index.get(employee_id, 999)
        parsed = parse_vacant_portage_line(profile.full_name)
        line_no = parsed[2] if parsed else 999
        return line_no, frame_index.get(employee_id, 999)

    return sorted(ft_members, key=_line_sort_key)


def enumerate_consecutive_weekend_pairs(
    period_start: date,
    period_end: date,
) -> List[Tuple[Tuple[date, date], Tuple[date, date]]]:
    pairs: List[Tuple[Tuple[date, date], Tuple[date, date]]] = []
    for saturday in _daterange(period_start, period_end):
        if saturday.weekday() != 5:
            continue
        sunday = saturday + timedelta(days=1)
        next_saturday = saturday + timedelta(days=7)
        next_sunday = next_saturday + timedelta(days=1)
        if next_sunday > period_end:
            continue
        pairs.append(((saturday, sunday), (next_saturday, next_sunday)))
    return pairs


def enumerate_staggered_weekend_blocks(
    period_start: date,
    period_end: date,
) -> List[frozenset[date]]:
    """Non-overlapping weekend blocks for peer lines: W1+W2, W3+W4, W5+W6, …"""

    saturdays = [
        day for day in _daterange(period_start, period_end) if day.weekday() == 5
    ]
    blocks: List[frozenset[date]] = []
    index = 0
    while index + 1 < len(saturdays):
        sat1 = saturdays[index]
        sat2 = saturdays[index + 1]
        blocks.append(
            frozenset(
                {
                    sat1,
                    sat1 + timedelta(days=1),
                    sat2,
                    sat2 + timedelta(days=1),
                }
            )
        )
        index += 2
    return blocks


def _ft_members_in_frame_order(
    frame: pd.DataFrame,
    member_ids: Sequence[str],
    *,
    employee_target_hours: Mapping[str, float],
) -> List[str]:
    """Preserve grid row order so line N+1 gets the next non-overlapping weekend block."""

    member_set = set(member_ids)
    ordered: List[str] = []
    for employee_id in frame["employee_id"].astype(str):
        if employee_id not in member_set:
            continue
        if employee_id in ordered:
            continue
        if not portage_is_fulltime_catalog_hours(
            float(employee_target_hours.get(employee_id, 0.0))
        ):
            continue
        ordered.append(employee_id)
    return ordered


def _pool_key(
    employee: EmployeeProfile,
    *,
    target_hours: float,
    qual_codes: Mapping[str, str],
) -> Optional[Tuple[str, str, int]]:
    from lab_scheduler.solver.cpsat_fill import _vacant_line_type_key

    return _vacant_line_type_key(employee, float(target_hours))


def _count_alt_shifts(
    frame: pd.DataFrame,
    employee_id: str,
    row_idx: int,
    dates: Sequence[date],
    alt_band: str,
) -> int:
    return sum(
        1
        for day in dates
        if _get_token(frame, row_idx, day) == alt_band
    )


def _pool_alt_spread(
    member_ids: Sequence[str],
    frame: pd.DataFrame,
    row_lookup: Mapping[str, int],
    employees_by_id: Mapping[str, EmployeeProfile],
    dates: Sequence[date],
) -> Tuple[int, int]:
    counts: List[int] = []
    for employee_id in member_ids:
        employee = employees_by_id.get(employee_id)
        if employee is None:
            continue
        alt_band = alternate_band_for_contract_line(employee.contract_line_type)
        row_idx = row_lookup.get(employee_id)
        if row_idx is None:
            continue
        counts.append(_count_alt_shifts(frame, employee_id, row_idx, dates, alt_band))
    if not counts:
        return 0, 0
    return min(counts), max(counts)


def _daily_band_count(
    frame: pd.DataFrame,
    row_lookup: Mapping[str, int],
    day: date,
    band: str,
) -> int:
    from lab_scheduler.scheduling.weekend_placement_rules import daily_band_count

    return daily_band_count(frame, row_lookup, day, band)


def _daily_band_cap(band: str) -> int:
    from lab_scheduler.scheduling.weekend_placement_rules import daily_band_cap

    return daily_band_cap(band)


def _ft_stagger_pools_by_qual_contract(
    groups: Mapping[object, Sequence[str]],
    employee_target_hours: Mapping[str, float],
    *,
    employees_by_id: Mapping[str, EmployeeProfile],
) -> Dict[Tuple[str, str], List[str]]:
    """Merge weekend-stagger vacant lines across catalog hour tiers."""

    merged: Dict[Tuple[str, str], List[str]] = defaultdict(list)
    seen: Dict[Tuple[str, str], Set[str]] = defaultdict(set)
    for pool_key, member_ids in groups.items():
        qual, contract, _hours = pool_key  # type: ignore[misc]
        pool_key_tuple = (str(qual), str(contract).upper())
        for employee_id in member_ids:
            employee = employees_by_id.get(employee_id)
            if employee is None:
                continue
            target_hours = float(employee_target_hours.get(employee_id, 0.0))
            if not _included_in_weekend_stagger_distribution(employee, target_hours):
                continue
            if employee_id in seen[pool_key_tuple]:
                continue
            merged[pool_key_tuple].append(employee_id)
            seen[pool_key_tuple].add(employee_id)
    return dict(merged)


def _build_staggered_weekend_assignments(
    frame: pd.DataFrame,
    *,
    groups: Mapping[object, Sequence[str]],
    period_start: date,
    period_end: date,
    employee_target_hours: Mapping[str, float],
    employees_by_id: Mapping[str, EmployeeProfile],
    warnings: List[str],
) -> Dict[str, frozenset[date]]:
    blocks = enumerate_staggered_weekend_blocks(period_start, period_end)
    assignments: Dict[str, frozenset[date]] = {}
    if not blocks:
        return assignments

    stagger_pools = _ft_stagger_pools_by_qual_contract(
        groups,
        employee_target_hours,
        employees_by_id=employees_by_id,
    )

    def _stagger_pool_sort_key(item: Tuple[Tuple[str, str], List[str]]) -> Tuple[str, int, str]:
        (qual, contract), _member_ids = item
        contract_rank = 0 if contract == "D/N" else 1
        return (qual, contract_rank, contract)

    for (qual, contract), member_ids in sorted(
        stagger_pools.items(), key=_stagger_pool_sort_key
    ):
        ordered = _weekend_block_member_order(
            frame,
            member_ids,
            employees_by_id=employees_by_id,
            employee_target_hours=employee_target_hours,
        )
        pool_label = f"{qual} {contract}"
        for employee_id in ordered:
            employee = employees_by_id.get(employee_id)
            if employee is None:
                continue
            line_no = _vacant_catalog_line_number(employee)
            if line_no is None:
                warnings.append(
                    f"{employee.full_name}: cannot parse vacant line number for weekend stagger."
                )
                continue
            block_index = _weekend_stagger_block_index(line_no)
            if block_index >= len(blocks):
                warnings.append(
                    f"{employee_id}: only {len(blocks)} staggered weekend block(s) "
                    f"fit in period for {pool_label}; cannot assign block "
                    f"{block_index + 1}."
                )
                continue
            assignments[employee_id] = blocks[block_index]

    return assignments


def _phase1_weekend_blocks(
    frame: pd.DataFrame,
    *,
    profiles: Sequence[EmployeeProfile],
    dates: Sequence[date],
    period_start: date,
    period_end: date,
    employee_target_hours: Mapping[str, float],
    locked_cells: Set[Tuple[str, date]],
    blocked_map: Mapping[str, Mapping[date, str]],
    qual_codes: Mapping[str, str],
    weekend_assignments: Mapping[str, frozenset[date]],
    warnings: List[str],
) -> int:
    row_lookup = schedule_frame_row_index_by_employee_id(frame)
    employees_by_id = {profile.id: profile for profile in profiles}
    if not weekend_assignments:
        warnings.append("No staggered weekend blocks fit in this period.")
        return 0

    changed = 0
    weekend_employee_ids = [
        employee_id
        for employee_id in weekend_assignments
        if row_lookup.get(employee_id) is not None
    ]

    def _weekend_placement_order(employee_id: str) -> Tuple[int, int]:
        employee = employees_by_id.get(employee_id)
        if employee is None:
            return (9, 999)
        token = weekend_shift_token_for_employee(employee)
        pass_rank = 0 if token in {"E", "N"} else 1
        line_no = _vacant_catalog_line_number(employee) or 999
        return (pass_rank, line_no)

    for employee_id in sorted(weekend_employee_ids, key=_weekend_placement_order):
        employee = employees_by_id.get(employee_id)
        row_idx = row_lookup.get(employee_id)
        target_days = weekend_assignments.get(employee_id)
        if employee is None or row_idx is None or not target_days:
            continue
        weekend_token = weekend_shift_token_for_employee(employee)

        for day in dates:
            if day.weekday() < 5:
                continue
            if not _is_editable(
                employee_id,
                day,
                locked_cells=locked_cells,
                blocked_map=blocked_map,
            ):
                continue
            if day in target_days:
                can_place = _can_place_weekend_token(
                    frame,
                    row_lookup,
                    employees_by_id,
                    qual_codes,
                    employee_id=employee_id,
                    day=day,
                    token=weekend_token,
                )
                if not can_place:
                    warnings.append(
                        f"{employee.full_name}: weekend cap blocked {day.isoformat()}."
                    )
                    continue
                if _set_token(frame, row_idx, day, weekend_token):
                    changed += 1
            else:
                if _get_token(frame, row_idx, day) in {"D", "E", "N"}:
                    if _set_token(frame, row_idx, day, OFF_DISPLAY):
                        changed += 1
    return changed


def _isolation_score(tokens: Sequence[str], alt_band: str) -> int:
    """Lower is better — counts day↔alt transitions."""

    score = 0
    prev: Optional[str] = None
    for token in tokens:
        if token not in {"D", "E", "N"}:
            prev = None
            continue
        if prev is not None and prev != token:
            if (prev == "D" and token == alt_band) or (prev == alt_band and token == "D"):
                score += 1
        prev = token
    return score


def _cluster_window_for_block(
    block_days: frozenset[date],
) -> Tuple[date, date]:
    block_start = min(block_days)
    block_end = max(block_days)
    return block_start - timedelta(days=5), block_end + timedelta(days=5)


def _sort_alt_candidate_days(
    frame: pd.DataFrame,
    row_idx: int,
    candidates: Sequence[date],
    alt_band: str,
) -> List[date]:
    """Prefer days adjacent to existing alternate shifts (cluster + alt-over-day priority)."""

    alt_days = {
        day
        for day in candidates
        if _get_token(frame, row_idx, day) == alt_band
    }

    def _score(day: date) -> Tuple[int, int]:
        neighbors = sum(
            1
            for offset in (-1, 1)
            if _get_token(frame, row_idx, day + timedelta(days=offset)) == alt_band
        )
        near_block = 0
        for existing in alt_days:
            if abs((day - existing).days) <= 2:
                near_block = 1
                break
        return (-neighbors, -near_block, day.toordinal())

    return sorted(candidates, key=_score)


def _try_same_day_peer_alt_swap(
    frame: pd.DataFrame,
    *,
    row_lookup: Mapping[str, int],
    pool_member_ids: Sequence[str],
    employees_by_id: Mapping[str, EmployeeProfile],
    qual_codes: Mapping[str, str],
    employee_id: str,
    day: date,
    alt_band: str,
    locked_cells: Set[Tuple[str, date]],
    blocked_map: Mapping[str, Mapping[date, str]],
) -> bool:
    """Pool-neutral swap: donor takes D on ``day`` while receiver gets the alt band."""

    receiver_idx = row_lookup.get(employee_id)
    if receiver_idx is None or _get_token(frame, receiver_idx, day) != "D":
        return False
    if not _can_place_daily_alt(
        frame,
        row_lookup,
        employees_by_id,
        qual_codes,
        employee_id=employee_id,
        day=day,
        band=alt_band,
    ):
        return False

    for donor_id in pool_member_ids:
        if donor_id == employee_id:
            continue
        donor_idx = row_lookup.get(donor_id)
        if donor_idx is None:
            continue
        if _get_token(frame, donor_idx, day) != alt_band:
            continue
        if not _is_editable(
            donor_id,
            day,
            locked_cells=locked_cells,
            blocked_map=blocked_map,
        ):
            continue
        if not _is_editable(
            employee_id,
            day,
            locked_cells=locked_cells,
            blocked_map=blocked_map,
        ):
            continue
        if _set_token(frame, receiver_idx, day, alt_band) and _set_token(
            frame, donor_idx, day, "D"
        ):
            return True
    return False


def _phase2_cluster_alternates(
    frame: pd.DataFrame,
    *,
    profiles: Sequence[EmployeeProfile],
    dates: Sequence[date],
    employee_target_hours: Mapping[str, float],
    employees_by_id: Mapping[str, EmployeeProfile],
    qual_codes: Mapping[str, str],
    locked_cells: Set[Tuple[str, date]],
    blocked_map: Mapping[str, Mapping[date, str]],
    weekend_assignments: Mapping[str, frozenset[date]],
    pool_members_by_employee: Mapping[str, Sequence[str]],
) -> int:
    row_lookup = schedule_frame_row_index_by_employee_id(frame)
    weekday_dates = [day for day in dates if day.weekday() < 5]
    changed = 0

    for profile in profiles:
        if not portage_is_fulltime_catalog_hours(
            float(employee_target_hours.get(profile.id, 0.0))
        ):
            continue
        row_idx = row_lookup.get(profile.id)
        if row_idx is None:
            continue
        alt_band = alternate_band_for_contract_line(profile.contract_line_type)
        target = portage_alt_shift_target_for_employee(
            profile,
            float(employee_target_hours.get(profile.id, 0.0)),
        )
        if target <= 0:
            continue

        block_days = weekend_assignments.get(profile.id)
        if not block_days:
            continue
        block_start, block_end = _cluster_window_for_block(block_days)
        preferred = [day for day in weekday_dates if block_start <= day <= block_end]
        pool_members = pool_members_by_employee.get(profile.id, [profile.id])

        current_alt = [
            day
            for day in weekday_dates
            if _get_token(frame, row_idx, day) == alt_band
            and _is_editable(
                profile.id,
                day,
                locked_cells=locked_cells,
                blocked_map=blocked_map,
            )
        ]
        deficit = target - len(current_alt)

        def _place_alt_on_day(day: date) -> bool:
            nonlocal changed, deficit
            if deficit <= 0:
                return False
            if not _is_editable(
                profile.id,
                day,
                locked_cells=locked_cells,
                blocked_map=blocked_map,
            ):
                return False
            if _get_token(frame, row_idx, day) != "D":
                return False
            if _can_place_daily_alt(
                frame,
                row_lookup,
                employees_by_id,
                qual_codes,
                employee_id=profile.id,
                day=day,
                band=alt_band,
            ):
                if _set_token(frame, row_idx, day, alt_band):
                    changed += 1
                    deficit -= 1
                    return True
            if _try_same_day_peer_alt_swap(
                frame,
                row_lookup=row_lookup,
                pool_member_ids=pool_members,
                employees_by_id=employees_by_id,
                qual_codes=qual_codes,
                employee_id=profile.id,
                day=day,
                alt_band=alt_band,
                locked_cells=locked_cells,
                blocked_map=blocked_map,
            ):
                changed += 2
                deficit -= 1
                return True
            return False

        for day in _sort_alt_candidate_days(
            frame,
            row_idx,
            [
                day
                for day in preferred
                if _get_token(frame, row_idx, day) == "D"
                and _is_editable(
                    profile.id,
                    day,
                    locked_cells=locked_cells,
                    blocked_map=blocked_map,
                )
            ],
            alt_band,
        ):
            if deficit <= 0:
                break
            _place_alt_on_day(day)

        if deficit > 0:
            for day in _sort_alt_candidate_days(
                frame,
                row_idx,
                [
                    day
                    for day in weekday_dates
                    if day not in preferred
                    and _get_token(frame, row_idx, day) == "D"
                    and _is_editable(
                        profile.id,
                        day,
                        locked_cells=locked_cells,
                        blocked_map=blocked_map,
                    )
                ],
                alt_band,
            ):
                if deficit <= 0:
                    break
                _place_alt_on_day(day)

        ordered = [_get_token(frame, row_idx, day) for day in weekday_dates]
        _isolation_score(ordered, alt_band)

    return changed


def _planned_assignments_from_frame(
    frame: pd.DataFrame,
    *,
    employees: Sequence[EmployeeProfile],
    dates: Sequence[date],
    db_templates: Mapping[str, Mapping[str, object]],
) -> List[object]:
    from lab_scheduler.scheduling.auto_generate import PlannedAssignment

    scheduled = assignments_from_schedule_frame(
        frame,
        employees=employees,
        dates=dates,
        templates=db_templates,
    )
    return [
        PlannedAssignment(
            employee_id=row.employee_id,
            shift_template_id=row.shift_template_id,
            assignment_date=row.assignment_date,
        )
        for row in scheduled
    ]


def _apply_planned_to_frame(
    frame: pd.DataFrame,
    assignments: Sequence[object],
    *,
    dates: Sequence[date],
    shift_templates: Mapping[str, ShiftTemplateInfo],
    employee_ids: Set[str],
    locked_cells: Set[Tuple[str, date]],
    blocked_map: Mapping[str, Mapping[date, str]],
) -> int:
    row_lookup = schedule_frame_row_index_by_employee_id(frame)
    assignment_map: Dict[Tuple[str, date], str] = {}
    for assignment in assignments:
        employee_id = str(getattr(assignment, "employee_id", ""))
        if employee_id not in employee_ids:
            continue
        template = shift_templates.get(getattr(assignment, "shift_template_id", ""))
        if template is None:
            continue
        band = shift_band_from_template_code(template.code)
        if band in {"D", "E", "N"}:
            assignment_map[(employee_id, assignment.assignment_date)] = band

    changed = 0
    for employee_id in employee_ids:
        row_idx = row_lookup.get(employee_id)
        if row_idx is None:
            continue
        for day in dates:
            if not _is_editable(
                employee_id,
                day,
                locked_cells=locked_cells,
                blocked_map=blocked_map,
            ):
                continue
            token = assignment_map.get((employee_id, day), "")
            if _set_token(frame, row_idx, day, token or OFF_DISPLAY):
                changed += 1
    return changed


def _enforce_daily_band_caps_on_frame(
    frame: pd.DataFrame,
    *,
    dates: Sequence[date],
    locked_cells: Set[Tuple[str, date]],
    blocked_map: Mapping[str, Mapping[date, str]],
) -> int:
    """Hard 2E/2N weekday caps — demote surplus alternate bands to day shifts."""

    row_lookup = schedule_frame_row_index_by_employee_id(frame)
    changed = 0
    weekday_dates = [day for day in dates if day.weekday() < 5]
    for band in ("E", "N"):
        cap = _daily_band_cap(band)
        for day in weekday_dates:
            while _daily_band_count(frame, row_lookup, day, band) > cap:
                demoted = False
                for employee_id, row_idx in row_lookup.items():
                    if not _is_editable(
                        employee_id,
                        day,
                        locked_cells=locked_cells,
                        blocked_map=blocked_map,
                    ):
                        continue
                    if _get_token(frame, row_idx, day) != band:
                        continue
                    if _set_token(frame, row_idx, day, "D"):
                        changed += 1
                        demoted = True
                        break
                if not demoted:
                    break
    return changed


def _build_pool_members_by_employee(
    profiles: Sequence[EmployeeProfile],
    employee_target_hours: Mapping[str, float],
) -> Dict[str, List[str]]:
    from lab_scheduler.solver.cpsat_fill import _vacant_line_type_groups

    ft_profiles = [
        profile
        for profile in profiles
        if portage_is_fulltime_catalog_hours(
            float(employee_target_hours.get(profile.id, 0.0))
        )
    ]
    groups = _vacant_line_type_groups(ft_profiles, employee_target_hours)
    mapping: Dict[str, List[str]] = {}
    for member_ids in groups.values():
        ft_members = [
            employee_id
            for employee_id in member_ids
            if portage_is_fulltime_catalog_hours(
                float(employee_target_hours.get(employee_id, 0.0))
            )
        ]
        for employee_id in ft_members:
            mapping[employee_id] = list(ft_members)
    return mapping


def _phase3_peer_fairness(
    frame: pd.DataFrame,
    *,
    profiles: Sequence[EmployeeProfile],
    dates: Sequence[date],
    period_start: date,
    period_end: date,
    weeks_in_period: int,
    rules: JurisdictionRules,
    shift_templates: Mapping[str, ShiftTemplateInfo],
    db_templates: Mapping[str, Mapping[str, object]],
    employee_target_hours: Mapping[str, float],
    availability_blocked: Optional[Mapping[str, Set[date]]],
    qual_codes: Mapping[str, str],
    locked_cells: Set[Tuple[str, date]],
    blocked_map: Mapping[str, Mapping[date, str]],
) -> int:
    from lab_scheduler.scheduling.auto_generate import (
        _EmployeeState,
        _enforce_alt_shift_peer_day_swaps,
        _rebuild_states_from_assignments,
    )

    ft_ids = {
        profile.id
        for profile in profiles
        if portage_is_fulltime_catalog_hours(
            float(employee_target_hours.get(profile.id, 0.0))
        )
    }
    if not ft_ids:
        return 0

    assignments = _planned_assignments_from_frame(
        frame,
        employees=profiles,
        dates=dates,
        db_templates=db_templates,
    )
    states = {
        profile.id: _EmployeeState(
            profile=profile,
            target_hours=float(employee_target_hours.get(profile.id, 0.0)),
        )
        for profile in profiles
    }
    _rebuild_states_from_assignments(states, assignments, dict(shift_templates))
    before = list(assignments)
    edits = _enforce_alt_shift_peer_day_swaps(
        assignments,
        states,
        employees=profiles,
        shift_templates=dict(shift_templates),
        rules=rules,
        period_start=period_start,
        period_end=period_end,
        availability_blocked=availability_blocked,
        qual_codes=qual_codes,
        employee_target_hours=employee_target_hours,
        max_rounds=128,
        alt_equity_scope="ft_peers_only",
        parity_mode="catalog_target",
    )
    if edits <= 0 and assignments == before:
        return 0
    return _apply_planned_to_frame(
        frame,
        assignments,
        dates=dates,
        shift_templates=shift_templates,
        employee_ids=ft_ids,
        locked_cells=locked_cells,
        blocked_map=blocked_map,
    )


def distribute_alternate_shifts(
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
    availability_blocked: Optional[Mapping[str, Set[date]]] = None,
) -> Tuple[pd.DataFrame, DistributeAltResult]:
    """Redistribute FT weekend shifts: E/N on lines 1–4, D on D/E lines 5–8."""

    result = DistributeAltResult()
    working = draft.copy(deep=True)
    profiles = [
        EmployeeProfile(
            id=str(employee["id"]),
            full_name=str(employee.get("full_name") or employee["id"]),
            fte=float(employee.get("fte") or 1.0),
            qualification_ids=set(emp_quals.get(str(employee["id"]), set())),
            contract_line_type=employee.get("contract_line_type"),
        )
        for employee in employees
    ]
    from lab_scheduler.solver.cpsat_fill import _vacant_line_type_groups

    row_lookup = schedule_frame_row_index_by_employee_id(working)
    employees_by_id = {profile.id: profile for profile in profiles}
    groups = _vacant_line_type_groups(profiles, employee_target_hours)
    for pool_key, member_ids in groups.items():
        ft_members = [
            employee_id
            for employee_id in member_ids
            if portage_is_fulltime_catalog_hours(
                float(employee_target_hours.get(employee_id, 0.0))
            )
        ]
        if len(ft_members) < 2:
            continue
        qual, contract, hours = pool_key
        label = f"{qual} {contract} ({int(hours)}h)"
        result.alt_spread_before[label] = _pool_alt_spread(
            ft_members, working, row_lookup, employees_by_id, dates
        )

    original = draft.copy(deep=True)
    locked = set(locked_cells)
    blocked = blocked_map

    weekend_assignments = _build_staggered_weekend_assignments(
        working,
        groups=groups,
        period_start=period_start,
        period_end=period_end,
        employee_target_hours=employee_target_hours,
        employees_by_id=employees_by_id,
        warnings=result.warnings,
    )
    pool_members_by_employee = _build_pool_members_by_employee(
        profiles,
        employee_target_hours,
    )

    changed = 0
    phase1_changed = _phase1_weekend_blocks(
        working,
        profiles=profiles,
        dates=dates,
        period_start=period_start,
        period_end=period_end,
        employee_target_hours=employee_target_hours,
        locked_cells=locked,
        blocked_map=blocked,
        qual_codes=qual_codes,
        weekend_assignments=weekend_assignments,
        warnings=result.warnings,
    )
    changed += phase1_changed
    phase2_changed = _phase2_cluster_alternates(
        working,
        profiles=profiles,
        dates=dates,
        employee_target_hours=employee_target_hours,
        employees_by_id=employees_by_id,
        qual_codes=qual_codes,
        locked_cells=locked,
        blocked_map=blocked,
        weekend_assignments=weekend_assignments,
        pool_members_by_employee=pool_members_by_employee,
    )
    changed += phase2_changed
    changed += _phase3_peer_fairness(
        working,
        profiles=profiles,
        dates=dates,
        period_start=period_start,
        period_end=period_end,
        weeks_in_period=weeks_in_period,
        rules=rules,
        shift_templates=shift_templates,
        db_templates=templates,
        employee_target_hours=employee_target_hours,
        availability_blocked=availability_blocked,
        qual_codes=qual_codes,
        locked_cells=locked,
        blocked_map=blocked,
    )
    changed += _enforce_daily_band_caps_on_frame(
        working,
        dates=dates,
        locked_cells=locked,
        blocked_map=blocked,
    )
    changed += _phase1_weekend_blocks(
        working,
        profiles=profiles,
        dates=dates,
        period_start=period_start,
        period_end=period_end,
        employee_target_hours=employee_target_hours,
        locked_cells=locked,
        blocked_map=blocked,
        qual_codes=qual_codes,
        weekend_assignments=weekend_assignments,
        warnings=result.warnings,
    )
    changed += _phase2_cluster_alternates(
        working,
        profiles=profiles,
        dates=dates,
        employee_target_hours=employee_target_hours,
        employees_by_id=employees_by_id,
        qual_codes=qual_codes,
        locked_cells=locked,
        blocked_map=blocked,
        weekend_assignments=weekend_assignments,
        pool_members_by_employee=pool_members_by_employee,
    )
    changed += _enforce_daily_band_caps_on_frame(
        working,
        dates=dates,
        locked_cells=locked,
        blocked_map=blocked,
    )

    row_lookup = schedule_frame_row_index_by_employee_id(working)
    for pool_key, member_ids in groups.items():
        ft_members = [
            employee_id
            for employee_id in member_ids
            if portage_is_fulltime_catalog_hours(
                float(employee_target_hours.get(employee_id, 0.0))
            )
        ]
        if len(ft_members) < 2:
            continue
        qual, contract, hours = pool_key
        label = f"{qual} {contract} ({int(hours)}h)"
        spread = _pool_alt_spread(
            ft_members, working, row_lookup, employees_by_id, dates
        )
        result.alt_spread_after[label] = spread
        before = result.alt_spread_before.get(label, spread)
        result.pool_summaries.append(
            f"{label}: alt {before[0]}-{before[1]} → {spread[0]}-{spread[1]}"
        )

    touched: Set[str] = set()
    for employee_id, row_idx in row_lookup.items():
        for day in dates:
            key = day.isoformat()
            if normalize_grid_shift_token(original.at[row_idx, key]) != normalize_grid_shift_token(
                working.at[row_idx, key]
            ):
                touched.add(employee_id)
                break

    result.lines_touched = len(touched)
    result.cells_changed = max(changed, sum(
        1
        for employee_id, row_idx in row_lookup.items()
        for day in dates
        if normalize_grid_shift_token(original.at[row_idx, day.isoformat()])
        != normalize_grid_shift_token(working.at[row_idx, day.isoformat()])
    ))
    return working, result
