"""Deferred save orchestration for the schedule workspace."""

from __future__ import annotations

import sqlite3
from datetime import date
from typing import Callable, Dict, List, MutableMapping, Optional, Set, Tuple

from lab_scheduler.compliance.jurisdictions import JurisdictionRules
from lab_scheduler.ui.schedule_session import (
    SCHEDULE_TAB_NAME,
    pop_save_requested,
    redirect_save_to_schedule_tab,
    request_save,
)


SaveFn = Callable[..., Tuple[bool, str]]
RerunFn = Callable[[], None]
ToastFn = Callable[..., None]


def handle_save_button_click(
    session_state: MutableMapping[str, object],
    period_id: str,
    *,
    rerun: RerunFn,
) -> None:
    """Stage save for completion after the grid iframe flushes edits this run."""

    del rerun
    request_save(session_state, period_id)


def ensure_schedule_tab_for_pending_save(
    session_state: MutableMapping[str, object],
    period_id: str,
    *,
    current_tab: str,
    toast: ToastFn,
    rerun: RerunFn,
) -> bool:
    """Redirect to Schedule when save was requested from another tab."""

    if redirect_save_to_schedule_tab(
        session_state,
        period_id,
        current_tab=current_tab,
    ):
        toast(
            "Open the Schedule tab to apply grid edits before saving.",
            icon="ℹ️",
        )
        rerun()
        return True
    return False


def maybe_complete_deferred_save(
    conn: sqlite3.Connection,
    session_state: MutableMapping[str, object],
    *,
    period_id: str,
    tenant_id: str,
    period: object,
    rules: JurisdictionRules,
    employees: List[Dict],
    templates: Dict[str, Dict],
    employee_target_hours: Dict[str, float],
    availability_blocked: Dict[str, Set[date]],
    standard_manual_save: SaveFn,
    workspace_publish_notice_key: Callable[[str], str],
    set_publish_notice: Callable[[str, str], None],
    toast: ToastFn,
    rerun: RerunFn,
) -> None:
    """Complete a deferred save after the grid has flushed iframe edits."""

    if not pop_save_requested(session_state, period_id):
        return False
    saved, message = standard_manual_save(
        conn,
        tenant_id=tenant_id,
        period=period,
        rules=rules,
        employees=employees,
        templates=templates,
        employee_target_hours=employee_target_hours,
        availability_blocked=availability_blocked,
    )
    if saved and message:
        set_publish_notice(workspace_publish_notice_key(period_id), message)
        toast(message.replace("`", ""), icon="✅")
        rerun()
        return True
    if not saved:
        toast(message or "Save failed — resolve grid errors and try again.", icon="⚠️")
    return False


__all__ = [
    "SCHEDULE_TAB_NAME",
    "ensure_schedule_tab_for_pending_save",
    "handle_save_button_click",
    "maybe_complete_deferred_save",
]
