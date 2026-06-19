
import pytest

pytestmark = pytest.mark.legacy

"""Category 2: FT vacant finalize top-up aligned to payroll 320h."""

from datetime import date, timedelta

from lab_scheduler.compliance import MANITOBA
from lab_scheduler.compliance.engine import ShiftTemplateInfo
from lab_scheduler.scheduling.auto_generate import (
    CATALOG_PERSIST_TRIM_TOLERANCE,
    AutoGenerateResult,
    PlannedAssignment,
    _EmployeeState,
    _catalog_contract_top_up_pass,
    _contract_finalize_target,
    _finalize_for_persist_gate,
    _has_catalog_contract_deficit,
    _has_contract_finalize_deficit,
    _rebuild_states_from_assignments,
    _top_up_injection_blocked_for_employee,
    _try_peer_transfer_shift_on_date,
)
from lab_scheduler.scheduling.post_pass_guard import PostPassGuard
from lab_scheduler.scheduling.profiles import EmployeeProfile


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


def _ft_vacant_dn() -> EmployeeProfile:
    return EmployeeProfile(
        "ft-vacant-01",
        "Vacant MLA D/N - Line 01",
        1.0,
        {"qual-mla"},
        contract_line_type="D/N",
    )


def test_has_contract_finalize_deficit_flags_312_on_320_payroll_target() -> None:
    employee = _ft_vacant_dn()
    payroll_targets = {employee.id: 320.0}
    catalog_targets = {employee.id: 312.0}

    assert _contract_finalize_target(
        employee,
        payroll_targets=payroll_targets,
        catalog_targets=catalog_targets,
    ) == 320.0
    assert _has_contract_finalize_deficit(
        employee,
        312.0,
        payroll_targets=payroll_targets,
        catalog_targets=catalog_targets,
    )
    assert not _has_contract_finalize_deficit(
        employee,
        320.0,
        payroll_targets=payroll_targets,
        catalog_targets=catalog_targets,
    )
    assert not _has_catalog_contract_deficit(employee, 312.0, catalog_targets)


def test_catalog_contract_top_up_fills_ft_vacant_from_312_to_320() -> None:
    employee = _ft_vacant_dn()
    period_start = date(2026, 6, 1)
    period_end = period_start + timedelta(days=55)
    payroll_targets = {employee.id: 320.0}
    catalog_targets = {employee.id: 312.0}
    states = {
        employee.id: _EmployeeState(
            profile=employee,
            target_hours=320.0,
            total_hours=312.0,
        )
    }
    assignments: list[PlannedAssignment] = []

    added = _catalog_contract_top_up_pass(
        employees=[employee],
        states=states,
        assignments=assignments,
        shift_templates=_templates(),
        rules=MANITOBA,
        period_start=period_start,
        period_end=period_end,
        availability_blocked=None,
        qual_codes={employee.id: "MLA"},
        catalog_targets=catalog_targets,
        payroll_targets=payroll_targets,
    )

    assert added >= 1
    assert states[employee.id].total_hours >= 320.0 - CATALOG_PERSIST_TRIM_TOLERANCE
    assert states[employee.id].total_hours <= 320.0 + CATALOG_PERSIST_TRIM_TOLERANCE + 0.01


def test_ft_payroll_top_up_respects_surplus_cap_at_320() -> None:
    employee = _ft_vacant_dn()
    period_start = date(2026, 6, 1)
    period_end = period_start + timedelta(days=55)
    payroll_targets = {employee.id: 320.0}
    catalog_targets = {employee.id: 328.0}
    states = {
        employee.id: _EmployeeState(
            profile=employee,
            target_hours=320.0,
            total_hours=320.0,
        )
    }
    assignments: list[PlannedAssignment] = []

    added = _catalog_contract_top_up_pass(
        employees=[employee],
        states=states,
        assignments=assignments,
        shift_templates=_templates(),
        rules=MANITOBA,
        period_start=period_start,
        period_end=period_end,
        availability_blocked=None,
        qual_codes={employee.id: "MLA"},
        catalog_targets=catalog_targets,
        payroll_targets=payroll_targets,
    )

    assert added == 0
    assert states[employee.id].total_hours == 320.0


def test_catalog_top_up_blocked_when_pt_at_payroll_ceiling() -> None:
    employee = EmployeeProfile(
        "mlt-de-09",
        "Vacant MLT D/E - Line 09",
        0.2,
        {"qual-mlt"},
        contract_line_type="D/E",
    )
    period_start = date(2026, 6, 1)
    period_end = period_start + timedelta(days=13)
    payroll_targets = {employee.id: 64.0}
    catalog_targets = {employee.id: 64.0}
    states = {
        employee.id: _EmployeeState(
            profile=employee,
            target_hours=64.0,
            total_hours=64.0,
        )
    }
    assignments: list[PlannedAssignment] = []

    added = _catalog_contract_top_up_pass(
        employees=[employee],
        states=states,
        assignments=assignments,
        shift_templates=_templates(),
        rules=MANITOBA,
        period_start=period_start,
        period_end=period_end,
        availability_blocked=None,
        qual_codes={employee.id: "MLT"},
        catalog_targets=catalog_targets,
        payroll_targets=payroll_targets,
    )

    assert added == 0
    assert states[employee.id].total_hours == 64.0


def test_persist_gate_allows_pt_top_up_while_ft_under_payroll() -> None:
    ft_employee = EmployeeProfile(
        "mlt-de-02",
        "Vacant MLT D/E - Line 02",
        1.0,
        {"qual-mlt"},
        contract_line_type="D/E",
    )
    pt_employee = EmployeeProfile(
        "mla-de-07",
        "Vacant MLA D/E - Line 07",
        0.5,
        {"qual-mla"},
        contract_line_type="D/E",
    )
    payroll_targets = {ft_employee.id: 320.0, pt_employee.id: 192.0}
    catalog_targets = {ft_employee.id: 328.0, pt_employee.id: 240.0}
    states = {
        ft_employee.id: _EmployeeState(
            profile=ft_employee,
            target_hours=320.0,
            total_hours=312.0,
        ),
        pt_employee.id: _EmployeeState(
            profile=pt_employee,
            target_hours=240.0,
            total_hours=208.0,
        ),
    }

    assert _top_up_injection_blocked_for_employee(
        pt_employee,
        states,
        employees=[ft_employee, pt_employee],
        payroll_targets=payroll_targets,
        catalog_targets=catalog_targets,
        persist_gate=False,
    )
    assert not _top_up_injection_blocked_for_employee(
        pt_employee,
        states,
        employees=[ft_employee, pt_employee],
        payroll_targets=payroll_targets,
        catalog_targets=catalog_targets,
        persist_gate=True,
    )


def test_finalize_for_persist_gate_trims_ft_328h_to_payroll_320() -> None:
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
    shift_templates = _templates()
    assignments = [
        PlannedAssignment(
            employee_id=employee.id,
            shift_template_id="shift-morning",
            assignment_date=period_start + timedelta(days=offset),
        )
        for offset in range(41)
    ]
    result = AutoGenerateResult(
        assignments=list(assignments),
        coverage_complete=True,
    )
    states = {
        employee.id: _EmployeeState(
            profile=employee,
            target_hours=320.0,
            total_hours=328.0,
        )
    }
    _rebuild_states_from_assignments(states, result.assignments, shift_templates)

    _finalize_for_persist_gate(
        result,
        states=states,
        employees=[employee],
        expanded_slots=[],
        shift_templates=shift_templates,
        shift_required_qualifications={
            "shift-morning": {"qual-mla"},
            "shift-evening": {"qual-mla"},
            "shift-night": {"qual-mla"},
        },
        rules=MANITOBA,
        period_start=period_start,
        period_end=period_end,
        availability_blocked=None,
        qual_codes={employee.id: "MLA"},
        catalog_targets=catalog_targets,
        payroll_targets=payroll_targets,
        filled_smooth_seats=set(),
        post_pass_guard=PostPassGuard(frozen_master_cells=set()),
        template_bands={
            "shift-morning": "D",
            "shift-evening": "E",
            "shift-night": "N",
        },
        weeks_in_period=8,
    )

    assert states[employee.id].total_hours <= 320.0 + CATALOG_PERSIST_TRIM_TOLERANCE + 0.01


def test_finalize_for_persist_gate_trims_mlt_l09_128h_to_64h() -> None:
    employee = EmployeeProfile(
        "mlt-de-09",
        "Vacant MLT D/E - Line 09",
        0.2,
        {"qual-mlt"},
        contract_line_type="D/E",
    )
    donor = EmployeeProfile(
        "mlt-de-08",
        "Vacant MLT D/E - Line 08",
        0.4,
        {"qual-mlt"},
        contract_line_type="D/E",
    )
    period_start = date(2026, 6, 1)
    period_end = date(2026, 7, 26)
    payroll_targets = {employee.id: 64.0, donor.id: 160.0}
    catalog_targets = {employee.id: 64.0, donor.id: 168.0}
    shift_templates = _templates()
    assignments = [
        PlannedAssignment(
            employee_id=employee.id,
            shift_template_id="shift-morning",
            assignment_date=period_start + timedelta(days=offset * 2),
        )
        for offset in range(16)
    ]
    result = AutoGenerateResult(
        assignments=list(assignments),
        coverage_complete=True,
    )
    states = {
        employee.id: _EmployeeState(profile=employee, target_hours=64.0, total_hours=128.0),
        donor.id: _EmployeeState(profile=donor, target_hours=160.0, total_hours=0.0),
    }
    _rebuild_states_from_assignments(states, result.assignments, shift_templates)

    _finalize_for_persist_gate(
        result,
        states=states,
        employees=[employee, donor],
        expanded_slots=[],
        shift_templates=shift_templates,
        shift_required_qualifications={
            "shift-morning": {"qual-mlt"},
            "shift-evening": {"qual-mlt"},
            "shift-night": {"qual-mlt"},
        },
        rules=MANITOBA,
        period_start=period_start,
        period_end=period_end,
        availability_blocked=None,
        qual_codes={employee.id: "MLT", donor.id: "MLT"},
        catalog_targets=catalog_targets,
        payroll_targets=payroll_targets,
        filled_smooth_seats=set(),
        post_pass_guard=PostPassGuard(frozen_master_cells=set()),
        template_bands={
            "shift-morning": "D",
            "shift-evening": "E",
            "shift-night": "N",
        },
        weeks_in_period=8,
    )

    assert states[employee.id].total_hours <= 64.0 + CATALOG_PERSIST_TRIM_TOLERANCE + 0.01


def test_peer_transfer_blocked_when_recipient_at_payroll_ceiling() -> None:
    donor = EmployeeProfile(
        "mlt-de-08",
        "Vacant MLT D/E - Line 08",
        0.4,
        {"qual-mlt"},
        contract_line_type="D/E",
    )
    recipient = EmployeeProfile(
        "mlt-de-09",
        "Vacant MLT D/E - Line 09",
        0.2,
        {"qual-mlt"},
        contract_line_type="D/E",
    )
    period_start = date(2026, 6, 1)
    period_end = date(2026, 6, 20)
    swap_date = date(2026, 6, 14)
    payroll_targets = {donor.id: 160.0, recipient.id: 64.0}
    catalog_targets = {donor.id: 168.0, recipient.id: 64.0}
    shift_templates = _templates()
    assignments = [
        PlannedAssignment(
            employee_id=donor.id,
            shift_template_id="shift-morning",
            assignment_date=swap_date,
        )
    ]
    states = {
        donor.id: _EmployeeState(profile=donor, target_hours=160.0, total_hours=8.0),
        recipient.id: _EmployeeState(profile=recipient, target_hours=64.0, total_hours=64.0),
    }
    for offset in range(8):
        assignments.append(
            PlannedAssignment(
                employee_id=recipient.id,
                shift_template_id="shift-morning",
                assignment_date=period_start + timedelta(days=offset),
            )
        )
    _rebuild_states_from_assignments(states, assignments, shift_templates)

    transferred = _try_peer_transfer_shift_on_date(
        assignments,
        states,
        donor_id=donor.id,
        recipient_id=recipient.id,
        swap_date=swap_date,
        employees_by_id={donor.id: donor, recipient.id: recipient},
        shift_templates=shift_templates,
        rules=MANITOBA,
        period_start=period_start,
        period_end=period_end,
        availability_blocked=None,
        qual_codes={donor.id: "MLT", recipient.id: "MLT"},
        expected_donor_band="D",
        payroll_targets=payroll_targets,
        catalog_targets=catalog_targets,
    )

    assert not transferred
    assert states[recipient.id].total_hours == 64.0
