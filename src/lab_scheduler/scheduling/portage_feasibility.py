"""Portage schedule feasibility: where catalog equity targets conflict with ops caps."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Mapping, Sequence, Tuple

from lab_scheduler.engine.demand import (
    CLINICAL_FLOOR,
    WEEKEND_CLINICAL_MAX_PER_QUAL,
    infer_qual_code,
)
from lab_scheduler.scheduling.persist_validation import FULLTIME_CONTRACT_HOUR_TOLERANCE
from lab_scheduler.scheduling.portage_equity_policy import PortageSchedulingPolicy
from lab_scheduler.scheduling.portage_equity_targets import (
    portage_alt_shift_target,
    portage_alt_shift_target_for_employee,
    portage_weekend_shift_target,
    PORTAGE_ALT_SHIFT_DENSITY,
    PORTAGE_FULLTIME_WEEKEND_SHIFTS,
    scale_weekend_ideals_to_pool_capacity,
)
from lab_scheduler.scheduling.portage_template import parse_vacant_portage_line
from lab_scheduler.scheduling.profiles import EmployeeProfile
from lab_scheduler.simulation.portage_blueprint import portage_equity_role_for_employee

# Pool-wide clinical floor (evening + night seats per calendar day).
DAILY_ALT_SEATS_PER_BAND = 2


@dataclass(frozen=True, slots=True)
class FeasibilityConflict:
    code: str
    severity: str  # "hard" | "soft"
    message: str
    pool: str = ""
    demand: float = 0.0
    capacity: float = 0.0


@dataclass(frozen=True, slots=True)
class PortageFeasibilityReport:
    weekend_day_count: int
    period_day_count: int
    conflicts: Tuple[FeasibilityConflict, ...] = field(default_factory=tuple)

    @property
    def has_hard_conflicts(self) -> bool:
        return any(item.severity == "hard" for item in self.conflicts)


@dataclass(frozen=True, slots=True)
class PortageRuleContractEntry:
    tier: str  # "hard" | "primary" | "equity" | "aspirational"
    code: str
    label: str
    detail: str


@dataclass(frozen=True, slots=True)
class PortageRulesContract:
    policy_id: str
    policy_title: str
    entries: Tuple[PortageRuleContractEntry, ...] = field(default_factory=tuple)

    def entries_by_tier(self, tier: str) -> Tuple[PortageRuleContractEntry, ...]:
        return tuple(entry for entry in self.entries if entry.tier == tier)


def build_portage_rules_contract(
    policy: PortageSchedulingPolicy,
) -> PortageRulesContract:
    """Export ranked Portage rules from code constants and the active scheduling policy."""

    evening_floor = int(CLINICAL_FLOOR.get("EVENING", 2))
    night_floor = int(CLINICAL_FLOOR.get("NIGHT", 2))
    mlt_weekend_cap = int(WEEKEND_CLINICAL_MAX_PER_QUAL.get("MLT", 2))
    mla_weekend_cap = int(WEEKEND_CLINICAL_MAX_PER_QUAL.get("MLA", 1))

    entries: List[PortageRuleContractEntry] = [
        PortageRuleContractEntry(
            tier="hard",
            code="UNION_TURNAROUND",
            label="Union turnaround and fatigue guardrails",
            detail="Minimum rest between shifts; consecutive work/night limits enforced on persist.",
        ),
        PortageRuleContractEntry(
            tier="hard",
            code="WEEKEND_QUAL_CAP",
            label="Weekend clinical qual caps",
            detail=(
                f"Max {mlt_weekend_cap} MLT and {mla_weekend_cap} MLA per weekend day "
                "(pool-wide, not per catalog peer group)."
            ),
        ),
        PortageRuleContractEntry(
            tier="hard",
            code="CONTRACT_HOUR_TOLERANCE",
            label="Contract hour persist tolerance",
            detail=f"Full-time lines may not exceed catalog by more than {FULLTIME_CONTRACT_HOUR_TOLERANCE:.0f}h.",
        ),
    ]

    objective_labels = {
        "clinical_2en": (
            "Clinical 2E/2N daily",
            f"At least {evening_floor} evening and {night_floor} night seats filled every calendar day.",
        ),
        "catalog_hours": (
            "Catalog contract hours",
            "Every vacant line reaches catalog hours (320h FT, proportional PT) within persist tolerance.",
        ),
        "union_clean": (
            "Union-clean export",
            "Persist gate passes with minimal union violations; soft clinical caps allowed.",
        ),
    }
    for objective in policy.primary_objectives:
        label, detail = objective_labels.get(
            objective,
            (objective.replace("_", " ").title(), ""),
        )
        entries.append(
            PortageRuleContractEntry(
                tier="primary",
                code=f"PRIMARY_{objective.upper()}",
                label=label,
                detail=detail,
            )
        )

    if policy.weekend_mode == "proportional":
        entries.append(
            PortageRuleContractEntry(
                tier="equity",
                code="WEEKEND_PROPORTIONAL",
                label="Proportional weekend share",
                detail=(
                    "Within each qual+contract pool, FT lines share available weekend slots evenly "
                    "when catalog ideals exceed qual caps."
                ),
            )
        )
    else:
        entries.append(
            PortageRuleContractEntry(
                tier="equity",
                code="WEEKEND_CATALOG",
                label="Catalog weekend count",
                detail=f"Each line targets {PORTAGE_FULLTIME_WEEKEND_SHIFTS} weekend shifts per 8-week block.",
            )
        )

    if policy.alt_equity_scope == "ft_peers_only":
        entries.append(
            PortageRuleContractEntry(
                tier="equity",
                code="ALT_FT_PEERS",
                label="FT alternate-shift parity",
                detail=(
                    "Full-time vacant lines (core_ft role) target 20% alternate-band share "
                    "within FT peers; gap_fill_pt lines target ~35% as clinical gap-fillers."
                ),
            )
        )
    else:
        entries.append(
            PortageRuleContractEntry(
                tier="equity",
                code="ALT_ALL_PEERS",
                label="Alternate-shift parity (all peers)",
                detail=f"Every vacant line targets {PORTAGE_ALT_SHIFT_DENSITY:.0%} alternate-band share.",
            )
        )

    entries.append(
        PortageRuleContractEntry(
            tier="aspirational",
            code="FT_WEEKEND_CATALOG_IDEAL",
            label="Historical 8-weekend FT ideal",
            detail=(
                f"Display/catalog label of {PORTAGE_FULLTIME_WEEKEND_SHIFTS} weekend shifts per FT line; "
                "scaled down when qual caps require proportional share."
            ),
        )
    )

    return PortageRulesContract(
        policy_id=policy.id,
        policy_title=policy.title,
        entries=tuple(entries),
    )


def _qual_contract_groups(
    employees: Sequence[EmployeeProfile],
    catalog_targets: Mapping[str, float],
    qual_codes: Mapping[str, str],
) -> Dict[Tuple[str, str], List[Tuple[str, float]]]:
    groups: Dict[Tuple[str, str], List[Tuple[str, float]]] = {}
    for employee in employees:
        if parse_vacant_portage_line(employee.full_name) is None:
            continue
        qual = qual_codes.get(employee.id) or infer_qual_code(employee, qual_codes=qual_codes)
        contract = (employee.contract_line_type or "D/E").upper()
        groups.setdefault((qual.upper(), contract), []).append(
            (employee.id, float(catalog_targets.get(employee.id, 0.0)))
        )
    return groups


def build_portage_pool_budget_rows(
    employees: Sequence[EmployeeProfile],
    catalog_targets: Mapping[str, float],
    qual_codes: Mapping[str, str],
    *,
    weekend_day_count: int,
    period_day_count: int,
) -> Tuple[Dict[str, object], ...]:
    """Per qual+contract pool: weekend and alternate-band demand vs ops capacity."""

    employee_by_id = {employee.id: employee for employee in employees}
    groups = _qual_contract_groups(employees, catalog_targets, qual_codes)
    rows: list[Dict[str, object]] = []

    for (qual, contract), members in sorted(groups.items()):
        pool_label = f"{qual} {contract}"
        max_per_day = int(WEEKEND_CLINICAL_MAX_PER_QUAL.get(qual, 1))
        weekend_capacity = weekend_day_count * max_per_day
        alt_band = "E" if contract == "D/E" else "N"
        alt_capacity = period_day_count * DAILY_ALT_SEATS_PER_BAND

        ideals: list[int] = []
        alt_demand = 0
        for employee_id, hours in members:
            employee = employee_by_id[employee_id]
            role = portage_equity_role_for_employee(employee)
            ideals.append(portage_weekend_shift_target(hours, equity_role=role))
            alt_demand += portage_alt_shift_target_for_employee(employee, hours)

        scaled = scale_weekend_ideals_to_pool_capacity(
            ideals,
            qual_code=qual,
            weekend_day_count=weekend_day_count,
        )
        weekend_demand = sum(ideals)
        weekend_scaled = sum(scaled)
        line_count = len(members)

        rows.append(
            {
                "Pool": pool_label,
                "Lines": line_count,
                "Weekend demand": weekend_demand,
                "Weekend capacity": weekend_capacity,
                "Weekend headroom": weekend_capacity - weekend_demand,
                "Scaled weekend total": weekend_scaled,
                "Avg scaled / line": round(weekend_scaled / line_count, 1) if line_count else 0,
                f"{alt_band} alt demand": alt_demand,
                f"{alt_band} alt capacity": alt_capacity,
                f"{alt_band} alt headroom": alt_capacity - alt_demand,
            }
        )

    return tuple(rows)


def portage_qual_contract_weekend_targets(
    member_catalog_hours: Sequence[float],
    *,
    qual_code: str,
    weekend_day_count: int,
) -> Tuple[int, ...]:
    """
    Split qual-wide weekend capacity across all lines in a (qual, contract) roster slice.

    Uses proportional scaling when catalog ideals exceed ``WEEKEND_CLINICAL_MAX_PER_QUAL``.
    """

    if not member_catalog_hours or weekend_day_count <= 0:
        return tuple()

    ideals = [portage_weekend_shift_target(hours) for hours in member_catalog_hours]
    return scale_weekend_ideals_to_pool_capacity(
        ideals,
        qual_code=qual_code,
        weekend_day_count=weekend_day_count,
    )


def portage_qual_contract_weekend_targets_from_stamps(
    member_stamped_weekends: Sequence[int],
    *,
    qual_code: str,
    weekend_day_count: int,
) -> Tuple[int, ...]:
    """Pool-scale stamped catalog weekend ideals for a qual+contract slice."""

    return scale_weekend_ideals_to_pool_capacity(
        member_stamped_weekends,
        qual_code=qual_code,
        weekend_day_count=weekend_day_count,
    )


def portage_qual_contract_weekend_target_map(
    members: Sequence[Tuple[str, float]],
    *,
    qual_code: str,
    weekend_day_count: int,
) -> Dict[str, int]:
    catalog_hours = [hours for _employee_id, hours in members]
    targets = portage_qual_contract_weekend_targets(
        catalog_hours,
        qual_code=qual_code,
        weekend_day_count=weekend_day_count,
    )
    return {
        employee_id: targets[index]
        for index, (employee_id, _hours) in enumerate(members)
    }


def analyze_portage_feasibility(
    employees: Sequence[EmployeeProfile],
    catalog_targets: Mapping[str, float],
    *,
    qual_codes: Mapping[str, str],
    weekend_day_count: int,
    period_day_count: int,
) -> PortageFeasibilityReport:
    """Return hard/soft conflicts between catalog equity and Portage ops caps."""

    conflicts: List[FeasibilityConflict] = []
    groups = _qual_contract_groups(employees, catalog_targets, qual_codes)
    employee_by_id = {employee.id: employee for employee in employees}

    for (qual, contract), members in sorted(groups.items()):
        pool_label = f"{qual} {contract}"
        catalog_hours = [hours for _employee_id, hours in members]
        ideals = []
        for employee_id, hours in members:
            employee = employee_by_id[employee_id]
            role = portage_equity_role_for_employee(employee)
            ideals.append(portage_weekend_shift_target(hours, equity_role=role))
        max_per_day = int(WEEKEND_CLINICAL_MAX_PER_QUAL.get(qual, 1))
        wk_capacity = weekend_day_count * max_per_day
        wk_demand = sum(ideals)
        if wk_demand > wk_capacity:
            conflicts.append(
                FeasibilityConflict(
                    code="WEEKEND_QUAL_CAPACITY",
                    severity="hard",
                    pool=pool_label,
                    demand=float(wk_demand),
                    capacity=float(wk_capacity),
                    message=(
                        f"{pool_label}: catalog weekend demand is {wk_demand} shift-days "
                        f"but qual cap allows {wk_capacity} "
                        f"({max_per_day}/day × {weekend_day_count} weekend days). "
                        f"Proportional cap ~ {wk_capacity / max(len(members), 1):.1f} per line."
                    ),
                )
            )

        alt_demand = sum(
            portage_alt_shift_target_for_employee(employee_by_id[employee_id], hours)
            for employee_id, hours in members
        )
        alt_band = "E" if contract == "D/E" else "N"
        alt_capacity = period_day_count * DAILY_ALT_SEATS_PER_BAND
        if alt_demand > alt_capacity:
            conflicts.append(
                FeasibilityConflict(
                    code="ALT_BAND_POOL_CAPACITY",
                    severity="hard",
                    pool=pool_label,
                    demand=float(alt_demand),
                    capacity=float(alt_capacity),
                    message=(
                        f"{pool_label}: {alt_demand} {alt_band} shifts needed for "
                        f"{PORTAGE_ALT_SHIFT_DENSITY:.0%} catalog density but pool allows "
                        f"{alt_capacity} ({DAILY_ALT_SEATS_PER_BAND}/day × {period_day_count} days)."
                    ),
                )
            )

        ft_members = [h for h in catalog_hours if h >= 312.0]
        if ft_members:
            ft_wk_ideal = portage_weekend_shift_target(320.0)
            if ft_wk_ideal * len(ft_members) > wk_capacity:
                conflicts.append(
                    FeasibilityConflict(
                        code="FT_WEEKEND_CATALOG_VS_CAP",
                        severity="hard",
                        pool=pool_label,
                        demand=float(ft_wk_ideal * len(ft_members)),
                        capacity=float(wk_capacity),
                        message=(
                            f"{pool_label}: {len(ft_members)} full-time lines × "
                            f"{PORTAGE_FULLTIME_WEEKEND_SHIFTS} weekend shifts = "
                            f"{ft_wk_ideal * len(ft_members)} demand vs {wk_capacity} qual capacity."
                        ),
                    )
                )

    return PortageFeasibilityReport(
        weekend_day_count=weekend_day_count,
        period_day_count=period_day_count,
        conflicts=tuple(conflicts),
    )


def format_feasibility_manager_notes(
    report: PortageFeasibilityReport,
    employees: Sequence[EmployeeProfile],
    catalog_targets: Mapping[str, float],
    qual_codes: Mapping[str, str],
) -> Tuple[str, ...]:
    """
    Plain-language notes for managers when catalog ideals exceed ops caps.

    Includes proportional weekend targets per qual+contract pool.
    """

    notes: List[str] = []
    groups = _qual_contract_groups(employees, catalog_targets, qual_codes)
    weekend_days = report.weekend_day_count

    for conflict in report.conflicts:
        if conflict.code == "FT_WEEKEND_CATALOG_VS_CAP":
            notes.append(
                f"**Weekend cap:** {conflict.message} "
                "A staffing manager would use **proportional weekend share**, not 8 per FT line."
            )
        elif conflict.severity == "hard":
            notes.append(f"**Hard limit:** {conflict.message}")

    for (qual, contract), members in sorted(groups.items()):
        if not members:
            continue
        targets = portage_qual_contract_weekend_target_map(
            members,
            qual_code=qual,
            weekend_day_count=weekend_days,
        )
        if not targets:
            continue
        unique = sorted(set(targets.values()))
        if len(unique) == 1:
            notes.append(
                f"**{qual} {contract}:** proportional weekend target ≈ **{unique[0]}** "
                f"shift-days per line ({weekend_days} weekend days in period)."
            )
        else:
            notes.append(
                f"**{qual} {contract}:** proportional weekend targets vary "
                f"({min(unique)}–{max(unique)} shift-days per line)."
            )

    return tuple(notes)
