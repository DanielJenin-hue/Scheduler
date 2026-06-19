from datetime import date

from lab_scheduler.compliance import MANITOBA
from lab_scheduler.compliance.compliance_rules import (
    APPROVED_STRETCH_CODE,
    JOANNE_STYLE_STRETCH_CODE,
    ShiftTransition,
    clinical_floor_stretch_allowed,
)
from lab_scheduler.compliance.engine import ScheduledShift
from lab_scheduler.audit.compliance import ComplianceValidator
from lab_scheduler.scheduling.auto_generate import (
    PlannedAssignment,
    _clinical_floor_stretch_for_assignment,
    _EmployeeState,
    validate_assignment_change,
)
from lab_scheduler.scheduling.coverage_aggressor import (
    collect_clinical_stretch_flags,
    format_aggressive_fill_flags_html,
)
from lab_scheduler.scheduling.profiles import EmployeeProfile
from lab_scheduler.engine.demand import fatigue_guardrail_violation
from lab_scheduler.simulation.hospital_stress import (
    QUAL_MLT,
    shift_required_qualifications,
    shift_templates,
)


def test_modified_work_schedule_allows_seventh_consecutive_day() -> None:
    start = date(2026, 6, 1)
    work_dates = {start + __import__("datetime").timedelta(days=offset) for offset in range(6)}
    seventh = start + __import__("datetime").timedelta(days=6)
    assert fatigue_guardrail_violation(work_dates, seventh) is not None
    assert (
        fatigue_guardrail_violation(
            work_dates,
            seventh,
            modified_work_schedule=True,
            max_consecutive_work_days=12,
        )
        is None
    )


def test_clinical_floor_stretch_allows_evening_to_morning_within_24h() -> None:
    evening = ShiftTransition(
        code="EVENING",
        start=__import__("datetime").datetime(2026, 6, 1, 15, 0),
        end=__import__("datetime").datetime(2026, 6, 1, 23, 0),
    )
    morning = ShiftTransition(
        code="MORNING",
        start=__import__("datetime").datetime(2026, 6, 2, 7, 0),
        end=__import__("datetime").datetime(2026, 6, 2, 15, 0),
    )
    assert clinical_floor_stretch_allowed(evening, morning) is True


def test_compliance_validator_skips_clinical_floor_stretch_turnaround(tmp_path) -> None:
    assignments = [
        ScheduledShift(
            "emp-a1",
            "Avery Miller",
            date(2026, 6, 1),
            "shift-evening",
        ),
        ScheduledShift(
            "emp-a1",
            "Avery Miller",
            date(2026, 6, 2),
            "shift-morning",
            clinical_floor_stretch=True,
        ),
    ]
    validator = ComplianceValidator(project_root=tmp_path)
    result = validator.validate(
        rules=MANITOBA,
        employees=[
            EmployeeProfile("emp-a1", "Avery Miller", 1.0, {QUAL_MLT}),
        ],
        assignments=assignments,
        shift_templates=shift_templates(),
        period_start=date(2026, 6, 1),
        period_end=date(2026, 6, 28),
        weeks_in_period=4,
        require_contract_fte=False,
    )
    turnaround = [c for c in result.conflicts if c.code == "UNION_TURNAROUND_15H"]
    assert turnaround == []


def test_compliance_validator_skips_approved_stretch_turnaround(tmp_path) -> None:
    assignments = [
        ScheduledShift(
            "emp-a1",
            "Avery Miller",
            date(2026, 6, 1),
            "shift-evening",
        ),
        ScheduledShift(
            "emp-a1",
            "Avery Miller",
            date(2026, 6, 2),
            "shift-morning",
            approved_stretch=True,
        ),
    ]
    validator = ComplianceValidator(project_root=tmp_path)
    result = validator.validate(
        rules=MANITOBA,
        employees=[
            EmployeeProfile("emp-a1", "Avery Miller", 1.0, {QUAL_MLT}),
        ],
        assignments=assignments,
        shift_templates=shift_templates(),
        period_start=date(2026, 6, 1),
        period_end=date(2026, 6, 28),
        weeks_in_period=4,
        require_contract_fte=False,
    )
    turnaround = [c for c in result.conflicts if c.code == "UNION_TURNAROUND_15H"]
    assert turnaround == []


def test_validate_assignment_change_respects_approved_stretch() -> None:
    employee = EmployeeProfile("emp-a1", "Avery Miller", 1.0, {QUAL_MLT}, contract_line_type="D/E")
    scheduled = [
        ScheduledShift("emp-a1", "Avery Miller", date(2026, 6, 1), "shift-evening"),
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
        assignment_date=date(2026, 6, 2),
        new_shift_template_id="shift-morning",
        enforce_fte_target=False,
        approved_stretch=True,
    )
    assert violation is None


def test_collect_clinical_stretch_flags_labels_joanne_style() -> None:
    employee = EmployeeProfile("emp-a1", "Avery Miller", 1.0, {QUAL_MLT})
    flags = collect_clinical_stretch_flags(
        [
            PlannedAssignment(
                "emp-a1",
                "shift-morning",
                date(2026, 6, 2),
                clinical_floor_stretch=True,
            )
        ],
        employees=[employee],
        shift_templates=shift_templates(),
    )
    assert len(flags) == 1
    assert flags[0].code == JOANNE_STYLE_STRETCH_CODE
    assert flags[0].stretch_type == "joanne_style"
    html = format_aggressive_fill_flags_html(flags)
    assert "Joanne-Style Extended Shifts" in html


def test_detect_clinical_floor_stretch_on_state() -> None:
    employee = EmployeeProfile("emp-a1", "Avery Miller", 1.0, {QUAL_MLT}, contract_line_type="D/E")
    templates = shift_templates()
    state = _EmployeeState(profile=employee, target_hours=160.0)
    state.work_dates.add(date(2026, 6, 1))
    state.assignment_records.append((date(2026, 6, 1), "shift-evening"))
    assert _clinical_floor_stretch_for_assignment(
        state,
        date(2026, 6, 2),
        templates["shift-morning"],
        templates,
    )


def test_annotate_clinical_floor_stretches_tags_template_morning_after_evening() -> None:
    from lab_scheduler.scheduling.auto_generate import _annotate_clinical_floor_stretches

    assignments = [
        PlannedAssignment("emp-a1", "shift-evening", date(2026, 6, 1)),
        PlannedAssignment("emp-a1", "shift-morning", date(2026, 6, 2)),
    ]
    annotated = _annotate_clinical_floor_stretches(assignments, shift_templates())
    assert annotated[0].clinical_floor_stretch is False
    assert annotated[1].clinical_floor_stretch is True
