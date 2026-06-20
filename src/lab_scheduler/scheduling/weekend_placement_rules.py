"""Shared weekend and daily alternate placement cap checks for grid fills."""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from typing import Dict, Mapping

import pandas as pd

from lab_scheduler.engine.demand import WEEKEND_CLINICAL_MAX_PER_QUAL, infer_qual_code
from lab_scheduler.policy.frame_bridge import normalize_grid_shift_token
from lab_scheduler.scheduling.load_balancing import weekend_morning_fill_blocked
from lab_scheduler.scheduling.profiles import EmployeeProfile
from lab_scheduler.solver.cpsat_fill import DAILY_EVENING_CAP, DAILY_NIGHT_CAP

OFF_DISPLAY = "—"


def is_empty_grid_token(value: object) -> bool:
    return normalize_grid_shift_token(value) == ""


def get_grid_token(frame: pd.DataFrame, row_idx: int, day: date) -> str:
    key = day.isoformat()
    if key not in frame.columns:
        return ""
    return normalize_grid_shift_token(frame.at[row_idx, key])


def set_grid_token(frame: pd.DataFrame, row_idx: int, day: date, token: str) -> bool:
    key = day.isoformat()
    if key not in frame.columns:
        return False
    old = normalize_grid_shift_token(frame.at[row_idx, key])
    new = normalize_grid_shift_token(token)
    if old == new:
        return False
    frame.at[row_idx, key] = OFF_DISPLAY if not new else new
    return True


def mirror_weekend_partner(day: date) -> date | None:
    if day.weekday() == 5:
        return day + timedelta(days=1)
    if day.weekday() == 6:
        return day - timedelta(days=1)
    return None


def weekend_work_token(token: str) -> bool:
    return token in {"D", "E", "N"}


def weekend_sat_sun_tokens_mirrored(sat_token: str, sun_token: str) -> bool:
    """True when Sat/Sun are both off or both carry the same D/E/N band."""
    sat_work = weekend_work_token(sat_token)
    sun_work = weekend_work_token(sun_token)
    if not sat_work and not sun_work:
        return True
    return sat_work and sun_work and sat_token == sun_token


def is_editable_cell(
    employee_id: str,
    day: date,
    *,
    locked_cells: set[tuple[str, date]],
    blocked_map: Mapping[str, Mapping[date, str]],
) -> bool:
    if (employee_id, day) in locked_cells:
        return False
    if day in blocked_map.get(employee_id, {}):
        return False
    return True


def weekend_band_qual_count(
    frame: pd.DataFrame,
    row_lookup: Mapping[str, int],
    employees_by_id: Mapping[str, EmployeeProfile],
    qual_codes: Mapping[str, str],
    day: date,
    band: str,
) -> Dict[str, int]:
    counts: Dict[str, int] = defaultdict(int)
    if day.weekday() < 5 or band not in {"D", "E", "N"}:
        return counts
    for employee_id, row_idx in row_lookup.items():
        if get_grid_token(frame, row_idx, day) != band:
            continue
        employee = employees_by_id.get(employee_id)
        if employee is None:
            continue
        qual = infer_qual_code(employee, qual_codes=qual_codes)
        counts[qual] += 1
    return counts


def daily_band_qual_count(
    frame: pd.DataFrame,
    row_lookup: Mapping[str, int],
    employees_by_id: Mapping[str, EmployeeProfile],
    qual_codes: Mapping[str, str],
    day: date,
    band: str,
) -> Dict[str, int]:
    """Count E/N assignments per qualification on a weekday."""
    counts: Dict[str, int] = defaultdict(int)
    if day.weekday() >= 5 or band not in {"E", "N"}:
        return counts
    for employee_id, row_idx in row_lookup.items():
        if get_grid_token(frame, row_idx, day) != band:
            continue
        employee = employees_by_id.get(employee_id)
        if employee is None:
            continue
        qual = infer_qual_code(employee, qual_codes=qual_codes)
        counts[qual] += 1
    return counts


def operational_alt_band_cap_per_qual(band: str) -> int:
    """Clinical floor: one MLT + one MLA per band per day (footer target 2 total)."""
    if band in {"E", "N"}:
        return 1
    return 0


def weekend_alt_band_cap_per_qual(band: str) -> int:
    """Clinical floor on Sat/Sun: one MLT + one MLA evening/night (footer target 2 total)."""
    return operational_alt_band_cap_per_qual(band)


def weekend_day_total_count(
    frame: pd.DataFrame,
    row_lookup: Mapping[str, int],
    day: date,
) -> int:
    """Total Day assignments on a Sat/Sun (footer target is 2 lab-wide)."""
    if day.weekday() < 5:
        return 0
    return sum(
        1
        for row_idx in row_lookup.values()
        if get_grid_token(frame, row_idx, day) == "D"
    )


def can_place_weekend_token(
    frame: pd.DataFrame,
    row_lookup: Mapping[str, int],
    employees_by_id: Mapping[str, EmployeeProfile],
    qual_codes: Mapping[str, str],
    *,
    employee_id: str,
    day: date,
    token: str,
) -> bool:
    if token not in {"D", "E", "N"}:
        return True
    employee = employees_by_id.get(employee_id)
    if employee is None:
        return False
    qual = infer_qual_code(employee, qual_codes=qual_codes)
    row_idx = row_lookup.get(employee_id)
    if row_idx is None:
        return False
    existing = get_grid_token(frame, row_idx, day)
    if existing in {"D", "E", "N"}:
        return True
    if token == "D":
        band_counts = weekend_band_qual_count(
            frame, row_lookup, employees_by_id, qual_codes, day, "D"
        )
        return not weekend_morning_fill_blocked(band_counts, qual)
    cap = weekend_alt_band_cap_per_qual(token)
    band_counts = weekend_band_qual_count(
        frame, row_lookup, employees_by_id, qual_codes, day, token
    )
    return band_counts.get(qual, 0) < cap


def daily_band_count(
    frame: pd.DataFrame,
    row_lookup: Mapping[str, int],
    day: date,
    band: str,
) -> int:
    if band not in {"E", "N"}:
        return 0
    return sum(
        1
        for row_idx in row_lookup.values()
        if get_grid_token(frame, row_idx, day) == band
    )


def daily_band_cap(band: str) -> int:
    if band == "E":
        return DAILY_EVENING_CAP
    if band == "N":
        return DAILY_NIGHT_CAP
    return 0


def can_place_daily_alt(
    frame: pd.DataFrame,
    row_lookup: Mapping[str, int],
    employees_by_id: Mapping[str, EmployeeProfile],
    qual_codes: Mapping[str, str],
    *,
    employee_id: str,
    day: date,
    band: str,
) -> bool:
    if band not in {"E", "N"} or day.weekday() >= 5:
        return False
    row_idx = row_lookup.get(employee_id)
    if row_idx is None:
        return False
    existing = get_grid_token(frame, row_idx, day)
    if existing == band:
        return True
    employee = employees_by_id.get(employee_id)
    if employee is None:
        return False
    qual = infer_qual_code(employee, qual_codes=qual_codes)
    cap = operational_alt_band_cap_per_qual(band)
    if cap <= 0:
        return False
    counts = daily_band_qual_count(
        frame,
        row_lookup,
        employees_by_id,
        qual_codes,
        day,
        band,
    )
    return counts.get(qual, 0) < cap
