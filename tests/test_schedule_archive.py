"""Tests for portable schedule archive export/import."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from lab_scheduler.data.schedule_archive import (
    export_period_schedule,
    import_period_schedule,
    load_named_archive,
    save_named_archive,
)


@pytest.fixture
def archive_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "test.sqlite3"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE schedule_periods (
          id TEXT PRIMARY KEY,
          tenant_id TEXT NOT NULL,
          period_start TEXT NOT NULL,
          period_end_inclusive TEXT NOT NULL
        );
        CREATE TABLE employees (
          id TEXT PRIMARY KEY,
          tenant_id TEXT NOT NULL,
          is_active INTEGER DEFAULT 1
        );
        CREATE TABLE shift_assignments (
          id TEXT PRIMARY KEY,
          tenant_id TEXT NOT NULL,
          schedule_period_id TEXT NOT NULL,
          employee_id TEXT NOT NULL,
          shift_template_id TEXT NOT NULL,
          assignment_date TEXT NOT NULL,
          assignment_status TEXT,
          created_at TEXT,
          updated_at TEXT
        );
        INSERT INTO schedule_periods VALUES (
          'period-2026-summer', 'tenant-northstar-lab', '2026-06-01', '2026-07-26'
        );
        INSERT INTO employees VALUES ('portage-mlt-01', 'tenant-northstar-lab', 1);
        """
    )
    conn.commit()
    conn.close()
    return db_path


def test_round_trip_schedule_archive(archive_db: Path, tmp_path: Path) -> None:
    payload = {
        "archive_version": 1,
        "period_id": "period-2026-summer",
        "period_start": "2026-06-01",
        "employees": {
            "portage-mlt-01": ["DDDDDNN", "NNNDDNN", "NNNDDNN", "NNNDDNN", "NNNNN--", "DDDDN--", "NNNNN--", "DDDDN--"],
        },
    }
    conn = sqlite3.connect(archive_db)
    inserted = import_period_schedule(
        conn,
        payload,
        tenant_id="tenant-northstar-lab",
        period_id="period-2026-summer",
    )
    assert inserted > 0

    exported = export_period_schedule(
        conn,
        tenant_id="tenant-northstar-lab",
        period_id="period-2026-summer",
        name="round-trip",
    )
    conn.close()

    assert exported["employees"]["portage-mlt-01"][0] == payload["employees"]["portage-mlt-01"][0]

    archive_path = save_named_archive(exported, name="round-trip", saved_dir=tmp_path / "saved")
    loaded = load_named_archive(archive_path)
    assert loaded["name"] == "round-trip"


def test_export_includes_all_off_roster_line(archive_db: Path) -> None:
    conn = sqlite3.connect(archive_db)
    conn.execute(
        "INSERT INTO employees VALUES ('portage-mla-11', 'tenant-northstar-lab', 1)"
    )
    conn.commit()

    exported = export_period_schedule(
        conn,
        tenant_id="tenant-northstar-lab",
        period_id="period-2026-summer",
        name="all-off",
    )
    conn.close()

    assert exported["employees"]["portage-mla-11"] == ["-------"] * 8
