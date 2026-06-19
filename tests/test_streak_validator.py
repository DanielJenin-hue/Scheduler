from datetime import date, timedelta

from lab_scheduler.engine.demand import PORTAGE_MAX_CONSECUTIVE_WORK_DAYS
from lab_scheduler.scheduling.auto_generate import PlannedAssignment
from lab_scheduler.scheduling.profiles import EmployeeProfile
from lab_scheduler.scheduling.streak_validator import (
    is_worked_schedule_cell,
    validate_work_streaks_from_assignments,
    validate_work_streaks_from_schedule_rows,
)
from lab_scheduler.simulation.hospital_stress import QUAL_MLA, shift_templates


def test_is_worked_schedule_cell_is_shift_agnostic() -> None:
    assert is_worked_schedule_cell("D")
    assert is_worked_schedule_cell("E")
    assert is_worked_schedule_cell("N")
    assert is_worked_schedule_cell("M")
    assert not is_worked_schedule_cell("—")
    assert not is_worked_schedule_cell("")


def test_validate_work_streaks_from_schedule_rows_flags_eleven_day_run() -> None:
    start = date(2026, 6, 9)
    dates = [start + timedelta(days=offset) for offset in range(20)]
    row = {
        "Employee": "Vacant MLA D/E - Line 08",
        "employee_id": "portage-mla-08",
    }
    for index, day in enumerate(dates):
        if index < 11:
            row[day.isoformat()] = "E" if index % 3 == 1 else "D"
        else:
            row[day.isoformat()] = "—"

    violations = validate_work_streaks_from_schedule_rows(
        [row],
        employees=[{"id": "portage-mla-08", "full_name": "Vacant MLA D/E - Line 08"}],
        dates=dates,
    )

    assert len(violations) == 1
    assert violations[0].employee_id == "portage-mla-08"
    assert violations[0].length == 11
    assert str(PORTAGE_MAX_CONSECUTIVE_WORK_DAYS) in violations[0].message


def test_validate_work_streaks_from_schedule_rows_allows_six_days() -> None:
    start = date(2026, 6, 1)
    dates = [start + timedelta(days=offset) for offset in range(10)]
    row = {
        "Employee": "Vacant MLA D/E - Line 08",
        "employee_id": "portage-mla-08",
    }
    for index, day in enumerate(dates):
        row[day.isoformat()] = "D" if index < 6 else "—"

    violations = validate_work_streaks_from_schedule_rows(
        [row],
        employees=[{"id": "portage-mla-08", "full_name": "Vacant MLA D/E - Line 08"}],
        dates=dates,
    )

    assert not violations


def test_validate_work_streaks_from_assignments_matches_export_json_case() -> None:
    start = date(2026, 6, 9)
    employee = EmployeeProfile(
        "portage-mla-08",
        "Vacant MLA D/E - Line 08",
        1.0,
        {QUAL_MLA},
        contract_line_type="D/E",
    )
    assignments = [
        PlannedAssignment(
            "portage-mla-08",
            "shift-evening" if offset % 3 == 1 else "shift-morning",
            start + timedelta(days=offset),
        )
        for offset in range(11)
    ]

    violations = validate_work_streaks_from_assignments(
        assignments,
        employees=[employee],
        shift_templates=shift_templates(),
        period_start=start,
        period_end=start + timedelta(days=30),
    )

    assert len(violations) == 1
    assert violations[0].length == 11
