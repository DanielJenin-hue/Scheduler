import sqlite3
from datetime import date
from pathlib import Path

from lab_scheduler.audit.schedule_log import (
    ensure_audit_schema,
    fetch_audit_logs,
    log_auto_generation,
    log_manual_edit,
    log_seniority_bypass,
)


def _memory_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(
        """
        CREATE TABLE tenants (id TEXT PRIMARY KEY);
        INSERT INTO tenants VALUES ('tenant-a');
        CREATE TABLE employees (
          id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, first_name TEXT, last_name TEXT
        );
        INSERT INTO employees VALUES ('emp-1', 'tenant-a', 'Avery', 'Miller');
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
          change_type TEXT NOT NULL
        );
        """
    )
    return conn


def test_log_manual_edit_and_fetch() -> None:
    conn = _memory_db()
    log_id = log_manual_edit(
        conn,
        tenant_id="tenant-a",
        schedule_period_id="period-1",
        employee_id="emp-1",
        shift_date=date(2026, 6, 1),
        previous_shift_code="",
        new_shift_code="M",
    )
    assert log_id == 1

    rows = fetch_audit_logs(conn, tenant_id="tenant-a", schedule_period_id="period-1")
    assert len(rows) == 1
    assert rows[0].change_type == "manual_edit"
    assert rows[0].new_shift_code == "M"
    assert rows[0].employee_name == "Avery Miller"


def test_log_auto_generation_macro_event() -> None:
    conn = _memory_db()
    log_auto_generation(
        conn,
        tenant_id="tenant-a",
        schedule_period_id="period-1",
        assignments_written=42,
        slots_filled=40,
        slots_total=112,
    )
    rows = fetch_audit_logs(conn, tenant_id="tenant-a", schedule_period_id="period-1")
    assert rows[0].change_type == "auto_generation"
    assert rows[0].employee_id is None
    assert "GENERATED:42" in (rows[0].new_shift_code or "")


def test_log_seniority_bypass_migrates_legacy_change_type_check() -> None:
    conn = _memory_db()
    ensure_audit_schema(conn)
    log_id = log_seniority_bypass(
        conn,
        tenant_id="tenant-a",
        schedule_period_id="period-1",
        employee_id="emp-1",
        shift_date=date(2026, 6, 2),
        shift_code="M",
        justification="Most senior unavailable: 11h rest violation",
    )
    assert log_id == 1
    rows = fetch_audit_logs(conn, tenant_id="tenant-a", schedule_period_id="period-1")
    assert rows[0].change_type == "seniority_bypass"
    assert rows[0].seniority_bypass_flag is True
    assert rows[0].seniority_bypass_justification is not None


def test_log_constraint_violation_migrates_change_type_check() -> None:
    from lab_scheduler.audit.schedule_log import log_constraint_violation

    conn = _memory_db()
    ensure_audit_schema(conn)
    log_id = log_constraint_violation(
        conn,
        tenant_id="tenant-a",
        schedule_period_id="period-1",
        shift_date=date(2026, 6, 3),
        shift_code="MORNING",
        violation_code="LABOR_RULE",
        message="would violate 11h rest before Morning after Evening/Night (8.0h gap)",
        actor="tester [auto-pilot]",
    )
    assert log_id == 1
    rows = fetch_audit_logs(conn, tenant_id="tenant-a", schedule_period_id="period-1")
    assert rows[0].change_type == "constraint_violation"
    assert rows[0].previous_shift_code == "UNASSIGNED"
    assert "11h rest" in (rows[0].seniority_bypass_justification or "")

