"""Portage vacant-line fairness targets: weekend share and alternate-shift density."""

from __future__ import annotations

import math
from datetime import date
from typing import Dict, Mapping, Sequence, Tuple

from lab_scheduler.scheduling.contract_payroll import HOURS_PER_SHIFT
from lab_scheduler.scheduling.profiles import EmployeeProfile

# Full-time (320h / 8 weeks): eight weekend shift days (Sat+Sun count).
PORTAGE_FULLTIME_WEEKEND_SHIFTS = 8
PORTAGE_FULLTIME_PERIOD_HOURS = 320.0

# Alternate-band density by vacant-line equity role (see portage_blueprint.PortageEquityRole).
PORTAGE_ALT_SHIFT_DENSITY = 0.20
PORTAGE_DN_FT_NIGHT_SHIFT_TARGET = 14
PORTAGE_DN_FT_ALT_DENSITY = 0.35
PORTAGE_DN_FT_WEEKEND_PAIRS = 2
# 1.0 FTE full-time D/N: 320h period ÷ 8h/shift (FTE is contract authority; do not drift).
PORTAGE_DN_FT_PERIOD_WORK_SHIFTS = int(PORTAGE_FULLTIME_PERIOD_HOURS // HOURS_PER_SHIFT)
PORTAGE_EQUITY_ROLE_ALT_DENSITY: Dict[str, float] = {
    "core_ft": 0.20,
    "gap_fill_pt": 0.35,
    "light_pt": 0.25,
}
PORTAGE_EQUITY_ROLE_DN_FT_ALT_DENSITY: Dict[str, float] = {
    "core_ft": PORTAGE_DN_FT_ALT_DENSITY,
    "gap_fill_pt": 0.35,
    "light_pt": 0.25,
}

# Part-time fallback when role is unknown (30–40% gap-fill band).
PORTAGE_PARTTIME_ALT_SHIFT_DENSITY = 0.35
PORTAGE_PARTTIME_ALT_SHIFT_DENSITY_MIN = 0.30
PORTAGE_PARTTIME_ALT_SHIFT_DENSITY_MAX = 0.40

# Light part-time lines carry a reduced weekend share vs proportional catalog.
PORTAGE_LIGHT_PT_WEEKEND_FACTOR = 0.75
# Catalog hours at or above this threshold are full-time vacant master lines.
PORTAGE_FULLTIME_CATALOG_HOUR_FLOOR = 312.0

# Persist gate: part-time lines must match pool-scaled stamped weekend target exactly.
CATALOG_PERSIST_WEEKEND_TOLERANCE_PT = 0
# Full-time lines: block surplus beyond one Sat/Sun mirror pair; allow larger deficits.
CATALOG_PERSIST_WEEKEND_SURPLUS_FT = 2
CATALOG_PERSIST_WEEKEND_DEFICIT_FT = 4


def portage_is_fulltime_catalog_hours(target_hours: float) -> bool:
    return float(target_hours) >= PORTAGE_FULLTIME_CATALOG_HOUR_FLOOR


def portage_contract_shift_count(target_hours: float) -> int:
    if target_hours <= 0:
        return 0
    return int(float(target_hours)) // int(HOURS_PER_SHIFT)


def portage_is_dn_fulltime_employee(employee: EmployeeProfile) -> bool:
    if (employee.contract_line_type or "").upper() != "D/N":
        return False
    from lab_scheduler.simulation.portage_blueprint import portage_equity_role_for_employee

    return portage_equity_role_for_employee(employee) == "core_ft"


def portage_alt_shift_density(
    target_hours: float,
    *,
    equity_role: str | None = None,
    contract_line_type: str | None = None,
) -> float:
    if contract_line_type and contract_line_type.upper() == "D/N":
        if equity_role and equity_role in PORTAGE_EQUITY_ROLE_DN_FT_ALT_DENSITY:
            return PORTAGE_EQUITY_ROLE_DN_FT_ALT_DENSITY[equity_role]
    if equity_role and equity_role in PORTAGE_EQUITY_ROLE_ALT_DENSITY:
        return PORTAGE_EQUITY_ROLE_ALT_DENSITY[equity_role]
    if portage_is_parttime_catalog_hours(target_hours):
        return PORTAGE_PARTTIME_ALT_SHIFT_DENSITY
    return PORTAGE_ALT_SHIFT_DENSITY


def portage_de_evenings_per_catalog_hour() -> float:
    """Uniform D/E evening share: full-time 8 evenings per 320 catalog hours."""

    return PORTAGE_ALT_SHIFT_DENSITY / float(HOURS_PER_SHIFT)


def portage_hours_weighted_de_alt_target(catalog_hours: float) -> int:
    """
    D/E evening target from catalog hours at the pool average rate.

    Full-time lines round down; part-time lines round up so lighter FTE carries
    proportionally fewer (not density-inflated) evenings.
    """

    hours = float(catalog_hours)
    if hours <= 0:
        return 0
    exact = hours * portage_de_evenings_per_catalog_hour()
    if portage_is_fulltime_catalog_hours(hours):
        return max(0, math.floor(exact))
    if portage_is_parttime_catalog_hours(hours):
        return max(0, math.ceil(exact))
    return max(0, round(exact))


def portage_pool_hours_weighted_alt_targets(
    member_target_hours: Sequence[float],
) -> Tuple[int, ...]:
    """Per-line D/E evening targets from one pool evenings-per-hour rate."""

    if not member_target_hours:
        return tuple()
    return tuple(
        portage_hours_weighted_de_alt_target(hours) for hours in member_target_hours
    )


def portage_alt_shift_target(
    target_hours: float,
    *,
    equity_role: str | None = None,
    contract_line_type: str | None = None,
) -> int:
    """Alternate-band count: D/N nights for core FT; D/E hours-weighted evenings."""

    if (
        contract_line_type
        and contract_line_type.upper() == "D/N"
        and equity_role == "core_ft"
    ):
        return PORTAGE_DN_FT_NIGHT_SHIFT_TARGET

    if contract_line_type and contract_line_type.upper() == "D/E":
        return portage_hours_weighted_de_alt_target(target_hours)

    total_shifts = portage_contract_shift_count(target_hours)
    if total_shifts <= 0:
        return 0
    density = portage_alt_shift_density(
        target_hours,
        equity_role=equity_role,
        contract_line_type=contract_line_type,
    )
    return round(total_shifts * density)


def portage_alt_shift_target_for_employee(
    employee: EmployeeProfile,
    catalog_hours: float,
) -> int:
    from lab_scheduler.simulation.portage_blueprint import portage_equity_role_for_employee

    role = portage_equity_role_for_employee(employee)
    contract = employee.contract_line_type or ""
    return portage_alt_shift_target(
        catalog_hours,
        equity_role=role,
        contract_line_type=contract,
    )


def portage_alt_shift_density_for_employee(
    employee: EmployeeProfile,
    catalog_hours: float,
) -> float:
    from lab_scheduler.simulation.portage_blueprint import portage_equity_role_for_employee

    role = portage_equity_role_for_employee(employee)
    return portage_alt_shift_density(
        catalog_hours,
        equity_role=role,
        contract_line_type=employee.contract_line_type or "",
    )

def portage_is_parttime_catalog_hours(target_hours: float) -> bool:
    hours = float(target_hours)
    return 0.0 < hours < PORTAGE_FULLTIME_CATALOG_HOUR_FLOOR


def portage_weekend_shift_target(
    target_hours: float,
    *,
    equity_role: str | None = None,
    contract_line_type: str | None = None,
) -> int:
    """
    Weekend shift days (each Sat or Sun worked counts once) proportional to catalog hours.

    Full-time 320h → 8 weekend shifts. Values are even so Sat/Sun mirror pairs stay valid.
    """

    if target_hours <= 0:
        return 0
    if equity_role == "core_ft" and contract_line_type and contract_line_type.upper() == "D/N":
        return 2 * PORTAGE_DN_FT_WEEKEND_PAIRS
    active_weekend_pairs = round(
        (float(target_hours) / PORTAGE_FULLTIME_PERIOD_HOURS)
        * (PORTAGE_FULLTIME_WEEKEND_SHIFTS / 2)
    )
    weekend_shifts = max(0, 2 * int(active_weekend_pairs))
    if equity_role == "light_pt" and weekend_shifts > 0:
        pairs = weekend_shifts // 2
        reduced_pairs = max(1, int(pairs * PORTAGE_LIGHT_PT_WEEKEND_FACTOR))
        weekend_shifts = 2 * reduced_pairs
    return weekend_shifts

def portage_active_weekend_target(target_hours: float) -> int:
    """Active weekend pairs (Sat+Sun mirror blocks) implied by ``portage_weekend_shift_target``."""

    weekend_shifts = portage_weekend_shift_target(target_hours)
    return weekend_shifts // 2


def portage_pool_weekend_shift_targets(
    member_target_hours: Sequence[float],
    *,
    qual_code: str,
    weekend_day_count: int,
) -> Tuple[int, ...]:
    """
    Per-line weekend shift targets that honor pool ops caps (``WEEKEND_CLINICAL_MAX_PER_QUAL``).

    When catalog ideals exceed pool capacity, targets scale down proportionally (even counts).
    """

    if not member_target_hours or weekend_day_count <= 0:
        return tuple()

    ideals = tuple(portage_weekend_shift_target(hours) for hours in member_target_hours)
    return scale_weekend_ideals_to_pool_capacity(
        ideals,
        qual_code=qual_code,
        weekend_day_count=weekend_day_count,
    )


def scale_weekend_ideals_to_pool_capacity(
    ideals: Sequence[int],
    *,
    qual_code: str,
    weekend_day_count: int,
) -> Tuple[int, ...]:
    """Scale per-line stamped weekend ideals when qual weekend caps bind the pool."""

    from lab_scheduler.engine.demand import WEEKEND_CLINICAL_MAX_PER_QUAL

    if not ideals or weekend_day_count <= 0:
        return tuple()

    max_per_day = int(WEEKEND_CLINICAL_MAX_PER_QUAL.get(qual_code.upper(), 1))
    pool_capacity = weekend_day_count * max_per_day
    pool_demand = sum(max(0, int(ideal)) for ideal in ideals)
    if pool_demand <= 0:
        return tuple(0 for _ in ideals)
    if pool_demand <= pool_capacity:
        return tuple(max(0, int(ideal)) for ideal in ideals)

    scaled: list[int] = []
    remainders: list[tuple[int, float]] = []
    assigned = 0
    for index, ideal in enumerate(ideals):
        exact = max(0, int(ideal)) * pool_capacity / pool_demand
        base = int(exact // 2) * 2
        scaled.append(base)
        assigned += base
        remainders.append((index, exact - base))

    spare = pool_capacity - assigned
    remainders.sort(key=lambda item: item[1], reverse=True)
    for index, _frac in remainders:
        if spare < 2:
            break
        scaled[index] += 2
        spare -= 2
    return tuple(scaled)


def build_vacant_line_weekend_target_map(
    employees: Sequence[EmployeeProfile],
    catalog_targets: Mapping[str, float],
    qual_codes: Mapping[str, str],
    *,
    period_start: date,
    period_end: date,
) -> Dict[str, int]:
    """
    Pool-scaled weekend shift targets from stamped catalog Sat/Sun days.

    Falls back to proportional hours when a line has no stamped weekend days.
    """

    from lab_scheduler.engine.demand import infer_qual_code
    from lab_scheduler.scheduling.portage_template import (
        parse_vacant_portage_line,
        vacant_master_catalog_period_weekend_shifts,
    )

    weekend_day_count = sum(
        1
        for offset in range((period_end - period_start).days + 1)
        if date.fromordinal(period_start.toordinal() + offset).weekday() >= 5
    )
    if weekend_day_count <= 0:
        return {}

    groups: Dict[tuple[str, str], list[tuple[str, int]]] = {}
    for employee in employees:
        if parse_vacant_portage_line(employee.full_name) is None:
            continue
        qual = (qual_codes.get(employee.id) or infer_qual_code(employee, qual_codes=qual_codes)).upper()
        contract = (employee.contract_line_type or "D/E").upper()
        catalog_hours = float(catalog_targets.get(employee.id, 0.0))
        from lab_scheduler.simulation.portage_blueprint import portage_equity_role_for_employee

        equity_role = portage_equity_role_for_employee(employee)
        stamped = vacant_master_catalog_period_weekend_shifts(
            employee,
            period_start,
            period_end,
        )
        ideal = stamped if stamped > 0 else portage_weekend_shift_target(
            catalog_hours,
            equity_role=equity_role,
        )
        groups.setdefault((qual, contract), []).append((employee.id, ideal))

    targets: Dict[str, int] = {}
    for (qual, _contract), members in groups.items():
        employee_ids = [employee_id for employee_id, _ideal in members]
        ideals = [ideal for _employee_id, ideal in members]
        scaled = scale_weekend_ideals_to_pool_capacity(
            ideals,
            qual_code=qual,
            weekend_day_count=weekend_day_count,
        )
        for employee_id, target in zip(employee_ids, scaled, strict=True):
            targets[employee_id] = target
    return targets


def build_vacant_line_alt_target_map(
    employees: Sequence[EmployeeProfile],
    catalog_targets: Mapping[str, float],
    qual_codes: Mapping[str, str],
) -> Dict[str, int]:
    """Pool-grouped D/E evening targets using one catalog-hours rate per qual pool."""

    from lab_scheduler.engine.demand import infer_qual_code
    from lab_scheduler.scheduling.portage_template import parse_vacant_portage_line

    groups: Dict[tuple[str, str], list[tuple[str, float]]] = {}
    for employee in employees:
        if parse_vacant_portage_line(employee.full_name) is None:
            continue
        contract = (employee.contract_line_type or "D/E").upper()
        if contract != "D/E":
            continue
        qual = (qual_codes.get(employee.id) or infer_qual_code(employee, qual_codes=qual_codes)).upper()
        catalog_hours = float(catalog_targets.get(employee.id, 0.0))
        groups.setdefault((qual, contract), []).append((employee.id, catalog_hours))

    targets: Dict[str, int] = {}
    for _pool_key, members in groups.items():
        employee_ids = [employee_id for employee_id, _hours in members]
        hours = [hours for _employee_id, hours in members]
        scaled = portage_pool_hours_weighted_alt_targets(hours)
        for employee_id, target in zip(employee_ids, scaled, strict=True):
            targets[employee_id] = target
    return targets
