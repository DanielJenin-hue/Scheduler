from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
from typing import Mapping, Optional, Sequence, Set, Tuple, TYPE_CHECKING

from lab_scheduler.compliance.engine import ScheduledShift, ShiftTemplateInfo
from lab_scheduler.errors.schedule_error import (
    CONSECUTIVE_DAYS_WARNING_CODE,
    ScheduleError,
)

if TYPE_CHECKING:
    from lab_scheduler.audit.compliance import ComplianceConflict

PROVISIONAL_STRETCH_TURNAROUND_CODES = frozenset(
    {
        ScheduleError.UNION_TURNAROUND_15H.value,
        ScheduleError.UNION_MORNING_REST_11H.value,
        ScheduleError.PORTAGE_CONSECUTIVE_DAYS.value,
        ScheduleError.CONSECUTIVE_DAYS.value,
        CONSECUTIVE_DAYS_WARNING_CODE,
    }
)

from lab_scheduler.scheduling.provisional_constants import (
    APPROVED_CONTRACT_LINE_EXCEPTION_NOTE_PREFIX,
    APPROVED_STRETCH_NOTE_PREFIX,
    CLINICAL_FLOOR_CONTRACT_LINE_REASON,
    CLINICAL_FLOOR_MANDATE_REASON,
    CONTRACT_LINE_EXCEPTION_NOTE_PREFIX,
    CONTRACT_LINE_EXCEPTION_VIOLATION_CODE,
    PROVISIONAL_STRETCH_NOTE_PREFIX,
)


@dataclass(frozen=True, slots=True)
class ProvisionalAssignment:
    """One stretch or turnaround override suggested for manager approval."""

    employee_id: str
    employee_name: str
    assignment_date: date
    shift_template_id: str
    shift_code: str
    violation_code: str
    violation_label: str
    message: str
    reason: str = CLINICAL_FLOOR_MANDATE_REASON
    assignment_id: str = ""

    def assignment_key(self) -> Tuple[str, date, str]:
        return (self.employee_id, self.assignment_date, self.shift_template_id)

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload["assignment_date"] = self.assignment_date.isoformat()
        return payload


def is_provisional_violation_code(code: str) -> bool:
    return code in PROVISIONAL_STRETCH_TURNAROUND_CODES


def is_provisional_labor_violation(message: Optional[str]) -> bool:
    if not message:
        return False
    lower = message.lower()
    return (
        "15h turnaround" in lower
        or "11h rest" in lower
        or "consecutive work days" in lower
        or "fatigue guardrail" in lower
    )


def is_provisional_stretch_note(system_note: Optional[str]) -> bool:
    return bool(system_note and system_note.startswith(PROVISIONAL_STRETCH_NOTE_PREFIX))


def is_approved_stretch_note(system_note: Optional[str]) -> bool:
    return bool(system_note and system_note.startswith(APPROVED_STRETCH_NOTE_PREFIX))


def provisional_stretch_system_note(*, violation_label: str = "stretch/turnaround") -> str:
    return f"{PROVISIONAL_STRETCH_NOTE_PREFIX}{violation_label} pending manager approval"


def approved_stretch_system_note(*, actor: str = "manager") -> str:
    return f"{APPROVED_STRETCH_NOTE_PREFIX}Authorized by {actor}"


def approved_stretch_from_system_note(system_note: Optional[str]) -> bool:
    return is_approved_stretch_note(system_note)


def is_provisional_contract_line_exception_note(system_note: Optional[str]) -> bool:
    return bool(
        system_note and system_note.startswith(CONTRACT_LINE_EXCEPTION_NOTE_PREFIX)
    )


def is_approved_contract_line_exception_note(system_note: Optional[str]) -> bool:
    return bool(
        system_note
        and system_note.startswith(APPROVED_CONTRACT_LINE_EXCEPTION_NOTE_PREFIX)
    )


def contract_line_exception_system_note(*, violation_message: str) -> str:
    detail = violation_message.strip() or "Contract line borrow pending manager approval"
    return f"{CONTRACT_LINE_EXCEPTION_NOTE_PREFIX}{detail}"


def approved_contract_line_exception_system_note(*, actor: str = "manager") -> str:
    return f"{APPROVED_CONTRACT_LINE_EXCEPTION_NOTE_PREFIX}Authorized by {actor}"


def build_contract_line_provisional_assignment(
    *,
    employee_id: str,
    employee_name: str,
    assignment_date: date,
    shift_template_id: str,
    shift_code: str,
    violation_message: str,
) -> ProvisionalAssignment:
    return ProvisionalAssignment(
        employee_id=employee_id,
        employee_name=employee_name,
        assignment_date=assignment_date,
        shift_template_id=shift_template_id,
        shift_code=shift_code,
        violation_code=CONTRACT_LINE_EXCEPTION_VIOLATION_CODE,
        violation_label="Contract Line Exception",
        message=violation_message,
        reason=CLINICAL_FLOOR_CONTRACT_LINE_REASON,
    )


def _assignment_lookup(
    assignments: Sequence[ScheduledShift],
) -> dict[Tuple[str, date], ScheduledShift]:
    lookup: dict[Tuple[str, date], ScheduledShift] = {}
    for assignment in assignments:
        lookup[(assignment.employee_id, assignment.assignment_date)] = assignment
    return lookup


def build_provisional_assignments(
    conflicts: Sequence["ComplianceConflict"],
    *,
    assignments: Sequence[ScheduledShift],
    shift_templates: Mapping[str, ShiftTemplateInfo],
    approved_keys: Optional[Set[Tuple[str, date, str]]] = None,
) -> list[ProvisionalAssignment]:
    """Map stretch/turnaround conflicts to manager-review rows."""

    approved_keys = approved_keys or set()
    by_employee_date = _assignment_lookup(assignments)
    provisional: list[ProvisionalAssignment] = []
    seen: Set[Tuple[str, date, str]] = set()

    for conflict in conflicts:
        if not is_provisional_violation_code(conflict.code):
            continue
        if not conflict.employee_id or conflict.assignment_date is None:
            continue
        assignment = by_employee_date.get((conflict.employee_id, conflict.assignment_date))
        if assignment is None:
            continue
        if assignment.approved_stretch:
            continue
        if assignment.clinical_floor_stretch and conflict.code in {
            ScheduleError.UNION_TURNAROUND_15H.value,
            ScheduleError.UNION_MORNING_REST_11H.value,
        }:
            continue
        key = (assignment.employee_id, assignment.assignment_date, assignment.shift_template_id)
        if key in approved_keys or key in seen:
            continue
        template = shift_templates.get(assignment.shift_template_id)
        shift_code = template.code if template is not None else assignment.shift_template_id
        provisional.append(
            ProvisionalAssignment(
                employee_id=assignment.employee_id,
                employee_name=conflict.employee_name or assignment.employee_name,
                assignment_date=assignment.assignment_date,
                shift_template_id=assignment.shift_template_id,
                shift_code=shift_code,
                violation_code=conflict.code,
                violation_label=conflict.manager_label,
                message=conflict.message,
            )
        )
        seen.add(key)
    return provisional


def partition_provisional_conflicts(
    conflicts: Sequence["ComplianceConflict"],
    *,
    assignments: Sequence[ScheduledShift],
    shift_templates: Mapping[str, ShiftTemplateInfo],
    approved_keys: Optional[Set[Tuple[str, date, str]]] = None,
) -> tuple[list["ComplianceConflict"], list[ProvisionalAssignment]]:
    """Split stretch/turnaround conflicts into manager-review provisional rows."""

    from lab_scheduler.audit.compliance import ComplianceConflict as _ComplianceConflict

    provisional = build_provisional_assignments(
        conflicts,
        assignments=assignments,
        shift_templates=shift_templates,
        approved_keys=approved_keys,
    )
    provisional_keys = {item.assignment_key() for item in provisional}
    hard_conflicts: list[_ComplianceConflict] = []
    for conflict in conflicts:
        if not is_provisional_violation_code(conflict.code):
            hard_conflicts.append(conflict)
            continue
        if conflict.employee_id and conflict.assignment_date is not None:
            assignment = _assignment_lookup(assignments).get(
                (conflict.employee_id, conflict.assignment_date)
            )
            if assignment is not None:
                key = (
                    assignment.employee_id,
                    assignment.assignment_date,
                    assignment.shift_template_id,
                )
                if key in provisional_keys:
                    continue
        hard_conflicts.append(conflict)
    return hard_conflicts, provisional
