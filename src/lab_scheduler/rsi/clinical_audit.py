from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Dict, List, Mapping, Optional, Sequence, Set, Tuple

from lab_scheduler.compliance.engine import ShiftTemplateInfo
from lab_scheduler.engine.demand import (
    CLINICAL_FLOOR,
    ExpandedScheduleSlot,
    clinical_floor_filled_for_day,
    expand_schedule_slots,
    infer_qual_code,
    portage_concurrent_demands,
)
from lab_scheduler.rsi.project_health import ClinicalRiskInstance
from lab_scheduler.scheduling.profiles import EmployeeProfile


@dataclass(frozen=True, slots=True)
class ClinicalFloorBreach:
    assignment_date: date
    shift_code: str
    required_seats: int
    filled_seats: int


def _daterange(start: date, end_inclusive: date) -> List[date]:
    days: List[date] = []
    cursor = start
    while cursor <= end_inclusive:
        days.append(cursor)
        cursor += timedelta(days=1)
    return days


def build_seat_fill_counts(
    assignments: Sequence[Mapping[str, object]],
    employees: Sequence[EmployeeProfile],
    *,
    qual_codes: Optional[Mapping[str, str]] = None,
) -> Dict[Tuple[date, str, Optional[str]], int]:
    emp_by_id = {employee.id: employee for employee in employees}
    counts: Dict[Tuple[date, str, Optional[str]], int] = defaultdict(int)
    for assignment in assignments:
        employee_id = str(assignment["employee_id"])
        employee = emp_by_id.get(employee_id)
        if employee is None:
            continue
        assignment_date = assignment["assignment_date"]
        if not isinstance(assignment_date, date):
            assignment_date = date.fromisoformat(str(assignment_date))
        shift_template_id = str(assignment["shift_template_id"])
        qual = infer_qual_code(employee, qual_codes=qual_codes)
        counts[(assignment_date, shift_template_id, qual)] += 1
    return dict(counts)


def detect_forced_clinical_ot(
    assignments: Sequence[Mapping[str, object]],
    shift_templates: Mapping[str, ShiftTemplateInfo],
) -> List[ClinicalRiskInstance]:
    risks: List[ClinicalRiskInstance] = []
    for assignment in assignments:
        forced = bool(assignment.get("forced_clinical_ot"))
        system_note = str(assignment.get("system_note") or "")
        if not forced and system_note != "FORCED_CLINICAL_OT":
            continue
        assignment_date = assignment["assignment_date"]
        if not isinstance(assignment_date, date):
            assignment_date = date.fromisoformat(str(assignment_date))
        template = shift_templates.get(str(assignment["shift_template_id"]))
        shift_code = template.code if template else "UNKNOWN"
        risks.append(
            ClinicalRiskInstance(
                assignment_date=assignment_date,
                shift_code=shift_code,
                employee_id=str(assignment["employee_id"]),
                risk_type="forced_clinical_ot",
                detail="Shift filled via Forced Clinical OT authorization",
                assignment_id=str(assignment.get("id")) if assignment.get("id") else None,
            )
        )
    return risks


def detect_clinical_floor_breaches(
    *,
    fill_counts: Mapping[Tuple[date, str, Optional[str]], int],
    shift_templates: Mapping[str, ShiftTemplateInfo],
    period_start: date,
    period_end: date,
    expanded_slots: Sequence[ExpandedScheduleSlot],
) -> List[ClinicalFloorBreach]:
    breaches: List[ClinicalFloorBreach] = []
    for assignment_date in _daterange(period_start, period_end):
        for shift_code, required in CLINICAL_FLOOR.items():
            filled = clinical_floor_filled_for_day(
                assignment_date,
                shift_code,
                fill_counts=fill_counts,
                expanded_slots=expanded_slots,
                shift_templates=shift_templates,
            )
            if filled != required:
                breaches.append(
                    ClinicalFloorBreach(
                        assignment_date=assignment_date,
                        shift_code=shift_code,
                        required_seats=required,
                        filled_seats=filled,
                    )
                )
    return breaches


def breaches_to_risk_instances(breaches: Sequence[ClinicalFloorBreach]) -> List[ClinicalRiskInstance]:
    return [
        ClinicalRiskInstance(
            assignment_date=breach.assignment_date,
            shift_code=breach.shift_code,
            employee_id="SYSTEM",
            risk_type="clinical_floor_breach",
            detail=(
                f"Clinical floor breach: {breach.filled_seats}/{breach.required_seats} "
                f"seats filled on {breach.assignment_date.isoformat()}"
            ),
        )
        for breach in breaches
    ]


def operational_reliability_pct(
    *,
    period_start: date,
    period_end: date,
    breaches: Sequence[ClinicalFloorBreach],
    forced_ot_count: int,
) -> float:
    total_days = len(_daterange(period_start, period_end))
    if total_days <= 0:
        return 100.0
    bands_per_day = len(CLINICAL_FLOOR)
    total_checks = total_days * bands_per_day
    failed_checks = len(breaches)
    base = max(0.0, 100.0 * (1.0 - failed_checks / max(total_checks, 1)))
    penalty = min(15.0, forced_ot_count * 0.25)
    return round(max(0.0, base - penalty), 2)


def expand_portage_slots(
    *,
    period_start: date,
    period_end: date,
    shift_templates: Mapping[str, ShiftTemplateInfo],
) -> List[ExpandedScheduleSlot]:
    return expand_schedule_slots(
        period_start=period_start,
        period_end=period_end,
        shift_templates=shift_templates,
        concurrent_demands=portage_concurrent_demands(),
    )
