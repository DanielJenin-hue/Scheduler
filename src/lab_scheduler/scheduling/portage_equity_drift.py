"""Soft Portage equity drift indicators (alternate % and weekends), separate from persist gate."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Literal, Mapping, Optional, Sequence

from lab_scheduler.scheduling.portage_equity_targets import (
    build_vacant_line_weekend_target_map,
    portage_active_weekend_target,
    portage_alt_shift_target_for_employee,
    portage_contract_shift_count,
    portage_is_fulltime_catalog_hours,
    portage_weekend_shift_target,
    PORTAGE_DN_FT_WEEKEND_PAIRS,
)
from lab_scheduler.scheduling.portage_template import parse_vacant_portage_line
from lab_scheduler.scheduling.profiles import EmployeeProfile
from lab_scheduler.simulation.portage_blueprint import portage_equity_role_for_employee

DriftStatus = Literal["ok", "low", "high", "na"]

# Soft UI tolerance — persist gate uses wider bands in persist_validation.py.
PORTAGE_EQUITY_ALT_TOLERANCE_FT = 1
PORTAGE_EQUITY_ALT_TOLERANCE_PT = 1
PORTAGE_EQUITY_WEEKEND_TOLERANCE = 1


@dataclass(frozen=True, slots=True)
class PortageEquityDrift:
    """Per-line alternate-band and weekend drift vs role-scaled catalog targets."""

    equity_role: str | None
    role_label: str
    alt_actual: int
    alt_target: int
    alt_density_pct: float
    alt_target_density_pct: float
    alt_status: DriftStatus
    weekend_actual: int
    weekend_target: int
    active_weekend_target: int
    weekend_status: DriftStatus

    @property
    def has_drift(self) -> bool:
        return self.alt_status not in ("ok", "na") or self.weekend_status not in ("ok", "na")


def _role_label(role: str | None, catalog_hours: float) -> str:
    if role == "gap_fill_pt":
        return "gap-fill PT"
    if role == "light_pt":
        return "light PT"
    if role == "core_ft" or portage_is_fulltime_catalog_hours(catalog_hours):
        return "core FT"
    return "PT"


def _alt_drift_status(
    *,
    alternate_shifts: int,
    alt_target: int,
    catalog_hours: float,
) -> DriftStatus:
    if alt_target <= 0:
        return "na"
    delta = alternate_shifts - alt_target
    if portage_is_fulltime_catalog_hours(catalog_hours):
        if abs(delta) <= PORTAGE_EQUITY_ALT_TOLERANCE_FT:
            return "ok"
        return "low" if delta < 0 else "high"
    if alternate_shifts >= alt_target:
        if alternate_shifts <= alt_target + PORTAGE_EQUITY_ALT_TOLERANCE_PT:
            return "ok"
        return "high"
    if alt_target - alternate_shifts > PORTAGE_EQUITY_ALT_TOLERANCE_PT:
        return "low"
    return "ok"


def _weekend_drift_status(*, weekend_shifts: int, weekend_target: int) -> DriftStatus:
    if weekend_target <= 0:
        return "na"
    delta = weekend_shifts - weekend_target
    if abs(delta) <= PORTAGE_EQUITY_WEEKEND_TOLERANCE:
        return "ok"
    return "low" if delta < 0 else "high"


def evaluate_portage_equity_drift(
    employee: EmployeeProfile,
    catalog_hours: float,
    *,
    alternate_shifts: int,
    total_shifts: int,
    weekend_shifts: int,
    weekend_target: int | None = None,
) -> Optional[PortageEquityDrift]:
    """Return drift metrics for a vacant Portage catalog line, else None."""

    if parse_vacant_portage_line(employee.full_name) is None:
        return None
    if catalog_hours <= 0 or total_shifts <= 0:
        return None

    role = portage_equity_role_for_employee(employee)
    alt_target = portage_alt_shift_target_for_employee(employee, catalog_hours)
    alt_density = round(100.0 * alternate_shifts / total_shifts, 1)
    contract_shifts = portage_contract_shift_count(catalog_hours)
    target_density = (
        round(100.0 * alt_target / contract_shifts, 1) if contract_shifts else 0.0
    )
    if weekend_target is None:
        weekend_target = portage_weekend_shift_target(
            catalog_hours,
            equity_role=role,
            contract_line_type=employee.contract_line_type or "",
        )

    active_weekend_target = max(0, weekend_target // 2)
    if (
        role == "core_ft"
        and (employee.contract_line_type or "").upper() == "D/N"
        and weekend_target > 0
    ):
        active_weekend_target = PORTAGE_DN_FT_WEEKEND_PAIRS
    elif role == "light_pt" and weekend_target > 0:
        active_weekend_target = max(1, active_weekend_target)

    return PortageEquityDrift(
        equity_role=role,
        role_label=_role_label(role, catalog_hours),
        alt_actual=alternate_shifts,
        alt_target=alt_target,
        alt_density_pct=alt_density,
        alt_target_density_pct=target_density,
        alt_status=_alt_drift_status(
            alternate_shifts=alternate_shifts,
            alt_target=alt_target,
            catalog_hours=catalog_hours,
        ),
        weekend_actual=weekend_shifts,
        weekend_target=weekend_target,
        active_weekend_target=active_weekend_target,
        weekend_status=_weekend_drift_status(
            weekend_shifts=weekend_shifts,
            weekend_target=weekend_target,
        ),
    )


def build_portage_equity_drift_map(
    employees: Sequence[EmployeeProfile],
    catalog_targets: Mapping[str, float],
    *,
    alternate_shifts_by_employee: Mapping[str, int],
    total_shifts_by_employee: Mapping[str, int],
    weekend_shifts_by_employee: Mapping[str, int],
    qual_codes: Mapping[str, str],
    period_start,
    period_end,
) -> Dict[str, PortageEquityDrift]:
    """Build per-employee drift rows for vacant Portage lines."""

    weekend_targets = build_vacant_line_weekend_target_map(
        employees,
        catalog_targets,
        qual_codes,
        period_start=period_start,
        period_end=period_end,
    )
    drift: Dict[str, PortageEquityDrift] = {}
    for employee in employees:
        catalog_hours = float(catalog_targets.get(employee.id, 0.0))
        total_shifts = int(total_shifts_by_employee.get(employee.id, 0))
        if total_shifts <= 0:
            continue
        row = evaluate_portage_equity_drift(
            employee,
            catalog_hours,
            alternate_shifts=int(alternate_shifts_by_employee.get(employee.id, 0)),
            total_shifts=total_shifts,
            weekend_shifts=int(weekend_shifts_by_employee.get(employee.id, 0)),
            weekend_target=int(weekend_targets.get(employee.id, 0)) or None,
        )
        if row is not None:
            drift[employee.id] = row
    return drift
