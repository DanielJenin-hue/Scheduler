from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Optional


@dataclass(frozen=True, slots=True)
class UnfilledSlot:
    assignment_date: date
    shift_template_id: str
    shift_code: str
    reason: str
    is_constraint_violation: bool = False
    constraint_summary: Optional[str] = None
    violation_kind: Optional[str] = None
    is_coverage_gap: bool = False
    is_impossible_coverage: bool = False


@dataclass(frozen=True, slots=True)
class PlannedAssignment:
    employee_id: str
    shift_template_id: str
    assignment_date: date
    forced_clinical_ot: bool = False
    overtime_compliance_bypassed: bool = False
    approved_stretch: bool = False
    clinical_floor_stretch: bool = False
    provisional_compliance: bool = False
    contract_line_exception: bool = False
    contract_line_exception_message: str = ""
    master_template_frozen: bool = False


@dataclass(frozen=True, slots=True)
class SlotSuggestion:
    """Ranked employee recommendation for an open shift slot."""

    employee_id: str
    employee_name: str
    score: float
    seniority_bypass: bool = False
    seniority_bypass_justification: Optional[str] = None
    requires_seniority_justification: bool = False


# Common typo alias — keeps imports resilient in UI code.
SlotsSuggestion = SlotSuggestion
