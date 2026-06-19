"""Grid lock markup tests."""

from __future__ import annotations

from datetime import date
from unittest.mock import patch

import pandas as pd


def test_locked_cells_use_green_ring_class() -> None:
    from scripts.app import _shift_pill_html

    html = _shift_pill_html(
        employee_id="line-1",
        day_key="2026-06-01",
        display_value="D",
        edit_mode=True,
        is_locked=True,
    )
    assert "lab-shift-locked" in html
    assert "lab-night-anchor" not in html


def test_edit_grid_includes_week_start_class_on_mondays() -> None:
    from scripts.app import _build_master_schedule_grid_html

    matrix = pd.DataFrame(
        [
            {
                "Employee": "Vacant MLT D/E - Line 01",
                "employee_id": "vacant-01",
                date(2026, 6, 1).isoformat(): "D",
                date(2026, 6, 8).isoformat(): "E",
            }
        ]
    )
    view_dates = [date(2026, 6, 1), date(2026, 6, 8)]

    with patch("scripts.app.st") as mock_st:
        mock_st.session_state = {}
        html = _build_master_schedule_grid_html(
            matrix,
            view_dates,
            period_id="period-test",
            edit_mode=True,
        )

    assert html.count("lab-week-start") >= 2
