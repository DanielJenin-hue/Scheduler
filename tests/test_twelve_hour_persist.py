"""Integration tests for twelve-hour assignment persistence and HTML export."""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from pathlib import Path

import pytest

from lab_scheduler.scheduling.auto_pilot import persist_auto_pilot_schedule
from lab_scheduler.scheduling.breakroom_print import generate_breakroom_print_html
from lab_scheduler.scheduling.schedule_export import build_schedule_export_rows
from lab_scheduler.scheduling.strategies import ScheduleArchetype
from lab_scheduler.scheduling.strategies.twelve_hour_7on7off_strategy import (
    FTE_TOPUP_TEMPLATE_ID,
    generate_schedule,
)
from lab_scheduler.scheduling.twelve_hour_templates import (
    ensure_twelve_hour_shift_templates,
    fte_topup_template_id_for_tenant,
    remap_topup_assignments_for_persist,
)

from portage_fixtures import portage_generate_kwargs

pytestmark = pytest.mark.legacy


def _memory_db_with_shift_schema() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        PRAGMA foreign_keys = ON;
        CREATE TABLE tenants (id TEXT PRIMARY KEY);
        CREATE TABLE qualifications (
          id TEXT PRIMARY KEY,
          tenant_id TEXT NOT NULL REFERENCES tenants(id),
          code TEXT NOT NULL,
          name TEXT NOT NULL,
          is_active INTEGER NOT NULL DEFAULT 1,
          created_at TEXT NOT NULL
        );
        CREATE TABLE shift_templates (
          id TEXT PRIMARY KEY,
          tenant_id TEXT NOT NULL REFERENCES tenants(id),
          code TEXT NOT NULL,
          name TEXT NOT NULL,
          start_time TEXT NOT NULL,
          end_time TEXT NOT NULL,
          duration_minutes INTEGER NOT NULL,
          crosses_midnight INTEGER NOT NULL DEFAULT 0,
          is_active INTEGER NOT NULL DEFAULT 1,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          UNIQUE (tenant_id, code)
        );
        CREATE TABLE shift_template_qualifications (
          tenant_id TEXT NOT NULL REFERENCES tenants(id),
          shift_template_id TEXT NOT NULL,
          qualification_id TEXT NOT NULL,
          created_at TEXT NOT NULL,
          PRIMARY KEY (tenant_id, shift_template_id, qualification_id),
          FOREIGN KEY (tenant_id, shift_template_id)
            REFERENCES shift_templates (tenant_id, id),
          FOREIGN KEY (tenant_id, qualification_id)
            REFERENCES qualifications (tenant_id, id)
        );
        CREATE UNIQUE INDEX uq_shift_templates_tenant_id
          ON shift_templates (tenant_id, id);
        CREATE UNIQUE INDEX uq_qualifications_tenant_id
          ON qualifications (tenant_id, id);
        CREATE TABLE shift_assignments (
          id TEXT PRIMARY KEY,
          tenant_id TEXT NOT NULL,
          schedule_period_id TEXT NOT NULL,
          employee_id TEXT NOT NULL,
          shift_template_id TEXT NOT NULL,
          assignment_date TEXT NOT NULL,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          UNIQUE (tenant_id, employee_id, assignment_date),
          FOREIGN KEY (tenant_id, shift_template_id)
            REFERENCES shift_templates (tenant_id, id)
        );
        INSERT INTO tenants (id) VALUES ('tenant-a');
        INSERT INTO qualifications (id, tenant_id, code, name, is_active, created_at)
        VALUES
          ('qual-mlt', 'tenant-a', 'MLT', 'MLT', 1, '2026-05-26T00:00:00Z'),
          ('qual-mla', 'tenant-a', 'MLA', 'MLA', 1, '2026-05-26T00:00:00Z');
        INSERT INTO shift_templates (
          id, tenant_id, code, name, start_time, end_time,
          duration_minutes, crosses_midnight, is_active, created_at, updated_at
        ) VALUES
          (
            'shift-morning', 'tenant-a', 'MORNING', 'Morning', '07:00', '15:00',
            480, 0, 1, '2026-05-26T00:00:00Z', '2026-05-26T00:00:00Z'
          ),
          (
            'shift-night', 'tenant-a', 'NIGHT', 'Night', '23:00', '07:00',
            480, 1, 1, '2026-05-26T00:00:00Z', '2026-05-26T00:00:00Z'
          );
        INSERT INTO shift_template_qualifications (
          tenant_id, shift_template_id, qualification_id, created_at
        ) VALUES
          ('tenant-a', 'shift-morning', 'qual-mlt', '2026-05-26T00:00:00Z'),
          ('tenant-a', 'shift-morning', 'qual-mla', '2026-05-26T00:00:00Z'),
          ('tenant-a', 'shift-night', 'qual-mlt', '2026-05-26T00:00:00Z'),
          ('tenant-a', 'shift-night', 'qual-mla', '2026-05-26T00:00:00Z');
        """
    )
    return conn


def test_persist_twelve_hour_topup_assignments() -> None:
    kwargs = portage_generate_kwargs(
        period_start=date(2026, 6, 1),
        period_end=date(2026, 7, 26),
        weeks_in_period=8,
    )
    result = generate_schedule(**kwargs)
    topups = [
        assignment
        for assignment in result.assignments
        if assignment.shift_template_id == FTE_TOPUP_TEMPLATE_ID
    ]
    assert topups

    conn = _memory_db_with_shift_schema()
    ensure_twelve_hour_shift_templates(conn, "tenant-a")
    persisted_assignments = remap_topup_assignments_for_persist(
        result.assignments,
        "tenant-a",
        conn=conn,
    )
    inserted = persist_auto_pilot_schedule(
        conn,
        tenant_id="tenant-a",
        schedule_period_id="period-a",
        assignments=persisted_assignments,
        replace_existing=True,
    )
    assert inserted == len(result.assignments)
    persisted_topup_id = fte_topup_template_id_for_tenant("tenant-a", conn=conn)
    stored_topups = conn.execute(
        """
        SELECT COUNT(*) FROM shift_assignments
        WHERE shift_template_id = ?
        """,
        (persisted_topup_id,),
    ).fetchone()[0]
    assert stored_topups == len(topups)


def test_topup_templates_are_tenant_scoped() -> None:
    conn = _memory_db_with_shift_schema()
    conn.execute("INSERT INTO tenants (id) VALUES ('tenant-b')")
    conn.executemany(
        """
        INSERT INTO qualifications (id, tenant_id, code, name, is_active, created_at)
        VALUES (?, ?, ?, ?, 1, '2026-05-26T00:00:00Z')
        """,
        [
            ("qual-mlt-b", "tenant-b", "MLT", "MLT"),
            ("qual-mla-b", "tenant-b", "MLA", "MLA"),
        ],
    )
    conn.commit()

    ensure_twelve_hour_shift_templates(conn, "tenant-a")
    ensure_twelve_hour_shift_templates(conn, "tenant-b")

    template_a = fte_topup_template_id_for_tenant("tenant-a", conn=conn)
    template_b = fte_topup_template_id_for_tenant("tenant-b", conn=conn)
    assert template_a != template_b

    row_a = conn.execute(
        "SELECT tenant_id FROM shift_templates WHERE id = ?",
        (template_a,),
    ).fetchone()
    row_b = conn.execute(
        "SELECT tenant_id FROM shift_templates WHERE id = ?",
        (template_b,),
    ).fetchone()
    assert row_a == ("tenant-a",)
    assert row_b == ("tenant-b",)


def test_twelve_hour_html_export_shows_topup_and_no_union_risk(tmp_path: Path) -> None:
    kwargs = portage_generate_kwargs(
        period_start=date(2026, 6, 1),
        period_end=date(2026, 7, 26),
        weeks_in_period=8,
    )
    result = generate_schedule(**kwargs)
    dates = [date(2026, 6, 1) + timedelta(days=offset) for offset in range(56)]
    template_dict = {
        shift_id: {
            "id": shift_id,
            "code": template.code,
            "short": template.code,
            "name": template.name,
        }
        for shift_id, template in kwargs["shift_templates"].items()
    }
    template_dict[FTE_TOPUP_TEMPLATE_ID] = {
        "id": FTE_TOPUP_TEMPLATE_ID,
        "code": "TOPUP",
        "short": "T",
        "name": "FTE Top-up Shift",
    }
    emp_rows = [
        {
            "id": employee.id,
            "full_name": employee.full_name,
            "fte": employee.fte,
            "contract_line_type": employee.contract_line_type or "",
        }
        for employee in kwargs["employees"]
    ]
    assignment_rows = [
        {
            "employee_id": assignment.employee_id,
            "assignment_date": assignment.assignment_date,
            "shift_template_id": assignment.shift_template_id,
        }
        for assignment in result.assignments
    ]
    schedule_rows = build_schedule_export_rows(emp_rows, dates, assignment_rows, template_dict)
    html = generate_breakroom_print_html(
        facility_name="Northstar Medical Laboratory",
        period_name="Summer 2026 Master Rotation",
        period_start=date(2026, 6, 1),
        period_end=date(2026, 7, 26),
        week_count=8,
        employees=emp_rows,
        dates=dates,
        schedule_rows=schedule_rows,
        schedule_archetype=ScheduleArchetype.TWELVE_HOUR.value,
    )
    export_path = tmp_path / "breakroom.html"
    export_path.write_text(html, encoding="utf-8")
    assert "313.9h actual" not in html
    assert "320h actual" in html
    assert "print-token-t" in html
