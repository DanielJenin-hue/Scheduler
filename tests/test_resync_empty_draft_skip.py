from __future__ import annotations

from datetime import date
from unittest.mock import patch

import pandas as pd


def test_resync_empty_draft_skips_when_pending_clear_mutations() -> None:
    from scripts.app import _resync_empty_draft_from_assignments

    day_key = date(2026, 6, 1).isoformat()
    empty_draft = pd.DataFrame([{"employee_id": "line-1", "Employee": "Line 01", day_key: "—"}])
    filled_baseline = pd.DataFrame([{"employee_id": "line-1", "Employee": "Line 01", day_key: "D"}])
    employees = [{"id": "line-1", "full_name": "Line 01"}]
    templates: dict = {}
    assignments = [{"employee_id": "line-1", "assignment_date": day_key, "shift_code": "D"}]
    session: dict = {}

    with patch("scripts.app.st") as mock_st:
        mock_st.session_state = session
        with patch("scripts.app._load_pending_mutations", return_value=[{"employee_id": "line-1"}]):
            result = _resync_empty_draft_from_assignments(
                period_id="period-1",
                draft_key="draft_key",
                baseline_key="baseline_key",
                draft=empty_draft,
                baseline_from_db=filled_baseline,
                dates=[date(2026, 6, 1)],
                employees=employees,
                templates=templates,
                assignments=assignments,
            )

    assert result.at[0, day_key] == "—"
    assert "draft_key" not in session
