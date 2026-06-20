"""Pattern invariants for Portage alternate-shift fills on a clean grid."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Dict, List, Mapping, Sequence, Set

import pandas as pd

from lab_scheduler.engine.demand import infer_qual_code
from lab_scheduler.scheduling.portage_equity_targets import (
    PORTAGE_DN_FT_NIGHT_SHIFT_TARGET,
    portage_alt_shift_target_for_employee,
    portage_contract_shift_count,
    portage_is_fulltime_catalog_hours,
)
from lab_scheduler.scheduling.rotation_spec import (
    FT_DE_EVENING_BLOCK_DAYS,
    FT_DE_EVENING_TARGET,
)
from lab_scheduler.scheduling.schedule_tallies import (
    calculate_daily_shift_tallies,
    shift_target_for_date,
)
from lab_scheduler.scheduling.weekend_placement_rules import (
    daily_band_qual_count,
    get_grid_token,
    weekend_sat_sun_tokens_mirrored,
)
from lab_scheduler.solver.cpsat_fill import is_vacant_portage_line


@dataclass(slots=True)
class InvariantViolation:
    invariant_id: str
    message: str


@dataclass(slots=True)
class InvariantReport:
    passed: bool
    violations: List[InvariantViolation] = field(default_factory=list)


def _max_consecutive_e_run(row_idx: int, frame: pd.DataFrame, dates: Sequence[date]) -> int:
    best = 0
    current = 0
    for day in dates:
        if get_grid_token(frame, row_idx, day) == "E":
            current += 1
            best = max(best, current)
        else:
            current = 0
    return best


def _weekend_evening_is_pt_only_orphan(
    frame: pd.DataFrame,
    day: date,
    row_lookup: Mapping[str, int],
    employees_by_id: Mapping[str, object],
    qual_codes: Mapping[str, str],
    *,
    employee_target_hours: Mapping[str, float],
) -> bool:
    """True when the only weekend E is on a PT D/E line (footer pair impossible without FT +1)."""
    from lab_scheduler.scheduling.portage_equity_targets import (
        portage_is_fulltime_catalog_hours,
    )

    evening_ids: list[str] = []
    for employee_id, row_idx in row_lookup.items():
        if get_grid_token(frame, row_idx, day) != "E":
            continue
        profile = employees_by_id.get(employee_id)
        if profile is None:
            continue
        contract = (getattr(profile, "contract_line_type", None) or "").upper()
        if contract != "D/E":
            continue
        evening_ids.append(employee_id)
    if len(evening_ids) != 1:
        return False
    only_id = evening_ids[0]
    hours = float(employee_target_hours.get(only_id, 0.0))
    return not portage_is_fulltime_catalog_hours(hours)


def check_rotation_invariants(
    frame: pd.DataFrame,
    *,
    dates: Sequence[date],
    row_lookup: Mapping[str, int],
    employees_by_id: Mapping[str, object],
    qual_codes: Mapping[str, str],
    employee_target_hours: Mapping[str, float],
) -> InvariantReport:
    violations: List[InvariantViolation] = []
    date_keys = [day.isoformat() for day in dates]
    tallies = calculate_daily_shift_tallies(frame, dates=date_keys)

    for day in dates:
        key = day.isoformat()
        e_count = tallies.evenings.get(key, 0)
        e_target = shift_target_for_date(day, "E")
        if e_count != e_target:
            if (
                day.weekday() >= 5
                and e_count == 1
                and e_target == 2
                and _weekend_evening_is_pt_only_orphan(
                    frame,
                    day,
                    row_lookup,
                    employees_by_id,
                    qual_codes,
                    employee_target_hours=employee_target_hours,
                )
            ):
                pass
            else:
                violations.append(
                    InvariantViolation(
                        "footer_evening_2_2",
                        f"{key} evening {e_count}/{e_target}",
                    )
                )
        n_count = tallies.nights.get(key, 0)
        n_target = shift_target_for_date(day, "N")
        if n_count != n_target:
            violations.append(
                InvariantViolation(
                    "footer_night_2_2",
                    f"{key} night {n_count}/{n_target}",
                )
            )
        if day.weekday() >= 5:
            d_count = tallies.days.get(key, 0)
            d_target = shift_target_for_date(day, "D")
            if d_count != d_target:
                violations.append(
                    InvariantViolation(
                        "footer_day_weekend_2",
                        f"{key} day {d_count}/{d_target}",
                    )
                )

    for day in dates:
        if day.weekday() >= 5:
            continue
        counts = daily_band_qual_count(
            frame, row_lookup, employees_by_id, qual_codes, day, "E"
        )
        if counts.get("MLT", 0) != 1 or counts.get("MLA", 0) != 1:
            violations.append(
                InvariantViolation(
                    "de_stagger_coverage",
                    f"{day.isoformat()} MLT={counts.get('MLT', 0)} MLA={counts.get('MLA', 0)}",
                )
            )

    for employee_id, row_idx in row_lookup.items():
        profile = employees_by_id.get(employee_id)
        if profile is None:
            continue
        name = getattr(profile, "full_name", "")
        contract = (getattr(profile, "contract_line_type", None) or "").upper()
        if not is_vacant_portage_line(name):
            continue
        e_count = sum(
            1 for day in dates if get_grid_token(frame, row_idx, day) == "E"
        )
        if contract == "D/E":
            catalog_hours = float(employee_target_hours.get(employee_id, 0.0))
            target = portage_alt_shift_target_for_employee(profile, catalog_hours)
            if portage_is_fulltime_catalog_hours(catalog_hours):
                if e_count != target:
                    violations.append(
                        InvariantViolation(
                            "de_ft_evening_count",
                            f"{name}: {e_count} E != target {target}",
                        )
                    )
                max_run = _max_consecutive_e_run(row_idx, frame, dates)
                if max_run < FT_DE_EVENING_BLOCK_DAYS:
                    violations.append(
                        InvariantViolation(
                            "de_evening_shape",
                            f"{name}: max E run {max_run} < {FT_DE_EVENING_BLOCK_DAYS}",
                        )
                    )
        elif contract == "D/N" and portage_is_fulltime_catalog_hours(
            float(employee_target_hours.get(employee_id, 0.0))
        ):
            catalog_hours = float(employee_target_hours.get(employee_id, 0.0))
            n_count = sum(
                1 for day in dates if get_grid_token(frame, row_idx, day) == "N"
            )
            if n_count != PORTAGE_DN_FT_NIGHT_SHIFT_TARGET:
                violations.append(
                    InvariantViolation(
                        "dn_ft_night_count",
                        f"{name}: {n_count} N != {PORTAGE_DN_FT_NIGHT_SHIFT_TARGET}",
                    )
                )
            weekday_d = sum(
                1
                for day in dates
                if day.weekday() < 5 and get_grid_token(frame, row_idx, day) == "D"
            )
            expected_weekday_d = (
                portage_contract_shift_count(catalog_hours)
                - PORTAGE_DN_FT_NIGHT_SHIFT_TARGET
            )
            if weekday_d != expected_weekday_d:
                violations.append(
                    InvariantViolation(
                        "dn_ft_weekday_day_count",
                        f"{name}: {weekday_d} weekday D != {expected_weekday_d}",
                    )
                )

    for employee_id, row_idx in row_lookup.items():
        profile = employees_by_id.get(employee_id)
        if profile is None:
            continue
        name = getattr(profile, "full_name", employee_id)
        for day in dates:
            if day.weekday() != 5:
                continue
            sunday = day + timedelta(days=1)
            if sunday > max(dates):
                continue
            sat_token = get_grid_token(frame, row_idx, day)
            sun_token = get_grid_token(frame, row_idx, sunday)
            if weekend_sat_sun_tokens_mirrored(sat_token, sun_token):
                continue
            violations.append(
                InvariantViolation(
                    "weekend_sat_sun_mirror",
                    (
                        f"{name}: split weekend {day.isoformat()}/"
                        f"{sunday.isoformat()} {sat_token or '-'}|{sun_token or '-'}"
                    ),
                )
            )

    return InvariantReport(passed=not violations, violations=violations)
