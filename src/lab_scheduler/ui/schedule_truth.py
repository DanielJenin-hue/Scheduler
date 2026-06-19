"""Single source of truth for draft-based schedule metrics."""

from __future__ import annotations

from datetime import date
from typing import Dict, List, Mapping, MutableMapping, Optional, Sequence, Tuple

import pandas as pd

from lab_scheduler.compliance.engine import ShiftTemplateInfo
from lab_scheduler.policy.frame_bridge import (
    assignments_from_schedule_frame,
    count_open_shift_gaps_from_frame,
)


def schedule_truth_frame(
    session_state: Mapping[str, object],
    *,
    period_id: str,
    dates: Sequence[date],
    fallback_frame: pd.DataFrame,
    sanitize: Optional[callable] = None,
) -> pd.DataFrame:
    """Return the editable draft when present, otherwise the DB-built fallback."""

    from lab_scheduler.ui.schedule_session import draft_key

    draft = session_state.get(draft_key(period_id))
    if isinstance(draft, pd.DataFrame) and not draft.empty:
        frame = draft.copy()
    else:
        frame = fallback_frame.copy()
    if sanitize is not None:
        return sanitize(frame, list(dates))
    return frame


def contract_hours_deficit_from_frame(
    schedule_frame: pd.DataFrame,
    *,
    employees: Sequence[Mapping[str, object]],
    dates: Sequence[date],
    templates: Mapping[str, Mapping[str, object]],
    employee_target_hours: Mapping[str, float],
    hours_by_token: Mapping[str, float],
    normalize_token: callable,
    is_tally_employee_id: callable,
    schedule_archetype: str,
    paid_hours_per_shift: float,
    worked_shift_tokens: frozenset[str],
    fte_topup_token: str,
) -> Tuple[float, float, float]:
    """Return (contractual_needed, scheduled_actual, net_delta)."""

    contractual_needed = sum(
        float(employee_target_hours.get(str(employee["id"]), 0.0)) for employee in employees
    )
    date_keys = [day.isoformat() for day in dates]
    is_twelve_hour = schedule_archetype == "TWELVE_HOUR"
    scheduled_actual = 0.0
    for _, row in schedule_frame.iterrows():
        employee_id = row.get("employee_id")
        if is_tally_employee_id(employee_id):
            continue
        if is_twelve_hour:
            worked = 0
            has_topup = False
            for day_key in date_keys:
                token = normalize_token(row.get(day_key, ""))
                if token in worked_shift_tokens:
                    worked += 1
                elif token == fte_topup_token:
                    has_topup = True
            line_actual = worked * paid_hours_per_shift
            line_target = float(employee_target_hours.get(employee_id, 0.0))
            if has_topup and line_actual < line_target:
                line_actual = line_target
            scheduled_actual += line_actual
        else:
            for day_key in date_keys:
                token = normalize_token(row.get(day_key, ""))
                if token in hours_by_token:
                    scheduled_actual += float(hours_by_token[token])
    return contractual_needed, scheduled_actual, scheduled_actual - contractual_needed


def open_shift_gaps_from_frame(
    schedule_frame: pd.DataFrame,
    *,
    employees: Sequence[Mapping[str, object]],
    dates: Sequence[date],
    db_templates: Mapping[str, Mapping[str, object]],
    shift_templates: Mapping[str, ShiftTemplateInfo],
    period_start: date,
    period_end: date,
    schedule_archetype: str = "STANDARD",
) -> int:
    return count_open_shift_gaps_from_frame(
        schedule_frame,
        employees=employees,
        dates=dates,
        db_templates=db_templates,
        shift_templates=shift_templates,
        period_start=period_start,
        period_end=period_end,
        schedule_archetype=schedule_archetype,
    )


def assignments_from_truth_frame(
    schedule_frame: pd.DataFrame,
    *,
    employees: Sequence[Mapping[str, object]],
    dates: Sequence[date],
    templates: Mapping[str, Mapping[str, object]],
) -> List[dict]:
    shifts = assignments_from_schedule_frame(
        schedule_frame,
        employees=employees,
        dates=dates,
        templates=templates,
    )
    return [
        {
            "employee_id": shift.employee_id,
            "assignment_date": shift.assignment_date,
            "shift_template_id": shift.shift_template_id,
        }
        for shift in shifts
    ]
