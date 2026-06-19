import sqlite3
from datetime import date

from lab_scheduler.compliance import (
    MANITOBA,
    ComplianceReport,
    ComplianceViolation,
    ScheduledShift,
)
from lab_scheduler.staff.lifecycle import ensure_staff_lifecycle_schema, log_audit_warning
from lab_scheduler.validation.union_compliance_report import (
    build_union_compliance_report,
    fetch_break_glass_events,
    generate_union_compliance_report,
    render_union_compliance_report_html,
)


def _memory_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """
        CREATE TABLE employees (
          id TEXT NOT NULL,
          tenant_id TEXT NOT NULL,
          first_name TEXT NOT NULL,
          last_name TEXT NOT NULL,
          PRIMARY KEY (tenant_id, id)
        )
        """
    )
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
          is_compliance_overridden INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE shift_templates (
          tenant_id TEXT NOT NULL,
          id TEXT NOT NULL,
          code TEXT NOT NULL,
          PRIMARY KEY (tenant_id, id)
        )
        """
    )
    conn.execute(
        "INSERT INTO employees VALUES ('emp-a1', 'tenant-a', 'Avery', 'Miller')"
    )
    conn.execute(
        "INSERT INTO shift_templates VALUES ('tenant-a', 'shift-morning', 'M')"
    )
    conn.execute(
        """
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
        )
        """
    )
    ensure_staff_lifecycle_schema(conn)
    return conn


def test_fetch_break_glass_events_from_override_flag() -> None:
    conn = _memory_db()
    conn.execute(
        """
        INSERT INTO shift_assignments (
          id, tenant_id, schedule_period_id, employee_id, shift_template_id,
          assignment_date, created_at, updated_at, is_compliance_overridden
        ) VALUES (
          'asg-1', 'tenant-a', 'period-a', 'emp-a1', 'shift-morning',
          '2026-06-03', '2026-01-01T00:00:00Z', '2026-06-03T12:00:00Z', 1
        )
        """
    )
    conn.commit()
    events = fetch_break_glass_events(
        conn,
        tenant_id="tenant-a",
        period_start=date(2026, 6, 1),
        period_end=date(2026, 6, 7),
    )
    assert len(events) == 1
    assert events[0].employee_name == "Avery Miller"
    assert events[0].shift_date == date(2026, 6, 3)


def test_union_compliance_report_html_includes_attestation() -> None:
    conn = _memory_db()
    log_audit_warning(
        conn,
        tenant_id="tenant-a",
        manager_id="mgr-1",
        employee_id="emp-a1",
        warning_message="Rest window override",
        is_compliance_overridden=True,
        context={"assignment_date": "2026-06-03", "violation": "DAILY_REST"},
    )
    conn.commit()

    assignments = [
        ScheduledShift("emp-a1", "Avery Miller", date(2026, 6, 1), "shift-morning"),
    ]
    compliance = ComplianceReport(jurisdiction_code="MB")
    report, html_doc = generate_union_compliance_report(
        conn,
        tenant_id="tenant-a",
        tenant_name="Northstar Medical Laboratory",
        period_id="period-a",
        period_name="Summer 2026",
        period_start=date(2026, 6, 1),
        period_end=date(2026, 6, 7),
        rules=MANITOBA,
        compliance_report=compliance,
        assignments=assignments,
    )
    assert report.total_shifts_managed == 1
    assert len(report.break_glass_events) >= 1
    assert report.overall_legal_alignment is True
    assert "Union-Compliance Audit Report" in html_doc
    assert report.content_hash in html_doc
    assert "Break-Glass manual overrides" in html_doc
    assert "read-only system export" in html_doc.lower()


def test_build_report_flags_rest_window_errors() -> None:
    report = ComplianceReport(jurisdiction_code="MB")
    report.violations.append(
        ComplianceViolation(
            code="DAILY_REST",
            severity="error",
            employee_id="emp-a1",
            employee_name="Avery Miller",
            message="Insufficient rest between shifts.",
            rule_reference="Manitoba ESC — daily rest",
        )
    )
    built = build_union_compliance_report(
        tenant_name="Demo Lab",
        period_id="period-a",
        period_name="Summer 2026",
        period_start=date(2026, 6, 1),
        period_end=date(2026, 6, 7),
        rules=MANITOBA,
        compliance_report=report,
        assignments=[
            ScheduledShift("emp-a1", "Avery Miller", date(2026, 6, 1), "shift-morning"),
        ],
        break_glass_events=(),
        audit_entries=[],
    )
    assert built.rest_window_compliant is False
    assert built.overall_legal_alignment is False
    html_doc = render_union_compliance_report_html(built)
    assert "REVIEW REQUIRED" in html_doc
