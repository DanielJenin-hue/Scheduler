"""Ops ribbon metrics derived from the draft schedule frame."""

from __future__ import annotations

from typing import Any, Dict, Mapping, Optional, Set

import pandas as pd

from lab_scheduler.compliance.engine import ShiftTemplateInfo
from lab_scheduler.policy.frame_bridge import count_open_shift_gaps_from_frame
from lab_scheduler.policy.policy_engine import SchedulePolicyEngine


def live_gap_count_from_draft(
    draft_frame: pd.DataFrame,
    *,
    employees: list,
    dates: list,
    templates: Dict[str, Dict],
    template_info: Mapping[str, ShiftTemplateInfo],
    period_start: object,
    period_end: object,
    schedule_archetype: str,
) -> int:
    return count_open_shift_gaps_from_frame(
        draft_frame,
        employees=employees,
        dates=dates,
        db_templates=templates,
        shift_templates=template_info,
        period_start=period_start,
        period_end=period_end,
        schedule_archetype=schedule_archetype,
    )


def live_policy_view_from_draft(
    draft_frame: pd.DataFrame,
    *,
    employees: list,
    dates: list,
    week_count: int,
    pending_mutations: list,
    cell_errors: Dict[str, str],
    schedule_archetype: str,
    contract_target_hours: Dict[str, float],
) -> object:
    policy_engine = SchedulePolicyEngine()
    return policy_engine.derive_view_model(
        draft_frame,
        employees=employees,
        dates=dates,
        week_count=week_count,
        pending_mutations=pending_mutations,
        cell_errors=cell_errors,
        schedule_archetype=schedule_archetype,
        contract_target_hours=contract_target_hours,
    )


def refresh_ops_ribbon_slot(
    ribbon_slot: Any,
    *,
    draft_frame: pd.DataFrame,
    render_ribbon: callable,
    ribbon_kwargs: Dict[str, object],
    gap_count: int,
    policy_view: object,
) -> None:
    """Render ops ribbon inside a Streamlit empty slot."""

    payload = dict(ribbon_kwargs)
    payload["schedule_frame"] = draft_frame
    payload["gap_count"] = gap_count
    payload["policy_view"] = policy_view
    with ribbon_slot.container():
        render_ribbon(**payload)
