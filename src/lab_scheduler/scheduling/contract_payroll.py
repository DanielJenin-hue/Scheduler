from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING, Mapping, Sequence

from lab_scheduler.compliance.jurisdictions import JurisdictionRules
from lab_scheduler.scheduling.profiles import EmployeeProfile

if TYPE_CHECKING:
    from lab_scheduler.scheduling.pool_manager import ElasticPoolManager
    from lab_scheduler.scheduling.auto_generate import _EmployeeState

FULLTIME_FTE_THRESHOLD = 0.99
HOURS_PER_SHIFT = 8.0
VACANT_HOUR_CEILING_EPSILON = 1e-9


def resolve_employee_fte(employee: Mapping[str, object] | EmployeeProfile) -> float:
    """Read FTE from profiles or dict rows (Streamlit reload-safe duck typing)."""

    fte = getattr(employee, "fte", None)
    if fte is not None:
        return float(fte)
    if isinstance(employee, Mapping):
        return float(employee.get("fte", 0.0) or 0.0)
    return 0.0


def paid_hours_per_shift(*, schedule_archetype: str = "STANDARD") -> float:
    """Paid hours per worked shift token for contract tracking (8h standard, 11.625 twelve-hour)."""

    normalized = str(schedule_archetype or "STANDARD").strip().upper().replace("-", "_")
    if normalized in {"TWELVE_HOUR", "TWELVEHOUR", "12H", "7ON7OFF"}:
        from lab_scheduler.scheduling.strategies.twelve_hour_7on7off_strategy import (
            TWELVE_HOUR_PAID_HOURS,
        )

        return TWELVE_HOUR_PAID_HOURS
    return HOURS_PER_SHIFT


def period_contract_hours_for_fte(
    *,
    fte: float,
    weeks_in_period: int,
    standard_weekly_hours: float = 40.0,
) -> float:
    """Payroll contract hours for one line: FTE × standard weekly hours × weeks."""

    return round(float(fte) * standard_weekly_hours * weeks_in_period, 2)


def fulltime_period_contract_hours(
    *,
    rules: JurisdictionRules,
    weeks_in_period: int,
) -> float:
    """Period target for a 1.0 FTE line on the payroll basis (e.g. 320h over 8 weeks)."""

    return period_contract_hours_for_fte(
        fte=1.0,
        weeks_in_period=weeks_in_period,
        standard_weekly_hours=rules.standard_hours_per_week_at_1_0_fte,
    )


def aggregate_payroll_contract_hours(
    employees: Sequence[Mapping[str, object] | EmployeeProfile],
    *,
    rules: JurisdictionRules,
    weeks_in_period: int,
) -> float:
    """Sum of payroll contract hours across a roster (matches export contract tracking)."""

    total = 0.0
    for employee in employees:
        fte = resolve_employee_fte(employee)
        total += period_contract_hours_for_fte(
            fte=fte,
            weeks_in_period=weeks_in_period,
            standard_weekly_hours=rules.standard_hours_per_week_at_1_0_fte,
        )
    return round(total, 2)


def build_solver_target_hours_map(
    employees: Sequence[EmployeeProfile],
    *,
    rules: JurisdictionRules,
    weeks_in_period: int,
    employee_target_hours: Mapping[str, float] | None = None,
) -> dict[str, float]:
    """
    Solver targets: 1.0 FTE lines always use payroll basis (FTE × 40h × weeks);
    part-time lines keep supplied or default FTE-scaled targets.
    """

    fulltime_target = fulltime_period_contract_hours(
        rules=rules,
        weeks_in_period=weeks_in_period,
    )
    targets: dict[str, float] = {}
    for employee in employees:
        if employee.fte >= FULLTIME_FTE_THRESHOLD:
            targets[employee.id] = fulltime_target
        elif employee_target_hours is not None and employee.id in employee_target_hours:
            targets[employee.id] = float(employee_target_hours[employee.id])
        else:
            targets[employee.id] = period_contract_hours_for_fte(
                fte=employee.fte,
                weeks_in_period=weeks_in_period,
                standard_weekly_hours=rules.standard_hours_per_week_at_1_0_fte,
            )
    return targets


def build_elastic_target_hours_map(
    pool_manager: "ElasticPoolManager",
    *,
    rules: JurisdictionRules,
    weeks_in_period: int,
) -> dict[str, float]:
    """
    Even-distribution targets: each role pool shares payroll hours proportionally by FTE.

    New hires shift the pool average automatically because counts are read at runtime.
    """

    targets: dict[str, float] = {}
    for role, member_ids in pool_manager.role_pools.items():
        if not member_ids:
            continue
        role_capacity = pool_manager.role_capacity_hours(
            role,
            rules=rules,
            weeks_in_period=weeks_in_period,
        )
        total_fte = sum(pool_manager.members[employee_id].fte for employee_id in member_ids)
        for employee_id in member_ids:
            member = pool_manager.members[employee_id]
            if total_fte <= 0.0:
                targets[employee_id] = 0.0
            else:
                share = member.fte / total_fte
                targets[employee_id] = round(role_capacity * share, 2)
    return targets


def contract_fte_manager_label(
    *,
    rules: JurisdictionRules,
    weeks_in_period: int,
) -> str:
    target = fulltime_period_contract_hours(
        rules=rules,
        weeks_in_period=weeks_in_period,
    )
    return f"Contract FTE violation ({target:.0f}h target)"


def is_fulltime_contract_deficit(
    employee: EmployeeProfile,
    total_hours: float,
    *,
    fulltime_target: float,
) -> bool:
    return (
        employee.fte >= FULLTIME_FTE_THRESHOLD
        and total_hours < fulltime_target - 0.25
    )


def apply_catalog_targets_for_vacant_master_lines(
    employees: Sequence[EmployeeProfile],
    target_hours_map: Mapping[str, float],
    *,
    rules: JurisdictionRules,
    weeks_in_period: int,
    period_start: date | None = None,
    period_end: date | None = None,
) -> dict[str, float]:
    """
    Copy ``target_hours_map`` and replace entries for vacant Portage master lines
    with catalog-stamped period hours when dates are supplied, otherwise payroll FTE.
    """

    from lab_scheduler.scheduling.portage_template import (
        parse_vacant_portage_line,
        portage_master_line_spec,
        vacant_master_catalog_period_hours,
    )

    updated = dict(target_hours_map)
    for employee in employees:
        if parse_vacant_portage_line(employee.full_name) is None:
            continue
        payroll_hours = float(target_hours_map.get(employee.id, 0.0))
        if period_start is not None and period_end is not None:
            stamped_hours = vacant_master_catalog_period_hours(
                employee,
                period_start,
                period_end,
            )
            if employee.fte >= FULLTIME_FTE_THRESHOLD:
                # Full-time contract hours come from FTE (1.0 → 320h); catalog drives shifts only.
                if payroll_hours > 0.0:
                    updated[employee.id] = payroll_hours
                else:
                    updated[employee.id] = period_contract_hours_for_fte(
                        fte=employee.fte,
                        weeks_in_period=weeks_in_period,
                        standard_weekly_hours=rules.standard_hours_per_week_at_1_0_fte,
                    )
                continue
            if stamped_hours > 0.0:
                updated[employee.id] = stamped_hours
                continue
        spec = portage_master_line_spec(employee)
        if spec is None:
            continue
        updated[employee.id] = period_contract_hours_for_fte(
            fte=spec.target_fte,
            weeks_in_period=weeks_in_period,
            standard_weekly_hours=rules.standard_hours_per_week_at_1_0_fte,
        )
    return updated


def vacant_payroll_hour_ceiling(
    employee: EmployeeProfile,
    payroll_targets: Mapping[str, float],
) -> float:
    """Payroll contract hours cap for a vacant Portage master line."""

    from lab_scheduler.scheduling.portage_template import parse_vacant_portage_line

    if parse_vacant_portage_line(employee.full_name) is None:
        return 0.0
    return float(payroll_targets.get(employee.id, 0.0))


def vacant_assignment_hour_ceiling(
    employee: EmployeeProfile,
    payroll_targets: Mapping[str, float],
    catalog_targets: Mapping[str, float] | None = None,
) -> float:
    """
    Hard assignment ceiling for vacant Portage lines.

    Full-time vacant rows use payroll (320h). Part-time rows use the lower of payroll
    FTE hours and stamped catalog hours so post-passes cannot exceed either bound.
    """

    from lab_scheduler.scheduling.portage_equity_targets import portage_is_fulltime_catalog_hours
    from lab_scheduler.scheduling.portage_template import parse_vacant_portage_line

    if parse_vacant_portage_line(employee.full_name) is None:
        return 0.0
    payroll = vacant_payroll_hour_ceiling(employee, payroll_targets)
    if payroll <= 0.0:
        return 0.0
    if portage_is_fulltime_catalog_hours(payroll):
        return payroll
    if catalog_targets is not None:
        catalog = float(catalog_targets.get(employee.id, payroll))
        if catalog > 0.0:
            return min(payroll, catalog)
    return payroll


def would_exceed_vacant_assignment_ceiling(
    total_hours: float,
    shift_hours: float,
    employee: EmployeeProfile,
    payroll_targets: Mapping[str, float],
    catalog_targets: Mapping[str, float] | None = None,
) -> bool:
    ceiling = vacant_assignment_hour_ceiling(
        employee,
        payroll_targets,
        catalog_targets,
    )
    if ceiling <= 0.0:
        return True
    return total_hours + shift_hours > ceiling + VACANT_HOUR_CEILING_EPSILON


def vacant_assignment_ceiling_message(
    employee: EmployeeProfile,
    payroll_targets: Mapping[str, float],
    catalog_targets: Mapping[str, float] | None = None,
) -> str:
    ceiling = vacant_assignment_hour_ceiling(
        employee,
        payroll_targets,
        catalog_targets,
    )
    return f"would exceed contract hour ceiling ({ceiling:.0f}h)"
