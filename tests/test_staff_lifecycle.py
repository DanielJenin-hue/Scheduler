from datetime import date

import pytest
import sqlite3

from lab_scheduler.staff.lifecycle import (
    ASSIGNMENT_STATUS_ASSIGNED,
    ASSIGNMENT_STATUS_UNASSIGNED,
    CONTRACT_LINE_CHANGE_NOTE,
    STAFF_DEACTIVATION_NOTE,
    StaffLifecycleError,
    create_vacant_line,
    deactivate_employee,
    ensure_staff_lifecycle_schema,
    update_employee_roster_line,
)


def _memory_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(
        """
        CREATE TABLE tenants (id TEXT PRIMARY KEY);
        INSERT INTO tenants VALUES ('tenant-a');

        CREATE TABLE employees (
          id TEXT PRIMARY KEY,
          tenant_id TEXT NOT NULL,
          employee_code TEXT,
          first_name TEXT NOT NULL,
          last_name TEXT NOT NULL,
          hire_date TEXT NOT NULL,
          fte REAL NOT NULL,
          base_hourly_rate REAL NOT NULL DEFAULT 40.0,
          seniority_hours REAL NOT NULL DEFAULT 0.0,
          contract_line_type TEXT,
          is_active INTEGER NOT NULL DEFAULT 1,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );

        CREATE TABLE shift_assignments (
          id TEXT PRIMARY KEY,
          tenant_id TEXT NOT NULL,
          schedule_period_id TEXT NOT NULL,
          employee_id TEXT NOT NULL,
          shift_template_id TEXT NOT NULL,
          assignment_date TEXT NOT NULL,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          assignment_status TEXT NOT NULL DEFAULT 'assigned',
          system_note TEXT,
          vacated_from_employee_id TEXT,
          UNIQUE (tenant_id, employee_id, assignment_date)
        );

        CREATE TABLE shift_templates (
          id TEXT PRIMARY KEY,
          tenant_id TEXT NOT NULL,
          code TEXT NOT NULL
        );
        INSERT INTO shift_templates VALUES
          ('shift-morning', 'tenant-a', 'MORNING'),
          ('shift-evening', 'tenant-a', 'EVENING'),
          ('shift-night', 'tenant-a', 'NIGHT');
        """
    )
    ensure_staff_lifecycle_schema(conn)
    conn.execute(
        """
        INSERT INTO employees (
          id, tenant_id, employee_code, first_name, last_name, hire_date, fte,
          base_hourly_rate, contract_line_type, is_active, created_at, updated_at
        ) VALUES (
          'emp-a1', 'tenant-a', 'A001', 'Avery', 'Miller', '2024-01-01', 1.0,
          40.0, 'D/N', 1, '2024-01-01T00:00:00Z', '2024-01-01T00:00:00Z'
        )
        """
    )
    conn.executemany(
        """
        INSERT INTO shift_assignments (
          id, tenant_id, schedule_period_id, employee_id, shift_template_id,
          assignment_date, created_at, updated_at
        ) VALUES (?, 'tenant-a', 'period-a', 'emp-a1', ?, ?, '2024-01-01T00:00:00Z', '2024-01-01T00:00:00Z')
        """,
        [
            ("asg-past", "shift-morning", "2026-05-20"),
            ("asg-future-1", "shift-morning", "2026-06-10"),
            ("asg-future-2", "shift-evening", "2026-06-12"),
        ],
    )
    conn.commit()
    return conn


def test_deactivate_employee_clears_future_shifts() -> None:
    conn = _memory_db()
    as_of = date(2026, 6, 1)

    result = deactivate_employee(
        conn,
        tenant_id="tenant-a",
        employee_id="emp-a1",
        manager_id="mgr-1",
        as_of_date=as_of,
    )

    assert result.shifts_vacated_count == 2
    assert int(
        conn.execute(
            "SELECT is_active FROM employees WHERE id = 'emp-a1'"
        ).fetchone()[0]
    ) == 0

    past_status = conn.execute(
        "SELECT assignment_status FROM shift_assignments WHERE id = 'asg-past'"
    ).fetchone()[0]
    assert past_status == ASSIGNMENT_STATUS_ASSIGNED

    future_rows = conn.execute(
        """
        SELECT assignment_status, system_note, vacated_from_employee_id
        FROM shift_assignments
        WHERE id IN ('asg-future-1', 'asg-future-2')
        ORDER BY id
        """
    ).fetchall()
    assert future_rows == [
        (ASSIGNMENT_STATUS_UNASSIGNED, STAFF_DEACTIVATION_NOTE, "emp-a1"),
        (ASSIGNMENT_STATUS_UNASSIGNED, STAFF_DEACTIVATION_NOTE, "emp-a1"),
    ]


def test_deactivate_employee_logs_sys_audit_event() -> None:
    conn = _memory_db()

    result = deactivate_employee(
        conn,
        tenant_id="tenant-a",
        employee_id="emp-a1",
        manager_id="mgr-42",
        as_of_date=date(2026, 6, 1),
    )

    row = conn.execute(
        """
        SELECT manager_id, employee_id, shifts_vacated_count, action_type
        FROM sys_audit_log
        WHERE id = ?
        """,
        (result.audit_log_id,),
    ).fetchone()
    assert row == ("mgr-42", "emp-a1", 2, "employee_deactivation")


def test_create_vacant_line_assigns_incrementing_line_numbers() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(
        """
        CREATE TABLE tenants (id TEXT PRIMARY KEY);
        INSERT INTO tenants VALUES ('tenant-a');

        CREATE TABLE employees (
          id TEXT PRIMARY KEY,
          tenant_id TEXT NOT NULL,
          employee_code TEXT,
          first_name TEXT NOT NULL,
          last_name TEXT NOT NULL,
          hire_date TEXT NOT NULL,
          fte REAL NOT NULL,
          base_hourly_rate REAL NOT NULL DEFAULT 40.0,
          seniority_hours REAL NOT NULL DEFAULT 0.0,
          contract_line_type TEXT,
          is_active INTEGER NOT NULL DEFAULT 1,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          UNIQUE (tenant_id, employee_code)
        );

        CREATE TABLE qualifications (
          id TEXT PRIMARY KEY,
          tenant_id TEXT NOT NULL,
          code TEXT NOT NULL
        );
        INSERT INTO qualifications VALUES ('qual-mlt', 'tenant-a', 'MLT');

        CREATE TABLE employee_qualifications (
          tenant_id TEXT NOT NULL,
          employee_id TEXT NOT NULL,
          qualification_id TEXT NOT NULL,
          awarded_on TEXT,
          expires_on TEXT,
          created_at TEXT NOT NULL,
          PRIMARY KEY (tenant_id, employee_id, qualification_id)
        );
        """
    )

    first = create_vacant_line(
        conn,
        tenant_id="tenant-a",
        role="MLT",
        contract_line_type="D/N",
        target_weekly_hours=40.0,
        qualification_id="qual-mlt",
    )
    second = create_vacant_line(
        conn,
        tenant_id="tenant-a",
        role="MLT",
        contract_line_type="D/N",
        target_weekly_hours=40.0,
        qualification_id="qual-mlt",
    )

    assert first.display_name == "Vacant MLT D/N - Line 01"
    assert second.display_name == "Vacant MLT D/N - Line 02"
    assert first.employee_id != second.employee_id

    count = conn.execute(
        "SELECT COUNT(*) FROM employees WHERE tenant_id = 'tenant-a'"
    ).fetchone()[0]
    assert count == 2


def test_create_vacant_line_accepts_seven_tenths_fte_hours() -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE tenants (id TEXT PRIMARY KEY);
        INSERT INTO tenants VALUES ('tenant-a');
        CREATE TABLE employees (
          id TEXT PRIMARY KEY,
          tenant_id TEXT NOT NULL,
          employee_code TEXT,
          first_name TEXT NOT NULL,
          last_name TEXT NOT NULL,
          hire_date TEXT NOT NULL,
          fte REAL NOT NULL,
          base_hourly_rate REAL NOT NULL DEFAULT 40.0,
          seniority_hours REAL NOT NULL DEFAULT 0.0,
          contract_line_type TEXT,
          is_active INTEGER NOT NULL DEFAULT 1,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          UNIQUE (tenant_id, employee_code)
        );
        CREATE TABLE employee_qualifications (
          tenant_id TEXT NOT NULL,
          employee_id TEXT NOT NULL,
          qualification_id TEXT NOT NULL,
          awarded_on TEXT,
          expires_on TEXT,
          created_at TEXT NOT NULL,
          PRIMARY KEY (tenant_id, employee_id, qualification_id)
        );
        """
    )

    result = create_vacant_line(
        conn,
        tenant_id="tenant-a",
        role="MLT",
        contract_line_type="D/N",
        target_weekly_hours=28.0,
        qualification_id="qual-mlt",
    )
    assert result.line_number == 1
    fte = conn.execute("SELECT fte FROM employees WHERE tenant_id = 'tenant-a'").fetchone()[0]
    assert fte == 0.7


def test_update_employee_roster_line_rejects_invalid_weekly_hours() -> None:
    conn = _memory_db()
    with pytest.raises(StaffLifecycleError, match="Target weekly hours"):
        update_employee_roster_line(
            conn,
            tenant_id="tenant-a",
            employee_id="emp-a1",
            manager_id="mgr-1",
            seniority_hours=0.0,
            contract_line_type="D/N",
            target_weekly_hours=37.0,
        )


def test_update_employee_roster_line_vacates_night_shift_on_de_contract() -> None:
    conn = _memory_db()
    conn.execute(
        """
        INSERT INTO shift_assignments (
          id, tenant_id, schedule_period_id, employee_id, shift_template_id,
          assignment_date, created_at, updated_at
        ) VALUES (
          'asg-night', 'tenant-a', 'period-a', 'emp-a1', 'shift-night', '2026-06-15',
          '2024-01-01T00:00:00Z', '2024-01-01T00:00:00Z'
        )
        """
    )
    conn.commit()

    result = update_employee_roster_line(
        conn,
        tenant_id="tenant-a",
        employee_id="emp-a1",
        manager_id="mgr-1",
        seniority_hours=0.0,
        contract_line_type="D/E",
        target_weekly_hours=40.0,
        as_of_date=date(2026, 6, 1),
    )

    assert result.shifts_vacated_count >= 1
    night_row = conn.execute(
        """
        SELECT assignment_status, system_note
        FROM shift_assignments
        WHERE id = 'asg-night'
        """
    ).fetchone()
    assert night_row == (ASSIGNMENT_STATUS_UNASSIGNED, CONTRACT_LINE_CHANGE_NOTE)
