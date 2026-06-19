"""Save must not reload an empty DB over a filled draft after a failed first save."""

from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock, patch

import pandas as pd


def test_standard_manual_save_blocks_empty_draft_when_db_empty() -> None:
    from scripts.app import _schedule_draft_key, _standard_manual_save

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
    state = {_schedule_draft_key(period_id): draft}
    period = MagicMock(
        id=period_id,
        period_start=day,
        period_end_inclusive=day,
    )

    with patch("scripts.app.st") as mock_st:
        mock_st.session_state = state
        mock_st.toast = MagicMock()
        with patch("scripts.app._fetch_assignments", return_value=[]):
            ok, message = _standard_manual_save(
                MagicMock(),
                tenant_id="tenant-1",
                period=period,
                rules=MagicMock(),
                employees=[{"id": "line-1", "full_name": "Line 01"}],
                templates={
                    "shift-morning": {"id": "shift-morning", "code": "MORNING", "short": "D"},
                },
                employee_target_hours={"line-1": 320.0},
                availability_blocked={},
            )

    assert ok is False
    assert "Nothing to save yet" in message


def test_prepare_draft_for_save_merges_matrix_cache() -> None:
    from scripts.app import _prepare_draft_for_save, _schedule_draft_key, _schedule_matrix_key

    period_id = "period-1"
    day = date(2026, 6, 1)
    day_key = day.isoformat()
    draft = pd.DataFrame([{"employee_id": "line-1", "Employee": "Line 01", day_key: "—"}])
    cached = pd.DataFrame([{"employee_id": "line-1", "Employee": "Line 01", day_key: "D"}])
    state = {
        _schedule_draft_key(period_id): draft,
        _schedule_matrix_key(period_id): cached,
    }

    with patch("scripts.app.st") as mock_st:
        mock_st.session_state = state
        merged, draft_shifts, cache_shifts = _prepare_draft_for_save(
            period_id,
            dates=[day],
            employees=[{"id": "line-1", "full_name": "Line 01"}],
            templates={
                "shift-morning": {"id": "shift-morning", "code": "MORNING", "short": "D"},
            },
        )

    assert merged.at[0, day_key] == "D"
    assert draft_shifts == 1
    assert cache_shifts == 1
