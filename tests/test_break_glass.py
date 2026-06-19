import json
import sqlite3
from datetime import date
from pathlib import Path

from lab_scheduler.compliance import MANITOBA, ShiftTemplateInfo
from lab_scheduler.compliance.engine import ScheduledShift
from lab_scheduler.scheduling.auto_generate import EmployeeProfile
from lab_scheduler.scheduling.auto_pilot import run_auto_pilot_full_block
from lab_scheduler.scheduling.shift_swap import execute_shift_swap
from lab_scheduler.staff.lifecycle import ensure_staff_lifecycle_schema
from portage_fixtures import portage_generate_kwargs


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
    conn.execute("CREATE TABLE tenants (id TEXT PRIMARY KEY)")
    conn.execute("INSERT INTO tenants VALUES ('tenant-a')")
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
          is_compliance_overridden INTEGER NOT NULL DEFAULT 0,
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


def test_break_glass_swap_forces_assignment_despite_rest_violation() -> None:
    conn = _memory_db()
    conn.execute(
        """
        INSERT INTO shift_assignments (
          id, tenant_id, schedule_period_id, employee_id, shift_template_id,
          assignment_date, created_at, updated_at
        ) VALUES (
          'asg-bg-1', 'tenant-a', 'period-a', 'emp-a1', 'shift-morning',
          '2026-06-02', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z'
        )
        """
    )
    conn.commit()

    scheduled = [
        ScheduledShift("emp-a1", "Avery Miller", date(2026, 6, 2), "shift-morning"),
        ScheduledShift("emp-b1", "Jordan Patel", date(2026, 6, 1), "shift-evening"),
    ]

    blocked = execute_shift_swap(
        conn,
        tenant_id="tenant-a",
        shift_id="asg-bg-1",
        old_employee_id="emp-a1",
        new_employee_id="emp-b1",
        rules=MANITOBA,
        period_start=date(2026, 6, 1),
        period_end=date(2026, 6, 7),
        weeks_in_period=1,
        employees=_employees(),
        all_assignments=scheduled,
        shift_templates=_templates(),
        shift_required_qualifications=_required(),
    )
    assert blocked.success is False

    result = execute_shift_swap(
        conn,
        tenant_id="tenant-a",
        shift_id="asg-bg-1",
        old_employee_id="emp-a1",
        new_employee_id="emp-b1",
        rules=MANITOBA,
        period_start=date(2026, 6, 1),
        period_end=date(2026, 6, 7),
        weeks_in_period=1,
        employees=_employees(),
        all_assignments=scheduled,
        shift_templates=_templates(),
        shift_required_qualifications=_required(),
        bypass_compliance_rules=True,
        actor="northstar_admin",
    )
    assert result.success is True
    assert result.is_compliance_overridden is True
    owner = conn.execute(
        "SELECT employee_id FROM shift_assignments WHERE id = 'asg-bg-1'"
    ).fetchone()[0]
    assert owner == "emp-b1"
    overridden = conn.execute(
        "SELECT is_compliance_overridden FROM shift_assignments WHERE id = 'asg-bg-1'"
    ).fetchone()[0]
    assert overridden == 1


def test_break_glass_swap_logs_audit_warning_to_sys_audit_log() -> None:
    conn = _memory_db()
    conn.execute(
        """
        INSERT INTO shift_assignments (
          id, tenant_id, schedule_period_id, employee_id, shift_template_id,
          assignment_date, created_at, updated_at
        ) VALUES (
          'asg-bg-2', 'tenant-a', 'period-a', 'emp-a1', 'shift-morning',
          '2026-06-02', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z'
        )
        """
    )
    conn.commit()

    scheduled = [
        ScheduledShift("emp-a1", "Avery Miller", date(2026, 6, 2), "shift-morning"),
        ScheduledShift("emp-b1", "Jordan Patel", date(2026, 6, 1), "shift-evening"),
    ]

    execute_shift_swap(
        conn,
        tenant_id="tenant-a",
        shift_id="asg-bg-2",
        old_employee_id="emp-a1",
        new_employee_id="emp-b1",
        rules=MANITOBA,
        period_start=date(2026, 6, 1),
        period_end=date(2026, 6, 7),
        weeks_in_period=1,
        employees=_employees(),
        all_assignments=scheduled,
        shift_templates=_templates(),
        shift_required_qualifications=_required(),
        bypass_compliance_rules=True,
        actor="northstar_admin",
    )

    row = conn.execute(
        """
        SELECT action_type, metadata_json
        FROM sys_audit_log
        WHERE tenant_id = 'tenant-a'
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    assert row is not None
    assert row[0] == "audit_warning"
    metadata = json.loads(row[1])
    assert metadata["is_compliance_overridden"] is True
    assert "overriding" in metadata["warning_message"].lower()


def test_auto_pilot_bypass_compliance_rules_skips_post_validation_block() -> None:
    run_auto_pilot_full_block(
        **portage_generate_kwargs(),
        bypass_compliance_rules=True,
    )
