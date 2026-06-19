from datetime import date

from lab_scheduler.compliance import MANITOBA, ShiftTemplateInfo
from lab_scheduler.compliance.engine import ScheduledShift
from lab_scheduler.scheduling.auto_generate import EmployeeProfile
from lab_scheduler.scheduling.sick_call import (
    project_shift_cost,
    rank_emergency_replacements,
)


def _morning_template() -> ShiftTemplateInfo:
    return ShiftTemplateInfo(
        "shift-morning",
        "MORNING",
        "Morning",
        "07:00",
        "15:00",
        480,
        False,
    )


def test_project_shift_cost_regular_time() -> None:
    templates = {"shift-morning": _morning_template()}
    assignments = [
        ScheduledShift(
            employee_id="emp-a",
            employee_name="Avery Miller",
            assignment_date=date(2026, 6, 1),
            shift_template_id="shift-morning",
        ),
    ]
    cost, label = project_shift_cost(
        employee_id="emp-a",
        assignment_date=date(2026, 6, 2),
        shift_hours=8.0,
        assignments=assignments,
        shift_templates=templates,
        rules=MANITOBA,
        hourly_rate=40.0,
    )
    assert label == "Regular Time"
    assert cost == 320.0


def test_project_shift_cost_overtime() -> None:
    templates = {"shift-morning": _morning_template()}
    assignments = [
        ScheduledShift(
            employee_id="emp-a",
            employee_name="Avery Miller",
            assignment_date=date(2026, 6, d),
            shift_template_id="shift-morning",
        )
        for d in range(1, 6)
    ]
    cost, label = project_shift_cost(
        employee_id="emp-a",
        assignment_date=date(2026, 6, 6),
        shift_hours=8.0,
        assignments=assignments,
        shift_templates=templates,
        rules=MANITOBA,
        hourly_rate=40.0,
    )
    assert label == "Overtime"
    assert cost == 480.0


def test_rank_emergency_replacements_prefers_lower_cost() -> None:
    templates = {"shift-morning": _morning_template()}
    qual_codes = {"qual-mlt": "MLT"}
    shift_quals = {"shift-morning": {"qual-mlt"}}

    employees = [
        EmployeeProfile("emp-regular", "Jordan Patel", 1.0, {"qual-mlt"}),
        EmployeeProfile("emp-premium", "Avery Miller", 1.0, {"qual-mlt"}),
        EmployeeProfile("emp-wrong", "Casey Lee", 1.0, {"qual-mla"}),
    ]

    ranked = rank_emergency_replacements(
        rules=MANITOBA,
        period_start=date(2026, 6, 1),
        period_end=date(2026, 6, 7),
        weeks_in_period=1,
        employees=employees,
        all_assignments=[],
        shift_templates=templates,
        shift_required_qualifications=shift_quals,
        slot_date=date(2026, 6, 7),
        shift_template_id="shift-morning",
        qualification_codes=qual_codes,
        employee_hourly_rates={"emp-regular": 40.0, "emp-premium": 50.0},
        exclude_employee_ids={"emp-sick"},
    )

    assert len(ranked) == 2
    assert ranked[0].employee_name == "Jordan Patel"
    assert ranked[0].cost_label == "Regular Time"
    assert ranked[0].projected_cost == 320.0
    assert ranked[1].employee_name == "Avery Miller"
    assert ranked[1].projected_cost == 400.0
