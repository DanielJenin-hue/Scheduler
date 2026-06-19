"""Clear + Save must persist an empty draft over a populated database."""

from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock, patch

import pandas as pd

from lab_scheduler.policy.policy_engine import CellMutation


def test_is_intentional_clear_save_requires_clear_mutations() -> None:
    from scripts.app import _is_intentional_clear_save

    period_id = "period-1"
    day = date(2026, 6, 1)
    mutations = [
        CellMutation(
            employee_id="line-1",
            assignment_date=day,
            previous_token="D",
            new_token="—",
        )
    ]

    with patch("scripts.app.st") as mock_st:
        mock_st.session_state = {}
        with patch("scripts.app._load_pending_mutations", return_value=mutations):
            assert _is_intentional_clear_save(
                period_id,
                draft_shift_count=0,
                db_shift_count=10,
            )
            assert not _is_intentional_clear_save(
                period_id,
                draft_shift_count=1,
                db_shift_count=10,
            )
            assert not _is_intentional_clear_save(
                period_id,
                draft_shift_count=0,
                db_shift_count=0,
            )

    with patch("scripts.app.st") as mock_st:
        mock_st.session_state = {}
        with patch("scripts.app._load_pending_mutations", return_value=[]):
            assert not _is_intentional_clear_save(
                period_id,
                draft_shift_count=0,
                db_shift_count=10,
            )


def test_standard_manual_save_allows_intentional_clear() -> None:
    from scripts.app import (
        _schedule_draft_key,
        _standard_manual_save,
    )

    period_id = "period-1"
    day = date(2026, 6, 1)
    day_key = day.isoformat()
    draft = pd.DataFrame(
        [
            {
                "employee_id": "line-1",
                "Employee": "Line 01",
                day_key: "—",
            }
        ]
    )
    state = {
        _schedule_draft_key(period_id): draft,
    }
    period = MagicMock(
        id=period_id,
        period_start=day,
        period_end_inclusive=day,
    )
    db_assignment = {
        "employee_id": "line-1",
        "assignment_date": day,
        "shift_template_id": "shift-morning",
    }
    mutations = [
        CellMutation(
            employee_id="line-1",
            assignment_date=day,
            previous_token="D",
            new_token="—",
        )
    ]

    with patch("scripts.app.st") as mock_st:
        mock_st.session_state = state
        mock_st.toast = MagicMock()
        with patch("scripts.app._fetch_assignments", return_value=[db_assignment]):
            with patch("scripts.app._load_pending_mutations", return_value=mutations):
                with patch("scripts.app._publish_schedule_draft", return_value=True):
                    with patch(
                        "scripts.app._write_standard_json_backup",
                        return_value=MagicMock(name="period-1-manual.json"),
                    ):
                        ok, message = _standard_manual_save(
                            MagicMock(),
                            tenant_id="tenant-1",
                            period=period,
                            rules=MagicMock(),
                            employees=[{"id": "line-1", "full_name": "Line 01"}],
                            templates={
                                "shift-morning": {
                                    "id": "shift-morning",
                                    "code": "MORNING",
                                    "short": "D",
                                },
                            },
                            employee_target_hours={"line-1": 320.0},
                            availability_blocked={},
                        )

    assert ok is True
    assert "Saved to database" in message


def test_finish_pending_workspace_save_skips_early_when_not_intentional_clear() -> None:
    from scripts.app import (
        _finish_pending_workspace_save,
        _schedule_draft_key,
        _standard_manual_save,
    )

    period_id = "period-1"
    day = date(2026, 6, 1)
    day_key = day.isoformat()
    draft = pd.DataFrame(
        [
            {
                "employee_id": "line-1",
                "Employee": "Line 01",
                day_key: "D",
            }
        ]
    )
    state = {
        _schedule_draft_key(period_id): draft,
        "schedule_save_requested_period-1": True,
    }
    period = MagicMock(
        id=period_id,
        period_start=day,
        period_end_inclusive=day,
    )

    with patch("scripts.app.st") as mock_st:
        mock_st.session_state = state
        mock_st.toast = MagicMock()
        with patch("scripts.app._standard_manual_save") as save_fn:
            completed = _finish_pending_workspace_save(
                MagicMock(),
                tenant_id="tenant-1",
                period=period,
                rules=MagicMock(),
                employees=[{"id": "line-1", "full_name": "Line 01"}],
                templates={
                    "shift-morning": {
                        "id": "shift-morning",
                        "code": "MORNING",
                        "short": "D",
                    },
                },
                draft_key=_schedule_draft_key(period_id),
                baseline_key=f"schedule_baseline_{period_id}",
                dates=[day],
                assignments=[],
                blocked_map={},
                target_hours={"line-1": 320.0},
                blocked_sets={},
                only_intentional_clear=True,
            )

    assert completed is False
    assert state["schedule_save_requested_period-1"] is True
    save_fn.assert_not_called()
