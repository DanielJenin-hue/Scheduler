from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import List, Optional, Sequence

from lab_scheduler.compliance.engine import ScheduledShift, ShiftTemplateInfo
from lab_scheduler.compliance.jurisdictions import JurisdictionRules
from lab_scheduler.engine.swap_controller import ScheduleState, get_eligible_swap_candidates
from lab_scheduler.rsi.clinical_audit import ClinicalFloorBreach
from lab_scheduler.scheduling.profiles import EmployeeProfile


_BAND_TO_SWAP_TOKEN = {
    "MORNING": "D",
    "EVENING": "E",
    "NIGHT": "N",
}


@dataclass(frozen=True, slots=True)
class ProposedShiftSwap:
    assignment_date: date
    shift_code: str
    current_employee_id: Optional[str]
    proposed_employee_id: str
    proposed_employee_name: str
    rationale: str
    hour_deficit: float


@dataclass(frozen=True, slots=True)
class RiskMitigationReport:
    """Autonomous response when the immutable 2/2/2 clinical floor is breached."""

    report_date: date
    breach_count: int
    forced_ot_count: int
    breaches: Sequence[ClinicalFloorBreach]
    proposed_swaps: Sequence[ProposedShiftSwap]

    def to_dict(self) -> dict:
        return {
            "report_date": self.report_date.isoformat(),
            "breach_count": self.breach_count,
            "forced_ot_count": self.forced_ot_count,
            "breaches": [
                {
                    "assignment_date": breach.assignment_date.isoformat(),
                    "shift_code": breach.shift_code,
                    "required_seats": breach.required_seats,
                    "filled_seats": breach.filled_seats,
                }
                for breach in self.breaches
            ],
            "proposed_swaps": [
                {
                    "assignment_date": swap.assignment_date.isoformat(),
                    "shift_code": swap.shift_code,
                    "current_employee_id": swap.current_employee_id,
                    "proposed_employee_id": swap.proposed_employee_id,
                    "proposed_employee_name": swap.proposed_employee_name,
                    "rationale": swap.rationale,
                    "hour_deficit": round(swap.hour_deficit, 2),
                }
                for swap in self.proposed_swaps
            ],
        }


def _assignments_for_employee_on_day(
    employee_id: str,
    assignment_date: date,
    assignments: Sequence[ScheduledShift],
) -> List[ScheduledShift]:
    return [
        assignment
        for assignment in assignments
        if assignment.employee_id == employee_id and assignment.assignment_date == assignment_date
    ]


def propose_shift_swaps_for_breaches(
    *,
    breaches: Sequence[ClinicalFloorBreach],
    rules: JurisdictionRules,
    period_start: date,
    period_end: date,
    weeks_in_period: int,
    employees: Sequence[EmployeeProfile],
    assignments: Sequence[ScheduledShift],
    shift_templates: dict[str, ShiftTemplateInfo],
    shift_required_qualifications: dict[str, set[str]],
    employee_target_hours: Optional[dict[str, float]] = None,
    availability_blocked: Optional[dict[str, set[date]]] = None,
) -> List[ProposedShiftSwap]:
    """
    For each under-filled clinical floor breach, propose the best legal swap candidate
    from the current staff database (Smart Shift Assister ranking).
    """

    if not breaches:
        return []

    schedule_state = ScheduleState(
        rules=rules,
        period_start=period_start,
        period_end=period_end,
        weeks_in_period=weeks_in_period,
        employees=employees,
        assignments=assignments,
        shift_templates=shift_templates,
        shift_required_qualifications=shift_required_qualifications,
        employee_target_hours=employee_target_hours,
        availability_blocked=availability_blocked,
    )

    proposals: List[ProposedShiftSwap] = []
    seen: set[tuple[date, str, str]] = set()

    for breach in breaches:
        if breach.filled_seats >= breach.required_seats:
            continue

        swap_token = _BAND_TO_SWAP_TOKEN.get(breach.shift_code, breach.shift_code)
        deficit_seats = breach.required_seats - breach.filled_seats

        assigned_on_day = [
            assignment
            for assignment in assignments
            if assignment.assignment_date == breach.assignment_date
            and shift_templates.get(assignment.shift_template_id)
            and shift_templates[assignment.shift_template_id].code == breach.shift_code
        ]
        current_employee_id = assigned_on_day[0].employee_id if assigned_on_day else None

        candidates = get_eligible_swap_candidates(
            schedule_state,
            target_employee_id=current_employee_id or "",
            target_date=breach.assignment_date,
            target_shift_type=swap_token,
            limit=max(deficit_seats * 3, 3),
        )
        for candidate in candidates:
            if not candidate.is_eligible:
                continue
            key = (breach.assignment_date, breach.shift_code, candidate.employee_id)
            if key in seen:
                continue
            seen.add(key)
            proposals.append(
                ProposedShiftSwap(
                    assignment_date=breach.assignment_date,
                    shift_code=breach.shift_code,
                    current_employee_id=current_employee_id,
                    proposed_employee_id=candidate.employee_id,
                    proposed_employee_name=candidate.employee_name,
                    rationale=(
                        f"Cover {breach.shift_code} floor gap "
                        f"({breach.filled_seats}/{breach.required_seats}) with under-target "
                        f"{candidate.role_code} line ({candidate.hour_deficit:.1f}h deficit)."
                    ),
                    hour_deficit=candidate.hour_deficit,
                )
            )
            if sum(
                1
                for proposal in proposals
                if proposal.assignment_date == breach.assignment_date
                and proposal.shift_code == breach.shift_code
            ) >= deficit_seats:
                break

    return proposals


def build_risk_mitigation_report(
    *,
    report_date: date,
    breaches: Sequence[ClinicalFloorBreach],
    forced_ot_count: int,
    proposed_swaps: Sequence[ProposedShiftSwap],
) -> RiskMitigationReport:
    return RiskMitigationReport(
        report_date=report_date,
        breach_count=len(breaches),
        forced_ot_count=forced_ot_count,
        breaches=breaches,
        proposed_swaps=proposed_swaps,
    )
