"""Grid echo persistence and pending-mutation merge before save."""

from __future__ import annotations

from datetime import date

import pandas as pd


def test_apply_pending_mutations_to_draft() -> None:
    from scripts.app import _apply_pending_mutations_to_draft
    from lab_scheduler.policy.policy_engine import CellMutation

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
    from unittest.mock import patch

    mutations = [
        CellMutation(
            employee_id="line-1",
            assignment_date=day,
            previous_token="",
            new_token="D",
        )
    ]
    with patch("scripts.app._load_pending_mutations", return_value=mutations):
        merged = _apply_pending_mutations_to_draft(
            draft,
            period_id="period-1",
            dates=[day],
        )
    assert merged.at[0, day_key] == "D"


def test_merge_grid_cell_changes_deduplicates() -> None:
    from scripts.app import _merge_grid_cell_changes

    first = [{"employee_id": "line-1", "date": "2026-06-01", "token": "D"}]
    second = [{"employee_id": "line-1", "date": "2026-06-01", "token": "E"}]
    merged = _merge_grid_cell_changes(first, second)
    assert len(merged) == 1
    assert merged[0]["token"] == "E"


def test_publish_would_wipe_blocks_fully_empty_draft() -> None:
    from scripts.app import _publish_would_wipe_saved_schedule

    day = date(2026, 6, 1)
    employees = [{"id": "line-1", "full_name": "Line 01"}]
    templates = {
        "shift-morning": {"id": "shift-morning", "code": "MORNING", "short": "D"},
    }
    draft = pd.DataFrame(
        [
            {
                "employee_id": "line-1",
                "Employee": "Line 01",
                day.isoformat(): "—",
            }
        ]
    )
    assignments = [
        {
            "employee_id": "line-1",
            "assignment_date": day,
            "shift_template_id": "shift-morning",
        }
        for _ in range(12)
    ]
    diffs = [("line-1", day, "D", "") for _ in range(12)]

    assert _publish_would_wipe_saved_schedule(
        diffs=diffs,
        assignments=assignments,
        draft_frame=draft,
        employees=employees,
        dates=[day],
        templates=templates,
        min_saved_shifts=8,
    ) is True
