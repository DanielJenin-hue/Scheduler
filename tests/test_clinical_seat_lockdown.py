
import pytest

pytestmark = pytest.mark.legacy

from datetime import date
import sys

from lab_scheduler.compliance import MANITOBA
from lab_scheduler.engine.demand import (
    expand_schedule_slots,
    portage_concurrent_demands,
)
from lab_scheduler.scheduling.auto_generate import (
    EmployeeProfile,
    _EmployeeState,
    _run_clinical_seat_lockdown_pass,
    _run_coverage_aggressor_protocol,
    AutoGenerateResult,
    _seat_fill_counts,
)
from lab_scheduler.scheduling.clinical_seats import (
    clinical_seat_label,
    clinical_seat_number,
    evening_night_clinical_seat_slots,
    evening_night_clinical_seats_satisfied,
)
from lab_scheduler.simulation.hospital_stress import (
    QUAL_MLA,
    QUAL_MLT,
    shift_required_qualifications,
    shift_templates,
)


def test_evening_night_demand_uses_explicit_seat_objects() -> None:
    slots = expand_schedule_slots(
        period_start=date(2026, 6, 1),
        period_end=date(2026, 6, 1),
        shift_templates=shift_templates(),
        concurrent_demands=portage_concurrent_demands(),
    )
    templates = shift_templates()
    evening = [
        slot
        for slot in slots
        if templates[slot.shift_id].code == "EVENING"
        and slot.assignment_date == date(2026, 6, 1)
    ]
    night = [
        slot
        for slot in slots
        if templates[slot.shift_id].code == "NIGHT"
        and slot.assignment_date == date(2026, 6, 1)
    ]
    assert len(evening) == 2
    assert len(night) == 2
    assert {clinical_seat_label(slot.role_pool_id) for slot in evening} == {
        "Seat_01",
        "Seat_02",
    }
    assert {clinical_seat_number(slot.role_pool_id) for slot in evening} == {1, 2}


def test_clinical_lockdown_pass_fills_seat_one_before_seat_two_by_hours() -> None:
    period_day = date(2026, 6, 1)
    templates = shift_templates()
    expanded = expand_schedule_slots(
        period_start=period_day,
        period_end=period_day,
        shift_templates=templates,
        concurrent_demands=portage_concurrent_demands(),
    )
    employees = [
        EmployeeProfile("mlt-busy", "Busy MLT", 1.0, {QUAL_MLT}, contract_line_type="D/E"),
        EmployeeProfile("mlt-free", "Free MLT", 1.0, {QUAL_MLT}, contract_line_type="D/E"),
        EmployeeProfile("mla-free", "Free MLA", 1.0, {QUAL_MLA}, contract_line_type="D/E"),
    ]
    qual_codes = {QUAL_MLT: "MLT", QUAL_MLA: "MLA"}
    states = {
        "mlt-busy": _EmployeeState(profile=employees[0], target_hours=160.0, total_hours=80.0),
        "mlt-free": _EmployeeState(profile=employees[1], target_hours=160.0, total_hours=0.0),
        "mla-free": _EmployeeState(profile=employees[2], target_hours=160.0, total_hours=0.0),
    }
    fill_counts: dict = {}
    planned = _run_clinical_seat_lockdown_pass(
        employees=employees,
        states=states,
        expanded_slots=expanded,
        shift_templates=templates,
        shift_required_qualifications=shift_required_qualifications(),
        rules=MANITOBA,
        fill_counts=fill_counts,
        availability_blocked=None,
        qual_codes=qual_codes,
        log_critical_gaps=False,
    )
    evening_seats = [
        slot
        for slot in evening_night_clinical_seat_slots(expanded, shift_templates=templates)
        if templates[slot.shift_id].code == "EVENING"
    ]
    evening_assignments = [
        assignment
        for assignment in planned
        if templates[assignment.shift_template_id].code == "EVENING"
    ]
    assert len(evening_assignments) == 2
    seat_one = next(
        slot for slot in evening_seats if clinical_seat_number(slot.role_pool_id) == 1
    )
    seat_one_assignment = next(
        assignment
        for assignment in evening_assignments
        if assignment.shift_template_id == seat_one.shift_id
        and assignment.assignment_date == seat_one.assignment_date
    )
    assert seat_one_assignment.employee_id == "mlt-free"


def test_clinical_lockdown_pass_survives_broken_stdout(monkeypatch) -> None:
    class _BrokenStdout:
        def write(self, _text: str) -> int:
            raise OSError(22, "Invalid argument")

        def flush(self) -> None:
            raise OSError(22, "Invalid argument")

    monkeypatch.setattr(sys, "stdout", _BrokenStdout())

    period_day = date(2026, 6, 1)
    templates = shift_templates()
    expanded = expand_schedule_slots(
        period_start=period_day,
        period_end=period_day,
        shift_templates=templates,
        concurrent_demands=portage_concurrent_demands(),
    )
    employees = [
        EmployeeProfile("mlt-free", "Free MLT", 1.0, {QUAL_MLT}, contract_line_type="D/E"),
        EmployeeProfile("mla-free", "Free MLA", 1.0, {QUAL_MLA}, contract_line_type="D/E"),
    ]
    qual_codes = {QUAL_MLT: "MLT", QUAL_MLA: "MLA"}
    states = {
        "mlt-free": _EmployeeState(profile=employees[0], target_hours=160.0, total_hours=0.0),
        "mla-free": _EmployeeState(profile=employees[1], target_hours=160.0, total_hours=0.0),
    }
    _run_clinical_seat_lockdown_pass(
        employees=employees,
        states=states,
        expanded_slots=expanded,
        shift_templates=templates,
        shift_required_qualifications=shift_required_qualifications(),
        rules=MANITOBA,
        fill_counts={},
        availability_blocked=None,
        qual_codes=qual_codes,
        log_critical_gaps=False,
    )


def test_aggressor_skips_contract_and_even_phases_when_clinical_seats_open() -> None:
    period_day = date(2026, 6, 7)
    templates = shift_templates()
    expanded = expand_schedule_slots(
        period_start=period_day,
        period_end=period_day,
        shift_templates=templates,
        concurrent_demands=portage_concurrent_demands(),
    )
    employees = [
        EmployeeProfile("mlt-only", "Only MLT", 1.0, {QUAL_MLT}, contract_line_type="D/E"),
    ]
    qual_codes = {QUAL_MLT: "MLT"}
    states = {
        "mlt-only": _EmployeeState(profile=employees[0], target_hours=160.0, total_hours=0.0),
    }
    fill_counts: dict = {}
    result = AutoGenerateResult()
    added = _run_coverage_aggressor_protocol(
        result,
        employees=employees,
        states=states,
        expanded_slots=expanded,
        shift_templates=templates,
        shift_required_qualifications=shift_required_qualifications(),
        rules=MANITOBA,
        availability_blocked=None,
        qual_codes=qual_codes,
        fill_counts=fill_counts,
        filled_smooth_seats=set(),
        weeks_in_period=4,
        period_start=period_day,
        period_end=period_day,
        allow_contract_and_even_phases=False,
    )
    assert added >= 0
    assert not evening_night_clinical_seats_satisfied(
        fill_counts=fill_counts,
        expanded_slots=expanded,
        shift_templates=templates,
        period_start=period_day,
        period_end=period_day,
    )
    fill_counts_after = _seat_fill_counts(result.assignments, employees, qual_codes)
    assert fill_counts_after == fill_counts or len(result.assignments) == added
