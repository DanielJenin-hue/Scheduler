from __future__ import annotations

from datetime import date
from typing import Dict, List, Mapping, Optional, Sequence, Set

import pandas as pd

from lab_scheduler.compliance.engine import ScheduledShift, ShiftTemplateInfo
from lab_scheduler.engine.swap_controller import ScheduleState
from lab_scheduler.scheduling.auto_generate import EmployeeProfile
from lab_scheduler.scheduling.schedule_tallies import is_daily_tally_employee_id


def normalize_grid_shift_token(value: object) -> str:
    """Normalize a master-grid cell token to D/E/N/off codes or empty string."""

    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    text = str(value).strip().upper()
    if text in ("", "—", "-", "OFF", "NONE", "NAN", "."):
        return ""
    if text in ("S", "SPECIMEN"):
        return "D"
    if text in ("D", "M", "E", "N"):
        return "D" if text == "M" else text
    short = text[:1] if text else ""
    if short == "M":
        return "D"
    return short if short in {"D", "E", "N", "I", "V"} else ""


def schedule_frame_row_index_by_employee_id(frame: pd.DataFrame) -> Dict[str, int]:
    """Map roster employee_id to dataframe row index (Portage rows are sorted)."""

    if frame.empty or "employee_id" not in frame.columns:
        return {}
    lookup: Dict[str, int] = {}
    for index in frame.index:
        employee_id = str(frame.at[index, "employee_id"] or "")
        if employee_id and employee_id not in lookup:
            lookup[employee_id] = int(index)
    return lookup


def template_id_from_short(
    templates: Mapping[str, Mapping[str, object]],
    short: str,
) -> Optional[str]:
    if not short:
        return None
    normalized = "D" if short == "M" else short
    for template_id, template in templates.items():
        tmpl_short = str(template.get("short", "") or "")
        if tmpl_short == normalized or tmpl_short == short:
            return template_id
        if normalized == "D" and template.get("code") == "MORNING":
            return template_id
    return None


def assignments_from_schedule_frame(
    frame: pd.DataFrame,
    *,
    employees: Sequence[Mapping[str, object] | EmployeeProfile],
    dates: Sequence[date],
    templates: Mapping[str, Mapping[str, object]],
) -> List[ScheduledShift]:
    """Build ScheduledShift rows from the editable grid frame."""

    def _employee_id_and_name(employee: Mapping[str, object] | EmployeeProfile) -> tuple[str, str]:
        if isinstance(employee, EmployeeProfile):
            return employee.id, employee.full_name
        return str(employee["id"]), str(employee["full_name"])

    names = dict(_employee_id_and_name(employee) for employee in employees)
    scheduled: List[ScheduledShift] = []
    for _, row in frame.iterrows():
        employee_id = str(row.get("employee_id", "") or "")
        if not employee_id or is_daily_tally_employee_id(employee_id):
            continue
        for day in dates:
            day_key = day.isoformat()
            token = normalize_grid_shift_token(row.get(day_key, ""))
            if token not in {"D", "E", "N"}:
                continue
            template_id = template_id_from_short(templates, token)
            if template_id is None:
                continue
            scheduled.append(
                ScheduledShift(
                    employee_id=employee_id,
                    employee_name=names.get(employee_id, employee_id),
                    assignment_date=day,
                    shift_template_id=template_id,
                )
            )
    return scheduled


def count_open_shift_gaps_from_frame(
    frame: pd.DataFrame,
    *,
    employees: Sequence[Mapping[str, object] | EmployeeProfile],
    dates: Sequence[date],
    db_templates: Mapping[str, Mapping[str, object]],
    shift_templates: Mapping[str, ShiftTemplateInfo],
    period_start: date,
    period_end: date,
    schedule_archetype: str = "STANDARD",
) -> int:
    """Count facility-level D/E/N coverage gaps from the editable grid draft."""

    from lab_scheduler.engine.manager_dashboard import count_open_shift_gaps

    scheduled = assignments_from_schedule_frame(
        frame,
        employees=employees,
        dates=dates,
        templates=db_templates,
    )
    return count_open_shift_gaps(
        period_start=period_start,
        period_end=period_end,
        shift_templates=dict(shift_templates),
        assignments=scheduled,
        schedule_archetype=schedule_archetype,
    )


def build_schedule_state_from_frame(
    frame: pd.DataFrame,
    *,
    rules: object,
    period_start: date,
    period_end: date,
    weeks_in_period: int,
    employees: Sequence[EmployeeProfile],
    templates: Mapping[str, ShiftTemplateInfo],
    shift_required_qualifications: Mapping[str, Set[str]],
    db_templates: Mapping[str, Mapping[str, object]],
    dates: Sequence[date],
    employee_target_hours: Optional[Mapping[str, float]] = None,
    availability_blocked: Optional[Mapping[str, Set[date]]] = None,
) -> ScheduleState:
    """Construct a ScheduleState snapshot from the staged draft grid."""

    assignments = assignments_from_schedule_frame(
        frame,
        employees=employees,
        dates=dates,
        templates=db_templates,
    )
    return ScheduleState(
        rules=rules,
        period_start=period_start,
        period_end=period_end,
        weeks_in_period=weeks_in_period,
        employees=employees,
        assignments=assignments,
        shift_templates=templates,
        shift_required_qualifications=shift_required_qualifications,
        employee_target_hours=employee_target_hours,
        availability_blocked=availability_blocked,
    )
