
import pytest

pytestmark = pytest.mark.legacy

from datetime import date

from lab_scheduler.compliance import MANITOBA
from lab_scheduler.scheduling.auto_generate import (
    PlannedAssignment,
    _EmployeeState,
    _apply_portage_weekend_pairing_policy,
    _employee_assignment_on_date,
    _portage_split_weekend_orphan_count,
    _rebuild_states_from_assignments,
)
from lab_scheduler.scheduling.profiles import EmployeeProfile
from lab_scheduler.simulation.hospital_stress import shift_templates


def test_weekend_pairing_mirrors_saturday_evening_to_sunday() -> None:
    saturday = date(2026, 6, 6)
    sunday = date(2026, 6, 7)
    employee = EmployeeProfile(
        id="emp-de",
        full_name="Test DE",
        fte=1.0,
        qualification_ids={"qual-mlt"},
        contract_line_type="D/E",
    )
    employees = [employee]
    templates = shift_templates()
    assignments = [PlannedAssignment("emp-de", "shift-evening", saturday)]
    states = {
        employee.id: _EmployeeState(profile=employee, target_hours=320.0),
    }
    _rebuild_states_from_assignments(states, assignments, templates)

    _apply_portage_weekend_pairing_policy(
        assignments,
        states,
        employees=employees,
        shift_templates=templates,
        rules=MANITOBA,
        period_start=saturday,
        period_end=sunday,
        availability_blocked={},
        qual_codes={"qual-mlt": "MLT"},
    )

    assert (
        _portage_split_weekend_orphan_count(
            assignments,
            employees=employees,
            period_start=saturday,
            period_end=sunday,
        )
        == 0
    )
    sunday_assignment = _employee_assignment_on_date(
        assignments,
        employee_id=employee.id,
        assignment_date=sunday,
    )
    assert sunday_assignment is not None
    assert sunday_assignment.shift_template_id == "shift-evening"


def test_weekend_pairing_consolidates_cross_person_sat_sun_split() -> None:
    saturday = date(2026, 6, 6)
    sunday = date(2026, 6, 7)
    sat_worker = EmployeeProfile(
        id="emp-sat",
        full_name="Sat Worker",
        fte=1.0,
        qualification_ids={"qual-mlt"},
        contract_line_type="D/E",
    )
    sun_worker = EmployeeProfile(
        id="emp-sun",
        full_name="Sun Worker",
        fte=1.0,
        qualification_ids={"qual-mla"},
        contract_line_type="D/E",
    )
    employees = [sat_worker, sun_worker]
    templates = shift_templates()
    assignments = [
        PlannedAssignment("emp-sat", "shift-evening", saturday),
        PlannedAssignment("emp-sun", "shift-evening", sunday),
    ]
    states = {
        sat_worker.id: _EmployeeState(profile=sat_worker, target_hours=320.0),
        sun_worker.id: _EmployeeState(profile=sun_worker, target_hours=320.0),
    }
    _rebuild_states_from_assignments(states, assignments, templates)

    _apply_portage_weekend_pairing_policy(
        assignments,
        states,
        employees=employees,
        shift_templates=templates,
        rules=MANITOBA,
        period_start=saturday,
        period_end=sunday,
        availability_blocked={},
        qual_codes={"qual-mlt": "MLT", "qual-mla": "MLA"},
    )

    assert (
        _portage_split_weekend_orphan_count(
            assignments,
            employees=employees,
            period_start=saturday,
            period_end=sunday,
        )
        == 0
    )
    assert (
        _employee_assignment_on_date(
            assignments,
            employee_id=sat_worker.id,
            assignment_date=saturday,
        )
        is not None
    )
    assert (
        _employee_assignment_on_date(
            assignments,
            employee_id=sat_worker.id,
            assignment_date=sunday,
        )
        is not None
    )


def test_trim_dn_off_catalog_weekend_day_shift() -> None:
    from lab_scheduler.scheduling.auto_generate import _trim_dn_off_catalog_weekend_shifts

    saturday = date(2026, 6, 6)
    employee = EmployeeProfile(
        id="mla-dn-02",
        full_name="Vacant MLA D/N - Line 02",
        fte=0.8,
        qualification_ids={"qual-mla"},
        contract_line_type="D/N",
    )
    templates = shift_templates()
    assignments = [PlannedAssignment("mla-dn-02", "shift-morning", saturday)]
    states = {
        employee.id: _EmployeeState(profile=employee, target_hours=248.0),
    }
    _rebuild_states_from_assignments(states, assignments, templates)

    removed = _trim_dn_off_catalog_weekend_shifts(
        assignments,
        states,
        employees=[employee],
        shift_templates=templates,
        period_start=saturday,
        period_end=saturday,
    )

    assert removed == 1
    assert assignments == []


def test_trim_vacant_weekend_to_pool_scaled_target() -> None:
    from lab_scheduler.scheduling.auto_generate import (
        _peer_shift_metrics,
        _trim_portage_vacant_weekend_to_target,
    )

    saturday = date(2026, 6, 6)
    sunday = date(2026, 6, 7)
    employee = EmployeeProfile(
        id="mla-dn-02",
        full_name="Vacant MLA D/N - Line 02",
        fte=0.8,
        qualification_ids={"qual-mla"},
        contract_line_type="D/N",
    )
    templates = shift_templates()
    assignments = [
        PlannedAssignment("mla-dn-02", "shift-night", saturday),
        PlannedAssignment("mla-dn-02", "shift-night", sunday),
        PlannedAssignment("mla-dn-02", "shift-night", date(2026, 6, 13)),
        PlannedAssignment("mla-dn-02", "shift-night", date(2026, 6, 14)),
        PlannedAssignment("mla-dn-02", "shift-night", date(2026, 6, 27)),
        PlannedAssignment("mla-dn-02", "shift-night", date(2026, 6, 28)),
    ]
    states = {
        employee.id: _EmployeeState(profile=employee, target_hours=248.0),
    }
    _rebuild_states_from_assignments(states, assignments, templates)
    catalog_targets = {employee.id: 248.0}
    qual_codes = {"qual-mla": "MLA"}

    removed = _trim_portage_vacant_weekend_to_target(
        assignments,
        states,
        employees=[employee],
        shift_templates=templates,
        catalog_targets=catalog_targets,
        qual_codes=qual_codes,
        period_start=date(2026, 6, 1),
        period_end=date(2026, 7, 26),
    )

    assert removed >= 2
    _, _, weekend_count = _peer_shift_metrics(
        employee.id,
        assignments,
        templates,
        employee.contract_line_type,
        date(2026, 6, 1),
        date(2026, 7, 26),
    )
    assert weekend_count <= 4
