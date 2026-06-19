from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock, patch

import pandas as pd


def test_streamlit_html_component_wraps_grid_in_keyed_container() -> None:
    from lab_scheduler.ui.schedule_grid.component import streamlit_html_component

    mock_html = MagicMock(return_value={"changes": []})
    mock_container = MagicMock()
    mock_container.__enter__ = MagicMock(return_value=mock_container)
    mock_container.__exit__ = MagicMock(return_value=False)
    with patch("lab_scheduler.ui.schedule_grid.component.st") as mock_st:
        mock_st.container.return_value = mock_container
        mock_st.components.v1.html = mock_html
        streamlit_html_component(
            "<html></html>",
            height=400,
            component_key="grid_key_rev_3",
            interactive=True,
        )
    mock_st.container.assert_called_once_with(key="grid_key_rev_3")
    mock_html.assert_called_once_with(
        "<html></html>",
        height=400,
        scrolling=False,
    )


def test_filter_grid_changes_drops_stale_shift_replay() -> None:
    from scripts.app import _filter_grid_changes_against_draft

    day_key = date(2026, 6, 1).isoformat()
    draft = pd.DataFrame(
        [
            {
                "employee_id": "line-1",
                "Employee": "Vacant MLT D/E - Line 01",
                day_key: "—",
            }
        ]
    )
    employees = [{"id": "line-1", "full_name": "Vacant MLT D/E - Line 01"}]
    stale = [
        {
            "employee_id": "line-1",
            "date": day_key,
            "token": "D",
        }
    ]
    filtered = _filter_grid_changes_against_draft(draft, employees, stale)
    assert filtered == [{"employee_id": "line-1", "date": day_key, "token": "D"}]

    draft.at[0, day_key] = "D"
    filtered_after_match = _filter_grid_changes_against_draft(draft, employees, stale)
    assert filtered_after_match == []


def test_grid_html_does_not_restore_session_storage_queue() -> None:
    from scripts.app import _build_master_schedule_grid_html

    view_dates = [date(2026, 6, 1)]
    with patch("scripts.app.st") as mock_st:
        mock_st.session_state = {}
        html = _build_master_schedule_grid_html(
            pd.DataFrame(
                [
                    {
                        "Employee": "Vacant MLT D/E - Line 01",
                        "employee_id": "line-1",
                        date(2026, 6, 1).isoformat(): "D",
                    }
                ]
            ),
            view_dates,
            period_id="period-1",
            edit_mode=True,
            staging_revision=2,
        )

    assert "restorePendingGridChanges" not in html
    assert "snapshotInitialGridState" not in html
    assert 'sessionStorage.setItem(\n        "labGridPending_' not in html
