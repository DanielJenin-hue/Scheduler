from datetime import date

from lab_scheduler.audit.compliance import ComplianceConflict, ComplianceValidator
from lab_scheduler.scheduling.provisional_compliance import (
    ProvisionalAssignment,
    is_provisional_labor_violation,
    is_provisional_violation_code,
    partition_provisional_conflicts,
)
from lab_scheduler.compliance import MANITOBA
from lab_scheduler.compliance.engine import ScheduledShift, ShiftTemplateInfo
from lab_scheduler.errors.schedule_error import ScheduleError
from lab_scheduler.scheduling.auto_generate import (
    _build_employee_state,
    _would_violate_labor_rules,
)
from lab_scheduler.scheduling.profiles import EmployeeProfile


def _templates() -> dict[str, ShiftTemplateInfo]:
    return {
        "shift-morning": ShiftTemplateInfo(
            "shift-morning", "MORNING", "Morning", "07:00", "15:00", 480, False
        ),
        "shift-evening": ShiftTemplateInfo(
            "shift-evening", "EVENING", "Evening", "15:00", "23:00", 480, False
        ),
    }


def test_is_provisional_violation_code_includes_stretch_and_turnaround() -> None:
    assert is_provisional_violation_code(ScheduleError.UNION_TURNAROUND_15H.value)
    assert is_provisional_violation_code(ScheduleError.PORTAGE_CONSECUTIVE_DAYS.value)


def test_compliance_validator_flags_turnaround_as_provisional(tmp_path) -> None:
    employees = [EmployeeProfile("emp-a1", "Avery Miller", 1.0, {"qual-mlt"})]
    assignments = [
        ScheduledShift("emp-a1", "Avery Miller", date(2026, 6, 1), "shift-evening"),
        ScheduledShift("emp-a1", "Avery Miller", date(2026, 6, 2), "shift-morning"),
    ]
    validator = ComplianceValidator(project_root=tmp_path)
    result = validator.validate(
        rules=MANITOBA,
        employees=employees,
        assignments=assignments,
        shift_templates=_templates(),
        period_start=date(2026, 6, 1),
        period_end=date(2026, 6, 28),
        weeks_in_period=4,
        require_contract_fte=False,
        enforce_clinical_floors=False,
        enforce_weekend_limits=False,
    )
    assert result.passed
    assert not result.conflicts
    assert result.provisional_assignments
    assert result.provisional_assignments[0].violation_code == ScheduleError.UNION_TURNAROUND_15H.value


def test_would_violate_labor_rules_allows_provisional_turnaround() -> None:
    employee = EmployeeProfile("emp-a1", "Avery Miller", 1.0, {"qual-mlt"})
    templates = _templates()
    existing = [
        ScheduledShift("emp-a1", "Avery Miller", date(2026, 6, 1), "shift-evening"),
    ]
    state = _build_employee_state(
        employee,
        existing,
        templates,
        MANITOBA,
        weeks_in_period=4,
    )
    violation = _would_violate_labor_rules(
        state,
        date(2026, 6, 2),
        templates["shift-morning"],
        templates,
        MANITOBA,
        date(2026, 6, 1),
        date(2026, 6, 28),
        None,
        allow_provisional=True,
    )
    assert violation is None


def test_is_provisional_labor_violation_detects_turnaround_message() -> None:
    assert is_provisional_labor_violation(
        "would violate 15h turnaround (8.0h gap; requires 15h)"
    )


def test_partition_provisional_conflicts_removes_stretch_conflicts() -> None:
    assignment_date = date(2026, 6, 7)
    assignments = [
        ScheduledShift("emp-a1", "Avery Miller", assignment_date, "shift-morning"),
    ]
    conflicts = [
        ComplianceConflict(
            category="portage_fatigue",
            code=ScheduleError.PORTAGE_CONSECUTIVE_DAYS.value,
            manager_label="Consecutive work-day violation",
            message="7 consecutive work days",
            employee_id="emp-a1",
            employee_name="Avery Miller",
            assignment_date=assignment_date,
        )
    ]
    hard, provisional = partition_provisional_conflicts(
        conflicts,
        assignments=assignments,
        shift_templates=_templates(),
    )
    assert not hard
    assert len(provisional) == 1
    assert isinstance(provisional[0], ProvisionalAssignment)
