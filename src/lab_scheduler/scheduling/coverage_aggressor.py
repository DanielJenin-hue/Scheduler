from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from lab_scheduler.compliance.engine import ShiftTemplateInfo
from lab_scheduler.compliance.compliance_rules import (
    APPROVED_STRETCH_CODE,
    CONSECUTIVE_DAYS_WARNING_CODE,
    JOANNE_STYLE_STRETCH_CODE,
)
from lab_scheduler.errors.schedule_error import ScheduleError
from lab_scheduler.compliance.jurisdictions import JurisdictionRules
from lab_scheduler.engine.demand import ExpandedScheduleSlot
from lab_scheduler.scheduling.profiles import EmployeeProfile

@dataclass(frozen=True, slots=True)
class AggressiveFillFlag:
    """One compliance rule intentionally broken to preserve coverage."""

    category: str
    code: str
    message: str
    employee_id: str = ""
    employee_name: str = ""
    assignment_date: Optional[date] = None
    stretch_type: str = "normal"

    def export_line(self) -> str:
        parts = [self.code, self.category]
        if self.stretch_type == "joanne_style":
            parts.insert(0, "JOANNE-STYLE EXTENDED SHIFT")
        elif self.stretch_type == "approved_stretch":
            parts.insert(0, "MANAGER APPROVED STRETCH")
        if self.assignment_date is not None:
            parts.append(self.assignment_date.isoformat())
        if self.employee_name:
            parts.append(self.employee_name)
        parts.append(self.message)
        return " | ".join(parts)


def collect_clinical_stretch_flags(
    assignments: Sequence[object],
    *,
    employees: Sequence[EmployeeProfile],
    shift_templates: Mapping[str, ShiftTemplateInfo],
) -> List[AggressiveFillFlag]:
    """Surface Joanne-style clinical stretches and manager-approved stretches."""

    employees_by_id = {employee.id: employee for employee in employees}
    flags: List[AggressiveFillFlag] = []
    for assignment in assignments:
        approved = bool(getattr(assignment, "approved_stretch", False))
        clinical = bool(getattr(assignment, "clinical_floor_stretch", False))
        if not approved and not clinical:
            continue
        employee_id = str(getattr(assignment, "employee_id", ""))
        employee = employees_by_id.get(employee_id)
        template_id = str(getattr(assignment, "shift_template_id", ""))
        template = shift_templates.get(template_id)
        shift_code = template.code if template is not None else template_id
        assignment_date = getattr(assignment, "assignment_date", None)
        if clinical:
            flags.append(
                AggressiveFillFlag(
                    category="clinical_stretch",
                    code=JOANNE_STYLE_STRETCH_CODE,
                    message=(
                        f"{employee.full_name if employee else employee_id} assigned "
                        f"{shift_code} on "
                        f"{assignment_date.isoformat() if assignment_date else 'unknown'} "
                        f"as a Joanne-style extended shift (≤24h span) to secure clinical floor coverage."
                    ),
                    employee_id=employee_id,
                    employee_name=employee.full_name if employee else employee_id,
                    assignment_date=assignment_date if isinstance(assignment_date, date) else None,
                    stretch_type="joanne_style",
                )
            )
        elif approved:
            flags.append(
                AggressiveFillFlag(
                    category="manager_override",
                    code=APPROVED_STRETCH_CODE,
                    message=(
                        f"{employee.full_name if employee else employee_id} assigned "
                        f"{shift_code} on "
                        f"{assignment_date.isoformat() if assignment_date else 'unknown'} "
                        f"with Manager Override — Approved Stretch."
                    ),
                    employee_id=employee_id,
                    employee_name=employee.full_name if employee else employee_id,
                    assignment_date=assignment_date if isinstance(assignment_date, date) else None,
                    stretch_type="approved_stretch",
                )
            )
    return flags


def collect_aggressive_fill_flags(
    *,
    assignments: Sequence[object],
    employees: Sequence[EmployeeProfile],
    shift_templates: Mapping[str, ShiftTemplateInfo],
    rules: JurisdictionRules,
    period_start: date,
    period_end: date,
    weeks_in_period: int,
    employee_target_hours: Optional[Mapping[str, float]] = None,
    fill_counts: Optional[Mapping[Tuple[date, str, Optional[str]], int]] = None,
    expanded_slots: Optional[Sequence[ExpandedScheduleSlot]] = None,
    clinical_gap_messages: Optional[Sequence[str]] = None,
    scheduled_shifts_from_assignments,
) -> List[AggressiveFillFlag]:
    """Audit all rule breaks after coverage aggressor generation (non-blocking)."""

    from lab_scheduler.audit.compliance import (
        ComplianceValidator,
        build_overtime_compliance_bypass_conflicts,
    )

    scheduled = scheduled_shifts_from_assignments(assignments, employees)
    validator = ComplianceValidator()
    validation = validator.validate(
        rules=rules,
        employees=employees,
        assignments=scheduled,
        shift_templates=shift_templates,
        period_start=period_start,
        period_end=period_end,
        weeks_in_period=weeks_in_period,
        employee_target_hours=employee_target_hours,
        fill_counts=fill_counts,
        expanded_slots=expanded_slots,
        enforce_clinical_floors=True,
        require_contract_fte=True,
    )
    flags: List[AggressiveFillFlag] = []
    flags.extend(
        collect_clinical_stretch_flags(
            assignments,
            employees=employees,
            shift_templates=shift_templates,
        )
    )
    stretch_keys = {
        (flag.employee_id, flag.assignment_date, flag.code)
        for flag in flags
        if flag.assignment_date is not None
    }
    for conflict in validation.conflicts:
        key = (conflict.employee_id, conflict.assignment_date, conflict.code)
        if key in stretch_keys and conflict.code in {
            ScheduleError.UNION_TURNAROUND_15H.value,
            ScheduleError.UNION_MORNING_REST_11H.value,
        }:
            continue
        flags.append(
            AggressiveFillFlag(
                category=conflict.category,
                code=conflict.code,
                message=conflict.message,
                employee_id=conflict.employee_id,
                employee_name=conflict.employee_name,
                assignment_date=conflict.assignment_date,
                stretch_type="normal",
            )
        )
    for warning in validation.warnings:
        flags.append(
            AggressiveFillFlag(
                category=warning.category,
                code=warning.code,
                message=warning.message,
                employee_id=warning.employee_id,
                employee_name=warning.employee_name,
                assignment_date=warning.assignment_date,
                stretch_type="normal",
            )
        )
    for bypass in build_overtime_compliance_bypass_conflicts(
        assignments,
        employees=employees,
        shift_templates=shift_templates,
    ):
        flags.append(
            AggressiveFillFlag(
                category=bypass.category,
                code=bypass.code,
                message=bypass.message,
                employee_id=bypass.employee_id,
                employee_name=bypass.employee_name,
                assignment_date=bypass.assignment_date,
                stretch_type="normal",
            )
        )
    if clinical_gap_messages:
        for message in clinical_gap_messages:
            flags.append(
                AggressiveFillFlag(
                    category="clinical_coverage",
                    code=ScheduleError.CLINICAL_GAP_REMAINS.value,
                    message=message,
                    stretch_type="normal",
                )
            )
    return flags


def format_aggressive_fill_flags_csv_rows(
    flags: Sequence[AggressiveFillFlag],
) -> List[Dict[str, str]]:
    """Header rows for CSV schedule export."""

    rows: List[Dict[str, str]] = [
        {
            "Employee": "AGGRESSIVE_FILL_FLAGS",
            "employee_id": "COVERAGE_AGGRESSOR_MODE",
            "fte": "",
            "contract_line_type": f"{len(flags)} rule break(s) logged to achieve coverage",
        }
    ]
    for index, flag in enumerate(flags, start=1):
        label = f"FLAG {index:03d}"
        if flag.stretch_type == "joanne_style":
            label = f"JOANNE-STRETCH {index:03d}"
        elif flag.stretch_type == "approved_stretch":
            label = f"APPROVED-STRETCH {index:03d}"
        rows.append(
            {
                "Employee": label,
                "employee_id": flag.code,
                "fte": flag.category,
                "contract_line_type": flag.export_line(),
            }
        )
    rows.append(
        {
            "Employee": "— END AGGRESSIVE_FILL_FLAGS —",
            "employee_id": "",
            "fte": "",
            "contract_line_type": "",
        }
    )
    return rows


def format_aggressive_fill_flags_html(flags: Sequence[AggressiveFillFlag]) -> str:
    if not flags:
        return ""

    joanne_flags = [flag for flag in flags if flag.stretch_type == "joanne_style"]
    approved_flags = [flag for flag in flags if flag.stretch_type == "approved_stretch"]
    normal_flags = [flag for flag in flags if flag.stretch_type == "normal"]

    def _items(items: Sequence[AggressiveFillFlag], limit: int = 200) -> str:
        body = "".join(
            f"<li><strong>{flag.code}</strong> — {_esc(flag.message)}</li>"
            for flag in items[:limit]
        )
        if len(items) > limit:
            body += f"<li>… and {len(items) - limit} more</li>"
        return body

    joanne_section = ""
    if joanne_flags:
        joanne_section = f"""
  <h3>Joanne-Style Extended Shifts (Clinical Floor)</h3>
  <p class="aggressive-fill-note">These assignments use a ≤24h stretch to maintain the 2-seat Evening/Night clinical floor.</p>
  <ul class="aggressive-fill-list joanne-style">{_items(joanne_flags)}</ul>
"""

    approved_section = ""
    if approved_flags:
        approved_section = f"""
  <h3>Manager Approved Stretch</h3>
  <ul class="aggressive-fill-list approved-stretch">{_items(approved_flags)}</ul>
"""

    normal_section = ""
    if normal_flags:
        normal_section = f"""
  <h3>Other Compliance Flags</h3>
  <ul class="aggressive-fill-list">{_items(normal_flags)}</ul>
"""

    return f"""
<section class="aggressive-fill-flags">
  <h2>AGGRESSIVE_FILL_FLAGS — Coverage Aggressor Mode</h2>
  <p class="aggressive-fill-note">
    Schedule exported despite compliance gaps. {len(flags)} rule break(s) were accepted to preserve clinical coverage.
  </p>
{joanne_section}{approved_section}{normal_section}
</section>
"""


def _esc(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
