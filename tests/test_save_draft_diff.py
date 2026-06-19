"""Save publishes the session draft, not a stale pending-mutation queue."""

from __future__ import annotations

from datetime import date

import pandas as pd


def test_collect_schedule_frame_db_diffs_uses_draft_not_pending() -> None:
    from scripts.app import _collect_schedule_frame_db_diffs

    day = date(2026, 6, 1)
    day_key = day.isoformat()
    employees = [{"id": "line-1", "full_name": "Vacant MLT D/E - Line 01"}]
    templates = {
        "shift-morning": {"id": "shift-morning", "code": "MORNING", "short": "D"},
        "shift-evening": {"id": "shift-evening", "code": "EVENING", "short": "E"},
    }
    draft = pd.DataFrame(
        [
            {
                "employee_id": "line-1",
                "Employee": "Vacant MLT D/E - Line 01",
                day_key: "E",
            }
        ]
    )
    assignments = [
        {
            "employee_id": "line-1",
            "assignment_date": day,
            "shift_template_id": "shift-morning",
        }
    ]

    diffs = _collect_schedule_frame_db_diffs(
        draft,
        employees=employees,
        dates=[day],
        templates=templates,
        assignments=assignments,
    )

    assert diffs == [("line-1", day, "D", "E")]


def test_collect_schedule_frame_db_diffs_empty_when_draft_matches_db() -> None:
    from scripts.app import _collect_schedule_frame_db_diffs

    day = date(2026, 6, 1)
    day_key = day.isoformat()
    employees = [{"id": "line-1", "full_name": "Vacant MLT D/E - Line 01"}]
    templates = {
        "shift-morning": {"id": "shift-morning", "code": "MORNING", "short": "D"},
    }
    draft = pd.DataFrame(
        [
            {
                "employee_id": "line-1",
                "Employee": "Vacant MLT D/E - Line 01",
                day_key: "D",
            }
        ]
    )
    assignments = [
        {
            "employee_id": "line-1",
            "assignment_date": day,
            "shift_template_id": "shift-morning",
        }
    ]

    assert (
        _collect_schedule_frame_db_diffs(
            draft,
            employees=employees,
            dates=[day],
            templates=templates,
            assignments=assignments,
        )
        == []
    )


def test_schedule_shift_cells_equal_ignores_employee_label_drift() -> None:
    from scripts.app import _schedule_shift_cells_equal

    day = date(2026, 6, 1)
    day_key = day.isoformat()
    employees = [{"id": "line-1", "full_name": "Vacant MLT D/E - Line 01"}]
    left = pd.DataFrame(
        [
            {
                "employee_id": "line-1",
                "Employee": "Vacant MLT D/E - Line 01",
                day_key: "D",
            }
        ]
    )
    right = pd.DataFrame(
        [
            {
                "employee_id": "line-1",
                "Employee": "Vacant MLT D/E - Line 01 (320h)",
                day_key: "D",
            }
        ]
    )

    assert _schedule_shift_cells_equal(left, right, employees=employees, dates=[day]) is True
