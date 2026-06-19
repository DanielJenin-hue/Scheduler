from datetime import date, timedelta

from lab_scheduler.compliance import MANITOBA
from lab_scheduler.compliance.engine import ScheduledShift
from lab_scheduler.engine.demand import (
    TRANSITION_BURNOUT_WARNING,
    asymmetric_shift_transition_violation,
    find_day_night_transition_violations,
    find_night_day_transition_violations,
    is_transition_burnout_violation,
)
from lab_scheduler.scheduling.auto_generate import (
    auto_generate_schedule,
    validate_assignment_change,
)
from lab_scheduler.scheduling.profiles import EmployeeProfile
from lab_scheduler.simulation.hospital_stress import (
    QUAL_MLT,
    shift_required_qualifications,
    shift_templates,
)
from lab_scheduler.simulation.load_test import (
    build_portage_roster,
    portage_coverage_targets,
    portage_employee_target_hours,
    run_portage_load_test,
)


def test_asymmetric_transition_blocks_day_then_night() -> None:
    day_before = date(2026, 6, 10)
    night_day = date(2026, 6, 11)
    templates = shift_templates()
    violation = asymmetric_shift_transition_violation(
        [(day_before, "shift-morning")],
        night_day,
        "NIGHT",
        templates,
    )
    assert violation is not None
    assert TRANSITION_BURNOUT_WARNING in violation


def test_asymmetric_transition_blocks_night_then_day() -> None:
    night_before = date(2026, 6, 10)
    day_after = date(2026, 6, 11)
    templates = shift_templates()
    violation = asymmetric_shift_transition_violation(
        [(night_before, "shift-night")],
        day_after,
        "MORNING",
        templates,
    )
    assert violation is not None
    assert TRANSITION_BURNOUT_WARNING in violation


def test_asymmetric_transition_blocks_day_when_next_day_is_night() -> None:
    day = date(2026, 6, 12)
    night_next = date(2026, 6, 13)
    templates = shift_templates()
    violation = asymmetric_shift_transition_violation(
        [(night_next, "shift-night")],
        day,
        "MORNING",
        templates,
    )
    assert violation is not None
    assert is_transition_burnout_violation(violation)


def test_validate_assignment_change_blocks_drag_night_after_day() -> None:
    employee = EmployeeProfile(
        "emp-dn-01",
        "MLT 01 (1.0 D/N)",
        1.0,
        {QUAL_MLT},
        contract_line_type="D/N",
    )
    day = date(2026, 6, 8)
    night = day + timedelta(days=1)
    scheduled = [
        ScheduledShift(
            employee_id=employee.id,
            employee_name=employee.full_name,
            assignment_date=day,
            shift_template_id="shift-morning",
        )
    ]
    violation = validate_assignment_change(
        rules=MANITOBA,
        period_start=date(2026, 6, 1),
        period_end=date(2026, 6, 28),
        weeks_in_period=4,
        employee=employee,
        all_assignments=scheduled,
        shift_templates=shift_templates(),
        shift_required_qualifications=shift_required_qualifications(),
        assignment_date=night,
        new_shift_template_id="shift-night",
        enforce_fte_target=False,
    )
    assert violation is not None
    assert TRANSITION_BURNOUT_WARNING in violation


def test_validate_assignment_change_blocks_day_after_night() -> None:
    """Night followed by Day on the next calendar day is forbidden."""

    employee = EmployeeProfile(
        "emp-dn-02",
        "MLT 02 (1.0 D/N)",
        1.0,
        {QUAL_MLT},
        contract_line_type="D/N",
    )
    night = date(2026, 6, 9)
    day = night + timedelta(days=1)
    scheduled = [
        ScheduledShift(
            employee_id=employee.id,
            employee_name=employee.full_name,
            assignment_date=night,
            shift_template_id="shift-night",
        )
    ]
    violation = validate_assignment_change(
        rules=MANITOBA,
        period_start=date(2026, 6, 1),
        period_end=date(2026, 6, 28),
        weeks_in_period=4,
        employee=employee,
        all_assignments=scheduled,
        shift_templates=shift_templates(),
        shift_required_qualifications=shift_required_qualifications(),
        assignment_date=day,
        new_shift_template_id="shift-morning",
        enforce_fte_target=False,
    )
    assert violation is not None
    assert TRANSITION_BURNOUT_WARNING in violation


def test_portage_load_simulation_has_no_day_night_transition_violations() -> None:
    roster = build_portage_roster()
    targets = portage_coverage_targets(roster)
    target_hours = portage_employee_target_hours(
        roster,
        weeks_in_period=4,
        rules=MANITOBA,
    )
    result = auto_generate_schedule(
        rules=MANITOBA,
        period_start=date(2026, 6, 1),
        period_end=date(2026, 6, 28),
        weeks_in_period=4,
        employees=roster,
        shift_templates=shift_templates(),
        shift_required_qualifications=shift_required_qualifications(),
        employee_target_hours=target_hours,
        coverage_targets=targets,
        strict_complete_block=False,
    )
    templates = shift_templates()
    scan_rows = [
        (assignment.employee_id, assignment.assignment_date, assignment.shift_template_id)
        for assignment in result.assignments
    ]
    violations = find_day_night_transition_violations(scan_rows, templates)
    assert violations == []

    hours_by_employee: dict[str, float] = {employee.id: 0.0 for employee in roster}
    for assignment in result.assignments:
        template = templates[assignment.shift_template_id]
        hours_by_employee[assignment.employee_id] += template.duration_minutes / 60.0
    worked = [hours for hours in hours_by_employee.values() if hours > 0.01]
    if len(worked) >= 2:
        assert max(worked) - min(worked) < 160.0
