from __future__ import annotations

from datetime import date
from unittest.mock import patch

import pandas as pd


def test_format_grid_employee_cell_shortens_vacant_line_label() -> None:
    from scripts.app import _format_grid_employee_cell

    html = _format_grid_employee_cell("Vacant MLT D/E - Line 01 (320h)")
    assert "Line 01" in html
    assert "MLT D/E" in html
    assert "320h" in html
    assert "lab-emp-primary" in html


def test_view_mode_grid_html_uses_pills_not_selects() -> None:
    from scripts.app import _build_master_schedule_grid_html

    matrix = pd.DataFrame(
        [
            {
                "Employee": "Vacant MLT D/E - Line 01 (320h)",
                "employee_id": "vacant-01",
                date(2026, 6, 1).isoformat(): "D",
                date(2026, 6, 2).isoformat(): "E",
            }
        ]
    )
    view_dates = [date(2026, 6, 1), date(2026, 6, 2)]

    with patch("scripts.app.st") as mock_st:
        mock_st.session_state = {}
        html = _build_master_schedule_grid_html(
            matrix,
            view_dates,
            period_id="period-test",
            edit_mode=False,
        )

    assert "lab-shift-pill" in html
    assert "lab-shift-pill-readonly" in html
    assert "lab-shift-select" not in html
    assert "Mon 6/1" in html


def test_edit_mode_grid_html_marks_pills_editable() -> None:
    from scripts.app import _build_master_schedule_grid_html

    matrix = pd.DataFrame(
        [
            {
                "Employee": "Vacant MLT D/E - Line 01 (320h)",
                "employee_id": "vacant-01",
                date(2026, 6, 1).isoformat(): "D",
            }
        ]
    )
    view_dates = [date(2026, 6, 1)]

    with patch("scripts.app.st") as mock_st:
        mock_st.session_state = {}
        html = _build_master_schedule_grid_html(
            matrix,
            view_dates,
            period_id="period-test",
            edit_mode=True,
        )

    assert "lab-shift-inline-select" in html
    assert "lab-edit-mode" in html
    assert "const editMode = true" in html


def test_grid_html_includes_week_band_headers() -> None:
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
    view_dates = [date(2026, 6, day) for day in range(1, 15)]

    with patch("scripts.app.st") as mock_st:
        mock_st.session_state = {}
        html = _build_master_schedule_grid_html(
            matrix,
            view_dates,
            period_id="period-test",
            edit_mode=True,
        )

    assert "lab-week-band-row" in html
    assert ">W1<" in html
    assert ">W2<" in html
    assert "lab-emp-cell-compact" in html


def test_edit_mode_grid_html_includes_drag_area_fill() -> None:
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
        )

    assert "initDragAreaFill" in html
    assert "lab-drag-fill-palette" in html
    assert "pointerdown" in html
    assert "allowedTokensForSelection" in html
