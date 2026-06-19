from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Dict, List, Mapping, Optional, Protocol, Sequence, Set, Tuple

from lab_scheduler.compliance.engine import ShiftTemplateInfo
from lab_scheduler.engine.constraints import validate_contract_line_eligibility
from lab_scheduler.engine.demand import (
    CLINICAL_FLOOR,
    ExpandedScheduleSlot,
    employee_matches_seat_qual,
    infer_qual_code,
    is_clinical_floor_pool,
)
from lab_scheduler.scheduling.assignment_rejection_log import log_assignment_rejection
from lab_scheduler.scheduling.date_utils import daterange as _daterange
from lab_scheduler.scheduling.portage_template import parse_vacant_portage_line
from lab_scheduler.scheduling.profiles import EmployeeProfile

EVENING_NIGHT_CLINICAL_BANDS: Tuple[str, ...] = ("EVENING", "NIGHT")
CLINICAL_SEAT_PATTERN = re.compile(r"Seat_(\d+)", re.IGNORECASE)
from lab_scheduler.errors.schedule_error import CRITICAL_CLINICAL_GAP_CODE


class EmployeeScheduleState(Protocol):
    work_dates: Set[date]
    total_hours: float
    assignment_records: List[Tuple[date, str]]


@dataclass(frozen=True, slots=True)
class ClinicalContractLineProvisional:
    employee: EmployeeProfile
    violation_message: str


@dataclass(frozen=True, slots=True)
class ClinicalContractLineAssessment:
    """Contract-line gate result for a clinical-floor seat candidate."""

    violation_message: Optional[str]
    soft_warning: bool

    @property
    def hard_rejection(self) -> bool:
        return self.violation_message is not None and not self.soft_warning


@dataclass(frozen=True, slots=True)
class MandatoryClinicalCandidateAudit:
    eligible: Tuple[EmployeeProfile, ...]
    provisional_contract_line: Tuple[ClinicalContractLineProvisional, ...]
    rejections: Tuple[str, ...]


def is_portage_vacant_roster(employees: Sequence[EmployeeProfile]) -> bool:
    """True when every employee row is a Vacant Portage master line (hospital blueprint)."""

    if len(employees) < 10:
        return False
    return all(parse_vacant_portage_line(employee.full_name) is not None for employee in employees)


def allows_clinical_floor_contract_line_exception(
    *,
    role_pool_id: str,
    shift_code: str,
    employees: Optional[Sequence[EmployeeProfile]] = None,
) -> bool:
    """
    Evening/Night Seat_01 and Seat_02 may borrow across D/E vs D/N contract lines.

    Portage vacant-line rosters stay strict: D/E lines never work NIGHT, D/N never EVENING.
    """

    if employees is not None and is_portage_vacant_roster(employees):
        return False
    if shift_code not in EVENING_NIGHT_CLINICAL_BANDS:
        return False
    if not is_clinical_floor_pool(role_pool_id):
        return False
    return clinical_seat_number(role_pool_id) in (1, 2)


def assess_clinical_floor_contract_line(
    *,
    contract_line_type: Optional[str],
    shift_code: str,
    qual_code: Optional[str],
    role_pool_id: Optional[str],
) -> ClinicalContractLineAssessment:
    """
    Evaluate contract-line fit for one candidate.

    On Evening/Night Seat_01/Seat_02, mismatches become soft warnings (provisional
    overrides) instead of hard rejections. Standard shifts stay strict.
    """

    violation = validate_contract_line_eligibility(
        contract_line_type,
        shift_code,
        qual_code=qual_code,
    )
    if violation is None:
        return ClinicalContractLineAssessment(violation_message=None, soft_warning=False)
    if role_pool_id and allows_clinical_floor_contract_line_exception(
        role_pool_id=role_pool_id,
        shift_code=shift_code,
    ):
        return ClinicalContractLineAssessment(
            violation_message=violation,
            soft_warning=True,
        )
    return ClinicalContractLineAssessment(
        violation_message=violation,
        soft_warning=False,
    )


def _employee_is_qualified(profile: EmployeeProfile, required_qualification_ids: Set[str]) -> bool:
    if not required_qualification_ids:
        return True
    return bool(profile.qualification_ids & required_qualification_ids)


def is_vacant_portage_clinical_candidate(profile: EmployeeProfile) -> bool:
    """Vacant Portage master lines are deferred from clinical float (template owns E/N)."""

    return parse_vacant_portage_line(profile.full_name) is not None


def vacant_portage_clinical_deferred_to_template(
    profile: EmployeeProfile,
    assignment_date: date,
    *,
    shift_code: Optional[str] = None,
) -> bool:
    """Weekday vacant lines defer morning to template; E/N may fill gaps after template stamp."""

    if not is_vacant_portage_clinical_candidate(profile):
        return False
    if assignment_date.weekday() >= 5:
        return False
    if shift_code in EVENING_NIGHT_CLINICAL_BANDS:
        return False
    return True


def vacant_may_supersede_for_clinical_band(
    *,
    profile: EmployeeProfile,
    assignment_date: date,
    target_shift_code: str,
    state: EmployeeScheduleState,
    shift_templates: Mapping[str, ShiftTemplateInfo],
    period_start: Optional[date] = None,
    portage_rotation_strict: bool = False,
) -> bool:
    """Vacant master-line day shifts may yield to mandatory Evening/Night clinical seats."""

    if portage_rotation_strict:
        return False
    if not is_vacant_portage_clinical_candidate(profile):
        return False
    if target_shift_code not in EVENING_NIGHT_CLINICAL_BANDS:
        return False
    if assignment_date in state.work_dates and period_start is not None:
        from lab_scheduler.scheduling.portage_template import (
            vacant_master_rotation_permits_shift,
        )

        if not vacant_master_rotation_permits_shift(
            profile,
            assignment_date,
            period_start,
            target_shift_code,
        ):
            return False
    if assignment_date not in state.work_dates:
        return True
    for day, shift_id in state.assignment_records:
        if day != assignment_date:
            continue
        existing = shift_templates.get(shift_id)
        if existing is None:
            return False
        if existing.code in EVENING_NIGHT_CLINICAL_BANDS:
            return False
        return True
    return False


def mandatory_clinical_candidates_with_audit(
    *,
    employees: Sequence[EmployeeProfile],
    required: Set[str],
    states: Mapping[str, EmployeeScheduleState],
    assignment_date: date,
    template: ShiftTemplateInfo,
    qual_codes: Mapping[str, str],
    required_qual_code: Optional[str],
    availability_blocked: Optional[Mapping[str, Set[date]]],
    role_pool_id: Optional[str] = None,
    shift_templates: Optional[Mapping[str, ShiftTemplateInfo]] = None,
    clinical_mandatory: bool = False,
    period_start: Optional[date] = None,
    portage_rotation_strict: Optional[bool] = None,
) -> MandatoryClinicalCandidateAudit:
    """
    Qualified staff eligible for mandatory clinical fill, plus per-employee rejection reasons.

    MLT/MLA qualification boundaries stay strict. Contract-line conflicts on Evening/Night
    Seat_01/Seat_02 are returned as provisional candidates instead of hard rejections.
    """

    rotation_strict = (
        portage_rotation_strict
        if portage_rotation_strict is not None
        else is_portage_vacant_roster(employees)
    )
    allow_line_exception = False
    if role_pool_id is not None:
        allow_line_exception = allows_clinical_floor_contract_line_exception(
            role_pool_id=role_pool_id,
            shift_code=template.code,
            employees=employees,
        )

    eligible: List[EmployeeProfile] = []
    provisional_contract_line: List[ClinicalContractLineProvisional] = []
    rejections: List[str] = []

    for emp in employees:
        if rotation_strict and template.code in EVENING_NIGHT_CLINICAL_BANDS:
            contract = (emp.contract_line_type or "").upper()
            if template.code == "NIGHT" and contract != "D/N":
                reason = "Portage night skeleton requires D/N contract line"
                log_assignment_rejection(emp.id, assignment_date, reason)
                rejections.append(f"{emp.id}: {reason}")
                continue
            if template.code == "EVENING" and contract != "D/E":
                reason = "Portage evening skeleton requires D/E contract line"
                log_assignment_rejection(emp.id, assignment_date, reason)
                rejections.append(f"{emp.id}: {reason}")
                continue
        if vacant_portage_clinical_deferred_to_template(
            emp,
            assignment_date,
            shift_code=template.code,
        ):
            reason = "vacant Portage master line deferred to template rotation"
            log_assignment_rejection(emp.id, assignment_date, reason)
            rejections.append(f"{emp.id}: {reason}")
            continue
        if not _employee_is_qualified(emp, required):
            reason = "not qualified for shift requirements"
            log_assignment_rejection(emp.id, assignment_date, reason)
            rejections.append(f"{emp.id}: {reason}")
            continue
        if not employee_matches_seat_qual(
            emp,
            required_qual_code,
            qual_codes=qual_codes,
            shift_required_qualification_ids=required or None,
        ):
            reason = f"seat qual mismatch (needs {required_qual_code or 'ANY'})"
            log_assignment_rejection(emp.id, assignment_date, reason)
            rejections.append(f"{emp.id}: {reason}")
            continue

        if period_start is not None and template.code in EVENING_NIGHT_CLINICAL_BANDS:
            from lab_scheduler.scheduling.portage_template import (
                vacant_master_rotation_permits_shift,
            )

            if parse_vacant_portage_line(emp.full_name) is not None:
                state = states[emp.id]
                on_catalog = vacant_master_rotation_permits_shift(
                    emp,
                    assignment_date,
                    period_start,
                    template.code,
                )
                require_catalog = rotation_strict or assignment_date in state.work_dates
                if require_catalog and not on_catalog:
                    reason = (
                        f"master rotation does not call for {template.code} on "
                        f"{assignment_date.isoformat()}"
                    )
                    log_assignment_rejection(emp.id, assignment_date, reason)
                    rejections.append(f"{emp.id}: {reason}")
                    continue
                if (
                    not rotation_strict
                    and assignment_date not in state.work_dates
                    and not on_catalog
                    and not clinical_mandatory
                ):
                    reason = (
                        f"master rotation does not call for {template.code} on "
                        f"{assignment_date.isoformat()}"
                    )
                    log_assignment_rejection(emp.id, assignment_date, reason)
                    rejections.append(f"{emp.id}: {reason}")
                    continue

        state = states[emp.id]
        if availability_blocked and assignment_date in availability_blocked.get(emp.id, set()):
            reason = f"availability blocked on {assignment_date.isoformat()}"
            log_assignment_rejection(emp.id, assignment_date, reason)
            rejections.append(f"{emp.id}: {reason}")
            continue
        if assignment_date in state.work_dates:
            if (
                not rotation_strict
                and shift_templates is not None
                and vacant_may_supersede_for_clinical_band(
                    profile=emp,
                    assignment_date=assignment_date,
                    target_shift_code=template.code,
                    state=state,
                    shift_templates=shift_templates,
                    period_start=period_start,
                    portage_rotation_strict=rotation_strict,
                )
            ):
                pass
            else:
                reason = f"already assigned on {assignment_date.isoformat()}"
                log_assignment_rejection(emp.id, assignment_date, reason)
                rejections.append(f"{emp.id}: {reason}")
                continue

        emp_qual = infer_qual_code(emp, qual_codes=qual_codes)
        line_assessment = assess_clinical_floor_contract_line(
            contract_line_type=emp.contract_line_type,
            shift_code=template.code,
            qual_code=emp_qual,
            role_pool_id=role_pool_id,
        )
        if line_assessment.violation_message:
            if line_assessment.soft_warning and allow_line_exception:
                provisional_contract_line.append(
                    ClinicalContractLineProvisional(
                        employee=emp,
                        violation_message=line_assessment.violation_message,
                    )
                )
                continue
            log_assignment_rejection(
                emp.id,
                assignment_date,
                line_assessment.violation_message,
            )
            rejections.append(
                f"{emp.id}: contract line conflict ({line_assessment.violation_message})"
            )
            continue

        target_hours = getattr(state, "target_hours", None)
        shift_hours = template.duration_minutes / 60.0
        if (
            not clinical_mandatory
            and parse_vacant_portage_line(emp.full_name) is not None
            and target_hours is not None
        ):
            from lab_scheduler.scheduling.portage_template import vacant_master_rotation_fte

            rotation_fte = vacant_master_rotation_fte(emp)
            part_time_line = (rotation_fte if rotation_fte is not None else emp.fte) < 1.0
            if part_time_line and state.total_hours + shift_hours > float(target_hours) + 8.0 + 0.25:
                reason = (
                    f"part-time catalog contract band full "
                    f"({state.total_hours:.1f}h scheduled vs {float(target_hours):.0f}h target)"
                )
                log_assignment_rejection(emp.id, assignment_date, reason)
                rejections.append(f"{emp.id}: {reason}")
                continue
        if (
            not clinical_mandatory
            and target_hours is not None
            and parse_vacant_portage_line(emp.full_name) is not None
            and state.total_hours + shift_hours > float(target_hours) + 8.0 + 0.25
        ):
            reason = (
                f"catalog contract band full "
                f"({state.total_hours:.1f}h scheduled vs {float(target_hours):.0f}h target)"
            )
            log_assignment_rejection(emp.id, assignment_date, reason)
            rejections.append(f"{emp.id}: {reason}")
            continue

        eligible.append(emp)

    provisional_ids = {item.employee.id for item in provisional_contract_line}
    strict_ids = {profile.id for profile in eligible}
    for item in provisional_contract_line:
        if item.employee.id not in strict_ids:
            eligible.append(item.employee)

    eligible.sort(
        key=lambda profile: (
            _mandatory_clinical_catalog_alignment_rank(
                profile,
                assignment_date=assignment_date,
                period_start=period_start,
                shift_code=template.code,
            ),
            profile.id in provisional_ids,
            states[profile.id].total_hours,
        )
    )
    provisional_contract_line.sort(key=lambda item: states[item.employee.id].total_hours)
    return MandatoryClinicalCandidateAudit(
        eligible=tuple(eligible),
        provisional_contract_line=tuple(provisional_contract_line),
        rejections=tuple(rejections),
    )


def _mandatory_clinical_catalog_alignment_rank(
    profile: EmployeeProfile,
    *,
    assignment_date: date,
    period_start: Optional[date],
    shift_code: str,
) -> int:
    """Prefer vacant lines whose master catalog already calls for this shift band."""

    if period_start is None:
        return 1
    from lab_scheduler.scheduling.portage_template import (
        parse_vacant_portage_line,
        vacant_master_rotation_permits_shift,
    )

    if parse_vacant_portage_line(profile.full_name) is None:
        return 1
    if vacant_master_rotation_permits_shift(
        profile,
        assignment_date,
        period_start,
        shift_code,
    ):
        return 0
    return 1


def select_mandatory_clinical_candidate(
    audit: MandatoryClinicalCandidateAudit,
) -> Tuple[Optional[EmployeeProfile], Optional[ClinicalContractLineProvisional]]:
    """Pick lowest-hours eligible worker; flag provisional contract-line borrows."""

    if not audit.eligible:
        return None, None
    chosen = audit.eligible[0]
    provisional_map = {
        item.employee.id: item for item in audit.provisional_contract_line
    }
    return chosen, provisional_map.get(chosen.id)


@dataclass(frozen=True, slots=True)
class CriticalClinicalGap:
    assignment_date: date
    shift_code: str
    seat_label: str
    reason: str

    def log_line(self) -> str:
        return (
            f"CRITICAL CLINICAL GAP: {self.assignment_date.isoformat()} "
            f"{self.shift_code} {self.seat_label} — {self.reason}"
        )


def clinical_seat_label(role_pool_id: str) -> str:
    match = CLINICAL_SEAT_PATTERN.search(role_pool_id)
    if match:
        return f"Seat_{int(match.group(1)):02d}"
    if role_pool_id.endswith("- MLT"):
        return "Seat_01"
    if role_pool_id.endswith("- MLA"):
        return "Seat_02"
    return "Seat_01"


def clinical_seat_number(role_pool_id: str) -> int:
    match = CLINICAL_SEAT_PATTERN.search(role_pool_id)
    if match:
        return int(match.group(1))
    if role_pool_id.endswith("- MLA"):
        return 2
    return 1


def is_evening_night_clinical_seat_slot(
    slot: ExpandedScheduleSlot,
    *,
    shift_templates: Mapping[str, ShiftTemplateInfo],
) -> bool:
    code = shift_templates[slot.shift_id].code
    return code in EVENING_NIGHT_CLINICAL_BANDS and is_clinical_floor_pool(slot.role_pool_id)


def evening_night_clinical_seat_slots(
    expanded_slots: Sequence[ExpandedScheduleSlot],
    *,
    shift_templates: Mapping[str, ShiftTemplateInfo],
    weekend_first: bool = False,
) -> Tuple[ExpandedScheduleSlot, ...]:
    """All immutable Evening/Night clinical seat slots in calendar lockdown order."""

    filtered = [
        slot
        for slot in expanded_slots
        if is_evening_night_clinical_seat_slot(slot, shift_templates=shift_templates)
    ]
    band_order = {"EVENING": 0, "NIGHT": 1}
    return tuple(
        sorted(
            filtered,
            key=lambda slot: (
                0 if weekend_first and slot.assignment_date.weekday() >= 5 else 1,
                slot.assignment_date.toordinal(),
                band_order.get(shift_templates[slot.shift_id].code, 9),
                clinical_seat_number(slot.role_pool_id),
                slot.required_qual_code or "",
                slot.seat_index,
            ),
        )
    )


def evening_night_clinical_band_satisfied(
    assignment_date: date,
    shift_code: str,
    *,
    fill_counts: Mapping[Tuple[date, str, Optional[str]], int],
    expanded_slots: Sequence[ExpandedScheduleSlot],
    shift_templates: Mapping[str, ShiftTemplateInfo],
) -> bool:
    """True when the immutable two-seat clinical floor is met for one date and E/N band."""

    if shift_code not in EVENING_NIGHT_CLINICAL_BANDS:
        return True
    from lab_scheduler.engine.demand import clinical_floor_filled_for_day

    filled = clinical_floor_filled_for_day(
        assignment_date,
        shift_code,
        fill_counts=fill_counts,
        expanded_slots=expanded_slots,
        shift_templates=shift_templates,
    )
    return filled >= CLINICAL_FLOOR[shift_code]


def non_clinical_fill_blocked_until_clinical_floor(
    slot: ExpandedScheduleSlot,
    *,
    fill_counts: Mapping[Tuple[date, str, Optional[str]], int],
    expanded_slots: Sequence[ExpandedScheduleSlot],
    shift_templates: Mapping[str, ShiftTemplateInfo],
) -> bool:
    """
    Return True when a non-clinical slot must not be filled yet.

    Evening/Night bands stay blocked until Seat_01 + Seat_02 are secured for that date.
    """

    shift_code = shift_templates[slot.shift_id].code
    if shift_code not in EVENING_NIGHT_CLINICAL_BANDS:
        return False
    if is_clinical_floor_pool(slot.role_pool_id):
        return False
    return not evening_night_clinical_band_satisfied(
        slot.assignment_date,
        shift_code,
        fill_counts=fill_counts,
        expanded_slots=expanded_slots,
        shift_templates=shift_templates,
    )


def slot_is_filled(
    slot: ExpandedScheduleSlot,
    fill_counts: Mapping[Tuple[date, str, Optional[str]], int],
) -> bool:
    key = (slot.assignment_date, slot.shift_id, slot.required_qual_code)
    return fill_counts.get(key, 0) > slot.seat_index


def evening_night_clinical_seats_satisfied(
    *,
    fill_counts: Mapping[Tuple[date, str, Optional[str]], int],
    expanded_slots: Sequence[ExpandedScheduleSlot],
    shift_templates: Mapping[str, ShiftTemplateInfo],
    period_start: date,
    period_end: date,
) -> bool:
    seats = evening_night_clinical_seat_slots(
        expanded_slots,
        shift_templates=shift_templates,
    )
    if not seats:
        return True
    expected = len(seats)
    filled = sum(1 for seat in seats if slot_is_filled(seat, fill_counts))
    if filled != expected:
        return False
    for assignment_date in _daterange(period_start, period_end):
        for shift_code in EVENING_NIGHT_CLINICAL_BANDS:
            day_seats = [
                seat
                for seat in seats
                if seat.assignment_date == assignment_date
                and shift_templates[seat.shift_id].code == shift_code
            ]
            if len(day_seats) < CLINICAL_FLOOR[shift_code]:
                continue
            if not all(slot_is_filled(seat, fill_counts) for seat in day_seats):
                return False
    return True


def collect_critical_clinical_gaps(
    *,
    fill_counts: Mapping[Tuple[date, str, Optional[str]], int],
    expanded_slots: Sequence[ExpandedScheduleSlot],
    shift_templates: Mapping[str, ShiftTemplateInfo],
) -> List[CriticalClinicalGap]:
    gaps: List[CriticalClinicalGap] = []
    for slot in evening_night_clinical_seat_slots(
        expanded_slots,
        shift_templates=shift_templates,
    ):
        if slot_is_filled(slot, fill_counts):
            continue
        shift_code = shift_templates[slot.shift_id].code
        seat = clinical_seat_label(slot.role_pool_id)
        qual = slot.required_qual_code or "ANY"
        gaps.append(
            CriticalClinicalGap(
                assignment_date=slot.assignment_date,
                shift_code=shift_code,
                seat_label=seat,
                reason=(
                    f"{seat} ({qual}) remains empty — clinical lockdown incomplete "
                    f"(requires {CLINICAL_FLOOR[shift_code]} seats per band)"
                ),
            )
        )
    return gaps
