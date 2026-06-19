from __future__ import annotations

from datetime import date
from lab_scheduler.engine.constraints import IMPOSSIBLE_COVERAGE_TOOLTIP
from lab_scheduler.engine.demand import TRANSITION_BURNOUT_WARNING


def format_rule_violation_tooltip(
    reason: str,
    *,
    employee_name: Optional[str] = None,
    assignment_date: Optional[date] = None,
    slot_label: Optional[str] = None,
) -> str:
    """Human-readable XAI explanation for unassigned or blocked shift cells."""

    name = employee_name or "an employee"
    when = assignment_date.isoformat() if assignment_date else "the prior day"
    shift = slot_label or "shift"
    text = reason.strip()

    if TRANSITION_BURNOUT_WARNING in text:
        return (
            f"Reason: {TRANSITION_BURNOUT_WARNING} — Day cannot be followed by Night on "
            f"consecutive calendar days for {name} ({when})."
        )
    if "11h rest before Morning after Evening/Night" in text:
        return (
            f"Reason: 11-hour rest violation with {name}'s shift on {when}."
        )
    if "11h rest" in text.lower() or "11-hour" in text.lower():
        return f"Reason: 11-hour rest violation with {name}'s shift on {when}."
    if "consecutive work days" in text:
        return f"Reason: consecutive work-day limit would be exceeded for {name} on {when}."
    if "weekly rest" in text or "weekly statutory limit" in text:
        return f"Reason: weekly hours/rest limit would be exceeded for {name} on {when}."
    if "FTE target" in text or "fte" in text.lower():
        return f"Reason: FTE hour target would be exceeded for {name} on {when}."
    if "time off" in text.lower() or "approved time off" in text.lower():
        return f"Reason: {name} has approved time off on {when}."
    if IMPOSSIBLE_COVERAGE_TOOLTIP in text or "Insufficient staffing capacity" in text:
        return (
            f"Reason: Insufficient staffing capacity to meet coverage target "
            f"for this {shift} on {when}."
        )
    if "no qualified employees" in text:
        return f"Reason: no qualified staff available for this {shift} on {when}."
    if "no safe assignment" in text or "labor rules" in text:
        return f"Reason: all qualified staff would violate labor rules for this {shift} on {when}."
    if "No coverage scheduled" in text:
        return f"Reason: no employee assigned to this {shift} on {when}."
    return f"Reason: {text}"


def format_unfilled_slot_tooltip(
    *,
    shift_code: str,
    assignment_date: date,
    reason: str,
    constraint_summary: Optional[str] = None,
    is_impossible_coverage: bool = False,
) -> str:
    if is_impossible_coverage:
        return format_rule_violation_tooltip(
            IMPOSSIBLE_COVERAGE_TOOLTIP,
            assignment_date=assignment_date,
            slot_label=shift_code,
        )
    detail = constraint_summary or reason
    return format_rule_violation_tooltip(
        detail,
        assignment_date=assignment_date,
        slot_label=shift_code,
    )
