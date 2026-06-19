from __future__ import annotations

import json
import sqlite3
from datetime import date
from pathlib import Path

from lab_scheduler.scheduling.provisional_state_cleanup import (
    clear_provisional_session_files,
    clear_provisional_stretch_state,
)
from lab_scheduler.scheduling.provisional_compliance import (
    approved_stretch_system_note,
    provisional_stretch_system_note,
)


def _create_shift_assignments(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE shift_assignments (
          id TEXT PRIMARY KEY,
          tenant_id TEXT NOT NULL,
          schedule_period_id TEXT NOT NULL,
          employee_id TEXT NOT NULL,
          shift_template_id TEXT NOT NULL,
          assignment_date TEXT NOT NULL,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          system_note TEXT
        )
        """
    )


def test_clear_provisional_stretch_state_nulls_stretch_notes_only(tmp_path: Path) -> None:
    db_path = tmp_path / "demo.sqlite3"
    conn = sqlite3.connect(db_path)
    _create_shift_assignments(conn)
    conn.executemany(
        """
        INSERT INTO shift_assignments (
          id, tenant_id, schedule_period_id, employee_id, shift_template_id,
          assignment_date, created_at, updated_at, system_note
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                "asg-1",
                "tenant-a",
                "period-1",
                "emp-1",
                "shift-morning",
                "2026-06-01",
                "2026-06-01T00:00:00Z",
                "2026-06-01T00:00:00Z",
                provisional_stretch_system_note(),
            ),
            (
                "asg-2",
                "tenant-a",
                "period-1",
                "emp-2",
                "shift-evening",
                "2026-06-01",
                "2026-06-01T00:00:00Z",
                "2026-06-01T00:00:00Z",
                approved_stretch_system_note(actor="joanne"),
            ),
            (
                "asg-3",
                "tenant-a",
                "period-1",
                "emp-3",
                "shift-night",
                "2026-06-01",
                "2026-06-01T00:00:00Z",
                "2026-06-01T00:00:00Z",
                "FORCED_CLINICAL_OT",
            ),
            (
                "asg-4",
                "tenant-a",
                "period-2",
                "emp-4",
                "shift-night",
                "2026-06-02",
                "2026-06-01T00:00:00Z",
                "2026-06-01T00:00:00Z",
                provisional_stretch_system_note(),
            ),
        ],
    )
    conn.commit()

    result = clear_provisional_stretch_state(
        conn,
        tenant_id="tenant-a",
        schedule_period_id="period-1",
        project_root=tmp_path,
    )
    assert result.db_notes_cleared == 2

    notes = {
        row[0]
        for row in conn.execute(
            """
            SELECT system_note
            FROM shift_assignments
            WHERE schedule_period_id = 'period-1'
            ORDER BY id
            """
        ).fetchall()
    }
    assert notes == {None, None, "FORCED_CLINICAL_OT"}

    other_period_note = conn.execute(
        "SELECT system_note FROM shift_assignments WHERE id = 'asg-4'"
    ).fetchone()[0]
    assert other_period_note.startswith("PROVISIONAL_STRETCH|")


def test_clear_provisional_session_files_removes_cached_json(tmp_path: Path) -> None:
    exports = tmp_path / "exports"
    exports.mkdir()
    sidecar = exports / "provisional_assignments_period-2026-summer.json"
    sidecar.write_text(
        json.dumps(
            [
                {
                    "employee_id": "emp-1",
                    "violation_code": "PROVISIONAL_STRETCH",
                    "message": "stretch pending manager approval",
                    "assignment_date": date(2026, 6, 1).isoformat(),
                }
            ]
        ),
        encoding="utf-8",
    )

    removed = clear_provisional_session_files(
        tmp_path,
        schedule_period_id="period-2026-summer",
    )
    assert removed == [str(sidecar)]
    assert not sidecar.exists()
