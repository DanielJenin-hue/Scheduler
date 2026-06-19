from __future__ import annotations

import math
import os
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from lab_scheduler.compliance.jurisdictions import JurisdictionRules
from lab_scheduler.engine.demand import infer_qual_code, WEEKEND_CLINICAL_MAX_PER_QUAL
from lab_scheduler.models.employee import (
    allowed_shift_codes_for_role_contract,
    normalize_contract_line_type,
)
from lab_scheduler.scheduling.contract_payroll import HOURS_PER_SHIFT
from lab_scheduler.scheduling.portage_equity_targets import (
    portage_alt_shift_target,
    portage_contract_shift_count,
    portage_weekend_shift_target,
    PORTAGE_ALT_SHIFT_DENSITY,
    PORTAGE_FULLTIME_PERIOD_HOURS,
)
from lab_scheduler.scheduling.fairness_thresholds import (
    DEFAULT_FAIRNESS_THRESHOLDS,
    FairnessThresholds,
    WEIGHT_EVENING_CLUSTER,
    WEIGHT_POST_NIGHT_RECOVERY,
)
from lab_scheduler.scheduling.portage_template import parse_vacant_portage_line
from lab_scheduler.scheduling.profiles import EmployeeProfile
from lab_scheduler.time.workweek import workweek_for

try:
    from ortools.sat.python import cp_model
except ImportError:  # pragma: no cover - exercised via skip guards in tests
    cp_model = None  # type: ignore[assignment,misc]

BAND_TOKENS: Tuple[str, ...] = ("D", "E", "N")
BAND_TO_SHIFT_CODE: Dict[str, str] = {
    "D": "MORNING",
    "E": "EVENING",
    "N": "NIGHT",
}
SHIFT_CODE_TO_BAND: Dict[str, str] = {
    "MORNING": "D",
    "EVENING": "E",
    "NIGHT": "N",
}

# Portage fatigue hard caps (solver-enforced; replaces post-generation shuffles).
PORTAGE_MAX_CONSECUTIVE_WORK_DAYS = 6
PORTAGE_MAX_CONSECUTIVE_NIGHTS = 4

# Soft objective weights (maximize score == minimize weighted penalties).
#
# Two-tier hierarchy (validated in tests):
#   Tier A — contract / coverage: hour deficit, PT payroll/catalog caps.
#   Tier B — layout preferences: alt-shift spread, weekday smoothing, fairness slack.
# Every Tier-A weight must be >= CONTRACT_COVERAGE_WEIGHT_MULTIPLIER × max Tier-B weight
# so the solver accepts reference layouts with uneven alt bands before chasing drift.
CONTRACT_COVERAGE_WEIGHT_MULTIPLIER = 10
WEIGHT_PREFERENCE_ALT_SHIFT_EQUITY = 400
WEIGHT_PREFERENCE_ALT_SHIFT_UNFAIRNESS = 500
WEIGHT_PREFERENCE_PT_ALT_BAND_SLACK = 300
WEIGHT_PREFERENCE_DEFICIT_VARIANCE = 600
WEIGHT_PREFERENCE_WEEKDAY_DAY_SMOOTH = 400
WEIGHT_PREFERENCE_WEEKEND_SURPLUS = 200
WEIGHT_PREFERENCE_HOUR_SURPLUS = 200
WEIGHT_PREFERENCE_N_TO_D_FATIGUE = 75  # legacy; N→D is a hard constraint
WEIGHT_MAX_PREFERENCE = max(
    WEIGHT_PREFERENCE_ALT_SHIFT_EQUITY,
    WEIGHT_PREFERENCE_ALT_SHIFT_UNFAIRNESS,
    WEIGHT_PREFERENCE_PT_ALT_BAND_SLACK,
    WEIGHT_PREFERENCE_DEFICIT_VARIANCE,
    WEIGHT_PREFERENCE_WEEKDAY_DAY_SMOOTH,
    WEIGHT_PREFERENCE_WEEKEND_SURPLUS,
    WEIGHT_PREFERENCE_HOUR_SURPLUS,
    WEIGHT_PREFERENCE_N_TO_D_FATIGUE,
)
WEIGHT_MIN_CONTRACT_COVERAGE = WEIGHT_MAX_PREFERENCE * CONTRACT_COVERAGE_WEIGHT_MULTIPLIER

FULLTIME_PERIOD_TARGET_HOURS = int(PORTAGE_FULLTIME_PERIOD_HOURS)
WEIGHT_HOUR_SURPLUS = WEIGHT_PREFERENCE_HOUR_SURPLUS
WEIGHT_HOUR_DEFICIT = 400_000
WEIGHT_HOUR_DEFICIT_FULLTIME = 500_000
WEIGHT_COVERAGE_SHORTFALL = 500_000
WEIGHT_ALT_SHIFT_EQUITY = WEIGHT_PREFERENCE_ALT_SHIFT_EQUITY
FULLTIME_ALT_SHIFT_TARGET_MIN = portage_alt_shift_target(FULLTIME_PERIOD_TARGET_HOURS)
FULLTIME_ALT_SHIFT_TARGET_MAX = FULLTIME_ALT_SHIFT_TARGET_MIN
WEIGHT_ALT_SHIFT_UNFAIRNESS = WEIGHT_PREFERENCE_ALT_SHIFT_UNFAIRNESS
PARTTIME_ALT_SHIFT_DENSITY_CEILING = PORTAGE_ALT_SHIFT_DENSITY
PARTTIME_ALT_SHIFT_DENSITY_FLOOR = PORTAGE_ALT_SHIFT_DENSITY
WEIGHT_PT_ALT_SHIFT_CEILING_SLACK = WEIGHT_PREFERENCE_PT_ALT_BAND_SLACK
# Part-time vacant: payroll/catalog surplus must cost more than FT hour deficit relief.
WEIGHT_PT_PAYROLL_SURPLUS = 600_000
PT_CATALOG_SURPLUS_GRACE_HOURS = 8
WEIGHT_PT_CATALOG_SURPLUS_GRACE = 20_000
WEIGHT_PT_CATALOG_SURPLUS_EXCESS_MID = 650_000
WEIGHT_PT_CATALOG_SURPLUS_EXCESS_SEVERE = 800_000
# Retained for API compatibility; vacant-line open cells are optional off-days.
WEIGHT_UNFILLED_ESCALATED = 50_000
WEIGHT_DEFICIT_VARIANCE = WEIGHT_PREFERENCE_DEFICIT_VARIANCE
# Below hour deficit so N→D is last resort when filling is still legal.
WEIGHT_N_TO_D_FATIGUE = WEIGHT_PREFERENCE_N_TO_D_FATIGUE

# Retained for API compatibility; baseline coverage shortfall is a Tier-A objective term.
WEIGHT_WEEKEND_BASELINE_SURPLUS = WEIGHT_PREFERENCE_WEEKEND_SURPLUS
WEIGHT_WEEKDAY_SURPLUS_SMOOTH = WEIGHT_PREFERENCE_WEEKDAY_DAY_SMOOTH

# Portage macro board: hard daily band totals (pool-wide, all roster lines).
DAILY_EVENING_CAP = 2
DAILY_NIGHT_CAP = 2
WEEKEND_DAY_CAP = 2
FULLTIME_ACTIVE_WEEKENDS_REQUIRED = portage_weekend_shift_target(FULLTIME_PERIOD_TARGET_HOURS) // 2
PARTTIME_ACTIVE_WEEKENDS_MAX = 4
CLINICAL_FLOOR_QUAL_CODES: Tuple[str, ...] = ("MLT", "MLA")
CLINICAL_FLOOR_SEATS_PER_QUAL = 1

# Legacy alias kept for callers/tests that referenced the combined hour weight.
WEIGHT_HOUR_DEVIATION = WEIGHT_HOUR_DEFICIT


@dataclass(frozen=True, slots=True)
class CpSatFillResult:
    assignments: Tuple["PlannedAssignment", ...]
    status: str
    objective_value: int
    hour_deviation_total: int
    coverage_shortfall_total: int
    n_to_d_fatigue_total: int = 0
    unfilled_escalated_total: int = 0
    deficit_variance_total: int = 0
    alt_shift_spread_total: int = 0
    alt_shift_unfairness_total: int = 0
    pt_alt_band_slack_total: int = 0
    weekday_surplus_spread_total: int = 0
    weekend_baseline_surplus_total: int = 0
    fillable_slot_count: int = 0
    shift_equity_metrics: Dict[str, object] = field(default_factory=dict)
    evening_cluster_slack_total: int = 0
    post_night_recovery_slack_total: int = 0
    fairness_penalty_total: int = 0


def is_vacant_portage_line(full_name: str) -> bool:
    return parse_vacant_portage_line(full_name) is not None


def vacant_portage_employee_ids(employees: Sequence[EmployeeProfile]) -> Set[str]:
    return {
        employee.id
        for employee in employees
        if is_vacant_portage_line(employee.full_name)
    }


def _open_vacant_fillable_slots(
    employees: Sequence[EmployeeProfile],
    period_dates: Sequence[date],
    *,
    blocked: Mapping[str, Set[date]],
    occupied: Optional[Mapping[Tuple[str, date], str]] = None,
) -> List[Tuple[str, date]]:
    """One decision opportunity per vacant line per open calendar day (availability only)."""

    vacant_ids = vacant_portage_employee_ids(employees)
    reserved = occupied or {}
    fillable_slots: List[Tuple[str, date]] = []
    for employee in employees:
        if employee.id not in vacant_ids:
            continue
        for assignment_date in period_dates:
            if assignment_date in blocked.get(employee.id, set()):
                continue
            if (employee.id, assignment_date) in reserved:
                continue
            fillable_slots.append((employee.id, assignment_date))
    return fillable_slots


def band_to_shift_code(band: str) -> str:
    token = band.strip().upper()
    if token == "M":
        token = "D"
    return BAND_TO_SHIFT_CODE[token]


def shift_code_to_band(shift_code: str) -> Optional[str]:
    return SHIFT_CODE_TO_BAND.get(str(shift_code or "").strip().upper())


def _require_ortools() -> None:
    if cp_model is None:
        raise ImportError(
            "Google OR-Tools is required for CP-SAT scheduling. "
            "Install with: pip install ortools"
        )


def _daterange(start: date, end_inclusive: date) -> List[date]:
    days: List[date] = []
    current = start
    while current <= end_inclusive:
        days.append(current)
        current += timedelta(days=1)
    return days


def _weekend_sat_sun_pairs(period_dates: Sequence[date]) -> List[Tuple[date, date]]:
    """Saturday/Sunday pairs fully contained in ``period_dates``."""

    date_set = set(period_dates)
    pairs: List[Tuple[date, date]] = []
    for assignment_date in period_dates:
        if assignment_date.weekday() != 5:
            continue
        sunday = assignment_date + timedelta(days=1)
        if sunday in date_set:
            pairs.append((assignment_date, sunday))
    return pairs


def _is_fulltime_target_hours(target_hours: float) -> bool:
    return target_hours >= FULLTIME_PERIOD_TARGET_HOURS - 8


def _is_parttime_target_hours(target_hours: float) -> bool:
    return not _is_fulltime_target_hours(target_hours)


def _add_vacant_payroll_hour_ceiling(
    model: cp_model.CpModel,
    *,
    employee: EmployeeProfile,
    worked: cp_model.IntVar,
    payroll_target_hours: int,
    vacant_ids: Set[str],
    catalog_target_hours: Mapping[str, float] | None = None,
    payroll_targets: Mapping[str, float] | None = None,
) -> None:
    """Hard ceiling: vacant Portage lines never exceed payroll/catalog assignment cap."""

    if employee.id not in vacant_ids:
        return
    if not is_vacant_portage_line(employee.full_name):
        return

    from lab_scheduler.scheduling.contract_payroll import vacant_assignment_hour_ceiling

    payroll_map = payroll_targets or {employee.id: float(payroll_target_hours)}
    catalog_map = catalog_target_hours
    ceiling = vacant_assignment_hour_ceiling(employee, payroll_map, catalog_map)
    if ceiling <= 0:
        return
    model.Add(worked <= int(round(ceiling)))


def _band_assignment_state(
    employee_id: str,
    assignment_date: date,
    band: str,
    *,
    x: Mapping[Tuple[str, date, str], cp_model.IntVar],
    fixed: Mapping[Tuple[str, date], str],
) -> Tuple[Optional[cp_model.IntVar], int]:
    """Return decision var and/or fixed 0/1 for whether ``band`` is assigned on ``assignment_date``."""

    fixed_band = fixed.get((employee_id, assignment_date))
    if fixed_band is not None:
        return None, 1 if fixed_band == band else 0
    decision_var = x.get((employee_id, assignment_date, band))
    if decision_var is not None:
        return decision_var, 0
    return None, 0


def _add_portage_weekend_mirror_rule(
    model: cp_model.CpModel,
    *,
    employees: Sequence[EmployeeProfile],
    weekend_pairs: Sequence[Tuple[date, date]],
    qual_lookup: Mapping[str, str],
    x: Mapping[Tuple[str, date, str], cp_model.IntVar],
    fixed: Mapping[Tuple[str, date], str],
) -> None:
    """Hard mirror: Sat_D == Sun_D, Sat_E == Sun_E, Sat_N == Sun_N for every employee."""

    for employee in employees:
        allowed = _allowed_bands_for_employee(employee, qual_lookup)
        for saturday, sunday in weekend_pairs:
            for band in BAND_TOKENS:
                if band not in allowed:
                    continue
                sat_var, sat_const = _band_assignment_state(
                    employee.id,
                    saturday,
                    band,
                    x=x,
                    fixed=fixed,
                )
                sun_var, sun_const = _band_assignment_state(
                    employee.id,
                    sunday,
                    band,
                    x=x,
                    fixed=fixed,
                )
                if sat_var is None and sun_var is None:
                    if sat_const != sun_const:
                        model.Add(0 == 1)
                    continue
                if sat_var is None:
                    model.Add(sun_var == sat_const)
                    continue
                if sun_var is None:
                    model.Add(sat_var == sun_const)
                    continue
                model.Add(sat_var == sun_var)


def _saturday_work_indicator(
    model: cp_model.CpModel,
    employee_id: str,
    saturday: date,
    *,
    allowed_bands: Set[str],
    x: Mapping[Tuple[str, date, str], cp_model.IntVar],
    fixed: Mapping[Tuple[str, date], str],
) -> Tuple[Optional[cp_model.IntVar], int]:
    """Return (var, constant) where var+constant is 1 iff any allowed band is worked on Saturday."""

    fixed_band = fixed.get((employee_id, saturday))
    if fixed_band is not None:
        return None, 1 if fixed_band in allowed_bands else 0

    terms: List[cp_model.IntVar] = []
    for band in BAND_TOKENS:
        if band not in allowed_bands:
            continue
        decision_var = x.get((employee_id, saturday, band))
        if decision_var is not None:
            terms.append(decision_var)
    if not terms:
        return None, 0

    worked = model.NewIntVar(0, 1, f"active_wknd_{employee_id}_{saturday.isoformat()}")
    model.Add(worked == sum(terms))
    return worked, 0


def _add_portage_weekend_active_caps(
    model: cp_model.CpModel,
    *,
    employees: Sequence[EmployeeProfile],
    period_dates: Sequence[date],
    employee_target_hours: Mapping[str, float],
    qual_lookup: Mapping[str, str],
    x: Mapping[Tuple[str, date, str], cp_model.IntVar],
    fixed: Mapping[Tuple[str, date], str],
) -> None:
    """
    Require feasible weekend shift days per line (qual+contract pool caps applied).

    Sat/Sun mirror rule keeps weekend totals even. When frozen template cells fix a
    different weekend count, the hard equality is skipped and post-pass equity rebalances.
    """

    from lab_scheduler.scheduling.portage_feasibility import (
        _qual_contract_groups,
        portage_qual_contract_weekend_target_map,
    )

    max_weekend_shifts = sum(1 for day in period_dates if day.weekday() >= 5)
    if max_weekend_shifts <= 0:
        return

    weekend_day_count = max_weekend_shifts
    weekend_targets: Dict[str, int] = {}
    for (qual_code, _contract), members in _qual_contract_groups(
        employees, employee_target_hours, qual_lookup
    ).items():
        weekend_targets.update(
            portage_qual_contract_weekend_target_map(
                members,
                qual_code=qual_code,
                weekend_day_count=weekend_day_count,
            )
        )

    for employee in employees:
        target_hours = float(employee_target_hours.get(employee.id, 0.0))
        if target_hours <= 0:
            continue
        target_weekend_shifts = weekend_targets.get(employee.id, 0)
        if target_weekend_shifts <= 0:
            continue
        allowed = _allowed_bands_for_employee(employee, qual_lookup)
        weekend_terms: List[cp_model.IntVar] = []
        weekend_constant = 0
        for assignment_date in period_dates:
            if assignment_date.weekday() < 5:
                continue
            day_terms: List[cp_model.IntVar] = []
            fixed_band = fixed.get((employee.id, assignment_date))
            if fixed_band is not None:
                if fixed_band in allowed:
                    weekend_constant += 1
                continue
            for band in BAND_TOKENS:
                if band not in allowed:
                    continue
                decision_var = x.get((employee.id, assignment_date, band))
                if decision_var is not None:
                    day_terms.append(decision_var)
            if not day_terms:
                continue
            if len(day_terms) == 1:
                weekend_terms.append(day_terms[0])
                continue
            worked_day = model.NewIntVar(
                0,
                1,
                f"wknd_day_{employee.id}_{assignment_date.isoformat()}",
            )
            model.Add(worked_day == sum(day_terms))
            weekend_terms.append(worked_day)

        if weekend_terms:
            weekend_total = model.NewIntVar(
                0,
                max_weekend_shifts,
                f"wknd_total_{employee.id}",
            )
            model.Add(weekend_total == sum(weekend_terms) + weekend_constant)
            model.Add(weekend_total == target_weekend_shifts)
        elif weekend_constant != target_weekend_shifts:
            continue


def _shift_id_for_code(
    shift_code: str,
    shift_templates: Mapping[str, object],
) -> Optional[str]:
    for shift_id, template in shift_templates.items():
        code = getattr(template, "code", None)
        if code == shift_code:
            return str(shift_id)
    return None


def _allowed_bands_for_employee(
    employee: EmployeeProfile,
    qual_codes: Mapping[str, str],
) -> Set[str]:
    qual = qual_codes.get(employee.id) or infer_qual_code(employee)
    contract = normalize_contract_line_type(employee.contract_line_type or "") or "D/E"
    allowed_codes = allowed_shift_codes_for_role_contract(contract, qual_code=qual)
    bands = {SHIFT_CODE_TO_BAND[code] for code in allowed_codes if code in SHIFT_CODE_TO_BAND}
    return bands or set(BAND_TOKENS)


def _fixed_band_map(
    fixed_assignments: Sequence[object],
    shift_templates: Mapping[str, object],
) -> Dict[Tuple[str, date], str]:
    fixed: Dict[Tuple[str, date], str] = {}
    for assignment in fixed_assignments:
        employee_id = str(getattr(assignment, "employee_id"))
        assignment_date = getattr(assignment, "assignment_date")
        if not isinstance(assignment_date, date):
            assignment_date = date.fromisoformat(str(assignment_date))
        template = shift_templates[str(getattr(assignment, "shift_template_id"))]
        band = shift_code_to_band(str(getattr(template, "code", "")))
        if band is None:
            continue
        fixed[(employee_id, assignment_date)] = band
    return fixed


def _fixed_band_map_for_vacant_fill(
    fixed_assignments: Sequence[object],
    shift_templates: Mapping[str, object],
    employees: Sequence[EmployeeProfile],
    employee_target_hours: Mapping[str, float],
    *,
    freeze_master_template_stamps: bool,
) -> Dict[Tuple[str, date], str]:
    """Immutable vacant-line cells for CP-SAT (frozen master-template weekday stamps)."""

    fixed = _fixed_band_map(fixed_assignments, shift_templates)
    vacant_ids = vacant_portage_employee_ids(employees)
    if not freeze_master_template_stamps:
        return {key: band for key, band in fixed.items() if key[0] not in vacant_ids}
    frozen_keys: Set[Tuple[str, date]] = set()
    for assignment in fixed_assignments:
        if not getattr(assignment, "master_template_frozen", False):
            continue
        employee_id = getattr(assignment, "employee_id", None)
        assignment_date = getattr(assignment, "assignment_date", None)
        if employee_id is None or assignment_date is None:
            continue
        if employee_id not in vacant_ids:
            continue
        band = fixed.get((employee_id, assignment_date))
        if band is not None:
            frozen_keys.add((employee_id, assignment_date))
    if frozen_keys:
        return {
            key: band
            for key, band in fixed.items()
            if key[0] not in vacant_ids or key in frozen_keys
        }
    fulltime_vacant = {
        employee.id
        for employee in employees
        if employee.id in vacant_ids
        and _is_fulltime_target_hours(float(employee_target_hours.get(employee.id, 0.0)))
    }
    return {
        key: band
        for key, band in fixed.items()
        if key[0] not in vacant_ids or key[0] in fulltime_vacant
    }


def _hour_deficit_weight(target_hours: int) -> int:
    if target_hours >= FULLTIME_PERIOD_TARGET_HOURS - 8:
        return WEIGHT_HOUR_DEFICIT_FULLTIME
    return WEIGHT_HOUR_DEFICIT


def _is_parttime_vacant_compliance_line(
    employee: EmployeeProfile,
    *,
    vacant_ids: Set[str],
    employee_target_hours: Mapping[str, float],
    compliance_first: bool,
) -> bool:
    if not compliance_first or employee.id not in vacant_ids:
        return False
    if not is_vacant_portage_line(employee.full_name):
        return False
    return not _is_fulltime_target_hours(
        float(employee_target_hours.get(employee.id, 0.0))
    )


def _add_parttime_catalog_surplus_penalty_vars(
    model: cp_model.CpModel,
    *,
    worked: cp_model.IntVar,
    catalog_target_hours: int,
    max_period_hours: int,
    employee_id: str,
) -> Tuple[List[cp_model.IntVar], List[cp_model.IntVar], List[cp_model.IntVar]]:
    """
    Piecewise catalog surplus for PT vacant lines: low weight 0–8h, steep 8–16h and >16h.

    Payroll ``diff_up <= 8`` hard slack stays intact; this penalizes catalog over-scheduling
    that persist/union treat as contract risk (e.g. Line 07 +48h anomalies).
    """

    grace_cap = int(PT_CATALOG_SURPLUS_GRACE_HOURS)
    cat_diff_up = model.NewIntVar(0, max_period_hours, f"cat_diff_up_{employee_id}")
    cat_diff_down = model.NewIntVar(0, max_period_hours, f"cat_diff_down_{employee_id}")
    model.Add(worked - catalog_target_hours == cat_diff_up - cat_diff_down)

    grace = model.NewIntVar(0, grace_cap, f"pt_cat_grace_{employee_id}")
    excess_mid = model.NewIntVar(0, grace_cap, f"pt_cat_excess_mid_{employee_id}")
    excess_severe = model.NewIntVar(0, max_period_hours, f"pt_cat_excess_sev_{employee_id}")
    model.Add(grace + excess_mid + excess_severe == cat_diff_up)
    return [grace], [excess_mid], [excess_severe]


def _vacant_line_type_key(
    employee: EmployeeProfile,
    target_hours: float,
) -> Optional[Tuple[str, str, int]]:
    parsed = parse_vacant_portage_line(employee.full_name)
    if parsed is None:
        return None
    role, contract, _line = parsed
    return (role, contract, int(round(target_hours)))


def _vacant_line_type_groups(
    employees: Sequence[EmployeeProfile],
    employee_target_hours: Mapping[str, float],
) -> Dict[Tuple[str, str, int], List[str]]:
    groups: Dict[Tuple[str, str, int], List[str]] = defaultdict(list)
    for employee in employees:
        if not is_vacant_portage_line(employee.full_name):
            continue
        target_hours = float(employee_target_hours.get(employee.id, 0.0))
        line_key = _vacant_line_type_key(employee, target_hours)
        if line_key is None:
            continue
        groups[line_key].append(employee.id)
    return groups


def _fixed_alt_shift_count_by_employee(
    fixed: Mapping[Tuple[str, date], str],
) -> Dict[str, int]:
    counts: Dict[str, int] = defaultdict(int)
    for (employee_id, _day), band in fixed.items():
        if band in ("E", "N"):
            counts[employee_id] += 1
    return dict(counts)


def _build_employee_alt_shift_totals(
    model: cp_model.CpModel,
    *,
    employees: Sequence[EmployeeProfile],
    period_dates: Sequence[date],
    x: Mapping[Tuple[str, date, str], cp_model.IntVar],
    fixed: Mapping[Tuple[str, date], str],
    employee_ids: Set[str],
) -> Tuple[Dict[str, cp_model.IntVar], Dict[str, int]]:
    """Return decision alt totals (E+N) and fixed-only constants for selected employees."""

    fixed_alt = _fixed_alt_shift_count_by_employee(fixed)
    max_alt_shifts = len(period_dates)
    alt_total_by_employee: Dict[str, cp_model.IntVar] = {}
    alt_total_constant_by_employee: Dict[str, int] = {}

    for employee in employees:
        if employee.id not in employee_ids:
            continue
        alt_terms: List[cp_model.LinearExpr] = []
        for assignment_date in period_dates:
            for band in ("E", "N"):
                var = x.get((employee.id, assignment_date, band))
                if var is not None:
                    alt_terms.append(var)
        fixed_alt_count = fixed_alt.get(employee.id, 0)
        if alt_terms:
            alt_total = model.NewIntVar(0, max_alt_shifts, f"alt_total_{employee.id}")
            model.Add(alt_total == sum(alt_terms) + fixed_alt_count)
            alt_total_by_employee[employee.id] = alt_total
        else:
            alt_total_constant_by_employee[employee.id] = fixed_alt_count

    return alt_total_by_employee, alt_total_constant_by_employee


def _add_alt_shift_equity_objective(
    model: cp_model.CpModel,
    *,
    employees: Sequence[EmployeeProfile],
    employee_target_hours: Mapping[str, float],
    alt_total_by_employee: Mapping[str, cp_model.IntVar],
    alt_total_constant_by_employee: Mapping[str, int],
    period_dates: Sequence[date],
) -> List[cp_model.IntVar]:
    """Minimize E+N spread across identical vacant lines (role, contract, target_hours)."""

    line_type_groups = _vacant_line_type_groups(employees, employee_target_hours)
    max_alt_shifts = len(period_dates)
    spread_vars: List[cp_model.IntVar] = []
    for member_ids in line_type_groups.values():
        if len(member_ids) < 2:
            continue
        max_alt = model.NewIntVar(0, max_alt_shifts, f"max_alt_{member_ids[0]}")
        min_alt = model.NewIntVar(0, max_alt_shifts, f"min_alt_{member_ids[0]}")
        for employee_id in member_ids:
            if employee_id in alt_total_by_employee:
                model.Add(max_alt >= alt_total_by_employee[employee_id])
                model.Add(min_alt <= alt_total_by_employee[employee_id])
            else:
                alt_count = alt_total_constant_by_employee.get(employee_id, 0)
                model.Add(max_alt >= alt_count)
                model.Add(min_alt <= alt_count)
        spread = model.NewIntVar(0, max_alt_shifts, f"alt_spread_{member_ids[0]}")
        model.Add(spread == max_alt - min_alt)
        spread_vars.append(spread)

    return spread_vars


def _add_fulltime_alt_shift_range_objective(
    model: cp_model.CpModel,
    *,
    employees: Sequence[EmployeeProfile],
    employee_target_hours: Mapping[str, float],
    alt_total_by_employee: Mapping[str, cp_model.IntVar],
    alt_total_constant_by_employee: Mapping[str, int],
) -> Tuple[List[cp_model.IntVar], int]:
    """
    Soft 8–12 alternate-shift band for full-time vacant lines (target_hours >= 312).

    Returns slack vars for the objective and any fixed-only unfairness constant.
    """

    max_alt_shifts = FULLTIME_ALT_SHIFT_TARGET_MAX + FULLTIME_ALT_SHIFT_TARGET_MIN
    slack_vars: List[cp_model.IntVar] = []
    unfairness_constant = 0

    for employee in employees:
        if not is_vacant_portage_line(employee.full_name):
            continue
        target_hours = float(employee_target_hours.get(employee.id, 0.0))
        if not _is_fulltime_target_hours(target_hours):
            continue

        alt_total = alt_total_by_employee.get(employee.id)
        if alt_total is None:
            fixed_alt = alt_total_constant_by_employee.get(employee.id, 0)
            if fixed_alt < FULLTIME_ALT_SHIFT_TARGET_MIN:
                unfairness_constant += (
                    FULLTIME_ALT_SHIFT_TARGET_MIN - fixed_alt
                ) * WEIGHT_ALT_SHIFT_UNFAIRNESS
            elif fixed_alt > FULLTIME_ALT_SHIFT_TARGET_MAX:
                unfairness_constant += (
                    fixed_alt - FULLTIME_ALT_SHIFT_TARGET_MAX
                ) * WEIGHT_ALT_SHIFT_UNFAIRNESS
            continue

        under_slack = model.NewIntVar(
            0,
            FULLTIME_ALT_SHIFT_TARGET_MIN,
            f"alt_under_{employee.id}",
        )
        over_slack = model.NewIntVar(
            0,
            max_alt_shifts,
            f"alt_over_{employee.id}",
        )
        model.Add(
            under_slack >= FULLTIME_ALT_SHIFT_TARGET_MIN - alt_total
        )
        model.Add(
            over_slack >= alt_total - FULLTIME_ALT_SHIFT_TARGET_MAX
        )
        slack_vars.extend([under_slack, over_slack])

    return slack_vars, unfairness_constant


def _parttime_contract_shift_count(target_hours: float) -> int:
    return portage_contract_shift_count(target_hours)


def _parttime_max_allowed_alt_shifts(target_hours: float) -> int:
    return portage_alt_shift_target(target_hours)


def _parttime_min_allowed_alt_shifts(target_hours: float) -> int:
    return portage_alt_shift_target(target_hours)


def _parttime_allowed_alt_band(target_hours: float) -> Tuple[int, int]:
    target = portage_alt_shift_target(target_hours)
    return target, target


def _add_parttime_alt_shift_band_objective(
    model: cp_model.CpModel,
    *,
    employees: Sequence[EmployeeProfile],
    employee_target_hours: Mapping[str, float],
    alt_total_by_employee: Mapping[str, cp_model.IntVar],
    alt_total_constant_by_employee: Mapping[str, int],
    period_dates: Sequence[date],
) -> Tuple[List[cp_model.IntVar], int]:
    """
    Soft 10–25% alternate-shift density band for part-time vacant lines (target_hours < 312).
    """

    max_alt_shifts = len(period_dates)
    slack_vars: List[cp_model.IntVar] = []
    band_constant = 0

    for employee in employees:
        if not is_vacant_portage_line(employee.full_name):
            continue
        target_hours = float(employee_target_hours.get(employee.id, 0.0))
        if _is_fulltime_target_hours(target_hours):
            continue

        min_allowed_alt, max_allowed_alt = _parttime_allowed_alt_band(target_hours)
        alt_total = alt_total_by_employee.get(employee.id)
        if alt_total is None:
            fixed_alt = alt_total_constant_by_employee.get(employee.id, 0)
            if fixed_alt > max_allowed_alt:
                band_constant += (
                    fixed_alt - max_allowed_alt
                ) * WEIGHT_PT_ALT_SHIFT_CEILING_SLACK
            if fixed_alt < min_allowed_alt:
                band_constant += (
                    min_allowed_alt - fixed_alt
                ) * WEIGHT_PT_ALT_SHIFT_CEILING_SLACK
            continue

        pt_over_ceiling_slack = model.NewIntVar(
            0,
            max_alt_shifts,
            f"pt_over_ceiling_{employee.id}",
        )
        pt_under_floor_slack = model.NewIntVar(
            0,
            max_alt_shifts,
            f"pt_under_floor_{employee.id}",
        )
        model.Add(pt_over_ceiling_slack >= alt_total - max_allowed_alt)
        model.Add(pt_under_floor_slack >= min_allowed_alt - alt_total)
        slack_vars.extend([pt_over_ceiling_slack, pt_under_floor_slack])

    return slack_vars, band_constant


def _fixed_hours_by_employee(
    fixed: Mapping[Tuple[str, date], str],
    *,
    hours_per_shift: float = HOURS_PER_SHIFT,
) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for (employee_id, _day), _band in fixed.items():
        counts[employee_id] = counts.get(employee_id, 0) + 1
    return {
        employee_id: int(round(count * hours_per_shift))
        for employee_id, count in counts.items()
    }


def _work_day_terms_for_employee_date(
    employee_id: str,
    assignment_date: date,
    *,
    x: Mapping[Tuple[str, date, str], cp_model.IntVar],
    fixed: Mapping[Tuple[str, date], str],
) -> Tuple[List[cp_model.IntVar], int]:
    if (employee_id, assignment_date) in fixed:
        return [], 1
    return [
        x[(employee_id, assignment_date, band)]
        for band in BAND_TOKENS
        if (employee_id, assignment_date, band) in x
    ], 0


def _night_terms_for_employee_date(
    employee_id: str,
    assignment_date: date,
    *,
    x: Mapping[Tuple[str, date, str], cp_model.IntVar],
    fixed: Mapping[Tuple[str, date], str],
) -> Tuple[List[cp_model.IntVar], int]:
    if fixed.get((employee_id, assignment_date)) == "N":
        return [], 1
    night_var = x.get((employee_id, assignment_date, "N"))
    if night_var is not None:
        return [night_var], 0
    return [], 0


def _add_portage_consecutive_work_limit(
    model: cp_model.CpModel,
    *,
    employees: Sequence[EmployeeProfile],
    period_dates: Sequence[date],
    x: Mapping[Tuple[str, date, str], cp_model.IntVar],
    fixed: Mapping[Tuple[str, date], str],
    max_consecutive_work_days: int = PORTAGE_MAX_CONSECUTIVE_WORK_DAYS,
) -> None:
    """Hard cap: no more than ``max_consecutive_work_days`` worked days in any run."""

    window_size = max_consecutive_work_days + 1
    if len(period_dates) < window_size:
        return
    for employee in employees:
        for index in range(len(period_dates) - window_size + 1):
            window_terms: List[cp_model.IntVar] = []
            window_constant = 0
            for assignment_date in period_dates[index : index + window_size]:
                day_terms, day_constant = _work_day_terms_for_employee_date(
                    employee.id,
                    assignment_date,
                    x=x,
                    fixed=fixed,
                )
                window_terms.extend(day_terms)
                window_constant += day_constant
            if window_terms or window_constant:
                model.Add(sum(window_terms) + window_constant <= max_consecutive_work_days)


def _add_portage_consecutive_night_limit(
    model: cp_model.CpModel,
    *,
    employees: Sequence[EmployeeProfile],
    period_dates: Sequence[date],
    x: Mapping[Tuple[str, date, str], cp_model.IntVar],
    fixed: Mapping[Tuple[str, date], str],
    max_consecutive_nights: int = PORTAGE_MAX_CONSECUTIVE_NIGHTS,
) -> None:
    """Hard cap: no more than ``max_consecutive_nights`` night shifts in any calendar run."""

    window_size = max_consecutive_nights + 1
    if len(period_dates) < window_size:
        return
    for employee in employees:
        for index in range(len(period_dates) - window_size + 1):
            window_terms: List[cp_model.IntVar] = []
            window_constant = 0
            for assignment_date in period_dates[index : index + window_size]:
                night_terms, night_constant = _night_terms_for_employee_date(
                    employee.id,
                    assignment_date,
                    x=x,
                    fixed=fixed,
                )
                window_terms.extend(night_terms)
                window_constant += night_constant
            if window_terms or window_constant:
                model.Add(sum(window_terms) + window_constant <= max_consecutive_nights)


def _add_manitoba_work_day_limits(
    model: cp_model.CpModel,
    *,
    rules: JurisdictionRules,
    employees: Sequence[EmployeeProfile],
    period_dates: Sequence[date],
    x: Mapping[Tuple[str, date, str], cp_model.IntVar],
    fixed: Mapping[Tuple[str, date], str],
) -> None:
    """Hard caps aligned with compliance weekly-rest and consecutive-day checks."""

    max_days_per_week = rules.max_work_days_per_work_week
    if max_days_per_week is not None:
        week_starts = {workweek_for(assignment_date).start for assignment_date in period_dates}
        for employee in employees:
            for week_start in sorted(week_starts):
                week_terms: List[cp_model.IntVar] = []
                week_constant = 0
                for offset in range(7):
                    assignment_date = week_start + timedelta(days=offset)
                    if assignment_date not in period_dates:
                        continue
                    day_terms, day_constant = _work_day_terms_for_employee_date(
                        employee.id,
                        assignment_date,
                        x=x,
                        fixed=fixed,
                    )
                    week_terms.extend(day_terms)
                    week_constant += day_constant
                if week_terms or week_constant:
                    model.Add(sum(week_terms) + week_constant <= max_days_per_week)

    max_consecutive = rules.max_consecutive_work_days
    if max_consecutive is not None:
        window_size = max_consecutive + 1
        if len(period_dates) >= window_size:
            for employee in employees:
                for index in range(len(period_dates) - window_size + 1):
                    window_terms: List[cp_model.IntVar] = []
                    window_constant = 0
                    for assignment_date in period_dates[index : index + window_size]:
                        day_terms, day_constant = _work_day_terms_for_employee_date(
                            employee.id,
                            assignment_date,
                            x=x,
                            fixed=fixed,
                        )
                        window_terms.extend(day_terms)
                        window_constant += day_constant
                    if window_terms or window_constant:
                        model.Add(sum(window_terms) + window_constant <= max_consecutive)


def _daily_band_assigned_terms(
    band: str,
    assignment_date: date,
    *,
    employees: Sequence[EmployeeProfile],
    x: Mapping[Tuple[str, date, str], cp_model.IntVar],
    fixed: Mapping[Tuple[str, date], str],
) -> Tuple[List[cp_model.IntVar], int]:
    terms: List[cp_model.IntVar] = []
    constant = 0
    for employee in employees:
        if fixed.get((employee.id, assignment_date)) == band:
            constant += 1
            continue
        var = x.get((employee.id, assignment_date, band))
        if var is not None:
            terms.append(var)
    return terms, constant


def _daily_band_assigned_terms_for_qual(
    band: str,
    assignment_date: date,
    qual_code: str,
    *,
    employees: Sequence[EmployeeProfile],
    qual_lookup: Mapping[str, str],
    x: Mapping[Tuple[str, date, str], cp_model.IntVar],
    fixed: Mapping[Tuple[str, date], str],
) -> Tuple[List[cp_model.IntVar], int]:
    terms: List[cp_model.IntVar] = []
    constant = 0
    for employee in employees:
        if qual_lookup.get(employee.id) != qual_code:
            continue
        if fixed.get((employee.id, assignment_date)) == band:
            constant += 1
            continue
        var = x.get((employee.id, assignment_date, band))
        if var is not None:
            terms.append(var)
    return terms, constant


def _add_fixed_aware_pool_cap(
    model: cp_model.CpModel,
    *,
    terms: Sequence[cp_model.IntVar],
    fixed_count: int,
    cap: int,
    require_exact_fill: bool = False,
    coverage_shortfall_vars: Optional[List[cp_model.IntVar]] = None,
) -> None:
    """Honor immutable stamps; default ceiling-only so OPEN slots beat illegal overtime."""

    if not terms and fixed_count == 0:
        return
    if fixed_count >= cap:
        if terms:
            model.Add(sum(terms) == 0)
        return
    assigned = sum(terms) + fixed_count
    if require_exact_fill:
        model.Add(assigned == cap)
        return
    model.Add(assigned <= cap)
    if coverage_shortfall_vars is None:
        return
    shortfall = model.NewIntVar(0, cap, f"pool_short_{len(coverage_shortfall_vars)}")
    model.Add(shortfall == cap - assigned)
    coverage_shortfall_vars.append(shortfall)


def _add_clinical_floor_qual_caps(
    model: cp_model.CpModel,
    *,
    employees: Sequence[EmployeeProfile],
    period_dates: Sequence[date],
    qual_lookup: Mapping[str, str],
    x: Mapping[Tuple[str, date, str], cp_model.IntVar],
    fixed: Mapping[Tuple[str, date], str],
    require_exact_fill: bool = False,
    require_exact_weekend_day_fill: bool | None = None,
    coverage_shortfall_vars: Optional[List[cp_model.IntVar]] = None,
) -> None:
    """Hard 1 MLT + 1 MLA per Evening/Night day; 1 MLT + 1 MLA on weekend Day shifts."""

    weekend_day_exact = (
        require_exact_fill
        if require_exact_weekend_day_fill is None
        else require_exact_weekend_day_fill
    )

    for assignment_date in period_dates:
        for band in ("E", "N"):
            for qual_code in CLINICAL_FLOOR_QUAL_CODES:
                terms, fixed_count = _daily_band_assigned_terms_for_qual(
                    band,
                    assignment_date,
                    qual_code,
                    employees=employees,
                    qual_lookup=qual_lookup,
                    x=x,
                    fixed=fixed,
                )
                if not terms and fixed_count == 0:
                    continue
                _add_fixed_aware_pool_cap(
                    model,
                    terms=terms,
                    fixed_count=fixed_count,
                    cap=CLINICAL_FLOOR_SEATS_PER_QUAL,
                    require_exact_fill=require_exact_fill,
                    coverage_shortfall_vars=coverage_shortfall_vars,
                )

        if assignment_date.weekday() < 5:
            continue

        for qual_code in CLINICAL_FLOOR_QUAL_CODES:
            terms, fixed_count = _daily_band_assigned_terms_for_qual(
                "D",
                assignment_date,
                qual_code,
                employees=employees,
                qual_lookup=qual_lookup,
                x=x,
                fixed=fixed,
            )
            if not terms and fixed_count == 0:
                continue
            _add_fixed_aware_pool_cap(
                model,
                terms=terms,
                fixed_count=fixed_count,
                cap=CLINICAL_FLOOR_SEATS_PER_QUAL,
                require_exact_fill=weekend_day_exact,
                coverage_shortfall_vars=coverage_shortfall_vars,
            )


def _add_weekend_staffing_qual_caps(
    model: cp_model.CpModel,
    *,
    employees: Sequence[EmployeeProfile],
    period_dates: Sequence[date],
    qual_lookup: Mapping[str, str],
    x: Mapping[Tuple[str, date, str], cp_model.IntVar],
    fixed: Mapping[Tuple[str, date], str],
) -> None:
    """Hard cap: at most ``WEEKEND_CLINICAL_MAX_PER_QUAL`` roster lines per qual on each weekend day."""

    for assignment_date in period_dates:
        if assignment_date.weekday() < 5:
            continue
        for qual_code, maximum in WEEKEND_CLINICAL_MAX_PER_QUAL.items():
            terms: List[cp_model.IntVar] = []
            constant = 0
            for employee in employees:
                if qual_lookup.get(employee.id) != qual_code:
                    continue
                day_terms, day_constant = _work_day_terms_for_employee_date(
                    employee.id,
                    assignment_date,
                    x=x,
                    fixed=fixed,
                )
                terms.extend(day_terms)
                constant += day_constant
            if terms or constant:
                model.Add(sum(terms) + constant <= maximum)


def _add_portage_daily_band_caps(
    model: cp_model.CpModel,
    *,
    employees: Sequence[EmployeeProfile],
    period_dates: Sequence[date],
    qual_lookup: Mapping[str, str],
    x: Mapping[Tuple[str, date, str], cp_model.IntVar],
    fixed: Mapping[Tuple[str, date], str],
    require_exact_fill: bool = False,
    require_exact_en_fill: bool = False,
    coverage_shortfall_vars: Optional[List[cp_model.IntVar]] = None,
) -> None:
    """Hard pool-wide caps: 2 E and 2 N every day; 2 D on each weekend day (ceiling + shortfall)."""

    en_exact = require_exact_fill or require_exact_en_fill

    for assignment_date in period_dates:
        evening_terms, evening_fixed = _daily_band_assigned_terms(
            "E",
            assignment_date,
            employees=employees,
            x=x,
            fixed=fixed,
        )
        _add_fixed_aware_pool_cap(
            model,
            terms=evening_terms,
            fixed_count=evening_fixed,
            cap=DAILY_EVENING_CAP,
            require_exact_fill=en_exact,
            coverage_shortfall_vars=coverage_shortfall_vars,
        )

        night_terms, night_fixed = _daily_band_assigned_terms(
            "N",
            assignment_date,
            employees=employees,
            x=x,
            fixed=fixed,
        )
        _add_fixed_aware_pool_cap(
            model,
            terms=night_terms,
            fixed_count=night_fixed,
            cap=DAILY_NIGHT_CAP,
            require_exact_fill=en_exact,
            coverage_shortfall_vars=coverage_shortfall_vars,
        )

        if assignment_date.weekday() < 5:
            continue

        day_terms, day_fixed = _daily_band_assigned_terms(
            "D",
            assignment_date,
            employees=employees,
            x=x,
            fixed=fixed,
        )
        _add_fixed_aware_pool_cap(
            model,
            terms=day_terms,
            fixed_count=day_fixed,
            cap=WEEKEND_DAY_CAP,
            require_exact_fill=require_exact_fill,
            coverage_shortfall_vars=coverage_shortfall_vars,
        )

    _add_clinical_floor_qual_caps(
        model,
        employees=employees,
        period_dates=period_dates,
        qual_lookup=qual_lookup,
        x=x,
        fixed=fixed,
        require_exact_fill=en_exact,
        require_exact_weekend_day_fill=require_exact_fill,
        coverage_shortfall_vars=coverage_shortfall_vars,
    )


def _add_weekday_day_smoothing_objective(
    model: cp_model.CpModel,
    *,
    employees: Sequence[EmployeeProfile],
    period_dates: Sequence[date],
    x: Mapping[Tuple[str, date, str], cp_model.IntVar],
    fixed: Mapping[Tuple[str, date], str],
) -> List[cp_model.IntVar]:
    """Soft penalty: keep weekday D headcount as even as possible (uncapped release valve)."""

    weekday_dates = [day for day in period_dates if day.weekday() < 5]
    if len(weekday_dates) < 2:
        return []

    max_roster = max(len(employees), 1)
    max_daily = max_roster * len(weekday_dates)
    weekday_totals: List[cp_model.IntVar] = []
    max_weekday_d = model.NewIntVar(0, max_daily, "max_weekday_d")
    min_weekday_d = model.NewIntVar(0, max_daily, "min_weekday_d")

    for assignment_date in weekday_dates:
        day_terms, day_fixed = _daily_band_assigned_terms(
            "D",
            assignment_date,
            employees=employees,
            x=x,
            fixed=fixed,
        )
        if not day_terms and day_fixed == 0:
            continue
        total = model.NewIntVar(0, max_roster, f"weekday_d_{assignment_date.isoformat()}")
        model.Add(total == sum(day_terms) + day_fixed)
        weekday_totals.append(total)
        model.Add(max_weekday_d >= total)
        model.Add(min_weekday_d <= total)

    if len(weekday_totals) < 2:
        return []

    spread = model.NewIntVar(0, max_daily, "weekday_d_spread")
    model.Add(spread == max_weekday_d - min_weekday_d)
    return [spread]


def _format_equity_variance(delta: float) -> str:
    rounded = int(round(delta))
    if rounded == 0:
        return "0"
    if rounded > 0:
        return f"+{rounded}"
    return str(rounded)


def _equity_pool_name(qual_code: str, contract_line_type: str) -> str:
    contract_token = normalize_contract_line_type(contract_line_type).replace("/", "_")
    return f"{qual_code.upper()}_{contract_token}_Pool"


def _assignment_value(assignment: object, field: str):
    if isinstance(assignment, dict):
        return assignment.get(field)
    return getattr(assignment, field, None)


def _band_counts_for_employee(
    employee_id: str,
    assignments: Sequence[object],
    shift_templates: Mapping[str, object],
) -> Dict[str, int]:
    counts = {"D": 0, "E": 0, "N": 0}
    for assignment in assignments:
        if str(_assignment_value(assignment, "employee_id") or "") != employee_id:
            continue
        template = shift_templates.get(
            str(_assignment_value(assignment, "shift_template_id") or "")
        )
        if template is None:
            continue
        if isinstance(template, dict):
            code = str(template.get("code", ""))
        else:
            code = str(getattr(template, "code", ""))
        band = shift_code_to_band(code)
        if band in counts:
            counts[band] += 1
    return counts


def compute_employee_alternate_shift_share(
    employee_id: str,
    *,
    contract_line_type: str,
    assignments: Sequence[object],
    shift_templates: Mapping[str, object],
) -> Optional[Dict[str, object]]:
    """Evening % for D/E lines, night % for D/N lines (alternate band / total worked)."""

    counts = _band_counts_for_employee(employee_id, assignments, shift_templates)
    contract = normalize_contract_line_type(contract_line_type or "")
    if contract == "D/N":
        total = counts["D"] + counts["N"]
        alternate = counts["N"]
        band_token = "N"
        band_label = "night"
    elif contract == "D/E":
        total = counts["D"] + counts["E"]
        alternate = counts["E"]
        band_token = "E"
        band_label = "evening"
    else:
        total = counts["D"] + counts["E"] + counts["N"]
        alternate = counts["E"] + counts["N"]
        band_token = "E+N"
        band_label = "alternate"
    if total <= 0:
        return None
    alternate_pct = round(100.0 * alternate / total, 1)
    day_pct = round(100.0 * counts["D"] / total, 1)
    return {
        "alternate_band": band_token,
        "alternate_band_label": band_label,
        "alternate_shifts": alternate,
        "day_shifts": counts["D"],
        "total_shifts": total,
        "alternate_shift_pct": alternate_pct,
        "day_shift_pct": day_pct,
    }


def compute_shift_equity_metrics(
    employees: Sequence[EmployeeProfile],
    assignments: Sequence[object],
    *,
    shift_templates: Mapping[str, object],
    qual_lookup: Mapping[str, str],
) -> Dict[str, object]:
    """
    Summarize D/E/N distribution variance across vacant Portage lines in each contract pool.
    """

    pool_members: Dict[str, List[EmployeeProfile]] = defaultdict(list)
    for employee in employees:
        if not is_vacant_portage_line(employee.full_name):
            continue
        qual_code = qual_lookup.get(employee.id, infer_qual_code(employee))
        pool_name = _equity_pool_name(qual_code, employee.contract_line_type or "")
        pool_members[pool_name].append(employee)

    metrics: Dict[str, object] = {}
    for pool_name, members in sorted(pool_members.items()):
        line_payloads: Dict[str, Dict[str, object]] = {}
        night_totals: List[int] = []
        evening_totals: List[int] = []
        alternate_pcts: List[float] = []
        contract_type = normalize_contract_line_type(members[0].contract_line_type or "")

        for employee in sorted(
            members,
            key=lambda item: (
                parse_vacant_portage_line(item.full_name)[2]
                if parse_vacant_portage_line(item.full_name) is not None
                else 0
            ),
        ):
            spec = parse_vacant_portage_line(employee.full_name)
            if spec is None:
                continue
            _role, _contract, line_number = spec
            counts = _band_counts_for_employee(employee.id, assignments, shift_templates)
            share = compute_employee_alternate_shift_share(
                employee.id,
                contract_line_type=employee.contract_line_type or contract_type,
                assignments=assignments,
                shift_templates=shift_templates,
            )
            line_key = f"line_{line_number:02d}"
            if contract_type == "D/N":
                line_payloads[line_key] = {
                    "total_D": counts["D"],
                    "total_N": counts["N"],
                }
                night_totals.append(counts["N"])
            else:
                line_payloads[line_key] = {
                    "total_D": counts["D"],
                    "total_E": counts["E"],
                }
                evening_totals.append(counts["E"])
            if share is not None:
                line_payloads[line_key].update(
                    {
                        "alternate_band": share["alternate_band"],
                        "alternate_shifts": share["alternate_shifts"],
                        "total_shifts": share["total_shifts"],
                        "alternate_shift_pct": share["alternate_shift_pct"],
                        "day_shift_pct": share["day_shift_pct"],
                    }
                )
                alternate_pcts.append(float(share["alternate_shift_pct"]))

        pool_entry: Dict[str, object] = {}
        if alternate_pcts:
            pool_entry["pool_avg_alternate_shift_pct"] = round(
                sum(alternate_pcts) / len(alternate_pcts),
                1,
            )
        if contract_type == "D/N" and night_totals:
            from lab_scheduler.scheduling.portage_equity_targets import (
                PORTAGE_DN_FT_NIGHT_SHIFT_TARGET,
            )

            target_avg_nights = PORTAGE_DN_FT_NIGHT_SHIFT_TARGET
            pool_entry["target_avg_nights"] = target_avg_nights
            for line_key, payload in line_payloads.items():
                total_n = int(payload["total_N"])
                payload["variance_from_avg"] = _format_equity_variance(
                    total_n - target_avg_nights
                )
                pool_entry[line_key] = payload
        elif evening_totals:
            target_avg_evenings = int(round(sum(evening_totals) / len(evening_totals)))
            pool_entry["target_avg_evenings"] = target_avg_evenings
            for line_key, payload in line_payloads.items():
                total_e = int(payload["total_E"])
                payload["variance_from_avg"] = _format_equity_variance(
                    total_e - target_avg_evenings
                )
                pool_entry[line_key] = payload
        else:
            pool_entry.update(line_payloads)

        metrics[pool_name] = pool_entry

    return metrics


def compute_pool_equity_for_all_employees(
    employees: Sequence[EmployeeProfile],
    assignments: Sequence[object],
    *,
    shift_templates: Mapping[str, object],
    qual_lookup: Mapping[str, str],
) -> Dict[str, object]:
    """
    Pool-level D/E/N equity across all staff (named + vacant lines) in each contract pool.
    """

    pool_members: Dict[str, List[EmployeeProfile]] = defaultdict(list)
    for employee in employees:
        qual_code = qual_lookup.get(employee.id, infer_qual_code(employee))
        pool_name = _equity_pool_name(qual_code, employee.contract_line_type or "")
        pool_members[pool_name].append(employee)

    metrics: Dict[str, object] = {}
    for pool_name, members in sorted(pool_members.items()):
        line_payloads: Dict[str, Dict[str, object]] = {}
        night_totals: List[int] = []
        evening_totals: List[int] = []
        alternate_pcts: List[float] = []
        contract_type = normalize_contract_line_type(members[0].contract_line_type or "")

        for employee in sorted(members, key=lambda item: item.full_name.lower()):
            counts = _band_counts_for_employee(employee.id, assignments, shift_templates)
            share = compute_employee_alternate_shift_share(
                employee.id,
                contract_line_type=employee.contract_line_type or contract_type,
                assignments=assignments,
                shift_templates=shift_templates,
            )
            parsed = parse_vacant_portage_line(employee.full_name)
            if parsed is not None:
                _role, _contract, line_number = parsed
                line_key = f"line_{line_number:02d}"
            else:
                safe_id = employee.id.replace("-", "_")
                line_key = f"employee_{safe_id}"

            if contract_type == "D/N":
                line_payloads[line_key] = {
                    "employee_id": employee.id,
                    "employee_name": employee.full_name,
                    "total_D": counts["D"],
                    "total_N": counts["N"],
                }
                night_totals.append(counts["N"])
            else:
                line_payloads[line_key] = {
                    "employee_id": employee.id,
                    "employee_name": employee.full_name,
                    "total_D": counts["D"],
                    "total_E": counts["E"],
                }
                evening_totals.append(counts["E"])
            if share is not None:
                line_payloads[line_key].update(
                    {
                        "alternate_band": share["alternate_band"],
                        "alternate_shifts": share["alternate_shifts"],
                        "total_shifts": share["total_shifts"],
                        "alternate_shift_pct": share["alternate_shift_pct"],
                        "day_shift_pct": share["day_shift_pct"],
                    }
                )
                alternate_pcts.append(float(share["alternate_shift_pct"]))

        pool_entry: Dict[str, object] = {}
        if alternate_pcts:
            pool_entry["pool_avg_alternate_shift_pct"] = round(
                sum(alternate_pcts) / len(alternate_pcts),
                1,
            )
        if contract_type == "D/N" and night_totals:
            from lab_scheduler.scheduling.portage_equity_targets import (
                PORTAGE_DN_FT_NIGHT_SHIFT_TARGET,
            )

            target_avg_nights = PORTAGE_DN_FT_NIGHT_SHIFT_TARGET
            pool_entry["target_avg_nights"] = target_avg_nights
            for line_key, payload in line_payloads.items():
                total_n = int(payload["total_N"])
                payload["variance_from_avg"] = _format_equity_variance(
                    total_n - target_avg_nights
                )
                pool_entry[line_key] = payload
        elif evening_totals:
            target_avg_evenings = int(round(sum(evening_totals) / len(evening_totals)))
            pool_entry["target_avg_evenings"] = target_avg_evenings
            for line_key, payload in line_payloads.items():
                total_e = int(payload["total_E"])
                payload["variance_from_avg"] = _format_equity_variance(
                    total_e - target_avg_evenings
                )
                pool_entry[line_key] = payload
        else:
            pool_entry.update(line_payloads)

        metrics[pool_name] = pool_entry

    return metrics


def build_alternate_shift_distribution_rows(
    employees: Sequence[EmployeeProfile],
    assignments: Sequence[object],
    *,
    shift_templates: Mapping[str, object],
    qual_lookup: Mapping[str, str],
) -> List[Dict[str, object]]:
    """Tabular alternate-shift share per vacant Portage line for UI and export helpers."""

    rows: List[Dict[str, object]] = []
    for employee in employees:
        if not is_vacant_portage_line(employee.full_name):
            continue
        share = compute_employee_alternate_shift_share(
            employee.id,
            contract_line_type=str(employee.contract_line_type or ""),
            assignments=assignments,
            shift_templates=shift_templates,
        )
        if share is None:
            continue
        parsed = parse_vacant_portage_line(employee.full_name)
        if parsed is None:
            continue
        role, contract, line_number = parsed
        qual_code = qual_lookup.get(employee.id, infer_qual_code(employee))
        alt_label = "Evening" if share["alternate_band"] == "E" else "Night"
        rows.append(
            {
                "line": f"{role} {contract} Line {line_number:02d}",
                "pool": f"{qual_code} {contract}",
                "day_shifts": share["day_shifts"],
                "alternate_shifts": share["alternate_shifts"],
                "alternate_band": share["alternate_band"],
                "total_shifts": share["total_shifts"],
                "alternate_shift_pct": share["alternate_shift_pct"],
                "day_shift_pct": share["day_shift_pct"],
                "alternate_label": alt_label,
            }
        )
    rows.sort(key=lambda row: (str(row["pool"]), str(row["line"])))
    return rows


def alternate_shift_rows_from_equity_metrics(
    metrics: Mapping[str, object],
) -> List[Dict[str, object]]:
    """Flatten shift_equity_metrics JSON into table rows (includes alternate_shift_pct)."""

    rows: List[Dict[str, object]] = []
    for pool_name in sorted(metrics.keys()):
        pool = metrics[pool_name]
        if not isinstance(pool, dict):
            continue
        contract = "D/N" if "target_avg_nights" in pool else "D/E"
        alt_band = "N" if contract == "D/N" else "E"
        alt_label = "Night" if contract == "D/N" else "Evening"
        pool_display = pool_name.replace("_", " ")
        for line_key in sorted(key for key in pool if key.startswith("line_")):
            line = pool[line_key]
            if not isinstance(line, dict):
                continue
            if "alternate_shift_pct" not in line:
                continue
            line_number = line_key.replace("line_", "")
            rows.append(
                {
                    "line": f"{pool_display} {line_number}",
                    "pool": pool_display,
                    "day_shifts": line.get("total_D", 0),
                    "alternate_shifts": line.get("total_N" if contract == "D/N" else "total_E", 0),
                    "alternate_band": alt_band,
                    "total_shifts": line.get("total_shifts", 0),
                    "alternate_shift_pct": line.get("alternate_shift_pct"),
                    "day_shift_pct": line.get("day_shift_pct"),
                    "alternate_label": alt_label,
                }
            )
    return rows


def _fixed_band_count_in_window(
    employee_id: str,
    window_dates: Sequence[date],
    band: str,
    fixed: Mapping[Tuple[str, date], str],
) -> int:
    return sum(
        1 for assignment_date in window_dates if fixed.get((employee_id, assignment_date)) == band
    )


def _band_literal_or_var(
    model: cp_model.CpModel,
    *,
    employee_id: str,
    assignment_date: date,
    band: str,
    x: Mapping[Tuple[str, date, str], cp_model.IntVar],
    fixed: Mapping[Tuple[str, date], str],
) -> Tuple[Optional[cp_model.IntVar], int]:
    """Return decision var (if any) and fixed constant (0/1) for a band on a day."""

    fixed_band = fixed.get((employee_id, assignment_date))
    if fixed_band == band:
        return None, 1
    if fixed_band is not None:
        return None, 0
    var = x.get((employee_id, assignment_date, band))
    if var is not None:
        return var, 0
    return None, 0


def _add_evening_cluster_objective(
    model: cp_model.CpModel,
    *,
    x: Mapping[Tuple[str, date, str], cp_model.IntVar],
    fixed: Mapping[Tuple[str, date], str],
    period_dates: Sequence[date],
    employee_ids: Sequence[str],
    thresholds: FairnessThresholds,
) -> List[cp_model.IntVar]:
    """Penalize more than ``evening_cluster_max`` evening shifts in any rolling window."""

    window_days = thresholds.evening_cluster_window_days
    max_evenings = thresholds.evening_cluster_max
    slack_vars: List[cp_model.IntVar] = []
    if window_days <= 0:
        return slack_vars

    for employee_id in employee_ids:
        for start_idx in range(len(period_dates)):
            window = period_dates[start_idx : start_idx + window_days]
            if not window:
                continue
            fixed_e = _fixed_band_count_in_window(employee_id, window, "E", fixed)
            e_terms: List[cp_model.LinearExpr] = []
            for assignment_date in window:
                var, const = _band_literal_or_var(
                    model,
                    employee_id=employee_id,
                    assignment_date=assignment_date,
                    band="E",
                    x=x,
                    fixed=fixed,
                )
                if var is not None:
                    e_terms.append(var)
            if not e_terms and fixed_e == 0:
                continue
            if not e_terms and fixed_e <= max_evenings:
                continue
            max_count = len(window)
            e_count = model.NewIntVar(0, max_count, f"e_cluster_{employee_id}_{start_idx}")
            if e_terms:
                model.Add(e_count == sum(e_terms) + fixed_e)
            else:
                model.Add(e_count == fixed_e)
            slack = model.NewIntVar(0, max_count, f"e_cluster_slack_{employee_id}_{start_idx}")
            model.Add(slack >= e_count - max_evenings)
            slack_vars.append(slack)
    return slack_vars


def _add_post_night_recovery_objective(
    model: cp_model.CpModel,
    *,
    x: Mapping[Tuple[str, date, str], cp_model.IntVar],
    fixed: Mapping[Tuple[str, date], str],
    period_dates: Sequence[date],
    employee_ids: Sequence[str],
    thresholds: FairnessThresholds,
) -> List[cp_model.IntVar]:
    """Soft-penalize day shifts too soon after a consecutive-night block ends."""

    required_off = thresholds.post_night_recovery_off_days
    slack_vars: List[cp_model.IntVar] = []
    if required_off <= 0:
        return slack_vars

    date_set = set(period_dates)
    for employee_id in employee_ids:
        for assignment_date in period_dates:
            n_var, n_const = _band_literal_or_var(
                model,
                employee_id=employee_id,
                assignment_date=assignment_date,
                band="N",
                x=x,
                fixed=fixed,
            )
            if n_var is None and n_const == 0:
                continue

            next_day = assignment_date + timedelta(days=1)
            n1_var, n1_const = _band_literal_or_var(
                model,
                employee_id=employee_id,
                assignment_date=next_day,
                band="N",
                x=x,
                fixed=fixed,
            )

            night_end = model.NewBoolVar(
                f"night_end_{employee_id}_{assignment_date.isoformat()}"
            )
            if next_day in date_set and n1_var is not None:
                if n_var is not None:
                    model.Add(night_end <= n_var)
                    model.Add(night_end <= 1 - n1_var)
                    model.Add(night_end >= n_var + (1 - n1_var) - 1)
                else:
                    model.Add(night_end <= n_const)
                    model.Add(night_end <= 1 - n1_var)
                    model.Add(night_end >= n_const + (1 - n1_var) - 1)
            elif next_day in date_set:
                if n_var is not None:
                    model.Add(night_end <= n_var)
                    model.Add(night_end <= 1 - n1_const)
                    model.Add(night_end >= n_var + (1 - n1_const) - 1)
                else:
                    model.Add(night_end == n_const * (1 - n1_const))
            elif n_var is not None:
                model.Add(night_end == n_var)
            else:
                model.Add(night_end == n_const)

            for offset in range(1, required_off + 1):
                day_shift = assignment_date + timedelta(days=offset)
                if day_shift not in date_set:
                    continue
                d_var, d_const = _band_literal_or_var(
                    model,
                    employee_id=employee_id,
                    assignment_date=day_shift,
                    band="D",
                    x=x,
                    fixed=fixed,
                )
                if d_var is None and d_const == 0:
                    continue
                slack = model.NewIntVar(
                    0,
                    1,
                    f"post_night_slack_{employee_id}_{assignment_date.isoformat()}_{offset}",
                )
                if d_var is not None:
                    model.Add(slack >= night_end + d_var - 1)
                else:
                    model.Add(slack >= night_end + d_const - 1)
                slack_vars.append(slack)
    return slack_vars


def format_shift_equity_metrics_summary(metrics: Mapping[str, object]) -> str:
    """Compact pool fairness summary for Auto-Pilot banners and audit logs."""

    if not metrics:
        return ""
    pool_parts: List[str] = []
    for pool_name in sorted(metrics.keys()):
        pool = metrics[pool_name]
        if not isinstance(pool, dict):
            continue
        if "target_avg_nights" in pool:
            target_val = pool["target_avg_nights"]
            metric_label = "nights"
        elif "target_avg_evenings" in pool:
            target_val = pool["target_avg_evenings"]
            metric_label = "evenings"
        else:
            continue
        pool_avg_pct = pool.get("pool_avg_alternate_shift_pct")
        pct_suffix = (
            f", avg {pool_avg_pct}% alt"
            if pool_avg_pct is not None
            else ""
        )
        line_bits: List[str] = []
        for line_key in sorted(key for key in pool if key.startswith("line_")):
            line = pool[line_key]
            if not isinstance(line, dict):
                continue
            variance = str(line.get("variance_from_avg", "0"))
            alt_pct = line.get("alternate_shift_pct")
            pct_note = f" ({alt_pct}% alt)" if alt_pct is not None else ""
            if variance == "0":
                line_bits.append(f"{line_key} on target{pct_note}")
            else:
                line_bits.append(f"{line_key} {variance} {metric_label}{pct_note}")
        if line_bits:
            pool_parts.append(
                f"{pool_name} (avg {target_val} {metric_label}{pct_suffix}): "
                + "; ".join(line_bits)
            )
    return " · ".join(pool_parts)


def solve_vacant_unassigned_slots(
    *,
    rules: JurisdictionRules,
    period_start: date,
    period_end: date,
    weeks_in_period: int,
    employees: Sequence[EmployeeProfile],
    shift_templates: Mapping[str, object],
    fixed_assignments: Sequence[object],
    employee_target_hours: Mapping[str, float],
    catalog_target_hours: Mapping[str, float] | None = None,
    availability_blocked: Optional[Mapping[str, Set[date]]] = None,
    qual_codes: Optional[Mapping[str, str]] = None,
    time_limit_seconds: float = 120.0,
    portage_daily_band_caps: bool = True,
    fairness_weight_scale: float = 1.0,
    fairness_thresholds: FairnessThresholds = DEFAULT_FAIRNESS_THRESHOLDS,
    compliance_first: bool = False,
    freeze_master_template_stamps: bool = True,
) -> "CpSatFillResult":
    """
    Fill vacant-line open cells with CP-SAT.

    Hard constraints:
      - At most one shift per employee per day.
      - Evening on day *d* forbids Day on day *d+1* (no E→D clopening).
      - Day on day *d* forbids Night on day *d+1*.
      - Vacant-line payroll hours: ``worked <= employee_target_hours`` for every Portage vacant row.
      - Manitoba weekly rest: at most six scheduled days per Monday-start work week.
      - Portage consecutive work limit: at most six worked days (D/E/N) in any seven-day window.
      - Portage consecutive night limit: at most four night shifts in any five-day window.
      - Manitoba statutory consecutive-day ceiling (13-day window) when configured on rules.
      - Assignments in ``fixed_assignments`` on named (non-vacant) employees are immutable.
      - When ``portage_daily_band_caps`` is enabled (default): exactly two Evening and two Night
        shifts pool-wide every calendar day; exactly two Day shifts on each Saturday/Sunday.
        Each capped band also requires exactly one MLT and one MLA seat (immutable clinical floor).
        Monday–Friday Day shifts are uncapped (FTE pressure-release valve).
      - Weekend mirror: for every employee and weekend, Sat_D == Sun_D, Sat_E == Sun_E, Sat_N == Sun_N.
      - Full-time vacant lines (320h): exactly eight weekend shift days per 8-week block
        (proportional for part-time catalog hours).
      - Part-time vacant lines: weekend shift days scale with catalog hours (same ratio as FT).

    Vacant Portage lines always receive a fully open decision grid: every calendar day in the
    period is fillable except availability blocks. Upstream baseline, template, or clinical
    pre-assignments on vacant rows are ignored so CP-SAT can reach ``target_hours``.

    Soft objective (minimize penalty):
      - Hour deficit below contract target (Tier A; full 320h lines weighted highest).
      - Coverage shortfall on E/N/D pool and clinical-floor caps (Tier A; OPEN beats overtime).
      - Part-time payroll/catalog surplus (Tier A; moot when payroll ceiling is binding).
      - Alternate shift (E+N) spread across identical vacant line types (Tier B).
      - Deficit spread across identical vacant line types (Tier B; low vs hour deficit).
      - Weekday Day headcount variance smoothing (Tier B).
      - Evening cluster spread (Tier B; ``fairness_thresholds.evening_cluster_max``).
      - Post-night recovery slack (Tier B).

    Night-then-Day on consecutive calendar days is forbidden (hard constraint).

    Open vacant cells may remain off without penalty; hour deficit drives fill volume with
    weekday Day shifts absorbing remaining contract hours under the band caps.
    """

    _require_ortools()
    from lab_scheduler.scheduling.auto_generate import PlannedAssignment

    qual_lookup: Dict[str, str] = dict(qual_codes or {})
    for employee in employees:
        qual_lookup.setdefault(employee.id, infer_qual_code(employee))

    rotation_target_hours = catalog_target_hours or employee_target_hours

    period_dates = _daterange(period_start, period_end)
    all_bands = _fixed_band_map(fixed_assignments, shift_templates)
    immutable = _fixed_band_map_for_vacant_fill(
        fixed_assignments,
        shift_templates,
        employees,
        employee_target_hours,
        freeze_master_template_stamps=freeze_master_template_stamps,
    )
    from lab_scheduler.scheduling.anchor_tiers import merge_night_anchor_fixed_bands

    anchor_fixed = merge_night_anchor_fixed_bands(
        {},
        employees=employees,
        period_start=period_start,
        period_end=period_end,
    )
    immutable = {**immutable, **anchor_fixed}
    all_bands = {**all_bands, **anchor_fixed}
    occupancy = all_bands
    blocked = availability_blocked or {}

    fillable_slots = _open_vacant_fillable_slots(
        employees,
        period_dates,
        blocked=blocked,
        occupied=occupancy,
    )

    if not fillable_slots:
        return CpSatFillResult(
            assignments=tuple(),
            status="OPTIMAL",
            objective_value=0,
            hour_deviation_total=0,
            coverage_shortfall_total=0,
            n_to_d_fatigue_total=0,
            unfilled_escalated_total=0,
            deficit_variance_total=0,
            alt_shift_spread_total=0,
            alt_shift_unfairness_total=0,
            pt_alt_band_slack_total=0,
            weekday_surplus_spread_total=0,
            weekend_baseline_surplus_total=0,
            fillable_slot_count=0,
            evening_cluster_slack_total=0,
            post_night_recovery_slack_total=0,
            fairness_penalty_total=0,
        )

    employee_by_id = {employee.id: employee for employee in employees}
    model = cp_model.CpModel()
    x: Dict[Tuple[str, date, str], cp_model.IntVar] = {}

    for employee_id, assignment_date in fillable_slots:
        employee = employee_by_id[employee_id]
        allowed = _allowed_bands_for_employee(employee, qual_lookup)
        for band in BAND_TOKENS:
            if band not in allowed:
                continue
            if _shift_id_for_code(band_to_shift_code(band), shift_templates) is None:
                continue
            x[(employee_id, assignment_date, band)] = model.NewBoolVar(
                f"x_{employee_id}_{assignment_date.isoformat()}_{band}"
            )

    # HARD: one shift per day for every employee (fixed + decisions).
    for employee in employees:
        for assignment_date in period_dates:
            decision_vars = [
                x[(employee.id, assignment_date, band)]
                for band in BAND_TOKENS
                if (employee.id, assignment_date, band) in x
            ]
            fixed_band = occupancy.get((employee.id, assignment_date))
            if fixed_band is not None:
                continue
            if decision_vars:
                model.Add(sum(decision_vars) <= 1)

    # HARD: Evening -> no Day on the immediately following calendar day.
    for employee in employees:
        for index, assignment_date in enumerate(period_dates[:-1]):
            next_date = period_dates[index + 1]
            day_var = x.get((employee.id, next_date, "D"))
            if day_var is None:
                continue
            evening_fixed = occupancy.get((employee.id, assignment_date)) == "E"
            if evening_fixed:
                model.Add(day_var == 0)
                continue
            evening_var = x.get((employee.id, assignment_date, "E"))
            if evening_var is not None:
                model.Add(evening_var + day_var <= 1)

    # HARD: Day -> no Night on the immediately following calendar day.
    for employee in employees:
        for index, assignment_date in enumerate(period_dates[:-1]):
            next_date = period_dates[index + 1]
            night_var = x.get((employee.id, next_date, "N"))
            if night_var is None:
                continue
            day_fixed = occupancy.get((employee.id, assignment_date)) == "D"
            if day_fixed:
                model.Add(night_var == 0)
                continue
            day_var = x.get((employee.id, assignment_date, "D"))
            if day_var is not None:
                model.Add(day_var + night_var <= 1)

    # HARD: Night -> no Day on the immediately following calendar day.
    for employee in employees:
        for index, assignment_date in enumerate(period_dates[:-1]):
            next_date = period_dates[index + 1]
            day_var = x.get((employee.id, next_date, "D"))
            if day_var is None:
                continue
            night_fixed = occupancy.get((employee.id, assignment_date)) == "N"
            if night_fixed:
                model.Add(day_var == 0)
                continue
            night_var = x.get((employee.id, assignment_date, "N"))
            if night_var is not None:
                model.Add(night_var + day_var <= 1)

    _add_manitoba_work_day_limits(
        model,
        rules=rules,
        employees=employees,
        period_dates=period_dates,
        x=x,
        fixed=occupancy,
    )
    _add_portage_consecutive_work_limit(
        model,
        employees=employees,
        period_dates=period_dates,
        x=x,
        fixed=occupancy,
    )
    _add_portage_consecutive_night_limit(
        model,
        employees=employees,
        period_dates=period_dates,
        x=x,
        fixed=occupancy,
    )

    if portage_daily_band_caps:
        weekend_pairs = _weekend_sat_sun_pairs(period_dates)
        coverage_shortfall_vars: List[cp_model.IntVar] = []
        _add_portage_weekend_mirror_rule(
            model,
            employees=employees,
            weekend_pairs=weekend_pairs,
            qual_lookup=qual_lookup,
            x=x,
            fixed=occupancy,
        )
        if weeks_in_period >= 8:
            _add_portage_weekend_active_caps(
                model,
                employees=employees,
                period_dates=period_dates,
                employee_target_hours=rotation_target_hours,
                qual_lookup=qual_lookup,
                x=x,
                fixed=occupancy,
            )
        _add_portage_daily_band_caps(
            model,
            employees=employees,
            period_dates=period_dates,
            qual_lookup=qual_lookup,
            x=x,
            fixed=occupancy,
            require_exact_fill=False,
            require_exact_en_fill=False,
            coverage_shortfall_vars=coverage_shortfall_vars,
        )
        if compliance_first:
            _add_weekend_staffing_qual_caps(
                model,
                employees=employees,
                period_dates=period_dates,
                qual_lookup=qual_lookup,
                x=x,
                fixed=occupancy,
            )
    else:
        coverage_shortfall_vars = []

    weekday_d_spread_vars = (
        _add_weekday_day_smoothing_objective(
            model,
            employees=employees,
            period_dates=period_dates,
            x=x,
            fixed=occupancy,
        )
        if portage_daily_band_caps
        else []
    )

    # Hour deficit / surplus soft vars for every roster line.
    hour_surplus_vars: List[cp_model.IntVar] = []
    pt_payroll_surplus_vars: List[cp_model.IntVar] = []
    pt_catalog_grace_vars: List[cp_model.IntVar] = []
    pt_catalog_excess_mid_vars: List[cp_model.IntVar] = []
    pt_catalog_excess_severe_vars: List[cp_model.IntVar] = []
    hour_deficit_vars: List[Tuple[cp_model.IntVar, int]] = []
    hour_penalty_constant = 0
    diff_down_by_employee: Dict[str, cp_model.IntVar] = {}
    diff_down_constant_by_employee: Dict[str, int] = {}
    max_period_hours = len(period_dates) * int(HOURS_PER_SHIFT)
    fixed_hours = _fixed_hours_by_employee(occupancy)
    vacant_ids = vacant_portage_employee_ids(employees)
    for employee in employees:
        target_hours = int(round(float(employee_target_hours.get(employee.id, 0.0))))
        deficit_weight = _hour_deficit_weight(target_hours)
        parttime_vacant_compliance = _is_parttime_vacant_compliance_line(
            employee,
            vacant_ids=vacant_ids,
            employee_target_hours=employee_target_hours,
            compliance_first=compliance_first,
        )
        worked_terms: List[cp_model.LinearExpr] = []
        for assignment_date in period_dates:
            for band in BAND_TOKENS:
                var = x.get((employee.id, assignment_date, band))
                if var is not None:
                    worked_terms.append(var * int(HOURS_PER_SHIFT))
        if worked_terms:
            worked = model.NewIntVar(0, max_period_hours, f"hours_{employee.id}")
            model.Add(worked == sum(worked_terms) + fixed_hours.get(employee.id, 0))
            diff_up = model.NewIntVar(0, max_period_hours, f"diff_up_{employee.id}")
            diff_down = model.NewIntVar(0, max_period_hours, f"diff_down_{employee.id}")
            model.Add(worked - target_hours == diff_up - diff_down)
            _add_vacant_payroll_hour_ceiling(
                model,
                employee=employee,
                worked=worked,
                payroll_target_hours=target_hours,
                vacant_ids=vacant_ids,
                catalog_target_hours=rotation_target_hours,
                payroll_targets=employee_target_hours,
            )
            if parttime_vacant_compliance:
                catalog_target_hours = int(
                    round(float(rotation_target_hours.get(employee.id, 0.0)))
                )
                if catalog_target_hours > 0:
                    grace, excess_mid, excess_severe = _add_parttime_catalog_surplus_penalty_vars(
                        model,
                        worked=worked,
                        catalog_target_hours=catalog_target_hours,
                        max_period_hours=max_period_hours,
                        employee_id=employee.id,
                    )
                    pt_catalog_grace_vars.extend(grace)
                    pt_catalog_excess_mid_vars.extend(excess_mid)
                    pt_catalog_excess_severe_vars.extend(excess_severe)
            if _is_parttime_target_hours(float(employee_target_hours.get(employee.id, 0.0))):
                if employee.id in vacant_ids and is_vacant_portage_line(employee.full_name):
                    pass  # payroll ceiling is hard; surplus vars stay unused in feasible solutions
                else:
                    pt_payroll_surplus_vars.append(diff_up)
            else:
                hour_surplus_vars.append(diff_up)
            hour_deficit_vars.append((diff_down, deficit_weight))
            diff_down_by_employee[employee.id] = diff_down
        else:
            fixed_total = fixed_hours.get(employee.id, 0)
            deficit = max(0, target_hours - fixed_total)
            surplus = max(0, fixed_total - target_hours)
            hour_penalty_constant += surplus * WEIGHT_HOUR_SURPLUS
            hour_penalty_constant += deficit * deficit_weight
            diff_down_constant_by_employee[employee.id] = deficit

    # Equalize hour deficits across identical vacant line types.
    deficit_variance_vars: List[cp_model.IntVar] = []
    line_type_groups = _vacant_line_type_groups(employees, employee_target_hours)

    for member_ids in line_type_groups.values():
        if len(member_ids) < 2:
            continue
        max_deficit = model.NewIntVar(0, max_period_hours, f"max_def_{member_ids[0]}")
        min_deficit = model.NewIntVar(0, max_period_hours, f"min_def_{member_ids[0]}")
        for employee_id in member_ids:
            if employee_id in diff_down_by_employee:
                model.Add(max_deficit >= diff_down_by_employee[employee_id])
                model.Add(min_deficit <= diff_down_by_employee[employee_id])
            else:
                deficit = diff_down_constant_by_employee.get(employee_id, 0)
                model.Add(max_deficit >= deficit)
                model.Add(min_deficit <= deficit)
        spread = model.NewIntVar(0, max_period_hours, f"def_spread_{member_ids[0]}")
        model.Add(spread == max_deficit - min_deficit)
        deficit_variance_vars.append(spread)

    vacant_employee_ids = vacant_portage_employee_ids(employees)
    alt_total_by_employee, alt_total_constant_by_employee = _build_employee_alt_shift_totals(
        model,
        employees=employees,
        period_dates=period_dates,
        x=x,
        fixed=occupancy,
        employee_ids=vacant_employee_ids,
    )

    alt_shift_spread_vars = _add_alt_shift_equity_objective(
        model,
        employees=employees,
        employee_target_hours=rotation_target_hours,
        alt_total_by_employee=alt_total_by_employee,
        alt_total_constant_by_employee=alt_total_constant_by_employee,
        period_dates=period_dates,
    )
    alt_shift_unfairness_vars, alt_unfairness_constant = _add_fulltime_alt_shift_range_objective(
        model,
        employees=employees,
        employee_target_hours=rotation_target_hours,
        alt_total_by_employee=alt_total_by_employee,
        alt_total_constant_by_employee=alt_total_constant_by_employee,
    )
    pt_alt_band_vars, pt_band_constant = _add_parttime_alt_shift_band_objective(
        model,
        employees=employees,
        employee_target_hours=rotation_target_hours,
        alt_total_by_employee=alt_total_by_employee,
        alt_total_constant_by_employee=alt_total_constant_by_employee,
        period_dates=period_dates,
    )

    fillable_employee_ids = sorted({employee_id for employee_id, _day in fillable_slots})
    evening_cluster_vars = _add_evening_cluster_objective(
        model,
        x=x,
        fixed=occupancy,
        period_dates=period_dates,
        employee_ids=fillable_employee_ids,
        thresholds=fairness_thresholds,
    )
    post_night_vars = _add_post_night_recovery_objective(
        model,
        x=x,
        fixed=occupancy,
        period_dates=period_dates,
        employee_ids=fillable_employee_ids,
        thresholds=fairness_thresholds,
    )
    scaled_evening_weight = int(WEIGHT_EVENING_CLUSTER * fairness_weight_scale)
    scaled_post_night_weight = int(WEIGHT_POST_NIGHT_RECOVERY * fairness_weight_scale)

    hour_deficit_expr = sum(
        diff_down * weight for diff_down, weight in hour_deficit_vars
    )
    model.Minimize(
        hour_penalty_constant
        + alt_unfairness_constant
        + pt_band_constant
        + hour_deficit_expr
        + WEIGHT_HOUR_SURPLUS * sum(hour_surplus_vars)
        + WEIGHT_PT_PAYROLL_SURPLUS * sum(pt_payroll_surplus_vars)
        + WEIGHT_PT_CATALOG_SURPLUS_GRACE * sum(pt_catalog_grace_vars)
        + WEIGHT_PT_CATALOG_SURPLUS_EXCESS_MID * sum(pt_catalog_excess_mid_vars)
        + WEIGHT_PT_CATALOG_SURPLUS_EXCESS_SEVERE * sum(pt_catalog_excess_severe_vars)
        + WEIGHT_ALT_SHIFT_EQUITY * sum(alt_shift_spread_vars)
        + WEIGHT_ALT_SHIFT_UNFAIRNESS * sum(alt_shift_unfairness_vars)
        + WEIGHT_PT_ALT_SHIFT_CEILING_SLACK * sum(pt_alt_band_vars)
        + WEIGHT_DEFICIT_VARIANCE * sum(deficit_variance_vars)
        + WEIGHT_COVERAGE_SHORTFALL * sum(coverage_shortfall_vars)
        + WEIGHT_WEEKDAY_SURPLUS_SMOOTH * sum(weekday_d_spread_vars)
        + scaled_evening_weight * sum(evening_cluster_vars)
        + scaled_post_night_weight * sum(post_night_vars)
    )

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = float(time_limit_seconds)
    solver.parameters.num_search_workers = min(8, os.cpu_count() or 4)
    status_code = solver.Solve(model)
    status = solver.StatusName(status_code)

    new_assignments: List[PlannedAssignment] = []
    for (employee_id, assignment_date, band), var in x.items():
        if solver.Value(var) != 1:
            continue
        shift_id = _shift_id_for_code(band_to_shift_code(band), shift_templates)
        if shift_id is None:
            continue
        new_assignments.append(
            PlannedAssignment(
                employee_id=employee_id,
                shift_template_id=shift_id,
                assignment_date=assignment_date,
            )
        )

    hour_deviation_total = hour_penalty_constant + sum(
        int(solver.Value(diff_down)) * weight for diff_down, weight in hour_deficit_vars
    ) + sum(int(solver.Value(diff_up)) for diff_up in hour_surplus_vars)
    deficit_variance_total = sum(
        int(solver.Value(item)) for item in deficit_variance_vars
    )
    alt_shift_spread_total = sum(
        int(solver.Value(item)) for item in alt_shift_spread_vars
    )
    alt_shift_unfairness_total = alt_unfairness_constant + sum(
        int(solver.Value(item)) for item in alt_shift_unfairness_vars
    )
    pt_alt_band_slack_total = pt_band_constant + sum(
        int(solver.Value(item)) for item in pt_alt_band_vars
    )
    weekday_surplus_spread_total = sum(
        int(solver.Value(item)) for item in weekday_d_spread_vars
    )
    evening_cluster_slack_total = sum(
        int(solver.Value(item)) for item in evening_cluster_vars
    )
    post_night_recovery_slack_total = sum(
        int(solver.Value(item)) for item in post_night_vars
    )
    coverage_shortfall_total = sum(
        int(solver.Value(item)) for item in coverage_shortfall_vars
    )
    fairness_penalty_total = (
        scaled_evening_weight * evening_cluster_slack_total
        + scaled_post_night_weight * post_night_recovery_slack_total
    )

    merged_assignments = list(fixed_assignments) + new_assignments
    vacant_ids = vacant_portage_employee_ids(employees)
    equity_assignments = [
        assignment
        for assignment in merged_assignments
        if getattr(assignment, "employee_id", "") in vacant_ids
    ]
    shift_equity_metrics = compute_shift_equity_metrics(
        employees,
        equity_assignments,
        shift_templates=shift_templates,
        qual_lookup=qual_lookup,
    )

    return CpSatFillResult(
        assignments=tuple(new_assignments),
        status=status,
        objective_value=int(solver.ObjectiveValue()),
        hour_deviation_total=hour_deviation_total,
        coverage_shortfall_total=coverage_shortfall_total,
        n_to_d_fatigue_total=0,
        unfilled_escalated_total=0,
        deficit_variance_total=deficit_variance_total,
        alt_shift_spread_total=alt_shift_spread_total,
        alt_shift_unfairness_total=alt_shift_unfairness_total,
        pt_alt_band_slack_total=pt_alt_band_slack_total,
        weekday_surplus_spread_total=weekday_surplus_spread_total,
        weekend_baseline_surplus_total=0,
        fillable_slot_count=len(fillable_slots),
        shift_equity_metrics=shift_equity_metrics,
        evening_cluster_slack_total=evening_cluster_slack_total,
        post_night_recovery_slack_total=post_night_recovery_slack_total,
        fairness_penalty_total=fairness_penalty_total,
    )
