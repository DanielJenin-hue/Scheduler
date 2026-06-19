"""ScheduleSession key helpers and save redirect behavior."""

from __future__ import annotations

from datetime import date

from lab_scheduler.policy.policy_engine import CellMutation
from lab_scheduler.ui import schedule_session as sess


def test_pending_mutations_round_trip() -> None:
    state: dict = {}
    period_id = "period-1"
    mutations = [
        CellMutation(
            employee_id="line-1",
            assignment_date=date(2026, 6, 1),
            previous_token="",
            new_token="D",
        )
    ]
    sess.save_pending_mutations(state, period_id, mutations)
    loaded = sess.load_pending_mutations(state, period_id)
    assert len(loaded) == 1
    assert loaded[0].employee_id == "line-1"
    assert loaded[0].new_token == "D"


def test_save_requested_pop() -> None:
    state: dict = {}
    period_id = "period-1"
    assert sess.peek_save_requested(state, period_id) is False
    sess.request_save(state, period_id)
    assert sess.peek_save_requested(state, period_id) is True
    assert sess.pop_save_requested(state, period_id) is True
    assert sess.peek_save_requested(state, period_id) is False


def test_redirect_save_to_schedule_tab() -> None:
    state: dict = {}
    period_id = "period-1"
    sess.request_save(state, period_id)
    redirected = sess.redirect_save_to_schedule_tab(
        state,
        period_id,
        current_tab="Analytics",
    )
    assert redirected is True
    assert state[sess.manager_workspace_tab_key(period_id)] == sess.SCHEDULE_TAB_NAME


def test_redirect_skipped_on_schedule_tab() -> None:
    state: dict = {}
    period_id = "period-1"
    sess.request_save(state, period_id)
    assert (
        sess.redirect_save_to_schedule_tab(
            state,
            period_id,
            current_tab=sess.SCHEDULE_TAB_NAME,
        )
        is False
    )


def test_has_unsaved_edits() -> None:
    state: dict = {}
    period_id = "period-1"
    assert sess.has_unsaved_edits(state, period_id) is False
    sess.save_pending_mutations(state, period_id, [])
    assert sess.has_unsaved_edits(state, period_id) is False
    sess.save_pending_mutations(
        state,
        period_id,
        [
            CellMutation(
                employee_id="line-1",
                assignment_date=date(2026, 6, 1),
                previous_token="D",
                new_token="E",
            )
        ],
    )
    assert sess.has_unsaved_edits(state, period_id) is True


def test_sync_from_db_flag() -> None:
    state: dict = {}
    period_id = "period-1"
    assert sess.pop_sync_from_db(state, period_id) is False
    sess.mark_sync_from_db(state, period_id)
    assert sess.pop_sync_from_db(state, period_id) is True
    assert sess.pop_sync_from_db(state, period_id) is False
