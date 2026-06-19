"""Employee scheduling profiles for preference-driven fill."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Mapping, Optional, Sequence, Tuple

import pandas as pd

from lab_scheduler.engine.demand import infer_qual_code
from lab_scheduler.models.employee import normalize_contract_line_type
from lab_scheduler.scheduling.portage_equity_targets import (
    portage_alt_shift_target_for_employee,
    portage_contract_shift_count,
    portage_weekend_shift_target,
)
from lab_scheduler.scheduling.portage_template import parse_vacant_portage_line
from lab_scheduler.scheduling.preference_policy import SlotTier
from lab_scheduler.scheduling.profiles import EmployeeProfile
from lab_scheduler.simulation.portage_blueprint import portage_equity_role_for_employee
from lab_scheduler.solver.cpsat_fill import is_vacant_portage_line


def alternate_band_for_contract(contract_line_type: object) -> str:
    contract = normalize_contract_line_type(str(contract_line_type or "")) or "D/E"
    return "N" if contract == "D/N" else "E"


def derive_weekend_band(
    employee: EmployeeProfile,
    *,
    explicit_weekend_band: Optional[str] = None,
) -> str:
    if explicit_weekend_band in {"D", "E", "N"}:
        return explicit_weekend_band
    contract = normalize_contract_line_type(str(employee.contract_line_type or "")) or "D/E"
    if contract == "D/N":
        return "N"
    parsed = parse_vacant_portage_line(employee.full_name)
    line_no = parsed[2] if parsed else None
    if line_no is not None and 5 <= line_no <= 8:
        return "D"
    return "E"


@dataclass(frozen=True, slots=True)
class EmployeeSchedulingProfile:
    employee_id: str
    contract_line_type: str
    catalog_hours: float
    alternate_band: str
    weekend_band: str
    pool_key: Tuple[str, str]
    pool_index: int
    equity_role: str
    tier_targets: Dict[SlotTier, int] = field(default_factory=dict)
    tier_weight: Dict[SlotTier, float] = field(default_factory=dict)
    eligible_for_fill: bool = True


def compute_tier_targets(
    *,
    catalog_hours: float,
    contract_line_type: str,
    weekend_band: str,
    equity_role: Optional[str],
    employee: EmployeeProfile,
) -> Dict[SlotTier, int]:
    contract = normalize_contract_line_type(contract_line_type) or "D/E"
    payroll_shifts = portage_contract_shift_count(catalog_hours)
    weekend_target = portage_weekend_shift_target(
        catalog_hours,
        equity_role=equity_role,
        contract_line_type=contract,
    )
    alt_target = portage_alt_shift_target_for_employee(employee, catalog_hours)

    if contract == "D/N":
        weekend_alt = weekend_target
        weekday_alt = max(0, alt_target - weekend_alt)
        weekday_day = max(0, payroll_shifts - weekend_alt - weekday_alt)
        return {
            SlotTier.WEEKEND_ALT: weekend_alt,
            SlotTier.WEEKEND_DAY: 0,
            SlotTier.WEEKDAY_ALT: weekday_alt,
            SlotTier.WEEKDAY_DAY: weekday_day,
        }

    if weekend_band == "D":
        weekday_alt = alt_target
        weekday_day = max(0, payroll_shifts - weekend_target - weekday_alt)
        return {
            SlotTier.WEEKEND_ALT: 0,
            SlotTier.WEEKEND_DAY: weekend_target,
            SlotTier.WEEKDAY_ALT: weekday_alt,
            SlotTier.WEEKDAY_DAY: weekday_day,
        }

    weekend_alt = min(alt_target, weekend_target)
    weekday_alt = max(0, alt_target - weekend_alt)
    weekday_day = max(0, payroll_shifts - weekend_alt - weekday_alt)
    return {
        SlotTier.WEEKEND_ALT: weekend_alt,
        SlotTier.WEEKEND_DAY: 0,
        SlotTier.WEEKDAY_ALT: weekday_alt,
        SlotTier.WEEKDAY_DAY: weekday_day,
    }


def _default_tier_weights() -> Dict[SlotTier, float]:
    return {tier: 1.0 for tier in SlotTier}


def portage_employee_scheduling_profile(
    employee: EmployeeProfile,
    *,
    catalog_hours: float,
    pool_key: Tuple[str, str],
    pool_index: int,
    explicit_weekend_band: Optional[str] = None,
) -> EmployeeSchedulingProfile:
    contract = normalize_contract_line_type(str(employee.contract_line_type or "")) or "D/E"
    equity_role = portage_equity_role_for_employee(employee) or "core_ft"
    weekend_band = derive_weekend_band(
        employee,
        explicit_weekend_band=explicit_weekend_band,
    )
    tier_targets = compute_tier_targets(
        catalog_hours=catalog_hours,
        contract_line_type=contract,
        weekend_band=weekend_band,
        equity_role=equity_role,
        employee=employee,
    )
    return EmployeeSchedulingProfile(
        employee_id=employee.id,
        contract_line_type=contract,
        catalog_hours=float(catalog_hours),
        alternate_band=alternate_band_for_contract(contract),
        weekend_band=weekend_band,
        pool_key=pool_key,
        pool_index=pool_index,
        equity_role=str(equity_role),
        tier_targets=tier_targets,
        tier_weight=_default_tier_weights(),
        eligible_for_fill=is_vacant_portage_line(employee.full_name),
    )


def build_portage_scheduling_profiles(
    frame: pd.DataFrame,
    profiles: Sequence[EmployeeProfile],
    *,
    employee_target_hours: Mapping[str, float],
    qual_codes: Mapping[str, str],
    weekend_band_overrides: Optional[Mapping[str, str]] = None,
) -> Dict[str, EmployeeSchedulingProfile]:
    overrides = weekend_band_overrides or {}
    pool_members: Dict[Tuple[str, str], list[str]] = {}
    for profile in profiles:
        if not is_vacant_portage_line(profile.full_name):
            continue
        qual = infer_qual_code(profile, qual_codes=qual_codes)
        contract = normalize_contract_line_type(str(profile.contract_line_type or "")) or "D/E"
        pool_key = (qual, contract)
        pool_members.setdefault(pool_key, []).append(profile.id)

    frame_order = [
        str(employee_id)
        for employee_id in frame["employee_id"].astype(str)
        if str(employee_id)
    ]
    pool_index_by_employee: Dict[str, int] = {}
    pool_counters: Dict[Tuple[str, str], int] = {}
    for employee_id in frame_order:
        profile = next((item for item in profiles if item.id == employee_id), None)
        if profile is None or not is_vacant_portage_line(profile.full_name):
            continue
        qual = infer_qual_code(profile, qual_codes=qual_codes)
        contract = normalize_contract_line_type(str(profile.contract_line_type or "")) or "D/E"
        pool_key = (qual, contract)
        if employee_id not in pool_members.get(pool_key, []):
            continue
        index = pool_counters.get(pool_key, 0)
        pool_index_by_employee[employee_id] = index
        pool_counters[pool_key] = index + 1

    result: Dict[str, EmployeeSchedulingProfile] = {}
    profiles_by_id = {profile.id: profile for profile in profiles}
    for profile in profiles:
        catalog_hours = float(employee_target_hours.get(profile.id, 0.0))
        qual = infer_qual_code(profile, qual_codes=qual_codes)
        contract = normalize_contract_line_type(str(profile.contract_line_type or "")) or "D/E"
        pool_key = (qual, contract)
        if not is_vacant_portage_line(profile.full_name):
            result[profile.id] = EmployeeSchedulingProfile(
                employee_id=profile.id,
                contract_line_type=contract,
                catalog_hours=catalog_hours,
                alternate_band=alternate_band_for_contract(contract),
                weekend_band=derive_weekend_band(profile),
                pool_key=pool_key,
                pool_index=0,
                equity_role="",
                tier_targets={},
                tier_weight=_default_tier_weights(),
                eligible_for_fill=False,
            )
            continue
        result[profile.id] = portage_employee_scheduling_profile(
            profile,
            catalog_hours=catalog_hours,
            pool_key=pool_key,
            pool_index=pool_index_by_employee.get(profile.id, 0),
            explicit_weekend_band=overrides.get(profile.id),
        )
    return result
