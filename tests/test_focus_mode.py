"""Focus view builder and master-grid integration tests."""

from __future__ import annotations

from datetime import date
from unittest.mock import patch

import pandas as pd

from lab_scheduler.ui.schedule_focus.builder import (
    focus_mode_grid_stylesheet,
    lines_from_schedule_frame,
)


def test_lines_from_schedule_frame_extracts_cells() -> None:
    frame = pd.DataFrame(
        [
            {
                "employee_id": "line-1",
                "Employee": "Vacant MLT D/E - Line 01",
                "2026-06-01": "D",
                "2026-06-02": "E",
            }
        ]
    )
    employees = [{"id": "line-1", "full_name": "Vacant MLT D/E - Line 01"}]
    dates = [date(2026, 6, 1), date(2026, 6, 2)]
    lines = lines_from_schedule_frame(frame, employees=employees, dates=dates)
    assert len(lines) == 1
    assert lines[0]["cells"] == ["D", "E"]


def test_focus_controls_include_clear_schedule_in_normal_mode() -> None:
    import inspect

    from scripts.app import _render_focus_controls

    source = inspect.getsource(_render_focus_controls)
    assert "Clear schedule" in source
    assert "clear_schedule_" in source
    assert "Exit fullscreen" not in source
    assert "_render_focus_exit_control" not in source
    assert "Fullscreen" in source


def test_manager_sidebar_save_panel_exposes_save_and_exit() -> None:
    import inspect

    from scripts.app import _render_manager_sidebar_save_panel

    source = inspect.getsource(_render_manager_sidebar_save_panel)
    assert "Save" in source
    assert "handle_save_button_click" in source
    assert "Exit fullscreen" in source
    assert "Publish Changes" not in source


def test_focus_grid_sizer_targets_st_iframe() -> None:
    from scripts.app import _focus_grid_sizer_javascript

    js = _focus_grid_sizer_javascript()
    assert "focusGridFrame" in js
    assert "stIFrame" in js
    assert "lab-focus-grid-anchor" in js
    assert "focusSidebarOffset" in js
    assert "collapseFocusChrome" in js
    assert "viewportSize" in js
    assert "visualViewport" in js


def test_focus_fit_stylesheet_hides_select_chrome() -> None:
    css = focus_mode_grid_stylesheet()
    assert "::-ms-expand" in css
    assert "-moz-appearance: none" in css


def test_focus_fit_stylesheet_targets_zero_scroll() -> None:
    css = focus_mode_grid_stylesheet()
    assert "lab-schedule-wrap--focus-fit" in css
    assert "lab-focus-scaler" in css
    assert "overflow: hidden" in css
    assert "lab-emp-cell-compact" in css
    assert "lab-emp-meta-row" in css
    assert "tfoot tr.tally-row" in css
    assert "lab-epic-fs-bar" not in css
    assert "lab-exit-schedule-fab" not in css


def _sample_matrix() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "Employee": "Vacant MLT D/E - Line 01",
                "employee_id": "line-1",
                date(2026, 6, 1).isoformat(): "D",
            }
        ]
    )


def test_focus_fit_grid_uses_master_grid_html() -> None:
    from scripts.app import _build_master_schedule_grid_html

    view_dates = [date(2026, 6, 1)]
    with patch("scripts.app.st") as mock_st:
        mock_st.session_state = {}
        html = _build_master_schedule_grid_html(
            _sample_matrix(),
            view_dates,
            period_id="period-1",
            edit_mode=True,
            focus_fit=True,
        )

    assert "lab-schedule-wrap--focus-fit" in html
    assert "lab-focus-scaler" in html
    assert "lab-focus-scaler-inner" in html
    assert "<div class='lab-focus-hint'>" not in html
    assert "scaleX" in html or "scale(" in html
    assert "fitFocusGridToViewport" in html
    assert "initFullscreenControls" in html
    assert "lab-fs-stretch" in html
    assert "fullscreenViewportBox" in html
    assert "lab-fs-viewport" in html
    assert "window.fitFocusGridToViewport" in html
    assert "tally-row" in html
    assert "recomputeTallies" in html
    assert "lab-edit-mode" in html
    assert "lab-emp-cell-compact" in html
    assert "lab-epic-fs-bar" not in html
    assert "lab-toggle-browser-fs" not in html
    assert "lab-exit-schedule-fab" not in html
    assert "exit_schedule_view" not in html
    assert "requestFullscreen" not in html
    assert "epic_mode_sync" not in html
    assert "lab-night-anchor" not in html


def test_build_cleared_schedule_draft_blanks_worked_shifts() -> None:
    from scripts.app import _build_cleared_schedule_draft

    day_key = date(2026, 6, 1).isoformat()
    frame = pd.DataFrame(
        [
            {
                "employee_id": "line-1",
                "Employee": "Vacant MLT D/E - Line 01",
                day_key: "D",
            }
        ]
    )
    employees = [{"id": "line-1", "full_name": "Vacant MLT D/E - Line 01"}]
    cleared, count = _build_cleared_schedule_draft(
        frame,
        employees=employees,
        dates=[date(2026, 6, 1)],
        locked_cells=set(),
    )
    assert count == 1
    assert cleared.at[0, day_key] == "—"


def test_split_schedule_matrix_by_qual_pool() -> None:
    from scripts.app import _split_schedule_matrix_by_qual_pool

    frame = pd.DataFrame(
        [
            {
                "employee_id": "mlt-1",
                "Employee": "Vacant MLT D/E - Line 01",
                "2026-06-01": "D",
            },
            {
                "employee_id": "mla-1",
                "Employee": "Vacant MLA D/E - Line 02",
                "2026-06-01": "E",
            },
        ]
    )
    employees = [
        {"id": "mlt-1", "full_name": "Vacant MLT D/E - Line 01"},
        {"id": "mla-1", "full_name": "Vacant MLA D/E - Line 02"},
    ]
    emp_quals = {"mlt-1": {"qual-mlt"}, "mla-1": {"qual-mla"}}
    qual_id_to_code = {"qual-mlt": "MLT", "qual-mla": "MLA"}

    mlt_frame, mla_frame = _split_schedule_matrix_by_qual_pool(
        frame,
        employees,
        emp_quals=emp_quals,
        qual_id_to_code=qual_id_to_code,
    )

    assert list(mlt_frame["employee_id"]) == ["mlt-1"]
    assert list(mla_frame["employee_id"]) == ["mla-1"]


def test_focus_fit_tally_matrix_uses_full_roster_counts() -> None:
    from scripts.app import _build_master_schedule_grid_html

    view_dates = [date(2026, 6, 1)]
    mlt_only = pd.DataFrame(
        [
            {
                "Employee": "Vacant MLT D/E - Line 01",
                "employee_id": "mlt-1",
                date(2026, 6, 1).isoformat(): "D",
            }
        ]
    )
    full = pd.DataFrame(
        [
            {
                "Employee": "Vacant MLT D/E - Line 01",
                "employee_id": "mlt-1",
                date(2026, 6, 1).isoformat(): "D",
            },
            {
                "Employee": "Vacant MLA D/E - Line 02",
                "employee_id": "mla-1",
                date(2026, 6, 1).isoformat(): "E",
            },
        ]
    )
    with patch("scripts.app.st") as mock_st:
        mock_st.session_state = {}
        html = _build_master_schedule_grid_html(
            mlt_only,
            view_dates,
            period_id="period-1",
            edit_mode=True,
            focus_fit=True,
            tally_matrix=full,
        )

    assert "data-tally-band='E'" in html
    assert ">1/2<" in html
    with patch("scripts.app.st") as mock_st:
        mock_st.session_state = {}
        html_mlt_only_tally = _build_master_schedule_grid_html(
            mlt_only,
            view_dates,
            period_id="period-1",
            edit_mode=True,
            focus_fit=True,
        )
    assert ">0/2<" in html_mlt_only_tally
