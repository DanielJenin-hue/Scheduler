from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta
from enum import Enum
from typing import Dict, List, Mapping, Optional, Sequence, Set, Tuple

from lab_scheduler.compliance.engine import ShiftTemplateInfo, _consecutive_work_day_streaks
from lab_scheduler.compliance.jurisdictions import JurisdictionRules
from lab_scheduler.models.employee import (
    allowed_shift_codes_for_contract_line,
    normalize_contract_line_type,
    normalize_shift_band_code,
)
from lab_scheduler.scheduling.profiles import EmployeeProfile
from lab_scheduler.time import workweek_for

HARD_NIGHT_SHIFTS_PER_DAY = 2
MISSING_NIGHT_SHIFT_PENALTY = float("inf")

# Immutable clinical floor: exactly two seats (1 MLT + 1 MLA) per band per calendar day.
CLINICAL_FLOOR: Dict[str, int] = {
    "MORNING": 2,
    "EVENING": 2,
    "NIGHT": 2,
}
WEEKEND_CLINICAL_MIN_PER_QUAL: Dict[str, int] = {
    "MLT": 1,
    "MLA": 1,
}
# Strict weekend cap: prefer 2 MLT; allow 1 MLA + 1 MLT; never 2 MLA (MLA max 1).
WEEKEND_CLINICAL_MAX_PER_QUAL: Dict[str, int] = {
    "MLT": 2,
    "MLA": 1,
}
DEFAULT_SCHEDULE_PERIOD_WEEKS = 8
WEEKDAY_DAY_SHIFT_CAPACITY_LIMIT = 18
WEEKDAY_LOAD_BALANCE_TOLERANCE = 0.10
CLINICAL_FLOOR_DAY_CODE = "MORNING"
CLINICAL_FLOOR_POOL_PREFIX = "Clinical Floor -"
MISSING_CLINICAL_FLOOR_PENALTY = float("inf")
# Backward-compatible aliases
MISSING_NIGHT_SHIFT_PENALTY = MISSING_CLINICAL_FLOOR_PENALTY

MAX_CONSECUTIVE_BLOCK_DAYS = 6
FTE_WEEKLY_THRESHOLD_BUFFER = 8.0
PORTAGE_MAX_CONSECUTIVE_WORK_DAYS = 6
# Portage union: up to five consecutive calendar night shifts; six or more blocks persist.
PORTAGE_MAX_CONSECUTIVE_NIGHTS = 5
PORTAGE_MIN_INTER_BLOCK_REST_DAYS = 2
TRANSITION_BURNOUT_WARNING = "Transition Burnout Warning"
VACANT_LINE_PATTERN = re.compile(r"Line\s+(\d+)", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class QualSeatRequirement:
    qual_code: str
    headcount: int
    pool_id: str


@dataclass(frozen=True, slots=True)
class ShiftConcurrentDemand:
    """Concurrent staffing target for a shift band on selected days."""

    shift_code: str
    seats: Tuple[QualSeatRequirement, ...]
    weekdays_only: bool = False
    weekends_only: bool = False


@dataclass(frozen=True, slots=True)
class ExpandedScheduleSlot:
    assignment_date: date
    shift_id: str
    seat_index: int
    required_qual_code: Optional[str]
    role_pool_id: str


def _daterange(start: date, end_inclusive: date) -> List[date]:
    days: List[date] = []
    cur = start
    while cur <= end_inclusive:
        days.append(cur)
        cur += timedelta(days=1)
    return days


def _is_weekday(d: date) -> bool:
    return d.weekday() < 5


def _is_weekend(d: date) -> bool:
    return d.weekday() >= 5


class DemandTier(str, Enum):
    """Clinical demand priority: immutable floor vs supplemental coverage."""

    HARD_REQUIRED = "hard_required"
    TARGETED = "targeted"


def is_clinical_floor_pool(role_pool_id: str) -> bool:
    return role_pool_id.startswith(CLINICAL_FLOOR_POOL_PREFIX)


def _clinical_floor_seats(shift_label: str) -> Tuple[QualSeatRequirement, ...]:
    """Two explicit clinical seats per band: Seat_01 (MLT) and Seat_02 (MLA)."""
    return (
        QualSeatRequirement(
            "MLT",
            1,
            f"{CLINICAL_FLOOR_POOL_PREFIX} {shift_label} - Seat_01 - MLT",
        ),
        QualSeatRequirement(
            "MLA",
            1,
            f"{CLINICAL_FLOOR_POOL_PREFIX} {shift_label} - Seat_02 - MLA",
        ),
    )


@dataclass(frozen=True, slots=True)
class CoreDemandSpec:
    shift_code: str
    tier: DemandTier
    min_shifts_per_day: int
    concurrent_demands: Tuple[ShiftConcurrentDemand, ...]


def get_core_demands() -> Tuple[CoreDemandSpec, ...]:
    """
    Portage clinical demand tiers.

    Day (Morning), Evening, and Night each require an immutable two-seat clinical floor
    (1 MLT + 1 MLA) on every calendar day. Additional weekday/weekend morning capacity
    remains supplemental targeted coverage above the floor.
    """

    return (
        CoreDemandSpec(
            shift_code="MORNING",
            tier=DemandTier.HARD_REQUIRED,
            min_shifts_per_day=CLINICAL_FLOOR["MORNING"],
            concurrent_demands=(
                ShiftConcurrentDemand(
                    "MORNING",
                    _clinical_floor_seats("Day"),
                ),
                ShiftConcurrentDemand(
                    "MORNING",
                    (QualSeatRequirement("MLT", 6, "Weekday Morning - MLT"),),
                    weekdays_only=True,
                ),
                ShiftConcurrentDemand(
                    "MORNING",
                    (QualSeatRequirement("MLA", 5, "Weekday Morning - MLA"),),
                    weekdays_only=True,
                ),
            ),
        ),
        CoreDemandSpec(
            shift_code="EVENING",
            tier=DemandTier.HARD_REQUIRED,
            min_shifts_per_day=CLINICAL_FLOOR["EVENING"],
            concurrent_demands=(
                ShiftConcurrentDemand(
                    "EVENING",
                    _clinical_floor_seats("Evening"),
                ),
            ),
        ),
        CoreDemandSpec(
            shift_code="NIGHT",
            tier=DemandTier.HARD_REQUIRED,
            min_shifts_per_day=CLINICAL_FLOOR["NIGHT"],
            concurrent_demands=(
                ShiftConcurrentDemand(
                    "NIGHT",
                    _clinical_floor_seats("Night"),
                ),
            ),
        ),
    )


def portage_concurrent_demands() -> Tuple[ShiftConcurrentDemand, ...]:
    """
    Portage lab blueprint demand pools.

    Day (Morning): clinical floor 2 seats/day plus supplemental weekday/weekend pools.
    Evening/Night: immutable clinical floor of 2 seats on every calendar day.

    Expanded labor over an 8-week master rotation is 442 seats × 8h = 3,536h, matching the
    fixed 25-line Portage blueprint contract (sum of FTE × 320h over 8 weeks).
    """

    demands: List[ShiftConcurrentDemand] = []
    for spec in get_core_demands():
        demands.extend(spec.concurrent_demands)
    return tuple(demands)


def _shift_ids_for_band(
    shift_templates: Mapping[str, ShiftTemplateInfo],
    shift_code: str,
) -> Set[str]:
    return {
        shift_id
        for shift_id, template in shift_templates.items()
        if template.code == shift_code
    }


def _night_shift_template_ids(
    shift_templates: Mapping[str, ShiftTemplateInfo],
) -> Set[str]:
    return _shift_ids_for_band(shift_templates, "NIGHT")


def _slot_filled_from_counts(
    slot: ExpandedScheduleSlot,
    fill_counts: Mapping[Tuple[date, str, Optional[str]], int],
    *,
    shift_templates: Optional[Mapping[str, ShiftTemplateInfo]] = None,
) -> bool:
    key = (slot.assignment_date, slot.shift_id, slot.required_qual_code)
    if fill_counts.get(key, 0) > slot.seat_index:
        return True
    from lab_scheduler.scheduling.load_balancing import (
        weekend_morning_slot_credited_as_filled,
    )

    return weekend_morning_slot_credited_as_filled(
        slot,
        fill_counts,
        shift_templates=shift_templates,
    )


def clinical_floor_slots_for_day(
    assignment_date: date,
    shift_code: str,
    expanded_slots: Sequence[ExpandedScheduleSlot],
    *,
    shift_templates: Mapping[str, ShiftTemplateInfo],
) -> Tuple[ExpandedScheduleSlot, ...]:
    """Clinical-floor seat slots for one calendar day and shift band."""

    slots = [
        slot
        for slot in expanded_slots
        if slot.assignment_date == assignment_date
        and shift_templates[slot.shift_id].code == shift_code
    ]
    if shift_code in ("MORNING", "EVENING", "NIGHT"):
        return tuple(slot for slot in slots if is_clinical_floor_pool(slot.role_pool_id))
    return tuple(slots)


def clinical_floor_filled_for_day(
    assignment_date: date,
    shift_code: str,
    *,
    fill_counts: Mapping[Tuple[date, str, Optional[str]], int],
    expanded_slots: Sequence[ExpandedScheduleSlot],
    shift_templates: Mapping[str, ShiftTemplateInfo],
) -> int:
    floor_slots = clinical_floor_slots_for_day(
        assignment_date,
        shift_code,
        expanded_slots,
        shift_templates=shift_templates,
    )
    return sum(
        1
        for slot in floor_slots
        if _slot_filled_from_counts(
            slot,
            fill_counts,
            shift_templates=shift_templates,
        )
    )


def count_band_shifts_by_day(
    assignments: Sequence[object],
    *,
    shift_templates: Mapping[str, ShiftTemplateInfo],
    shift_code: str,
) -> Dict[date, int]:
    band_ids = _shift_ids_for_band(shift_templates, shift_code)
    counts: Dict[date, int] = {}
    for assignment in assignments:
        shift_template_id = getattr(assignment, "shift_template_id", None)
        assignment_date = getattr(assignment, "assignment_date", None)
        if shift_template_id not in band_ids or assignment_date is None:
            continue
        counts[assignment_date] = counts.get(assignment_date, 0) + 1
    return counts


def count_night_shifts_by_day(
    assignments: Sequence[object],
    *,
    shift_templates: Mapping[str, ShiftTemplateInfo],
) -> Dict[date, int]:
    night_ids = _night_shift_template_ids(shift_templates)
    counts: Dict[date, int] = {}
    for assignment in assignments:
        shift_template_id = getattr(assignment, "shift_template_id", None)
        assignment_date = getattr(assignment, "assignment_date", None)
        if shift_template_id not in night_ids or assignment_date is None:
            continue
        counts[assignment_date] = counts.get(assignment_date, 0) + 1
    return counts


def night_shifts_filled_for_day(
    assignment_date: date,
    *,
    fill_counts: Mapping[Tuple[date, str, Optional[str]], int],
    shift_templates: Mapping[str, ShiftTemplateInfo],
    expanded_slots: Optional[Sequence[ExpandedScheduleSlot]] = None,
) -> int:
    if expanded_slots is not None:
        return clinical_floor_filled_for_day(
            assignment_date,
            "NIGHT",
            fill_counts=fill_counts,
            expanded_slots=expanded_slots,
            shift_templates=shift_templates,
        )
    night_ids = _night_shift_template_ids(shift_templates)
    if not night_ids:
        return 0
    night_id = next(iter(night_ids))
    return sum(
        fill_counts.get((assignment_date, night_id, qual_code), 0)
        for qual_code in ("MLT", "MLA")
    )


def clinical_band_filled_for_day(
    assignment_date: date,
    shift_code: str,
    *,
    fill_counts: Mapping[Tuple[date, str, Optional[str]], int],
    shift_templates: Mapping[str, ShiftTemplateInfo],
    expanded_slots: Optional[Sequence[ExpandedScheduleSlot]] = None,
) -> int:
    if expanded_slots is not None:
        return clinical_floor_filled_for_day(
            assignment_date,
            shift_code,
            fill_counts=fill_counts,
            expanded_slots=expanded_slots,
            shift_templates=shift_templates,
        )
    band_ids = _shift_ids_for_band(shift_templates, shift_code)
    total = 0
    for shift_id in band_ids:
        for qual_code in ("MLT", "MLA"):
            total += fill_counts.get((assignment_date, shift_id, qual_code), 0)
    if shift_code == "MORNING":
        return min(total, CLINICAL_FLOOR["MORNING"])
    return total


def is_night_demand_satisfied(
    assignments: Sequence[object],
    *,
    shift_templates: Mapping[str, ShiftTemplateInfo],
    period_start: date,
    period_end: date,
) -> bool:
    """True when every calendar day has exactly the hard-required night seat count (2)."""

    counts = count_night_shifts_by_day(
        assignments,
        shift_templates=shift_templates,
    )
    for assignment_date in _daterange(period_start, period_end):
        if counts.get(assignment_date, 0) != CLINICAL_FLOOR["NIGHT"]:
            return False
    return True


def is_clinical_floor_satisfied(
    *,
    fill_counts: Mapping[Tuple[date, str, Optional[str]], int],
    shift_templates: Mapping[str, ShiftTemplateInfo],
    period_start: date,
    period_end: date,
    expanded_slots: Sequence[ExpandedScheduleSlot],
) -> bool:
    """True when every calendar day has exactly two filled seats per clinical floor band."""

    for assignment_date in _daterange(period_start, period_end):
        for shift_code, _required in CLINICAL_FLOOR.items():
            floor_slots = clinical_floor_slots_for_day(
                assignment_date,
                shift_code,
                expanded_slots,
                shift_templates=shift_templates,
            )
            if not floor_slots:
                continue
            filled = sum(
                1
                for slot in floor_slots
                if _slot_filled_from_counts(
                    slot,
                    fill_counts,
                    shift_templates=shift_templates,
                )
            )
            if filled != len(floor_slots):
                return False
    return True


def is_evening_night_clinical_floor_satisfied(
    *,
    fill_counts: Mapping[Tuple[date, str, Optional[str]], int],
    shift_templates: Mapping[str, ShiftTemplateInfo],
    period_start: date,
    period_end: date,
    expanded_slots: Sequence[ExpandedScheduleSlot],
) -> bool:
    """True when Evening and Night immutable floors are exactly filled every day."""

    for assignment_date in _daterange(period_start, period_end):
        for shift_code in ("EVENING", "NIGHT"):
            floor_slots = clinical_floor_slots_for_day(
                assignment_date,
                shift_code,
                expanded_slots,
                shift_templates=shift_templates,
            )
            if not floor_slots:
                continue
            filled = sum(
                1
                for slot in floor_slots
                if _slot_filled_from_counts(
                    slot,
                    fill_counts,
                    shift_templates=shift_templates,
                )
            )
            if filled != len(floor_slots):
                return False
    return True


def is_demand_satisfied(
    assignments: Sequence[object],
    *,
    shift_templates: Mapping[str, ShiftTemplateInfo],
    period_start: date,
    period_end: date,
    core_demands: Optional[Sequence[CoreDemandSpec]] = None,
    fill_counts: Optional[Mapping[Tuple[date, str, Optional[str]], int]] = None,
    expanded_slots: Optional[Sequence[ExpandedScheduleSlot]] = None,
) -> bool:
    """Verify all immutable clinical floor tiers are met exactly — no exceptions."""

    if fill_counts is not None and expanded_slots is not None:
        return is_clinical_floor_satisfied(
            fill_counts=fill_counts,
            shift_templates=shift_templates,
            period_start=period_start,
            period_end=period_end,
            expanded_slots=expanded_slots,
        )

    for spec in core_demands or get_core_demands():
        if spec.tier != DemandTier.HARD_REQUIRED:
            continue
        counts = count_band_shifts_by_day(
            assignments,
            shift_templates=shift_templates,
            shift_code=spec.shift_code,
        )
        required = CLINICAL_FLOOR[spec.shift_code]
        for assignment_date in _daterange(period_start, period_end):
            if spec.shift_code == "MORNING":
                if counts.get(assignment_date, 0) < required:
                    return False
            elif counts.get(assignment_date, 0) != required:
                return False
    return True


def missing_hard_demand_penalty(
    *,
    shift_template_code: str,
    assignment_date: date,
    night_shifts_filled_for_day_count: int,
    clinical_band_filled_for_day_count: Optional[int] = None,
) -> float:
    """
    Ranking penalty for unfilled immutable clinical floor demand.

    Any band below the two-seat floor is an infinite-priority violation.
    """

    filled = (
        clinical_band_filled_for_day_count
        if clinical_band_filled_for_day_count is not None
        else night_shifts_filled_for_day_count
    )
    band = normalize_shift_band_code(shift_template_code)
    if band not in CLINICAL_FLOOR:
        return 0.0
    if filled < CLINICAL_FLOOR[band]:
        return MISSING_CLINICAL_FLOOR_PENALTY
    return 0.0


def clinical_demand_slot_sort_key(
    slot: ExpandedScheduleSlot,
    *,
    shift_templates: Mapping[str, ShiftTemplateInfo],
    fill_counts: Mapping[Tuple[date, str, Optional[str]], int],
    expanded_slots: Optional[Sequence[ExpandedScheduleSlot]] = None,
) -> Tuple[float, int, date, str, int]:
    """
    Sort open seats for healing: immutable clinical floor gaps first, then smooth balance,
    then supplemental targeted demand.
    """

    shift_code = shift_templates[slot.shift_id].code
    slots_ctx = expanded_slots if expanded_slots is not None else (slot,)
    band_filled = clinical_floor_filled_for_day(
        slot.assignment_date,
        shift_code,
        fill_counts=fill_counts,
        expanded_slots=slots_ctx,
        shift_templates=shift_templates,
    )
    required = CLINICAL_FLOOR.get(shift_code, 0)
    is_floor_slot = is_clinical_floor_pool(slot.role_pool_id) or shift_code in {"EVENING", "NIGHT"}
    if is_floor_slot and band_filled < required:
        priority = -MISSING_CLINICAL_FLOOR_PENALTY
    elif is_smooth_day_balance_pool(slot.role_pool_id):
        priority = -1_000.0
    else:
        priority = 0.0
    smooth_tier = -1 if is_smooth_day_balance_pool(slot.role_pool_id) else 0
    return (priority, smooth_tier, slot.assignment_date, slot.role_pool_id, slot.seat_index)


def portage_expanded_slot_total(
    *,
    period_start: date,
    period_end: date,
    shift_templates: Mapping[str, ShiftTemplateInfo],
) -> int:
    """Expanded seat count for the Portage blueprint over a schedule period."""

    return count_expanded_slots(
        period_start=period_start,
        period_end=period_end,
        shift_templates=shift_templates,
        concurrent_demands=portage_concurrent_demands(),
    )


def portage_expanded_labor_hours(
    *,
    period_start: date,
    period_end: date,
    shift_templates: Mapping[str, ShiftTemplateInfo],
) -> float:
    """Total schedulable labor hours implied by the Portage demand matrix."""

    hours = 0.0
    for slot in expand_schedule_slots(
        period_start=period_start,
        period_end=period_end,
        shift_templates=shift_templates,
        concurrent_demands=portage_concurrent_demands(),
    ):
        template = shift_templates[slot.shift_id]
        hours += template.duration_minutes / 60.0
    return round(hours, 2)


def calendar_weeks_in_period(period_start: date, period_end: date) -> int:
    """Monday-start weeks spanned by an inclusive schedule period."""

    return max(1, (period_end - period_start).days // 7 + 1)


def portage_blueprint_period_contract_hours(
    *,
    weeks_in_period: int = DEFAULT_SCHEDULE_PERIOD_WEEKS,
    standard_weekly_hours: float = 40.0,
) -> float:
    """Sum of contractual period hours across the fixed 25-line Portage blueprint (FTE × weeks × standard)."""

    from lab_scheduler.simulation.portage_blueprint import PORTAGE_LINE_SPECS

    return round(
        sum(spec.fte * standard_weekly_hours * weeks_in_period for spec in PORTAGE_LINE_SPECS),
        2,
    )


def qualification_id_to_code(qual_id: str) -> str:
    """Map tenant qualification ids (e.g. qual-la, qual-mlt) to MLT/MLA role codes."""

    lower = qual_id.lower()
    if "mlt" in lower:
        return "MLT"
    if "mla" in lower or lower.endswith("-la"):
        return "MLA"
    return "MLT"


def build_qual_code_lookup(
    employees: Sequence[EmployeeProfile],
    shift_required_qualifications: Optional[Mapping[str, Set[str]]] = None,
) -> Dict[str, str]:
    """Build a stable qual-id → MLT/MLA map for the active roster and shift templates."""

    qual_ids: Set[str] = set()
    for employee in employees:
        qual_ids.update(employee.qualification_ids)
    if shift_required_qualifications:
        for required in shift_required_qualifications.values():
            qual_ids.update(required)
    return {qual_id: qualification_id_to_code(qual_id) for qual_id in qual_ids if qual_id}


def infer_qual_code(
    employee: EmployeeProfile,
    *,
    qual_codes: Optional[Mapping[str, str]] = None,
) -> str:
    qual_id = next(iter(employee.qualification_ids), "")
    if qual_codes and qual_id in qual_codes:
        return qual_codes[qual_id]
    return qualification_id_to_code(qual_id)


def resolve_seats_for_shift(
    assignment_date: date,
    shift_code: str,
    concurrent_demands: Sequence[ShiftConcurrentDemand],
    *,
    default_seats: Tuple[QualSeatRequirement, ...] = (
        QualSeatRequirement("ANY", 1, "General Coverage"),
    ),
) -> Tuple[QualSeatRequirement, ...]:
    weekday = _is_weekday(assignment_date)
    weekend = _is_weekend(assignment_date)
    matched: List[QualSeatRequirement] = []
    for demand in concurrent_demands:
        if demand.shift_code != shift_code:
            continue
        if demand.weekdays_only and not weekday:
            continue
        if demand.weekends_only and not weekend:
            continue
        matched.extend(demand.seats)
    if matched:
        return tuple(matched)
    if weekend:
        return ()
    return default_seats


SMOOTH_DAY_BALANCE_POOL_PREFIX = "Smooth Day Balance -"


@dataclass(frozen=True, slots=True)
class DemandBalancePlan:
    """Result of pre-flight payroll vs template reconciliation."""

    payroll_supply_hours: float
    baseline_template_hours: float
    balance_slot_count: int
    balance_hours: float
    balance_slots: Tuple[ExpandedScheduleSlot, ...]
    weekday_smooth_slot_count: int

    @property
    def is_balanced(self) -> bool:
        return abs(self.payroll_supply_hours - self.baseline_template_hours - self.balance_hours) < 0.01


def roster_period_contract_hours(
    employees: Sequence[EmployeeProfile],
    *,
    weeks_in_period: int,
    standard_weekly_hours: float,
) -> float:
    """Sum of roster contractual period hours (FTE × standard weekly × weeks)."""

    return round(
        sum(emp.fte * standard_weekly_hours * weeks_in_period for emp in employees),
        2,
    )


def is_smooth_day_balance_pool(role_pool_id: str) -> bool:
    return role_pool_id.startswith(SMOOTH_DAY_BALANCE_POOL_PREFIX)


def is_optional_supplemental_coverage_slot(slot: ExpandedScheduleSlot) -> bool:
    """Targeted supplemental demand above the clinical floor (not persist-blocking)."""

    return is_smooth_day_balance_pool(slot.role_pool_id) or slot.role_pool_id.startswith(
        "Weekday Morning -"
    )


def is_autonomous_balance_pool(role_pool_id: str) -> bool:
    return is_smooth_day_balance_pool(role_pool_id)


def _resolve_payroll_supply_hours(
    employees: Optional[Sequence[EmployeeProfile]],
    *,
    weeks_in_period: int,
    standard_weekly_hours: float,
) -> float:
    if employees:
        return roster_period_contract_hours(
            employees,
            weeks_in_period=weeks_in_period,
            standard_weekly_hours=standard_weekly_hours,
        )
    return portage_blueprint_period_contract_hours(
        weeks_in_period=weeks_in_period,
        standard_weekly_hours=standard_weekly_hours,
    )


def _expand_baseline_template_slots(
    *,
    period_start: date,
    period_end: date,
    shift_templates: Mapping[str, ShiftTemplateInfo],
    concurrent_demands: Sequence[ShiftConcurrentDemand],
) -> List[ExpandedScheduleSlot]:
    """Core Portage template matrix without autonomous balance layers."""

    slots: List[ExpandedScheduleSlot] = []
    for assignment_date in _daterange(period_start, period_end):
        for shift_id in sorted(shift_templates.keys(), key=lambda sid: shift_templates[sid].code):
            shift_code = shift_templates[shift_id].code
            seat_specs = resolve_seats_for_shift(assignment_date, shift_code, concurrent_demands)
            for seat_spec in seat_specs:
                qual_code = None if seat_spec.qual_code == "ANY" else seat_spec.qual_code
                for seat_index in range(seat_spec.headcount):
                    slots.append(
                        ExpandedScheduleSlot(
                            assignment_date=assignment_date,
                            shift_id=shift_id,
                            seat_index=seat_index,
                            required_qual_code=qual_code,
                            role_pool_id=seat_spec.pool_id,
                        )
                    )
    return slots


def _baseline_template_hours(
    *,
    period_start: date,
    period_end: date,
    shift_templates: Mapping[str, ShiftTemplateInfo],
    concurrent_demands: Sequence[ShiftConcurrentDemand],
) -> float:
    hours = 0.0
    for slot in _expand_baseline_template_slots(
        period_start=period_start,
        period_end=period_end,
        shift_templates=shift_templates,
        concurrent_demands=concurrent_demands,
    ):
        template = shift_templates[slot.shift_id]
        hours += template.duration_minutes / 60.0
    return round(hours, 2)


class AutonomousDemandBalancer:
    """
    Pre-flight reconciliation: align expanded demand slots with roster payroll supply.

    Uses smooth seat injection — one weekday morning seat per deficit shift, distributed
    sequentially across weekdays (day 1 → day N) until matrix hours match payroll supply.
    """

    def __init__(
        self,
        *,
        period_start: date,
        period_end: date,
        shift_templates: Mapping[str, ShiftTemplateInfo],
        concurrent_demands: Sequence[ShiftConcurrentDemand],
        employees: Optional[Sequence[EmployeeProfile]] = None,
        rules: Optional[JurisdictionRules] = None,
        weeks_in_period: int = DEFAULT_SCHEDULE_PERIOD_WEEKS,
    ) -> None:
        self.period_start = period_start
        self.period_end = period_end
        self.shift_templates = shift_templates
        self.concurrent_demands = concurrent_demands
        self.employees = employees
        self.rules = rules
        self.weeks_in_period = weeks_in_period

    @property
    def _standard_weekly_hours(self) -> float:
        if self.rules is not None:
            return self.rules.standard_hours_per_week_at_1_0_fte
        return 40.0

    @property
    def _morning_shift_hours(self) -> float:
        morning_id = _morning_shift_id(self.shift_templates)
        return self.shift_templates[morning_id].duration_minutes / 60.0

    def payroll_supply_hours(self) -> float:
        return _resolve_payroll_supply_hours(
            self.employees,
            weeks_in_period=self.weeks_in_period,
            standard_weekly_hours=self._standard_weekly_hours,
        )

    def baseline_template_hours(self) -> float:
        return _baseline_template_hours(
            period_start=self.period_start,
            period_end=self.period_end,
            shift_templates=self.shift_templates,
            concurrent_demands=self.concurrent_demands,
        )

    def reconcile(self) -> DemandBalancePlan:
        payroll = self.payroll_supply_hours()
        baseline = self.baseline_template_hours()
        deficit_hours = round(payroll - baseline, 2)

        if deficit_hours <= 0.01:
            return DemandBalancePlan(
                payroll_supply_hours=payroll,
                baseline_template_hours=baseline,
                balance_slot_count=0,
                balance_hours=0.0,
                balance_slots=(),
                weekday_smooth_slot_count=0,
            )

        morning_hours = self._morning_shift_hours
        remaining_slots = int(round(deficit_hours / morning_hours))
        if remaining_slots * morning_hours != deficit_hours:
            remaining_slots = int(deficit_hours // morning_hours)
            if remaining_slots * morning_hours < deficit_hours:
                remaining_slots += 1

        smooth_slots = self._build_smooth_weekday_seats(remaining_slots)
        balance_hours = round(len(smooth_slots) * morning_hours, 2)

        return DemandBalancePlan(
            payroll_supply_hours=payroll,
            baseline_template_hours=baseline,
            balance_slot_count=len(smooth_slots),
            balance_hours=balance_hours,
            balance_slots=tuple(smooth_slots),
            weekday_smooth_slot_count=len(smooth_slots),
        )

    def _build_smooth_weekday_seats(self, count: int) -> List[ExpandedScheduleSlot]:
        """Distribute deficit shifts one per weekday, cycling day 1 → day N."""

        weekdays = [
            day
            for day in _daterange(self.period_start, self.period_end)
            if _is_weekday(day)
        ]
        if not count or not weekdays:
            return []

        morning_id = _morning_shift_id(self.shift_templates)
        smooth_slots: List[ExpandedScheduleSlot] = []
        for index in range(count):
            assignment_date = weekdays[index % len(weekdays)]
            day_sequence = (index % len(weekdays)) + 1
            qual_code = "MLT" if index % 2 == 0 else "MLA"
            smooth_slots.append(
                ExpandedScheduleSlot(
                    assignment_date=assignment_date,
                    shift_id=morning_id,
                    seat_index=850 + index,
                    required_qual_code=qual_code,
                    role_pool_id=(
                        f"{SMOOTH_DAY_BALANCE_POOL_PREFIX} {qual_code} - Day {day_sequence:02d}"
                    ),
                )
            )
        return smooth_slots


def _reconcile_portage_balance_slots(
    *,
    period_start: date,
    period_end: date,
    shift_templates: Mapping[str, ShiftTemplateInfo],
    concurrent_demands: Sequence[ShiftConcurrentDemand],
    employees: Optional[Sequence[EmployeeProfile]] = None,
    rules: Optional[JurisdictionRules] = None,
    weeks_in_period: int = DEFAULT_SCHEDULE_PERIOD_WEEKS,
) -> Tuple[ExpandedScheduleSlot, ...]:
    if not _uses_portage_concurrent_demands(concurrent_demands):
        return ()
    return AutonomousDemandBalancer(
        period_start=period_start,
        period_end=period_end,
        shift_templates=shift_templates,
        concurrent_demands=concurrent_demands,
        employees=employees,
        rules=rules,
        weeks_in_period=weeks_in_period,
    ).reconcile().balance_slots


def _morning_shift_id(shift_templates: Mapping[str, ShiftTemplateInfo]) -> str:
    for shift_id, template in shift_templates.items():
        if template.code == "MORNING":
            return shift_id
    return next(iter(shift_templates.keys()))


def _uses_portage_concurrent_demands(
    concurrent_demands: Optional[Sequence[ShiftConcurrentDemand]],
) -> bool:
    if not concurrent_demands:
        return False
    return tuple(concurrent_demands) == portage_concurrent_demands()


PORTAGE_OPERATIONAL_SHIFT_CODES = frozenset({"MORNING", "EVENING", "NIGHT"})


def filter_portage_operational_shift_templates(
    shift_templates: Mapping[str, ShiftTemplateInfo],
) -> Dict[str, ShiftTemplateInfo]:
    """Portage M/E/N generate must ignore ancillary templates (e.g. 12h FTE top-up)."""

    return {
        shift_id: template
        for shift_id, template in shift_templates.items()
        if template.code in PORTAGE_OPERATIONAL_SHIFT_CODES
    }


def expand_schedule_slots(
    *,
    period_start: date,
    period_end: date,
    shift_templates: Mapping[str, ShiftTemplateInfo],
    concurrent_demands: Optional[Sequence[ShiftConcurrentDemand]] = None,
    employees: Optional[Sequence[EmployeeProfile]] = None,
    rules: Optional[JurisdictionRules] = None,
    weeks_in_period: int = DEFAULT_SCHEDULE_PERIOD_WEEKS,
    supplemental_balance_slots: Optional[Sequence[ExpandedScheduleSlot]] = None,
) -> List[ExpandedScheduleSlot]:
    """Expand (date, shift) pairs into per-seat slots for multi-person coverage."""

    effective_weeks = calendar_weeks_in_period(period_start, period_end)
    slots: List[ExpandedScheduleSlot] = []
    demands = concurrent_demands or ()
    use_multi = len(demands) > 0

    for assignment_date in _daterange(period_start, period_end):
        for shift_id in sorted(shift_templates.keys(), key=lambda sid: shift_templates[sid].code):
            shift_code = shift_templates[shift_id].code
            if use_multi:
                seat_specs = resolve_seats_for_shift(assignment_date, shift_code, demands)
            else:
                seat_specs = (QualSeatRequirement("ANY", 1, "General Coverage"),)

            for seat_spec in seat_specs:
                qual_code = None if seat_spec.qual_code == "ANY" else seat_spec.qual_code
                for seat_index in range(seat_spec.headcount):
                    slots.append(
                        ExpandedScheduleSlot(
                            assignment_date=assignment_date,
                            shift_id=shift_id,
                            seat_index=seat_index,
                            required_qual_code=qual_code,
                            role_pool_id=seat_spec.pool_id,
                        )
                    )

    if supplemental_balance_slots is not None:
        slots.extend(supplemental_balance_slots)
    elif _uses_portage_concurrent_demands(concurrent_demands):
        slots.extend(
            _reconcile_portage_balance_slots(
                period_start=period_start,
                period_end=period_end,
                shift_templates=shift_templates,
                concurrent_demands=demands,
                employees=employees,
                rules=rules,
                weeks_in_period=effective_weeks,
            )
        )
    return slots


def autonomous_balance_slot_sort_key(slot: ExpandedScheduleSlot) -> Tuple[int, date, str, int]:
    """Process smooth payroll-balance day seats before core template demand."""

    if is_smooth_day_balance_pool(slot.role_pool_id):
        return (-1, slot.assignment_date, slot.role_pool_id, slot.seat_index)
    return (0, slot.assignment_date, slot.role_pool_id, slot.seat_index)


def smooth_day_balance_allocation_weight(
    profile: EmployeeProfile,
    *,
    total_hours: float,
    period_target: float,
    role_pool_id: Optional[str],
) -> float:
    """
    High-priority weight for straight-time 1.0 FTE lines filling smooth balance seats.
    Lower tuple values rank earlier.
    """

    if not role_pool_id or not is_smooth_day_balance_pool(role_pool_id):
        return 0.0
    if profile.fte < 0.99:
        return 0.0
    hour_deficit = float(period_target) - total_hours
    if hour_deficit <= 0.5:
        return 0.0
    return -120.0 - hour_deficit


def count_expanded_slots(
    *,
    period_start: date,
    period_end: date,
    shift_templates: Mapping[str, ShiftTemplateInfo],
    concurrent_demands: Optional[Sequence[ShiftConcurrentDemand]] = None,
) -> int:
    return len(
        expand_schedule_slots(
            period_start=period_start,
            period_end=period_end,
            shift_templates=shift_templates,
            concurrent_demands=concurrent_demands,
        )
    )


def parse_vacant_line_number(full_name: str) -> Optional[int]:
    match = VACANT_LINE_PATTERN.search(full_name)
    if not match:
        return None
    return int(match.group(1))


def roster_line_number(profile: EmployeeProfile) -> Optional[int]:
    vacant = parse_vacant_line_number(profile.full_name)
    if vacant is not None:
        return vacant
    suffix = profile.id.rsplit("-", 1)[-1]
    if suffix.isdigit():
        return int(suffix)
    return None


def weekly_contract_hours_target(profile: EmployeeProfile, rules: JurisdictionRules) -> float:
    return rules.standard_hours_per_week_at_1_0_fte * profile.fte


def _worked_on(work_dates: Set[date], day: date) -> bool:
    return day in work_dates


def weekend_paired_day_rank_penalty(
    *,
    work_dates: Set[date],
    assignment_date: date,
) -> float:
    """
    Ranking penalty (lower is better) for weekend slots.

    Prefer extending an employee who already works the paired Sat/Sun day so the
    same person covers the full weekend block instead of splitting days across staff.
    """

    if assignment_date.weekday() == 5:
        paired_day = assignment_date + timedelta(days=1)
    elif assignment_date.weekday() == 6:
        paired_day = assignment_date - timedelta(days=1)
    else:
        return 0.0
    return 0.0 if paired_day in work_dates else 1.0


def isolated_shift_penalty(
    *,
    work_dates: Set[date],
    assignment_date: date,
    period_start: date,
    period_end: date,
) -> float:
    """
    Penalize shifts that would be isolated (non-working neighbors on both sides
    within the same Mon–Sun work week).
    """

    week_start = workweek_for(assignment_date).start
    week_end = min(week_start + timedelta(days=6), period_end)
    prev_day = assignment_date - timedelta(days=1)
    next_day = assignment_date + timedelta(days=1)

    prev_in_week = week_start <= prev_day <= week_end
    next_in_week = week_start <= next_day <= week_end
    if not prev_in_week or not next_in_week:
        return 0.0

    prev_off = not _worked_on(work_dates, prev_day)
    next_off = not _worked_on(work_dates, next_day)
    return 1.0 if prev_off and next_off else 0.0


def consecutive_block_bonus(
    *,
    work_dates: Set[date],
    assignment_records: Sequence[Tuple[date, str]],
    assignment_date: date,
    shift_id: str,
    shift_hours: float,
    week_hours: Mapping[date, float],
    profile: EmployeeProfile,
    rules: JurisdictionRules,
    shift_template_code: str = "",
    period_target_hours: float = 0.0,
    total_hours: float = 0.0,
    role_pool_id: Optional[str] = None,
) -> float:
    """
    Prefer extending an existing consecutive work block (up to 5 days) until the
    employee's contractual weekly hours target is met for that work week.
    """

    simulated_dates = sorted(work_dates | {assignment_date})
    streak = 1
    for _start, _end, length in _consecutive_work_day_streaks(simulated_dates):
        if _start <= assignment_date <= _end:
            streak = length
            break

    week_start = workweek_for(assignment_date).start
    weekly_target = weekly_contract_hours_target(profile, rules)
    current_week_hours = week_hours.get(week_start, 0.0)
    after_week_hours = current_week_hours + shift_hours

    if role_pool_id and is_smooth_day_balance_pool(role_pool_id):
        if period_target_hours > 0.0 and total_hours + shift_hours <= period_target_hours + 1e-6:
            return -12.0
        return 0.0

    bonus = 0.0
    yesterday = assignment_date - timedelta(days=1)
    if _worked_on(work_dates, yesterday):
        bonus -= 2.5
        if any(d == yesterday and tid == shift_id for d, tid in assignment_records):
            bonus -= 1.5
        if role_pool_id and is_smooth_day_balance_pool(role_pool_id):
            bonus -= 1.0

    if streak >= 2:
        bonus -= min(float(streak - 1) * 0.5, 2.0)

    if current_week_hours >= weekly_target - 1e-6:
        bonus += 2.0
    elif after_week_hours <= weekly_target + 1e-6 and bonus < 0:
        bonus -= 1.0

    if streak >= PORTAGE_MAX_CONSECUTIVE_WORK_DAYS:
        contract_mf = (profile.contract_line_type or "") == "M-F"
        filling_period = (
            period_target_hours > 0.0
            and total_hours + shift_hours < period_target_hours - 8.0
        )
        weekday_morning = (
            assignment_date.weekday() < 5
            and normalize_shift_band_code(shift_template_code) == "MORNING"
        )
        if contract_mf and weekday_morning and filling_period:
            bonus += 2.0
        else:
            bonus += 25.0
    elif streak > MAX_CONSECUTIVE_BLOCK_DAYS:
        bonus += 5.0

    return bonus


def fatigue_guardrail_violation(
    work_dates: Set[date],
    assignment_date: date,
    *,
    modified_work_schedule: bool = False,
    max_consecutive_work_days: int = 12,
) -> Optional[str]:
    """
    Portage humane scheduling: max 6 consecutive work days, then 2 calendar days off
    before the next work block may start.

    Staff on a modified work schedule may exceed the 6-day Portage cap up to the
    jurisdiction maximum (12 days in Manitoba).
    """

    simulated_dates = sorted(work_dates | {assignment_date})
    streak_limit = (
        max_consecutive_work_days
        if modified_work_schedule
        else PORTAGE_MAX_CONSECUTIVE_WORK_DAYS
    )
    for _start, _end, length in _consecutive_work_day_streaks(simulated_dates):
        if length > streak_limit:
            return (
                f"would exceed {streak_limit} consecutive work days "
                f"without a {PORTAGE_MIN_INTER_BLOCK_REST_DAYS}-day rest block"
            )

    prior_work = [work_day for work_day in work_dates if work_day < assignment_date]
    if not prior_work:
        return None

    last_work = max(prior_work)
    gap_days = (assignment_date - last_work).days
    if gap_days <= 1:
        return None

    off_days = gap_days - 1
    if off_days < PORTAGE_MIN_INTER_BLOCK_REST_DAYS:
        return (
            f"would violate {PORTAGE_MIN_INTER_BLOCK_REST_DAYS}-day rest block between "
            f"work streaks (only {off_days} day(s) off since {last_work.isoformat()})"
        )
    return None


def _day_night_calendar_band(shift_code: str) -> Optional[str]:
    """Map shift band to D/N calendar tokens for asymmetric transition rule."""

    band = normalize_shift_band_code(shift_code)
    if band == "MORNING":
        return "D"
    if band == "NIGHT":
        return "N"
    return None


def asymmetric_shift_transition_violation(
    assignment_records: Sequence[Tuple[date, str]],
    assignment_date: date,
    proposed_shift_code: str,
    shift_templates: Mapping[str, ShiftTemplateInfo],
) -> Optional[str]:
    """
    Asymmetric shift transition guardrail (Portage).

    Illegal: Day (D) on calendar day T-1 followed by Night (N) on day T — sequence [D, N].
    Illegal: Night (N) on calendar day T-1 followed by Day (D) on day T — sequence [N, D].
    """

    proposed = _day_night_calendar_band(proposed_shift_code)
    if proposed is None:
        return None

    by_date: Dict[date, str] = {work_date: template_id for work_date, template_id in assignment_records}

    def band_on(work_date: date) -> Optional[str]:
        template_id = by_date.get(work_date)
        if template_id is None:
            return None
        template = shift_templates.get(template_id)
        if template is None:
            return None
        return _day_night_calendar_band(template.code)

    prev_day = assignment_date - timedelta(days=1)
    next_day = assignment_date + timedelta(days=1)

    if proposed == "N" and band_on(prev_day) == "D":
        return (
            f"{TRANSITION_BURNOUT_WARNING}: Day shift on {prev_day.isoformat()} cannot be "
            f"followed by a Night shift on {assignment_date.isoformat()}."
        )

    if proposed == "D" and band_on(prev_day) == "N":
        return (
            f"{TRANSITION_BURNOUT_WARNING}: Night shift on {prev_day.isoformat()} cannot be "
            f"followed by a Day shift on {assignment_date.isoformat()}."
        )

    if proposed == "D" and band_on(next_day) == "N":
        return (
            f"{TRANSITION_BURNOUT_WARNING}: Day shift on {assignment_date.isoformat()} cannot "
            f"precede a Night shift on {next_day.isoformat()}."
        )

    return None


def is_transition_burnout_violation(message: Optional[str]) -> bool:
    return bool(message and TRANSITION_BURNOUT_WARNING in message)


def find_day_night_transition_violations(
    assignments: Sequence[Tuple[str, date, str]],
    shift_templates: Mapping[str, ShiftTemplateInfo],
) -> List[Tuple[str, date, date]]:
    """
    Scan employee assignments for illegal [D, N] on consecutive calendar days.

    Each item is ``(employee_id, day_band_d, day_band_n)``.
    """

    by_employee: Dict[str, Dict[date, str]] = {}
    for employee_id, assignment_date, template_id in assignments:
        by_employee.setdefault(employee_id, {})[assignment_date] = template_id

    violations: List[Tuple[str, date, date]] = []
    for employee_id, dated in by_employee.items():
        for work_date in sorted(dated):
            next_day = work_date + timedelta(days=1)
            if next_day not in dated:
                continue
            day_template = shift_templates.get(dated[work_date])
            night_template = shift_templates.get(dated[next_day])
            if day_template is None or night_template is None:
                continue
            if (
                _day_night_calendar_band(day_template.code) == "D"
                and _day_night_calendar_band(night_template.code) == "N"
            ):
                violations.append((employee_id, work_date, next_day))
    return violations


def find_night_day_transition_violations(
    assignments: Sequence[Tuple[str, date, str]],
    shift_templates: Mapping[str, ShiftTemplateInfo],
) -> List[Tuple[str, date, date]]:
    """
    Scan employee assignments for illegal [N, D] on consecutive calendar days.

    Each item is ``(employee_id, night_day, day_day)``.
    """

    by_employee: Dict[str, Dict[date, str]] = {}
    for employee_id, assignment_date, template_id in assignments:
        by_employee.setdefault(employee_id, {})[assignment_date] = template_id

    violations: List[Tuple[str, date, date]] = []
    for employee_id, dated in by_employee.items():
        for work_date in sorted(dated):
            next_day = work_date + timedelta(days=1)
            if next_day not in dated:
                continue
            night_template = shift_templates.get(dated[work_date])
            day_template = shift_templates.get(dated[next_day])
            if night_template is None or day_template is None:
                continue
            if (
                _day_night_calendar_band(night_template.code) == "N"
                and _day_night_calendar_band(day_template.code) == "D"
            ):
                violations.append((employee_id, work_date, next_day))
    return violations


def horizontal_workload_balance_key(
    total_hours: float,
    period_target_hours: float,
) -> Tuple[float, float]:
    """
    Ascending sort prefers underloaded lines and penalizes hour-heavy rows.

    Returns ``(-deficit_ratio, load_penalty)`` so starving lines rank ahead of
    136h-overloaded peers.
    """

    target = max(float(period_target_hours), 1e-6)
    fill_ratio = total_hours / target
    deficit_ratio = max(0.0, (target - total_hours) / target)
    if fill_ratio > 1.0:
        load_penalty = 50.0 + (fill_ratio - 1.0) * 120.0 + max(0.0, total_hours - target) * 0.5
    elif fill_ratio > 0.85:
        load_penalty = 15.0 + fill_ratio * 10.0
    else:
        load_penalty = fill_ratio * 8.0
    return (-deficit_ratio, load_penalty)


def _shift_band_from_template_id(shift_id: str) -> str:
    lowered = shift_id.lower()
    if "morning" in lowered:
        return "MORNING"
    if "evening" in lowered:
        return "EVENING"
    if "night" in lowered:
        return "NIGHT"
    return ""


def contract_band_weave_penalty(
    *,
    contract_line_type: Optional[str],
    assignment_records: Sequence[Tuple[date, str]],
    assignment_date: date,
    shift_template_code: str,
) -> float:
    """
    Discourage consecutive calendar weeks dominated by a single allowed band.

    For D/E and D/N lines, prefer alternating Day vs Evening or Day vs Night when
    the prior work week was >=85% one band.
    """

    line = normalize_contract_line_type(contract_line_type or "")
    if line not in {"D/E", "D/N"}:
        return 0.0

    allowed = allowed_shift_codes_for_contract_line(line)
    if len(allowed) < 2:
        return 0.0

    week_start = workweek_for(assignment_date).start
    prev_week_start = week_start - timedelta(days=7)
    prev_week_bands = [
        _shift_band_from_template_id(shift_id)
        for work_date, shift_id in assignment_records
        if prev_week_start <= work_date < week_start
        and _shift_band_from_template_id(shift_id) in allowed
    ]
    if len(prev_week_bands) < 4:
        return 0.0

    dominant = max(set(prev_week_bands), key=prev_week_bands.count)
    dominance_ratio = prev_week_bands.count(dominant) / len(prev_week_bands)
    if dominance_ratio < 0.85:
        return 0.0

    proposed = normalize_shift_band_code(shift_template_code)
    if proposed == dominant:
        return 8.0
    if proposed in allowed:
        return -4.0
    return 0.0


def horizontal_week_peer_balance_penalty(
    profile: EmployeeProfile,
    *,
    assignment_date: date,
    week_hours: Mapping[date, float],
    employees: Sequence[EmployeeProfile],
    employee_total_hours: Mapping[str, float],
    employee_target_hours: Mapping[str, float],
    qual_codes: Optional[Mapping[str, str]] = None,
    deficit_threshold: float = 0.20,
    role_pool_id: Optional[str] = None,
) -> float:
    """
    Penalize assigning another shift this week on a line that already worked when
    a qualified peer on the same contract band is materially below hour target.
    """

    if role_pool_id and is_smooth_day_balance_pool(role_pool_id):
        return 0.0

    week_start = workweek_for(assignment_date).start
    if week_hours.get(week_start, 0.0) <= 0.01:
        return 0.0

    my_qual = infer_qual_code(profile, qual_codes=qual_codes)
    my_contract = profile.contract_line_type or ""
    peer_under_target = False
    for other in employees:
        if other.id == profile.id:
            continue
        if infer_qual_code(other, qual_codes=qual_codes) != my_qual:
            continue
        if (other.contract_line_type or "") != my_contract:
            continue
        other_target = float(employee_target_hours.get(other.id, 0.0))
        if other_target <= 0.0:
            continue
        other_hours = float(employee_total_hours.get(other.id, 0.0))
        deficit_ratio = (other_target - other_hours) / other_target
        if deficit_ratio >= deficit_threshold:
            peer_under_target = True
            break

    if not peer_under_target:
        return 0.0

    my_target = float(employee_target_hours.get(profile.id, 1.0))
    my_hours = float(employee_total_hours.get(profile.id, 0.0))
    load_ratio = my_hours / max(my_target, 1e-6)
    penalty = 14.0 + min(load_ratio, 1.5) * 10.0
    if my_hours + 8.0 < my_target:
        return penalty * 0.2
    return penalty


def line_continuity_penalty(
    profile: EmployeeProfile,
    *,
    employees: Sequence[EmployeeProfile],
    employee_total_hours: Mapping[str, float],
    employee_target_hours: Mapping[str, float],
    qual_codes: Optional[Mapping[str, str]] = None,
) -> float:
    """
    For bulk-provisioned vacant contract lines, penalize seeding higher line numbers
    while lower-numbered vacant lines remain below their FTE hour targets.
    """

    my_line = parse_vacant_line_number(profile.full_name)
    if my_line is None:
        return 0.0

    my_qual = infer_qual_code(profile, qual_codes=qual_codes)
    my_contract = profile.contract_line_type or ""

    for other in employees:
        other_line = parse_vacant_line_number(other.full_name)
        if other_line is None or other_line >= my_line:
            continue
        if infer_qual_code(other, qual_codes=qual_codes) != my_qual:
            continue
        if (other.contract_line_type or "") != my_contract:
            continue
        other_target = employee_target_hours.get(other.id, 0.0)
        other_hours = employee_total_hours.get(other.id, 0.0)
        if other_hours + 1e-6 < other_target:
            return float(my_line - other_line) * 10.0
    return 0.0


def employee_matches_seat_qual(
    employee: EmployeeProfile,
    required_qual_code: Optional[str],
    *,
    qual_codes: Optional[Mapping[str, str]] = None,
    shift_required_qualification_ids: Optional[Set[str]] = None,
) -> bool:
    """Strict role-lock: pool seats accept only the designated qualification role."""

    if shift_required_qualification_ids:
        if not employee.qualification_ids & shift_required_qualification_ids:
            return False
    if required_qual_code is None:
        return True
    if len(employee.qualification_ids) != 1:
        return False
    return infer_qual_code(employee, qual_codes=qual_codes) == required_qual_code


def build_assignment_rank_key(
    *,
    profile: EmployeeProfile,
    work_dates: Set[date],
    assignment_records: Sequence[Tuple[date, str]],
    week_hours: Mapping[date, float],
    total_hours: float,
    assignment_date: date,
    shift_id: str,
    shift_hours: float,
    shift_template_code: str,
    rules: JurisdictionRules,
    period_start: date,
    period_end: date,
    employees: Sequence[EmployeeProfile],
    employee_total_hours: Mapping[str, float],
    employee_target_hours: Mapping[str, float],
    qual_codes: Optional[Mapping[str, str]],
    prioritize_coverage: bool,
    period_target_hours: Optional[Mapping[str, float]],
    coverage_priority_key_fn,
    cba_rank_key_fn,
    role_pool_id: Optional[str] = None,
    night_shifts_filled_for_day_count: Optional[int] = None,
    weekday_daily_staffing_penalty: float = 0.0,
) -> Tuple[float, ...]:
    """
    Ranking funnel with horizontal workload smoothing and an FTE weekly buffer.

    While an employee remains more than ``FTE_WEEKLY_THRESHOLD_BUFFER`` hours below
    their weekly contract target, consecutive block bonus can outrank coverage deficit
    after underloaded lines are prioritized.
    """

    line_penalty = line_continuity_penalty(
        profile,
        employees=employees,
        employee_total_hours=employee_total_hours,
        employee_target_hours=employee_target_hours,
        qual_codes=qual_codes,
    )
    period_target = (period_target_hours or {}).get(
        profile.id,
        employee_target_hours.get(profile.id, weekly_contract_hours_target(profile, rules)),
    )
    workload_key = horizontal_workload_balance_key(total_hours, period_target)
    period_hour_deficit = float(period_target) - total_hours
    filling_contract_hours = period_hour_deficit > 8.0
    smooth_balance_slot = is_smooth_day_balance_pool(role_pool_id or "")
    allocation_weight = smooth_day_balance_allocation_weight(
        profile,
        total_hours=total_hours,
        period_target=float(period_target),
        role_pool_id=role_pool_id,
    )
    weave_scale = 0.1 if smooth_balance_slot else (0.25 if filling_contract_hours else 1.0)
    weave_penalty = contract_band_weave_penalty(
        contract_line_type=profile.contract_line_type,
        assignment_records=assignment_records,
        assignment_date=assignment_date,
        shift_template_code=shift_template_code,
    ) * weave_scale
    week_peer_penalty = horizontal_week_peer_balance_penalty(
        profile,
        assignment_date=assignment_date,
        week_hours=week_hours,
        employees=employees,
        employee_total_hours=employee_total_hours,
        employee_target_hours=employee_target_hours,
        qual_codes=qual_codes,
        role_pool_id=role_pool_id,
    )
    if smooth_balance_slot or filling_contract_hours:
        week_peer_penalty *= 0.05 if smooth_balance_slot else 0.15
    isolated = isolated_shift_penalty(
        work_dates=work_dates,
        assignment_date=assignment_date,
        period_start=period_start,
        period_end=period_end,
    )
    weekend_pair_penalty = weekend_paired_day_rank_penalty(
        work_dates=work_dates,
        assignment_date=assignment_date,
    )
    if smooth_balance_slot:
        isolated *= 0.1
        line_penalty *= 0.1
        weekend_pair_penalty *= 0.1
    block_bonus = consecutive_block_bonus(
        work_dates=work_dates,
        assignment_records=assignment_records,
        assignment_date=assignment_date,
        shift_id=shift_id,
        shift_hours=shift_hours,
        week_hours=week_hours,
        profile=profile,
        rules=rules,
        shift_template_code=shift_template_code,
        period_target_hours=float(period_target),
        total_hours=total_hours,
        role_pool_id=role_pool_id,
    )

    hard_priority = -missing_hard_demand_penalty(
        shift_template_code=shift_template_code,
        assignment_date=assignment_date,
        night_shifts_filled_for_day_count=(
            night_shifts_filled_for_day_count
            if night_shifts_filled_for_day_count is not None
            else 0
        ),
        clinical_band_filled_for_day_count=night_shifts_filled_for_day_count,
    )
    load_balance_penalty = float(weekday_daily_staffing_penalty)

    week_start = workweek_for(assignment_date).start
    weekly_target = weekly_contract_hours_target(profile, rules)
    current_week_hours = week_hours.get(week_start, 0.0)
    still_building_weekly_block = (
        current_week_hours + FTE_WEEKLY_THRESHOLD_BUFFER < weekly_target
    )

    if prioritize_coverage:
        coverage_key = coverage_priority_key_fn(profile, total_hours, period_target)
        if filling_contract_hours:
            return (
                hard_priority,
                load_balance_penalty,
                weekend_pair_penalty,
                allocation_weight,
                workload_key[0],
                workload_key[1],
                weave_penalty,
                coverage_key[0],
                coverage_key[1],
                line_penalty,
                isolated,
                week_peer_penalty,
                block_bonus,
                coverage_key[2],
                coverage_key[3],
                coverage_key[4],
            )
        if still_building_weekly_block:
            return (
                hard_priority,
                load_balance_penalty,
                weekend_pair_penalty,
                allocation_weight,
                workload_key[0],
                workload_key[1],
                weave_penalty,
                week_peer_penalty,
                line_penalty,
                isolated,
                block_bonus,
                coverage_key[0],
                coverage_key[1],
                coverage_key[2],
                coverage_key[3],
                coverage_key[4],
            )
        return (
            hard_priority,
            load_balance_penalty,
            weekend_pair_penalty,
            allocation_weight,
            workload_key[0],
            workload_key[1],
            weave_penalty,
            week_peer_penalty,
            coverage_key[0],
            coverage_key[1],
            line_penalty,
            isolated,
            block_bonus,
            coverage_key[2],
            coverage_key[3],
            coverage_key[4],
        )

    cba_key = cba_rank_key_fn(profile)
    if still_building_weekly_block:
        return (
            hard_priority,
            load_balance_penalty,
            weekend_pair_penalty,
            allocation_weight,
            workload_key[0],
            workload_key[1],
            weave_penalty,
            week_peer_penalty,
            block_bonus,
            line_penalty,
            isolated,
            *cba_key,
        )
    return (
        hard_priority,
        load_balance_penalty,
        weekend_pair_penalty,
        allocation_weight,
        workload_key[0],
        workload_key[1],
        weave_penalty,
        week_peer_penalty,
        line_penalty,
        isolated,
        *cba_key,
        block_bonus,
    )


def assess_concurrent_capacity_shortfall(
    *,
    employees: Sequence[EmployeeProfile],
    shift_templates: Mapping[str, ShiftTemplateInfo],
    shift_required_qualifications: Mapping[str, Set[str]],
    period_start: date,
    period_end: date,
    weeks_in_period: int,
    rules: JurisdictionRules,
    concurrent_demands: Sequence[ShiftConcurrentDemand],
    qual_codes: Optional[Mapping[str, str]] = None,
) -> Tuple[Set[Tuple[date, str, Optional[str], int]], Set[str]]:
    """
    Detect expanded seat slots that cannot be filled given roster capacity.

    Returns (impossible_seat_keys, impossible_tier_ids).
    Seat key: (date, shift_id, required_qual_code, seat_index).
    """

    impossible_seats: Set[Tuple[date, str, Optional[str], int]] = set()
    impossible_tiers: Set[str] = set()
    weekly_standard = rules.standard_hours_per_week_at_1_0_fte
    max_period_hours = weekly_standard * weeks_in_period

    required_hours_by_qual: Dict[str, float] = {}
    seat_count_by_qual: Dict[str, int] = {}

    expanded = expand_schedule_slots(
        period_start=period_start,
        period_end=period_end,
        shift_templates=shift_templates,
        concurrent_demands=concurrent_demands,
    )

    for slot in expanded:
        qual_key = slot.required_qual_code or "ANY"
        template = shift_templates[slot.shift_id]
        hours = template.duration_minutes / 60.0
        required_hours_by_qual[qual_key] = required_hours_by_qual.get(qual_key, 0.0) + hours
        seat_count_by_qual[qual_key] = seat_count_by_qual.get(qual_key, 0) + 1

        qualified = [
            emp
            for emp in employees
            if employee_matches_seat_qual(
                emp,
                slot.required_qual_code,
                qual_codes=qual_codes,
                shift_required_qualification_ids=shift_required_qualifications.get(slot.shift_id),
            )
        ]
        if not qualified:
            impossible_seats.add(
                (slot.assignment_date, slot.shift_id, slot.required_qual_code, slot.seat_index)
            )

    for qual_key, required_hours in required_hours_by_qual.items():
        if qual_key == "ANY":
            continue
        qualified = [
            emp
            for emp in employees
            if infer_qual_code(emp, qual_codes=qual_codes) == qual_key
        ]
        if not qualified:
            for slot in expanded:
                if slot.required_qual_code == qual_key:
                    impossible_seats.add(
                        (slot.assignment_date, slot.shift_id, slot.required_qual_code, slot.seat_index)
                    )
            continue

        capacity_hours = sum(emp.fte * max_period_hours for emp in qualified)
        if capacity_hours + 1e-9 < required_hours:
            for slot in expanded:
                if slot.required_qual_code == qual_key:
                    impossible_seats.add(
                        (slot.assignment_date, slot.shift_id, slot.required_qual_code, slot.seat_index)
                    )
            for emp in qualified:
                impossible_tiers.add(emp.id)

    return impossible_seats, impossible_tiers
