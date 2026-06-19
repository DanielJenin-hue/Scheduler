import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from app import (  # noqa: E402
    _grid_component_cell_changes,
    _grid_component_lock_toggles,
    _grid_component_messages,
)


def test_grid_component_lock_toggles_parses_week_scope() -> None:
    payload = {
        "lock_toggles": [
            {
                "employee_id": "emp-1",
                "date": "2026-06-20",
                "locked": True,
                "scope": "week",
            },
        ]
    }
    toggles = _grid_component_lock_toggles(payload)
    assert toggles[0]["scope"] == "week"


def test_grid_component_messages_splits_changes_and_locks() -> None:
    payload = {
        "changes": [{"employee_id": "emp-1", "date": "2026-06-20", "token": "N"}],
        "lock_toggles": [{"employee_id": "emp-2", "date": "2026-06-21", "locked": True}],
    }
    messages = _grid_component_messages(payload)
    assert len(messages["changes"]) == 1
    assert messages["changes"][0]["token"] == "N"
    assert len(messages["lock_toggles"]) == 1
    assert _grid_component_cell_changes(payload)[0]["token"] == "N"
