from __future__ import annotations

from datetime import date
from unittest.mock import patch

import pandas as pd


def test_discard_stale_pending_when_draft_matches_db() -> None:
    from scripts.app import _discard_stale_pending_when_draft_matches_db

    day_key = date(2026, 6, 1).isoformat()
    frame = pd.DataFrame([{"employee_id": "line-1", "Employee": "Line 01", day_key: "D"}])
    session: dict = {}

    with patch("scripts.app.st") as mock_st:
        mock_st.session_state = session
        with patch("scripts.app._load_pending_mutations", return_value=[{"employee_id": "line-1"}]):
            with patch("scripts.app._save_pending_mutations") as save_pending:
                with patch("scripts.app._clear_accumulated_grid_changes") as clear_accum:
                    with patch("scripts.app._clear_grid_component_echo"):
                        with patch("scripts.app._clear_grid_session_storage_bridge"):
                            _discard_stale_pending_when_draft_matches_db(
                                period_id="period-1",
                                draft=frame,
                                baseline_from_db=frame.copy(),
                                dates=[date(2026, 6, 1)],
                            )

    save_pending.assert_called_once_with("period-1", [])
    clear_accum.assert_called_once_with("period-1")


def test_discard_stale_pending_keeps_real_edits() -> None:
    from scripts.app import _discard_stale_pending_when_draft_matches_db

    day_key = date(2026, 6, 1).isoformat()
    draft = pd.DataFrame([{"employee_id": "line-1", "Employee": "Line 01", day_key: "E"}])
    baseline = pd.DataFrame([{"employee_id": "line-1", "Employee": "Line 01", day_key: "D"}])

    with patch("scripts.app._load_pending_mutations", return_value=[{"employee_id": "line-1"}]):
        with patch("scripts.app._save_pending_mutations") as save_pending:
            _discard_stale_pending_when_draft_matches_db(
                period_id="period-1",
                draft=draft,
                baseline_from_db=baseline,
                dates=[date(2026, 6, 1)],
            )

    save_pending.assert_not_called()
