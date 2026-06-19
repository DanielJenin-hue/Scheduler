"""Deferred sidebar save and mass-delete guard."""

from __future__ import annotations

from datetime import date

import pandas as pd


def test_publish_would_wipe_saved_schedule_detects_empty_draft() -> None:
    from scripts.app import _publish_would_wipe_saved_schedule

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
                day_key: "—",
            }
        ]
    )
    assignments = [
        {
            "employee_id": "line-1",
            "assignment_date": day,
            "shift_template_id": "shift-morning",
        }
        for _ in range(3)
    ]
    diffs = [
        ("line-1", day, "D", "")
        for _ in range(3)
    ]

    assert _publish_would_wipe_saved_schedule(
        diffs=diffs,
        assignments=assignments,
        draft_frame=draft,
        employees=employees,
        dates=[day],
        templates=templates,
        min_saved_shifts=8,
    ) is True

    many_days = [date(2026, 6, 1 + index) for index in range(10)]
    empty_draft = pd.DataFrame(
        [
            {
                "employee_id": "line-1",
                "Employee": "Vacant MLT D/E - Line 01",
                **{d.isoformat(): "—" for d in many_days},
            }
        ]
    )
    many_assignments = [
        {
            "employee_id": "line-1",
            "assignment_date": d,
            "shift_template_id": "shift-morning",
        }
        for d in many_days
    ]
    many_diffs = [("line-1", d, "D", "") for d in many_days]

    assert _publish_would_wipe_saved_schedule(
        diffs=many_diffs,
        assignments=many_assignments,
        draft_frame=empty_draft,
        employees=employees,
        dates=many_days,
        templates=templates,
        min_saved_shifts=8,
    ) is True


def test_publish_would_wipe_allows_matching_draft() -> None:
    from scripts.app import _publish_would_wipe_saved_schedule

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

    assert _publish_would_wipe_saved_schedule(
        diffs=[],
        assignments=assignments,
        draft_frame=draft,
        employees=employees,
        dates=[day],
        templates=templates,
    ) is False
