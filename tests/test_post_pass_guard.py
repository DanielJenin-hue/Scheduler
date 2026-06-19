from datetime import date

from lab_scheduler.compliance.engine import ShiftTemplateInfo
from lab_scheduler.scheduling.post_pass_guard import PostPassGuard, should_bypass_post_cpsat_healing
from lab_scheduler.scheduling.profiles import EmployeeProfile


def test_should_bypass_post_cpsat_healing_when_compliance_first() -> None:
    assert should_bypass_post_cpsat_healing(
        coverage_gap_count=99,
        clinical_seats_locked=False,
        compliance_first=True,
    )


def test_should_not_bypass_when_gaps_remain_and_not_compliance_first() -> None:
    assert not should_bypass_post_cpsat_healing(
        coverage_gap_count=5,
        clinical_seats_locked=False,
        compliance_first=False,
    )


def test_post_pass_guard_blocks_frozen_cell() -> None:
    guard = PostPassGuard(frozen_master_cells={( "emp-1", date(2026, 6, 3))})
    templates = {
        "shift-morning": ShiftTemplateInfo(
            "shift-morning", "MORNING", "Morning", "07:00", "15:00", 480, False
        ),
    }
    employee = EmployeeProfile(
        "emp-1",
        "Vacant MLT D/E - Line 01",
        1.0,
        {"qual-mlt"},
        contract_line_type="D/E",
    )
    assert not guard.allows_assignment(
        assignments=[],
        employee_id="emp-1",
        assignment_date=date(2026, 6, 3),
        shift_template_id="shift-morning",
        shift_templates=templates,
        employees=[employee],
        qual_codes={"emp-1": "MLT"},
    )


def test_post_pass_guard_blocks_replace_on_locked_worked_cell() -> None:
    from lab_scheduler.scheduling.auto_generate import PlannedAssignment

    guard = PostPassGuard(
        frozen_master_cells=set(),
        manager_locked_cells={("emp-1", date(2026, 6, 3))},
    )
    templates = {
        "shift-morning": ShiftTemplateInfo(
            "shift-morning", "MORNING", "Morning", "07:00", "15:00", 480, False
        ),
        "shift-evening": ShiftTemplateInfo(
            "shift-evening", "EVENING", "Evening", "15:00", "23:00", 480, False
        ),
    }
    employee = EmployeeProfile(
        "emp-1",
        "Vacant MLT D/E - Line 01",
        1.0,
        {"qual-mlt"},
        contract_line_type="D/E",
    )
    assignments = [
        PlannedAssignment(
            employee_id="emp-1",
            assignment_date=date(2026, 6, 3),
            shift_template_id="shift-morning",
        )
    ]
    assert not guard.allows_assignment(
        assignments=assignments,
        employee_id="emp-1",
        assignment_date=date(2026, 6, 3),
        shift_template_id="shift-evening",
        shift_templates=templates,
        employees=[employee],
        qual_codes={"emp-1": "MLT"},
        replace_existing=True,
    )


def test_post_pass_guard_allows_fill_on_locked_empty_cell() -> None:
    guard = PostPassGuard(
        frozen_master_cells=set(),
        manager_locked_cells={("emp-1", date(2026, 6, 3))},
    )
    templates = {
        "shift-morning": ShiftTemplateInfo(
            "shift-morning", "MORNING", "Morning", "07:00", "15:00", 480, False
        ),
    }
    employee = EmployeeProfile(
        "emp-1",
        "Vacant MLT D/E - Line 01",
        1.0,
        {"qual-mlt"},
        contract_line_type="D/E",
    )
    assert guard.allows_assignment(
        assignments=[],
        employee_id="emp-1",
        assignment_date=date(2026, 6, 3),
        shift_template_id="shift-morning",
        shift_templates=templates,
        employees=[employee],
        qual_codes={"emp-1": "MLT"},
        replace_existing=False,
    )
