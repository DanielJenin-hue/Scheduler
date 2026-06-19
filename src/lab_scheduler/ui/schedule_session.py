"""Streamlit session keys and helpers for the editable schedule workspace."""

from __future__ import annotations

from typing import Any, List, Mapping, MutableMapping, Optional, Sequence

import pandas as pd

from lab_scheduler.policy.policy_engine import (
    CellMutation,
    cell_mutation_from_dict,
    cell_mutation_to_dict,
)

SCHEDULE_TAB_NAME = "Schedule"


def draft_key(period_id: str) -> str:
    return f"schedule_draft_{period_id}"


def baseline_key(period_id: str) -> str:
    return f"schedule_baseline_{period_id}"


def save_requested_key(period_id: str) -> str:
    return f"schedule_save_requested_{period_id}"


def sync_key(period_id: str) -> str:
    return f"schedule_sync_{period_id}"


def pending_mutations_key(period_id: str) -> str:
    return f"schedule_pending_mutations_{period_id}"


def ignore_grid_echo_key(period_id: str) -> str:
    return f"schedule_ignore_grid_echo_{period_id}"


def matrix_cache_key(period_id: str) -> str:
    return f"schedule_matrix_{period_id}"


def staging_revision_key(period_id: str) -> str:
    return f"schedule_grid_staging_revision_{period_id}"


def manager_workspace_tab_key(period_id: str) -> str:
    return f"manager_workspace_tab_{period_id}"


def billing_checkout_trigger_key(tenant_id: str) -> str:
    return f"billing_checkout_trigger_{tenant_id}"


def load_pending_mutations(
    session_state: MutableMapping[str, Any],
    period_id: str,
) -> List[CellMutation]:
    raw = session_state.get(pending_mutations_key(period_id), [])
    if not isinstance(raw, list):
        return []
    return [cell_mutation_from_dict(item) for item in raw if isinstance(item, dict)]


def save_pending_mutations(
    session_state: MutableMapping[str, Any],
    period_id: str,
    mutations: Sequence[CellMutation],
) -> None:
    session_state[pending_mutations_key(period_id)] = [
        cell_mutation_to_dict(mutation) for mutation in mutations
    ]


def request_save(session_state: MutableMapping[str, Any], period_id: str) -> None:
    session_state[save_requested_key(period_id)] = True


def peek_save_requested(session_state: Mapping[str, Any], period_id: str) -> bool:
    return bool(session_state.get(save_requested_key(period_id)))


def pop_save_requested(session_state: MutableMapping[str, Any], period_id: str) -> bool:
    return bool(session_state.pop(save_requested_key(period_id), False))


def mark_sync_from_db(session_state: MutableMapping[str, Any], period_id: str) -> None:
    session_state[sync_key(period_id)] = True


def pop_sync_from_db(session_state: MutableMapping[str, Any], period_id: str) -> bool:
    return bool(session_state.pop(sync_key(period_id), False))


def set_ignore_grid_echo(session_state: MutableMapping[str, Any], period_id: str) -> None:
    session_state[ignore_grid_echo_key(period_id)] = True


def pop_ignore_grid_echo(session_state: MutableMapping[str, Any], period_id: str) -> bool:
    return bool(session_state.pop(ignore_grid_echo_key(period_id), False))


def staging_revision(session_state: Mapping[str, Any], period_id: str) -> int:
    return int(session_state.get(staging_revision_key(period_id), 0) or 0)


def bump_staging_revision(session_state: MutableMapping[str, Any], period_id: str) -> int:
    revision = staging_revision(session_state, period_id) + 1
    session_state[staging_revision_key(period_id)] = revision
    return revision


def invalidate_matrix_cache(session_state: MutableMapping[str, Any], period_id: str) -> None:
    session_state.pop(matrix_cache_key(period_id), None)


def redirect_save_to_schedule_tab(
    session_state: MutableMapping[str, Any],
    period_id: str,
    *,
    current_tab: str,
) -> bool:
    """Switch to Schedule tab when save is pending elsewhere. Returns True if redirected."""

    if not peek_save_requested(session_state, period_id):
        return False
    if current_tab == SCHEDULE_TAB_NAME:
        return False
    session_state[manager_workspace_tab_key(period_id)] = SCHEDULE_TAB_NAME
    return True


def get_draft(
    session_state: Mapping[str, Any],
    period_id: str,
) -> Optional[pd.DataFrame]:
    frame = session_state.get(draft_key(period_id))
    if isinstance(frame, pd.DataFrame):
        return frame
    return None


def set_draft(
    session_state: MutableMapping[str, Any],
    period_id: str,
    frame: pd.DataFrame,
) -> None:
    session_state[draft_key(period_id)] = frame


def has_unsaved_edits(
    session_state: Mapping[str, Any],
    period_id: str,
) -> bool:
    return len(load_pending_mutations(session_state, period_id)) > 0
