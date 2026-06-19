from __future__ import annotations

from scripts.app import _bridge_changes_for_draft_apply


def test_bridge_sync_ignored_always() -> None:
    payload = {
        "source": "sync",
        "changes": [{"employee_id": "line-1", "date": "2026-06-01", "token": "D"}],
    }
    assert _bridge_changes_for_draft_apply(payload, save_pending=False) == []
    assert _bridge_changes_for_draft_apply(payload, save_pending=True) == []


def test_bridge_save_drain_allowed() -> None:
    payload = {
        "source": "save-drain",
        "changes": [{"employee_id": "line-1", "date": "2026-06-01", "token": "D"}],
    }
    assert _bridge_changes_for_draft_apply(payload, save_pending=True) == [
        {"employee_id": "line-1", "date": "2026-06-01", "token": "D"}
    ]


def test_bridge_message_changes_apply_normally() -> None:
    payload = {
        "source": "message",
        "changes": [{"employee_id": "line-1", "date": "2026-06-01", "token": "E"}],
    }
    assert _bridge_changes_for_draft_apply(payload, save_pending=False) == [
        {"employee_id": "line-1", "date": "2026-06-01", "token": "E"}
    ]
