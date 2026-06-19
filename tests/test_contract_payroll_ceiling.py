
import pytest

pytestmark = pytest.mark.legacy

"""Vacant-line payroll/catalog assignment ceiling helpers."""

from datetime import date, timedelta

from lab_scheduler.compliance import MANITOBA
from lab_scheduler.compliance.engine import ShiftTemplateInfo
from lab_scheduler.scheduling.auto_generate import (
    CATALOG_PERSIST_TRIM_TOLERANCE,
    PlannedAssignment,
    _EmployeeState,
    _trim_catalog_contract_surplus,
    _would_violate_labor_rules,
)
from lab_scheduler.scheduling.contract_payroll import (
    vacant_assignment_hour_ceiling,
    would_exceed_vacant_assignment_ceiling,
)
from lab_scheduler.scheduling.profiles import EmployeeProfile


def _templates() -> dict[str, ShiftTemplateInfo]:
    return {
        "shift-morning": ShiftTemplateInfo(
            "shift-morning", "MORNING", "Morning", "07:00", "15:00", 480, False
        ),
    }


def test_ft_vacant_ceiling_is_payroll_not_catalog() -> None:
    employee = EmployeeProfile(
        "mla-de-01",
        "Vacant MLA D/E - Line 01",
        1.0,
        {"qual-mla"},
        contract_line_type="D/E",
    )
    payroll = {employee.id: 320.0}
    catalog = {employee.id: 328.0}
    assert vacant_assignment_hour_ceiling(employee, payroll, catalog) == 320.0


def test_pt_vacant_ceiling_is_min_payroll_and_catalog() -> None:
    employee = EmployeeProfile(
        "mlt-de-09",
        "Vacant MLT D/E - Line 09",
        0.2,
        {"qual-mlt"},
        contract_line_type="D/E",
    )
    payroll = {employee.id: 64.0}
    catalog = {employee.id: 128.0}
    assert vacant_assignment_hour_ceiling(employee, payroll, catalog) == 64.0


def test_would_exceed_vacant_ceiling_blocks_pt_overtime() -> None:
    employee = EmployeeProfile(
        "mlt-de-09",
        "Vacant MLT D/E - Line 09",
        0.2,
        {"qual-mlt"},
        contract_line_type="D/E",
    )
    payroll = {employee.id: 64.0}
    catalog = {employee.id: 64.0}
    assert would_exceed_vacant_assignment_ceiling(
        57.0,
        8.0,
        employee,
        payroll,
        catalog,
    )
    assert not would_exceed_vacant_assignment_ceiling(
        48.0,
        8.0,
        employee,
        payroll,
        catalog,
    )


def test_labor_rules_reject_assignment_over_payroll_ceiling() -> None:
    employee = EmployeeProfile(
        "mla-de-01",
        "Vacant MLA D/E - Line 01",
        1.0,
        {"qual-mla"},
        contract_line_type="D/E",
    )
    payroll = {employee.id: 320.0}
    catalog = {employee.id: 328.0}
    state = _EmployeeState(profile=employee, target_hours=320.0, total_hours=320.0)
    template = _templates()["shift-morning"]
    violation = _would_violate_labor_rules(
        state,
        date(2026, 7, 20),
        template,
        _templates(),
        MANITOBA,
        date(2026, 6, 1),
        date(2026, 7, 26),
        None,
        payroll_targets=payroll,
        catalog_targets=catalog,
    )
    assert violation is not None
    assert "contract hour ceiling" in violation


def test_trim_catalog_surplus_trims_ft_328h_to_320h() -> None:
    employee = EmployeeProfile(
        "mla-de-01",
        "Vacant MLA D/E - Line 01",
        1.0,
        {"qual-mla"},
        contract_line_type="D/E",
    )
    period_start = date(2026, 6, 1)
    period_end = date(2026, 7, 26)
    payroll_targets = {employee.id: 320.0}
    catalog_targets = {employee.id: 328.0}
    assignments = [
        PlannedAssignment(
            employee_id=employee.id,
            shift_template_id="shift-morning",
            assignment_date=period_start + timedelta(days=offset),
        )
        for offset in range(41)
    ]
    states = {
        employee.id: _EmployeeState(
            profile=employee,
            target_hours=320.0,
            total_hours=328.0,
        )
    }

    removed = _trim_catalog_contract_surplus(
        assignments,
        states,
        employees=[employee],
        shift_templates=_templates(),
        catalog_targets=catalog_targets,
        period_start=period_start,
        period_end=period_end,
        allow_trim_frozen=True,
        tolerance=CATALOG_PERSIST_TRIM_TOLERANCE,
        payroll_targets=payroll_targets,
    )

    assert removed >= 1
    assert states[employee.id].total_hours <= 320.0 + CATALOG_PERSIST_TRIM_TOLERANCE + 0.01
