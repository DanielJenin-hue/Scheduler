import sqlite3
from datetime import date

from lab_scheduler.scheduling.shift_cell_locks import (
    apply_shift_cell_lock_toggles,
    ensure_shift_cell_locks_schema,
    expand_lock_toggle,
    fetch_shift_cell_locks,
    is_shift_cell_locked,
    set_shift_cell_lock,
    week_dates_for_lock,
)


def _memory_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(
        """
        CREATE TABLE tenants (id TEXT PRIMARY KEY);
        INSERT INTO tenants VALUES ('tenant-a');
        CREATE TABLE schedule_periods (
          tenant_id TEXT NOT NULL,
          id TEXT NOT NULL,
          PRIMARY KEY (tenant_id, id)
        );
        INSERT INTO schedule_periods VALUES ('tenant-a', 'period-1');
        CREATE TABLE employees (
          tenant_id TEXT NOT NULL,
          id TEXT NOT NULL,
          PRIMARY KEY (tenant_id, id)
        );
        INSERT INTO employees VALUES ('tenant-a', 'emp-1');
        CREATE TABLE schedule_audit_logs (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          tenant_id TEXT NOT NULL,
          schedule_period_id TEXT,
          recorded_at_utc TEXT NOT NULL,
          actor TEXT NOT NULL DEFAULT 'manager',
          employee_id TEXT,
          shift_date TEXT,
          previous_shift_code TEXT,
          new_shift_code TEXT,
          change_type TEXT NOT NULL,
          seniority_bypass_flag INTEGER NOT NULL DEFAULT 0,
          seniority_bypass_justification TEXT
        );
        """
    )
    return conn


def test_ensure_shift_cell_locks_schema_creates_table() -> None:
    conn = _memory_db()
    ensure_shift_cell_locks_schema(conn)
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='shift_cell_locks'"
    ).fetchone()
    assert row is not None


def test_set_fetch_and_clear_lock() -> None:
    conn = _memory_db()
    assignment_date = date(2026, 6, 20)
    set_shift_cell_lock(
        conn,
        tenant_id="tenant-a",
        schedule_period_id="period-1",
        employee_id="emp-1",
        assignment_date=assignment_date,
        locked=True,
        actor="manager",
    )
    locks = fetch_shift_cell_locks(
        conn,
        tenant_id="tenant-a",
        schedule_period_id="period-1",
    )
    assert locks == {("emp-1", assignment_date)}
    assert is_shift_cell_locked(locks, employee_id="emp-1", assignment_date=assignment_date)

    set_shift_cell_lock(
        conn,
        tenant_id="tenant-a",
        schedule_period_id="period-1",
        employee_id="emp-1",
        assignment_date=assignment_date,
        locked=False,
        actor="manager",
    )
    assert fetch_shift_cell_locks(
        conn,
        tenant_id="tenant-a",
        schedule_period_id="period-1",
    ) == set()


def test_week_dates_for_lock_respects_period_bounds() -> None:
    period_start = date(2026, 6, 1)
    period_end = date(2026, 6, 30)
    # Wednesday Jun 3 -> Mon Jun 1 through Sun Jun 7
    week = week_dates_for_lock(date(2026, 6, 3), period_start=period_start, period_end=period_end)
    assert week == [
        date(2026, 6, 1),
        date(2026, 6, 2),
        date(2026, 6, 3),
        date(2026, 6, 4),
        date(2026, 6, 5),
        date(2026, 6, 6),
        date(2026, 6, 7),
    ]


def test_expand_lock_toggle_week_scope() -> None:
    expanded = expand_lock_toggle(
        {"employee_id": "emp-1", "date": "2026-06-03", "locked": True, "scope": "week"},
        period_start=date(2026, 6, 1),
        period_end=date(2026, 6, 30),
    )
    assert len(expanded) == 7
    assert expanded[0]["date"] == "2026-06-01"
    assert all(item["employee_id"] == "emp-1" for item in expanded)


def test_apply_shift_cell_lock_toggles_writes_audit() -> None:
    conn = _memory_db()
    changed = apply_shift_cell_lock_toggles(
        conn,
        tenant_id="tenant-a",
        schedule_period_id="period-1",
        toggles=[
            {
                "employee_id": "emp-1",
                "date": "2026-06-03",
                "locked": True,
                "scope": "week",
            }
        ],
        actor="manager",
        period_start=date(2026, 6, 1),
        period_end=date(2026, 6, 30),
    )
    assert changed == 7
    locks = fetch_shift_cell_locks(
        conn,
        tenant_id="tenant-a",
        schedule_period_id="period-1",
    )
    assert len(locks) == 7
    audit = conn.execute(
        "SELECT COUNT(*) FROM schedule_audit_logs"
    ).fetchone()
    assert audit[0] == 7
