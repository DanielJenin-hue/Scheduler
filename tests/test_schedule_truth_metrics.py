"""Draft-based schedule truth metrics for ribbon and posting readiness."""

from __future__ import annotations

from datetime import date

import pandas as pd

from lab_scheduler.ui.schedule_truth import (
    assignments_from_truth_frame,
    schedule_truth_frame,
)


def test_schedule_truth_frame_prefers_draft() -> None:
    day = date(2026, 6, 1)
    day_key = day.isoformat()
    draft = pd.DataFrame([{"employee_id": "line-1", "Employee": "Line 1", day_key: "E"}])
    fallback = pd.DataFrame([{"employee_id": "line-1", "Employee": "Line 1", day_key: "D"}])
    state = {"schedule_draft_period-1": draft}

    truth = schedule_truth_frame(
        state,
        period_id="period-1",
        dates=[day],
        fallback_frame=fallback,
    )
    assert truth.at[0, day_key] == "E"


def test_schedule_truth_frame_falls_back_to_db_frame() -> None:
    day = date(2026, 6, 1)
    day_key = day.isoformat()
    fallback = pd.DataFrame([{"employee_id": "line-1", "Employee": "Line 1", day_key: "N"}])
    truth = schedule_truth_frame(
        {},
        period_id="period-1",
        dates=[day],
        fallback_frame=fallback,
    )
    assert truth.at[0, day_key] == "N"


def test_posting_readiness_uses_draft_assignments() -> None:
    from scripts.app import _evaluate_schedule_posting_readiness

    day = date(2026, 6, 1)
    day_key = day.isoformat()
    employees = [{"id": "line-1", "full_name": "Vacant MLT D/E - Line 01"}]
    templates = {
        "shift-morning": {"id": "shift-morning", "code": "MORNING", "short": "D"},
        "shift-evening": {"id": "shift-evening", "code": "EVENING", "short": "E"},
    }
    draft = pd.DataFrame(
        [{"employee_id": "line-1", "Employee": "Vacant MLT D/E - Line 01", day_key: "E"}]
    )
    db_assignments = [
        {
            "employee_id": "line-1",
            "assignment_date": day,
            "shift_template_id": "shift-morning",
        }
    ]
    period = type(
        "Period",
        (),
        {
            "period_start": day,
            "period_end_inclusive": day,
            "week_count": 1,
        },
    )()

    readiness = _evaluate_schedule_posting_readiness(
        assignments=db_assignments,
        employees=employees,
        period=period,
        template_info={},
        hours_delta=0.0,
        pending_mutations=1,
        schedule_frame=draft,
        templates=templates,
        dates=[day],
    )
    assert readiness.pending_mutations == 1
    assert not readiness.is_ready
    assert any("unpublished grid edit" in bullet for bullet in readiness.attention_bullets)

    draft_shifts = assignments_from_truth_frame(
        draft,
        employees=employees,
        dates=[day],
        templates=templates,
    )
    assert draft_shifts[0]["shift_template_id"] == "shift-evening"
