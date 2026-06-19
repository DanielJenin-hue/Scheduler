from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from lab_scheduler.compliance.engine import ShiftTemplateInfo
from lab_scheduler.compliance.jurisdictions import JurisdictionRules
from lab_scheduler.models.employee import (
    allowed_shift_codes_for_contract_line,
    allowed_shift_codes_for_role_contract,
    contract_line_violation_message,
    is_critical_contract_line_violation,
    normalize_shift_band_code,
)
from lab_scheduler.scheduling.profiles import EmployeeProfile

from lab_scheduler.errors.schedule_error import (
    IMPOSSIBLE_COVERAGE_TOOLTIP,
    ScheduleError,
    VIOLATION_COVERAGE_TARGET,
    VIOLATION_IMPOSSIBLE_COVERAGE,
    VIOLATION_LABOR_RULE,
)

# Portage rotation document FTE tier samples (informational labels).
PORTAGE_ROTATION_FTE_SAMPLES: Tuple[float, ...] = (1.0, 1.0, 0.7, 0.6, 0.5, 0.4, 0.2)


@dataclass(frozen=True, slots=True)
class CoverageTierTarget:
    """Hard coverage requirement for a roster line (e.g. MLT 01 (1.0 D/N))."""

    tier_id: str
    label: str
    target_fte: float
    qualification_ids: frozenset[str] = frozenset()
    allowed_shift_codes: Optional[frozenset[str]] = None


@dataclass(frozen=True, slots=True)
class CoverageTierResult:
    tier_id: str
    label: str
    target_fte: float
    actual_fte: float
    gap_fte: float
    target_hours: float
    actual_hours: float
    period_target_hours: float
    is_impossible: bool = False

    @property
    def meets_target(self) -> bool:
        return self.actual_hours + 1e-6 >= self.period_target_hours

    @property
    def success_ratio(self) -> float:
        if self.period_target_hours <= 0:
            return 1.0
        return min(1.0, self.actual_hours / self.period_target_hours)


def _daterange(start: date, end_inclusive: date) -> List[date]:
    days: List[date] = []
    cur = start
    while cur <= end_inclusive:
        days.append(cur)
        cur += timedelta(days=1)
    return days


def _tier_label(employee: EmployeeProfile, *, qual_code: str) -> str:
    seq = employee.id.rsplit("-", 1)[-1]
    shift_band = employee.contract_line_type or ("D/N" if qual_code == "MLT" else "D")
    return f"{qual_code} {seq} ({employee.fte:g} {shift_band})"


def _legacy_allowed_shift_codes(qual_code: str) -> frozenset[str]:
    if qual_code == "MLT":
        return frozenset({"EVENING", "NIGHT"})
    return frozenset({"MORNING"})


def allowed_shift_codes_for_employee(
    employee: EmployeeProfile,
    *,
    qual_code: str,
) -> frozenset[str]:
    if employee.contract_line_type:
        return allowed_shift_codes_for_role_contract(
            employee.contract_line_type,
            qual_code=qual_code,
        )
    return _legacy_allowed_shift_codes(qual_code)


def validate_contract_line_eligibility(
    contract_line_type: Optional[str],
    shift_template_code: str,
    *,
    qual_code: Optional[str] = None,
) -> Optional[str]:
    """Hard pre-filter: contract line must allow the shift band before labor/FTE checks."""

    if not contract_line_type:
        return None
    normalized = normalize_shift_band_code(shift_template_code)
    if qual_code:
        allowed = allowed_shift_codes_for_role_contract(
            contract_line_type,
            qual_code=qual_code,
        )
    else:
        allowed = allowed_shift_codes_for_contract_line(contract_line_type)
    if normalized in allowed:
        return None
    return contract_line_violation_message(
        contract_line_type,
        normalized,
        qual_code=qual_code,
    )


def _pool_group_key(target: CoverageTierTarget) -> Tuple[frozenset[str], frozenset[str]]:
    allowed = target.allowed_shift_codes or frozenset({"MORNING", "EVENING", "NIGHT"})
    return (target.qualification_ids, allowed)


def compute_shift_pool_hours(
    *,
    shift_templates: Mapping[str, ShiftTemplateInfo],
    period_start: date,
    period_end: date,
    allowed_shift_codes: frozenset[str],
) -> float:
    hours = 0.0
    for _date in _daterange(period_start, period_end):
        for template in shift_templates.values():
            if template.code in allowed_shift_codes:
                hours += template.duration_minutes / 60.0
    return hours


def compute_period_target_hours_map(
    targets: Sequence[CoverageTierTarget],
    *,
    shift_templates: Mapping[str, ShiftTemplateInfo],
    period_start: date,
    period_end: date,
) -> Dict[str, float]:
    """
    Qualification-aware fair share: MLA D-line targets use the morning pool only;
    MLT D/N-line targets use their eligible E/N (or configured) pool — not all 84 slots.
    """

    pool_hours_by_group: Dict[Tuple[frozenset[str], frozenset[str]], float] = {}
    group_fte: Dict[Tuple[frozenset[str], frozenset[str]], float] = {}

    for target in targets:
        group = _pool_group_key(target)
        if group not in pool_hours_by_group:
            allowed = group[1]
            pool_hours_by_group[group] = compute_shift_pool_hours(
                shift_templates=shift_templates,
                period_start=period_start,
                period_end=period_end,
                allowed_shift_codes=allowed,
            )
        group_fte[group] = group_fte.get(group, 0.0) + target.target_fte

    period_targets: Dict[str, float] = {}
    for target in targets:
        group = _pool_group_key(target)
        pool_hours = pool_hours_by_group[group]
        total_fte = group_fte[group]
        if total_fte <= 0 or pool_hours <= 0:
            period_targets[target.tier_id] = 0.0
        else:
            period_targets[target.tier_id] = round(
                (target.target_fte / total_fte) * pool_hours,
                2,
            )
    return period_targets


def build_coverage_targets_from_roster(
    employees: Sequence[EmployeeProfile],
    *,
    qual_codes: Optional[Mapping[str, str]] = None,
) -> Tuple[CoverageTierTarget, ...]:
    """Derive per-employee hard coverage targets from roster FTE contracts."""

    qual_codes = qual_codes or {}
    targets: List[CoverageTierTarget] = []
    for employee in employees:
        qual_id = next(iter(employee.qualification_ids), "")
        qual_code = qual_codes.get(qual_id, "MLT" if "mlt" in qual_id.lower() else "MLA")
        allowed = allowed_shift_codes_for_employee(employee, qual_code=qual_code)
        targets.append(
            CoverageTierTarget(
                tier_id=employee.id,
                label=_tier_label(employee, qual_code=qual_code),
                target_fte=employee.fte,
                qualification_ids=frozenset(employee.qualification_ids),
                allowed_shift_codes=allowed,
            )
        )
    return tuple(targets)


def portage_coverage_targets(employees: Sequence[EmployeeProfile]) -> Tuple[CoverageTierTarget, ...]:
    from lab_scheduler.simulation.hospital_stress import QUAL_MLA, QUAL_MLT

    return build_coverage_targets_from_roster(
        employees,
        qual_codes={QUAL_MLT: "MLT", QUAL_MLA: "MLA"},
    )


def portage_employee_target_hours(
    employees: Sequence[EmployeeProfile],
    *,
    weeks_in_period: int,
    rules: JurisdictionRules,
) -> Dict[str, float]:
    """
    Portage rotation weekly-hour patterns (40/40, 39/40, …) mapped to period targets.

    Full-time lines use 40h/week. MLT D/N Lines 03–04 use a 39/40 alternating week pattern.
    """

    from lab_scheduler.scheduling.portage_template import parse_vacant_portage_line

    weekly_standard = rules.standard_hours_per_week_at_1_0_fte
    targets: Dict[str, float] = {}
    for employee in employees:
        base = weekly_standard * employee.fte * weeks_in_period
        vacant = parse_vacant_portage_line(employee.full_name)
        use_3940 = (
            employee.fte >= 0.99
            and vacant is not None
            and vacant[0] == "MLT"
            and vacant[1] == "D/N"
            and vacant[2] in (3, 4)
        )
        if use_3940:
            odd_weeks = (weeks_in_period + 1) // 2
            even_weeks = weeks_in_period // 2
            targets[employee.id] = round(
                employee.fte * (39.0 * odd_weeks + 40.0 * even_weeks),
                2,
            )
        else:
            targets[employee.id] = round(base, 2)
    return targets


def evaluate_coverage_tier_results(
    *,
    targets: Sequence[CoverageTierTarget],
    employee_hours: Mapping[str, float],
    rules: JurisdictionRules,
    weeks_in_period: int,
    employee_target_hours: Optional[Mapping[str, float]] = None,
    impossible_tier_ids: Optional[Set[str]] = None,
    slots_total: int = 0,
    shift_hours: float = 8.0,
    shift_templates: Optional[Mapping[str, ShiftTemplateInfo]] = None,
    period_start: Optional[date] = None,
    period_end: Optional[date] = None,
) -> Tuple[CoverageTierResult, ...]:
    impossible_tier_ids = impossible_tier_ids or set()
    results: List[CoverageTierResult] = []
    weekly_standard = rules.standard_hours_per_week_at_1_0_fte

    pool_targets: Dict[str, float] = {}
    if shift_templates and period_start and period_end:
        pool_targets = compute_period_target_hours_map(
            targets,
            shift_templates=shift_templates,
            period_start=period_start,
            period_end=period_end,
        )

    for target in targets:
        contractual_fte = target.target_fte
        if target.tier_id in pool_targets:
            period_target_hours = pool_targets[target.tier_id]
        elif slots_total > 0:
            total_roster_fte = sum(item.target_fte for item in targets)
            period_target_hours = round(
                (contractual_fte / total_roster_fte) * slots_total * shift_hours,
                2,
            )
        else:
            period_target_hours = weekly_standard * contractual_fte * weeks_in_period

        contractual_target_hours = weekly_standard * contractual_fte * weeks_in_period
        actual_hours = float(employee_hours.get(target.tier_id, 0.0))
        actual_fte = actual_hours / max(weekly_standard * weeks_in_period, 1e-9)
        gap_fte = round(max(0.0, contractual_fte - actual_fte), 4)

        results.append(
            CoverageTierResult(
                tier_id=target.tier_id,
                label=target.label,
                target_fte=round(contractual_fte, 4),
                actual_fte=round(actual_fte, 4),
                gap_fte=gap_fte,
                target_hours=round(contractual_target_hours, 2),
                actual_hours=round(actual_hours, 2),
                period_target_hours=round(period_target_hours, 2),
                is_impossible=target.tier_id in impossible_tier_ids,
            )
        )
    return tuple(results)


def compute_coverage_success_rate_pct(results: Sequence[CoverageTierResult]) -> float:
    if not results:
        return 100.0
    actionable = [result for result in results if not result.is_impossible]
    if not actionable:
        return 100.0
    return round(
        100.0 * sum(result.success_ratio for result in actionable) / len(actionable),
        2,
    )


def is_schedule_coverage_complete(
    *,
    unfilled_coverage_gaps: int,
    tier_results: Sequence[CoverageTierResult],
) -> bool:
    if unfilled_coverage_gaps > 0:
        return False
    return all(
        result.meets_target or result.is_impossible for result in tier_results
    )


def assess_impossible_coverage_slots(
    *,
    employees: Sequence[EmployeeProfile],
    shift_templates: Mapping[str, ShiftTemplateInfo],
    shift_required_qualifications: Mapping[str, Set[str]],
    period_start: date,
    period_end: date,
    weeks_in_period: int,
    rules: JurisdictionRules,
) -> Tuple[Set[Tuple[date, str]], Set[str]]:
    """
    Detect shift slots and roster tiers that cannot mathematically meet coverage targets.

    Returns (impossible_slot_keys, impossible_tier_ids).
    """

    impossible_slots: Set[Tuple[date, str]] = set()
    impossible_tiers: Set[str] = set()

    slot_hours_by_qual: Dict[frozenset[str], float] = {}
    slot_count_by_qual: Dict[frozenset[str], int] = {}

    for assignment_date in _daterange(period_start, period_end):
        for shift_id, template in shift_templates.items():
            required = shift_required_qualifications.get(shift_id, set())
            qual_key = frozenset(required)
            hours = template.duration_minutes / 60.0
            slot_hours_by_qual[qual_key] = slot_hours_by_qual.get(qual_key, 0.0) + hours
            slot_count_by_qual[qual_key] = slot_count_by_qual.get(qual_key, 0) + 1

            qualified = [
                emp
                for emp in employees
                if not required or bool(emp.qualification_ids & required)
            ]
            if not qualified:
                impossible_slots.add((assignment_date, shift_id))
                continue

    weekly_standard = rules.standard_hours_per_week_at_1_0_fte
    max_period_hours = weekly_standard * weeks_in_period

    for qual_key, required_hours in slot_hours_by_qual.items():
        if not qual_key:
            continue
        qualified = [
            emp for emp in employees if bool(emp.qualification_ids & set(qual_key))
        ]
        if not qualified:
            for assignment_date in _daterange(period_start, period_end):
                for shift_id, required in shift_required_qualifications.items():
                    if frozenset(required) == qual_key:
                        impossible_slots.add((assignment_date, shift_id))
            for emp in employees:
                if emp.qualification_ids & set(qual_key):
                    impossible_tiers.add(emp.id)
            continue

        capacity_hours = sum(emp.fte * max_period_hours for emp in qualified)
        if capacity_hours + 1e-9 < required_hours:
            for assignment_date in _daterange(period_start, period_end):
                for shift_id, required in shift_required_qualifications.items():
                    if frozenset(required) == qual_key:
                        impossible_slots.add((assignment_date, shift_id))
            for emp in qualified:
                impossible_tiers.add(emp.id)

    return impossible_slots, impossible_tiers


def coverage_deficit_rank(
    state_total_hours: float,
    period_target_hours: float,
) -> float:
    if period_target_hours <= 0:
        return 0.0
    return max(0.0, (period_target_hours - state_total_hours) / period_target_hours)


def coverage_priority_key(
    profile: EmployeeProfile,
    state_total_hours: float,
    period_target_hours: float,
) -> Tuple[float, float, float, float]:
    deficit = coverage_deficit_rank(state_total_hours, period_target_hours)
    overload = max(0.0, state_total_hours - period_target_hours)
    seniority, fte, wage = (
        -profile.seniority_hours,
        profile.fte,
        profile.base_hourly_rate,
    )
    return (-deficit, overload, seniority, fte, wage)
