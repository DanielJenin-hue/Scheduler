"""Deferred save pipeline: tab redirect and ops-console completion."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from lab_scheduler.ui.save_pipeline import (
    ensure_schedule_tab_for_pending_save,
    handle_save_button_click,
    maybe_complete_deferred_save,
)


def test_handle_save_button_click_sets_flag_without_rerun() -> None:
    state: dict = {}
    rerun = MagicMock()
    handle_save_button_click(state, "period-1", rerun=rerun)
    assert state["schedule_save_requested_period-1"] is True
    rerun.assert_not_called()


def test_ensure_schedule_tab_redirects_from_analytics() -> None:
    state = {"schedule_save_requested_period-1": True}
    toast = MagicMock()
    rerun = MagicMock()
    blocked = ensure_schedule_tab_for_pending_save(
        state,
        "period-1",
        current_tab="Analytics",
        toast=toast,
        rerun=rerun,
    )
    assert blocked is True
    assert state["manager_workspace_tab_period-1"] == "Schedule"
    toast.assert_called_once()
    rerun.assert_called_once()


def test_ensure_schedule_tab_allows_schedule_tab() -> None:
    state = {"schedule_save_requested_period-1": True}
    blocked = ensure_schedule_tab_for_pending_save(
        state,
        "period-1",
        current_tab="Schedule",
        toast=MagicMock(),
        rerun=MagicMock(),
    )
    assert blocked is False


def test_maybe_complete_deferred_save_ops_console_path() -> None:
    """Save completion must not require manager_mode (ops demo tenants)."""

    state = {"schedule_save_requested_period-1": True}
    save_fn = MagicMock(return_value=(True, "Saved to database."))
    rerun = MagicMock()
    toast = MagicMock()
    notices: dict = {}

    maybe_complete_deferred_save(
        MagicMock(),
        state,
        period_id="period-1",
        tenant_id="tenant-northstar-lab",
        period=MagicMock(id="period-1"),
        rules=MagicMock(),
        employees=[],
        templates={},
        employee_target_hours={},
        availability_blocked={},
        standard_manual_save=save_fn,
        workspace_publish_notice_key=lambda pid: f"publish_notice_{pid}",
        set_publish_notice=lambda key, msg: notices.__setitem__(key, msg),
        toast=toast,
        rerun=rerun,
    )

    save_fn.assert_called_once()
    toast.assert_called_once()
    rerun.assert_called_once()
    assert "publish_notice_period-1" in notices


def test_maybe_complete_deferred_save_skips_when_not_requested() -> None:
    state: dict = {}
    save_fn = MagicMock()
    maybe_complete_deferred_save(
        MagicMock(),
        state,
        period_id="period-1",
        tenant_id="tenant-1",
        period=MagicMock(id="period-1"),
        rules=MagicMock(),
        employees=[],
        templates={},
        employee_target_hours={},
        availability_blocked={},
        standard_manual_save=save_fn,
        workspace_publish_notice_key=lambda pid: f"publish_notice_{pid}",
        set_publish_notice=lambda key, msg: None,
        toast=MagicMock(),
        rerun=MagicMock(),
    )
    save_fn.assert_not_called()


def test_deferred_save_wrapper_does_not_gate_on_manager_mode() -> None:
    from scripts.app import _maybe_complete_deferred_schedule_save

    with patch("scripts.app._complete_deferred_save") as complete:
        _maybe_complete_deferred_schedule_save(
            MagicMock(),
            tenant_id="tenant-northstar-lab",
            period=MagicMock(id="period-1"),
            rules=MagicMock(),
            employees=[],
            templates={},
            employee_target_hours={},
            availability_blocked={},
            manager_mode=False,
        )
        complete.assert_called_once()
