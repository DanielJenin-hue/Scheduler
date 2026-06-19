"""Matrix cache merge before save prevents publishing a stale empty draft."""

from __future__ import annotations

from datetime import date
from unittest.mock import patch

import pandas as pd
import streamlit as st


def test_merge_matrix_cache_into_draft_applies_visible_edits() -> None:
    from scripts.app import _merge_matrix_cache_into_draft, _schedule_matrix_key

    period_id = "period-test"
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
    cached = pd.DataFrame(
        [
            {
                "employee_id": "line-1",
                "Employee": "Line 01",
                day_key: "D",
            }
        ]
    )
    session = {_schedule_matrix_key(period_id): cached}

    with patch.object(st, "session_state", session, create=True):
        merged = _merge_matrix_cache_into_draft(period_id, draft)

    assert merged.at[0, day_key] == "D"


def test_merge_matrix_cache_does_not_wipe_worked_shifts_with_empty_cache() -> None:
    from scripts.app import _merge_matrix_cache_into_draft, _schedule_matrix_key

    period_id = "period-test"
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
    cached = pd.DataFrame(
        [
            {
                "employee_id": "line-1",
                "Employee": "Line 01",
                day_key: "—",
            }
        ]
    )
    session = {_schedule_matrix_key(period_id): cached}

    with patch.object(st, "session_state", session, create=True):
        merged = _merge_matrix_cache_into_draft(period_id, draft)

    assert merged.at[0, day_key] == "D"


def test_publish_would_wipe_blocks_partial_mass_delete() -> None:
    from scripts.app import _publish_would_wipe_saved_schedule

    days = [date(2026, 6, 1 + index) for index in range(20)]
    day_keys = {d.isoformat(): "D" for d in days[:4]}
    empty_keys = {d.isoformat(): "—" for d in days[4:]}
    employees = [{"id": "line-1", "full_name": "Line 01"}]
    templates = {
        "shift-morning": {"id": "shift-morning", "code": "MORNING", "short": "D"},
    }
    partial_draft = pd.DataFrame(
        [
            {
                "employee_id": "line-1",
                "Employee": "Line 01",
                **day_keys,
                **empty_keys,
            }
        ]
    )
    assignments = [
        {
            "employee_id": "line-1",
            "assignment_date": d,
            "shift_template_id": "shift-morning",
        }
        for d in days
    ]
    diffs = [("line-1", d, "D", "") for d in days[4:]]

    assert _publish_would_wipe_saved_schedule(
        diffs=diffs,
        assignments=assignments,
        draft_frame=partial_draft,
        employees=employees,
        dates=days,
        templates=templates,
    ) is True


def test_resync_empty_draft_from_assignments_restores_db_baseline() -> None:
    from scripts.app import (
        _resync_empty_draft_from_assignments,
        _schedule_baseline_key,
        _schedule_draft_key,
    )

    period_id = "period-1"
    day = date(2026, 6, 1)
    day_key = day.isoformat()
    empty_draft = pd.DataFrame(
        [{"employee_id": "line-1", "Employee": "Line 01", day_key: "—"}]
    )
    baseline = pd.DataFrame(
        [{"employee_id": "line-1", "Employee": "Line 01", day_key: "D"}]
    )
    assignments = [
        {
            "employee_id": "line-1",
            "assignment_date": day,
            "shift_template_id": "shift-morning",
        }
    ]
    state = {
        _schedule_draft_key(period_id): empty_draft,
        _schedule_baseline_key(period_id): empty_draft,
    }

    with patch.object(st, "session_state", state, create=True):
        merged = _resync_empty_draft_from_assignments(
            period_id=period_id,
            draft_key=_schedule_draft_key(period_id),
            baseline_key=_schedule_baseline_key(period_id),
            draft=empty_draft,
            baseline_from_db=baseline,
            dates=[day],
            employees=[{"id": "line-1", "full_name": "Line 01"}],
            templates={
                "shift-morning": {"id": "shift-morning", "code": "MORNING", "short": "D"},
            },
            assignments=assignments,
        )

    assert merged.at[0, day_key] == "D"
    assert state[_schedule_draft_key(period_id)].at[0, day_key] == "D"
