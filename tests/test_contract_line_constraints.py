from __future__ import annotations

from datetime import date

from lab_scheduler.compliance import MANITOBA, ShiftTemplateInfo
from lab_scheduler.compliance.engine import ScheduledShift
from lab_scheduler.engine.constraints import validate_contract_line_eligibility
from lab_scheduler.models.employee import is_critical_contract_line_violation
from lab_scheduler.scheduling.auto_generate import (
    EmployeeProfile,
    suggest_employees_for_slot,
    validate_assignment_change,
)
from lab_scheduler.scheduling.shift_swap import execute_shift_swap
from lab_scheduler.staff.lifecycle import ensure_staff_lifecycle_schema
import sqlite3


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


def _required() -> dict[str, set[str]]:
    return {
        "shift-morning": {"qual-mlt", "qual-mla"},
        "shift-evening": {"qual-mlt"},
        "shift-night": {"qual-mlt"},
    }


def test_validate_assignment_change_allows_clinical_floor_contract_line_borrow() -> None:
    de_mla = EmployeeProfile(
        "emp-de-mla",
        "MLA D/E",
        1.0,
        {"qual-mla"},
        contract_line_type="D/E",
    )
    shift_required = {
        **_required(),
        "shift-night": {"qual-mlt", "qual-mla"},
    }
    violation = validate_assignment_change(
        rules=MANITOBA,
        period_start=date(2026, 6, 1),
        period_end=date(2026, 6, 28),
        weeks_in_period=4,
        employee=de_mla,
        all_assignments=[],
        shift_templates=_templates(),
        shift_required_qualifications=shift_required,
        assignment_date=date(2026, 6, 3),
        new_shift_template_id="shift-night",
        role_pool_id="Clinical Floor - Night - Seat_02 - MLA",
    )
    assert violation is None or "Contract Line Violation" not in violation


def test_validate_assignment_change_still_blocks_non_clinical_contract_line() -> None:
    de_worker = EmployeeProfile(
        "emp-de",
        "MLT 01 (1.0 D/E)",
        1.0,
        {"qual-mlt"},
        contract_line_type="D/E",
    )

    violation = validate_contract_line_eligibility("D/E", "NIGHT", qual_code="MLT")
    assert violation == (
        "CRITICAL: Contract Line Violation (Day/Evening Worker assigned to Night Shift)"
    )
    assert is_critical_contract_line_violation(violation)

    assignment_violation = validate_assignment_change(
        rules=MANITOBA,
        period_start=date(2026, 6, 1),
        period_end=date(2026, 6, 28),
        weeks_in_period=4,
        employee=de_worker,
        all_assignments=[],
        shift_templates=_templates(),
        shift_required_qualifications=_required(),
        assignment_date=date(2026, 6, 3),
        new_shift_template_id="shift-night",
    )
    assert assignment_violation == violation

    suggestions = suggest_employees_for_slot(
        rules=MANITOBA,
        period_start=date(2026, 6, 1),
        period_end=date(2026, 6, 28),
        weeks_in_period=4,
        employees=[de_worker],
        all_assignments=[],
        shift_templates=_templates(),
        shift_required_qualifications=_required(),
        slot_date=date(2026, 6, 3),
        shift_template_id="shift-night",
        limit=3,
    )
    assert suggestions == []


def test_dn_worker_ineligible_for_evening_shift() -> None:
    dn_worker = EmployeeProfile(
        "emp-dn",
        "MLT 02 (0.8 D/N)",
        0.8,
        {"qual-mlt"},
        contract_line_type="D/N",
    )

    violation = validate_contract_line_eligibility("D/N", "EVENING", qual_code="MLT")
    assert violation == (
        "CRITICAL: Contract Line Violation (Day/Night Worker assigned to Evening Shift)"
    )
    assert is_critical_contract_line_violation(violation)

    assignment_violation = validate_assignment_change(
        rules=MANITOBA,
        period_start=date(2026, 6, 1),
        period_end=date(2026, 6, 28),
        weeks_in_period=4,
        employee=dn_worker,
        all_assignments=[],
        shift_templates=_templates(),
        shift_required_qualifications=_required(),
        assignment_date=date(2026, 6, 4),
        new_shift_template_id="shift-evening",
    )
    assert assignment_violation == violation

    suggestions = suggest_employees_for_slot(
        rules=MANITOBA,
        period_start=date(2026, 6, 1),
        period_end=date(2026, 6, 28),
        weeks_in_period=4,
        employees=[dn_worker],
        all_assignments=[],
        shift_templates=_templates(),
        shift_required_qualifications=_required(),
        slot_date=date(2026, 6, 4),
        shift_template_id="shift-evening",
        limit=3,
    )
    assert suggestions == []


def test_mlt_de_worker_may_work_day_and_evening_shifts() -> None:
    assert validate_contract_line_eligibility("D/E", "MORNING", qual_code="MLT") is None
    assert validate_contract_line_eligibility("D/E", "EVENING", qual_code="MLT") is None
    assert validate_contract_line_eligibility("D/E", "NIGHT", qual_code="MLT") is not None


def test_mlt_dn_worker_may_work_day_and_night_shifts() -> None:
    assert validate_contract_line_eligibility("D/N", "MORNING", qual_code="MLT") is None
    assert validate_contract_line_eligibility("D/N", "NIGHT", qual_code="MLT") is None
    assert validate_contract_line_eligibility("D/N", "EVENING", qual_code="MLT") is not None


def test_mla_dn_worker_may_work_day_and_night_shifts() -> None:
    assert validate_contract_line_eligibility("D/N", "MORNING", qual_code="MLA") is None
    assert validate_contract_line_eligibility("D/N", "NIGHT", qual_code="MLA") is None
    assert validate_contract_line_eligibility("D/N", "EVENING", qual_code="MLA") is not None


def test_break_glass_cannot_override_contract_line_violation() -> None:
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
    ensure_staff_lifecycle_schema(conn)
    conn.execute(
        """
        INSERT INTO shift_assignments (
          id, tenant_id, schedule_period_id, employee_id, shift_template_id,
          assignment_date, created_at, updated_at
        ) VALUES (
          'asg-cl-1', 'tenant-a', 'period-a', 'emp-other', 'shift-night',
          '2026-06-03', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z'
        )
        """
    )
    conn.commit()

    de_worker = EmployeeProfile(
        "emp-de",
        "MLT 01 (1.0 D/E)",
        1.0,
        {"qual-mlt"},
        contract_line_type="D/E",
    )
    other = EmployeeProfile("emp-other", "Other MLT", 1.0, {"qual-mlt"})
    scheduled = [
        ScheduledShift("emp-other", "Other MLT", date(2026, 6, 3), "shift-night"),
    ]

    result = execute_shift_swap(
        conn,
        tenant_id="tenant-a",
        shift_id="asg-cl-1",
        old_employee_id="emp-other",
        new_employee_id="emp-de",
        rules=MANITOBA,
        period_start=date(2026, 6, 1),
        period_end=date(2026, 6, 28),
        weeks_in_period=4,
        employees=[de_worker, other],
        all_assignments=scheduled,
        shift_templates=_templates(),
        shift_required_qualifications=_required(),
        bypass_compliance_rules=True,
        actor="northstar_admin",
    )

    assert result.success is False
    assert "CRITICAL: Contract Line Violation (Day/Evening Worker assigned to Night Shift)" in (
        result.message or ""
    )
    owner = conn.execute(
        "SELECT employee_id FROM shift_assignments WHERE id = 'asg-cl-1'"
    ).fetchone()[0]
    assert owner == "emp-other"
