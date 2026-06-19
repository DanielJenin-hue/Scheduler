from datetime import date

import sqlite3

from lab_scheduler.compliance import MANITOBA, ShiftTemplateInfo
from lab_scheduler.compliance.engine import ScheduledShift
from lab_scheduler.scheduling.auto_generate import EmployeeProfile
from lab_scheduler.scheduling.shift_swap import (
    execute_shift_swap,
    list_swap_candidates,
)
from lab_scheduler.staff.lifecycle import ensure_staff_lifecycle_schema


def _templates() -> dict[str, ShiftTemplateInfo]:
    return {
        "shift-morning": ShiftTemplateInfo(
            "shift-morning", "MORNING", "Morning", "07:00", "15:00", 480, False
        ),
        "shift-evening": ShiftTemplateInfo(
            "shift-evening", "EVENING", "Evening", "15:00", "23:00", 480, False
        ),
        "shift-night": ShiftTemplateInfo(
            "shift-night", "NIGHT", "Night", "23:00", "07:00", 480, True
        ),
    }


def _employees() -> list[EmployeeProfile]:
    return [
        EmployeeProfile("emp-a1", "Avery Miller", 1.0, {"qual-mlt"}),
        EmployeeProfile("emp-b1", "Jordan Patel", 0.8, {"qual-mlt"}),
        EmployeeProfile("emp-c1", "Riley Chen", 0.6, {"qual-mla"}),
    ]


def _required() -> dict[str, set[str]]:
    return {
        "shift-morning": {"qual-mlt", "qual-mla"},
        "shift-evening": {"qual-mlt"},
        "shift-night": {"qual-mlt"},
    }


def _memory_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
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
          UNIQUE (tenant_id, employee_id, assignment_date)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE sys_sentry_logs (
          log_id INTEGER PRIMARY KEY AUTOINCREMENT,
          recorded_at_utc TEXT NOT NULL,
          tenant_id TEXT,
          username TEXT,
          exception_type TEXT NOT NULL,
          error_message TEXT NOT NULL,
          target_file TEXT,
          line_number INTEGER,
          clean_traceback TEXT NOT NULL,
          resolution_status TEXT NOT NULL DEFAULT 'unresolved',
          proposed_patch_code TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE schedule_audit_logs (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          tenant_id TEXT NOT NULL,
          schedule_period_id TEXT NOT NULL,
          recorded_at_utc TEXT NOT NULL,
          actor TEXT NOT NULL,
          employee_id TEXT,
          shift_date TEXT,
          previous_shift_code TEXT,
          new_shift_code TEXT,
          change_type TEXT NOT NULL
        )
        """
    )
    ensure_staff_lifecycle_schema(conn)
    return conn


def test_execute_shift_swap_allows_qualified_employee() -> None:
    conn = _memory_db()
    conn.execute(
        """
        INSERT INTO shift_assignments (
          id, tenant_id, schedule_period_id, employee_id, shift_template_id,
          assignment_date, created_at, updated_at
        ) VALUES (
          'asg-1', 'tenant-a', 'period-a', 'emp-a1', 'shift-morning',
          '2026-06-03', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z'
        )
        """
    )
    conn.commit()

    employees = _employees()
    templates = _templates()
    scheduled = [
        ScheduledShift("emp-a1", "Avery Miller", date(2026, 6, 3), "shift-morning"),
    ]

    result = execute_shift_swap(
        conn,
        tenant_id="tenant-a",
        shift_id="asg-1",
        old_employee_id="emp-a1",
        new_employee_id="emp-b1",
        rules=MANITOBA,
        period_start=date(2026, 6, 1),
        period_end=date(2026, 6, 7),
        weeks_in_period=1,
        employees=employees,
        all_assignments=scheduled,
        shift_templates=templates,
        shift_required_qualifications=_required(),
        actor="manager",
        username="manager",
    )

    assert result.success is True
    owner = conn.execute(
        "SELECT employee_id FROM shift_assignments WHERE id = 'asg-1'"
    ).fetchone()[0]
    assert owner == "emp-b1"


def test_execute_shift_swap_blocks_eleven_hour_rest_violation() -> None:
    conn = _memory_db()
    conn.execute(
        """
        INSERT INTO shift_assignments (
          id, tenant_id, schedule_period_id, employee_id, shift_template_id,
          assignment_date, created_at, updated_at
        ) VALUES (
          'asg-2', 'tenant-a', 'period-a', 'emp-a1', 'shift-morning',
          '2026-06-02', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z'
        )
        """
    )
    conn.commit()

    employees = _employees()
    templates = _templates()
    scheduled = [
        ScheduledShift("emp-a1", "Avery Miller", date(2026, 6, 2), "shift-morning"),
        ScheduledShift("emp-b1", "Jordan Patel", date(2026, 6, 1), "shift-evening"),
    ]

    result = execute_shift_swap(
        conn,
        tenant_id="tenant-a",
        shift_id="asg-2",
        old_employee_id="emp-a1",
        new_employee_id="emp-b1",
        rules=MANITOBA,
        period_start=date(2026, 6, 1),
        period_end=date(2026, 6, 7),
        weeks_in_period=1,
        employees=employees,
        all_assignments=scheduled,
        shift_templates=templates,
        shift_required_qualifications=_required(),
    )

    assert result.success is False
    assert "11h rest before Morning after Evening/Night" in result.message
    owner = conn.execute(
        "SELECT employee_id FROM shift_assignments WHERE id = 'asg-2'"
    ).fetchone()[0]
    assert owner == "emp-a1"


def test_list_swap_candidates_excludes_11_hour_rest_violator() -> None:
    from lab_scheduler.scheduling.shift_swap import ShiftAssignmentRecord

    shift_assignment = ShiftAssignmentRecord(
        shift_id="asg-3",
        tenant_id="tenant-a",
        schedule_period_id="period-a",
        employee_id="emp-a1",
        shift_template_id="shift-morning",
        assignment_date=date(2026, 6, 2),
    )
    employees = _employees()
    templates = _templates()
    scheduled = [
        ScheduledShift("emp-a1", "Avery Miller", date(2026, 6, 2), "shift-morning"),
        ScheduledShift("emp-b1", "Jordan Patel", date(2026, 6, 1), "shift-evening"),
    ]

    candidates = list_swap_candidates(
        shift_assignment=shift_assignment,
        rules=MANITOBA,
        period_start=date(2026, 6, 1),
        period_end=date(2026, 6, 7),
        weeks_in_period=1,
        employees=employees,
        all_assignments=scheduled,
        shift_templates=templates,
        shift_required_qualifications=_required(),
        include_ineligible=True,
    )

    blocked = next(item for item in candidates if item.employee_id == "emp-b1")
    assert blocked.is_eligible is False
    assert "11h rest before Morning after Evening/Night" in (blocked.block_reason or "")
