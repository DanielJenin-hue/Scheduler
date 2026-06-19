from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from lab_scheduler.audit.compliance import ComplianceValidator, write_conflict_report
from lab_scheduler.compliance import MANITOBA
from lab_scheduler.compliance.engine import ScheduledShift, ShiftTemplateInfo
from lab_scheduler.scheduling.auto_generate import (
    AutoGenerateResult,
    DeterministicScheduleFailure,
    PlannedAssignment,
    auto_generate_schedule,
    validate_generated_schedule,
)
from lab_scheduler.scheduling.auto_pilot import AutoPilotError, run_auto_pilot_full_block
from lab_scheduler.scheduling.profiles import EmployeeProfile
from lab_scheduler.simulation.hospital_stress import shift_required_qualifications, shift_templates
from lab_scheduler.simulation.load_test import (
    build_portage_roster,
    portage_coverage_targets,
    portage_employee_target_hours,
)


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


def test_compliance_validator_passes_clean_small_block(tmp_path: Path) -> None:
    employees = [
        EmployeeProfile("emp-a1", "Avery Miller", 1.0, {"qual-mlt"}),
    ]
    assignments = [
        ScheduledShift(
            "emp-a1",
            "Avery Miller",
            date(2026, 6, 1) + timedelta(weeks=week, days=day),
            "shift-morning",
        )
        for week in range(4)
        for day in range(5)
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
    assert result.pass_rate_pct == 100.0
    assert not result.conflicts


def test_compliance_validator_flags_15h_turnaround(tmp_path: Path) -> None:
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
    turnaround = [c for c in result.provisional_assignments if c.violation_code == "UNION_TURNAROUND_15H"]
    assert turnaround
    assert turnaround[0].violation_label == "15h turnaround violation"
    assert result.passed


def test_compliance_validator_flags_contract_fte_deficit(tmp_path: Path) -> None:
    employees = [EmployeeProfile("emp-a1", "Avery Miller", 1.0, {"qual-mlt"})]
    assignments = [
        ScheduledShift("emp-a1", "Avery Miller", date(2026, 6, 1), "shift-morning"),
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
    )
    contract = [c for c in result.conflicts if c.code == "CONTRACT_FTE_160"]
    assert contract
    assert "160h" in contract[0].manager_label


@pytest.mark.legacy
def test_compliance_validator_flags_weekday_day_shift_capacity(tmp_path: Path) -> None:
    employees = [
        EmployeeProfile(f"emp-{index}", f"Staff {index}", 1.0, {"qual-mlt"})
        for index in range(15)
    ]
    monday = date(2026, 6, 1)
    assignments = [
        ScheduledShift(employee.id, employee.full_name, monday, "shift-morning")
        for employee in employees
    ]
    validator = ComplianceValidator(project_root=tmp_path)
    result = validator.validate(
        rules=MANITOBA,
        employees=employees,
        assignments=assignments,
        shift_templates=_templates(),
        period_start=monday,
        period_end=monday,
        weeks_in_period=8,
        require_contract_fte=False,
        enforce_clinical_floors=False,
        enforce_weekend_limits=False,
    )
    capacity = [c for c in result.conflicts if c.code == "WEEKDAY_DAY_SHIFT_CAPACITY"]
    assert capacity
    assert "14" in capacity[0].message


def test_write_conflict_report_uses_dated_filename(tmp_path: Path) -> None:
    from lab_scheduler.audit.compliance import ComplianceConflict, ComplianceValidationResult

    validation = ComplianceValidationResult(
        passed=False,
        pass_rate_pct=0.0,
        conflicts=[
            ComplianceConflict(
                category="manitoba_union",
                code="UNION_TURNAROUND_15H",
                manager_label="15h turnaround violation",
                message="Example conflict",
            )
        ],
    )
    path = write_conflict_report(
        tmp_path,
        validation,
        period_start=date(2026, 6, 1),
        period_end=date(2026, 6, 28),
        week_count=4,
        report_date=date(2026, 5, 26),
    )
    assert path.name == "Conflict_Report_2026-05-26.json"
    payload = path.read_text(encoding="utf-8")
    assert "15h turnaround violation" in payload
    assert '"pass_rate_pct": 0.0' in payload


def test_validate_generated_schedule_aborts_and_writes_conflict_report(tmp_path: Path) -> None:
    result = AutoGenerateResult(
        assignments=[
            PlannedAssignment("emp-a1", "shift-morning", date(2026, 6, 1)),
        ]
    )
    employees = [EmployeeProfile("emp-a1", "Avery Miller", 1.0, {"qual-mlt"})]
    with pytest.raises(RuntimeError, match="ComplianceValidator"):
        validate_generated_schedule(
            result,
            rules=MANITOBA,
            employees=employees,
            shift_templates=_templates(),
            period_start=date(2026, 6, 1),
            period_end=date(2026, 6, 28),
            weeks_in_period=4,
            master_schedule=True,
            employee_target_hours={"emp-a1": 160.0},
        )
    assert result.conflict_report_path
    assert Path(result.conflict_report_path).is_file()


@pytest.mark.legacy
def test_portage_deterministic_first_blocks_on_contract_fte_gate() -> None:
    employees = build_portage_roster()
    targets = portage_employee_target_hours(
        employees,
        weeks_in_period=4,
        rules=MANITOBA,
    )
    templates = shift_templates()
    with pytest.raises(DeterministicScheduleFailure):
        auto_generate_schedule(
            rules=MANITOBA,
            period_start=date(2026, 6, 1),
            period_end=date(2026, 6, 28),
            weeks_in_period=4,
            employees=employees,
            shift_templates=templates,
            shift_required_qualifications=shift_required_qualifications(),
            employee_target_hours=targets,
            coverage_targets=portage_coverage_targets(employees),
            require_master_compliance=True,
        )


@pytest.mark.legacy
def test_portage_auto_pilot_surfaces_conflict_labels() -> None:
    employees = build_portage_roster()
    try:
        run_auto_pilot_full_block(
            rules=MANITOBA,
            period_start=date(2026, 6, 1),
            period_end=date(2026, 6, 28),
            weeks_in_period=4,
            employees=employees,
            shift_templates=shift_templates(),
            shift_required_qualifications=shift_required_qualifications(),
            employee_target_hours=portage_employee_target_hours(
                employees,
                weeks_in_period=4,
                rules=MANITOBA,
            ),
            coverage_targets=portage_coverage_targets(employees),
            require_master_compliance=True,
        )
    except AutoPilotError as error:
        assert (
            error.conflicts
            or error.conflict_report_path
            or str(error)
        )
