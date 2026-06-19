"""Manual grid assignment validation — delegates to legacy engine only when invoked."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from datetime import date
    from typing import Dict, Mapping, Sequence, Set

    from lab_scheduler.compliance.engine import JurisdictionRules, ScheduledShift, ShiftTemplateInfo
    from lab_scheduler.scheduling.profiles import EmployeeProfile


def validate_assignment_change(
    *,
    rules: "JurisdictionRules",
    period_start: "date",
    period_end: "date",
    weeks_in_period: int,
    employee: "EmployeeProfile",
    all_assignments: "Sequence[ScheduledShift]",
    shift_templates: "Dict[str, ShiftTemplateInfo]",
    shift_required_qualifications: "Dict[str, Set[str]]",
    assignment_date: "date",
    new_shift_template_id: Optional[str],
    employee_target_hours: Optional["Mapping[str, float]"] = None,
    availability_blocked: Optional["Mapping[str, Set[date]]"] = None,
    enforce_fte_target: bool = True,
    approved_stretch: bool = False,
    role_pool_id: Optional[str] = None,
) -> Optional[str]:
    from lab_scheduler.legacy.auto_generate import validate_assignment_change as _validate

    return _validate(
        rules=rules,
        period_start=period_start,
        period_end=period_end,
        weeks_in_period=weeks_in_period,
        employee=employee,
        all_assignments=all_assignments,
        shift_templates=shift_templates,
        shift_required_qualifications=shift_required_qualifications,
        assignment_date=assignment_date,
        new_shift_template_id=new_shift_template_id,
        employee_target_hours=employee_target_hours,
        availability_blocked=availability_blocked,
        enforce_fte_target=enforce_fte_target,
        approved_stretch=approved_stretch,
        role_pool_id=role_pool_id,
    )


def __getattr__(name: str) -> Any:
    if name == "validate_assignment_change":
        return validate_assignment_change
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
