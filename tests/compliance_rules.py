from datetime import date, datetime

import pytest

from lab_scheduler.compliance import MANITOBA, ShiftTemplateInfo
from lab_scheduler.compliance.compliance_rules import ShiftTransition, check_11_hour_rest
from lab_scheduler.scheduling.auto_generate import (
    EmployeeProfile,
    auto_generate_schedule,
    validate_assignment_change,
)
from lab_scheduler.compliance.engine import ScheduledShift
from portage_fixtures import portage_generate_kwargs


def _dt(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute)


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


def test_evening_to_morning_with_eight_hour_gap_is_blocked() -> None:
    evening = ShiftTransition("EVENING", _dt(date(2026, 6, 1), 15), _dt(date(2026, 6, 1), 23))
    morning = ShiftTransition("MORNING", _dt(date(2026, 6, 2), 7), _dt(date(2026, 6, 2), 15))
    assert check_11_hour_rest(evening, morning) is False


def test_evening_to_morning_with_eleven_hour_gap_is_allowed() -> None:
    evening = ShiftTransition("EVENING", _dt(date(2026, 6, 1), 12), _dt(date(2026, 6, 1), 20))
    morning = ShiftTransition("MORNING", _dt(date(2026, 6, 2), 7), _dt(date(2026, 6, 2), 15))
    assert check_11_hour_rest(evening, morning) is True


def test_night_to_morning_next_day_zero_gap_is_blocked() -> None:
    night = ShiftTransition("NIGHT", _dt(date(2026, 6, 1), 23), _dt(date(2026, 6, 2), 7))
    morning = ShiftTransition("MORNING", _dt(date(2026, 6, 2), 7), _dt(date(2026, 6, 2), 15))
    assert check_11_hour_rest(night, morning) is False


def test_night_to_morning_after_full_rest_day_is_allowed() -> None:
    night = ShiftTransition("NIGHT", _dt(date(2026, 6, 1), 23), _dt(date(2026, 6, 2), 7))
    morning = ShiftTransition("MORNING", _dt(date(2026, 6, 3), 7), _dt(date(2026, 6, 3), 15))
    assert check_11_hour_rest(night, morning) is True


def test_non_morning_transition_is_not_blocked_by_rule() -> None:
    evening = ShiftTransition("EVENING", _dt(date(2026, 6, 1), 15), _dt(date(2026, 6, 1), 23))
    night = ShiftTransition("NIGHT", _dt(date(2026, 6, 1), 23), _dt(date(2026, 6, 2), 7))
    assert check_11_hour_rest(evening, night) is True


def test_auto_pilot_blocks_evening_to_morning_transition_assignment() -> None:
    employee = EmployeeProfile("emp-a1", "Avery Miller", 1.0, {"qual-mlt"})
    templates = _templates()
    required = {
        "shift-morning": {"qual-mlt"},
        "shift-evening": {"qual-mlt"},
        "shift-night": {"qual-mlt"},
    }
    existing = [
        ScheduledShift("emp-a1", "Avery Miller", date(2026, 6, 1), "shift-evening"),
    ]
    violation = validate_assignment_change(
        rules=MANITOBA,
        period_start=date(2026, 6, 1),
        period_end=date(2026, 6, 7),
        weeks_in_period=1,
        employee=employee,
        all_assignments=existing,
        shift_templates=templates,
        shift_required_qualifications=required,
        assignment_date=date(2026, 6, 2),
        new_shift_template_id="shift-morning",
    )
    assert violation is not None
    assert "11h rest before Morning after Evening/Night" in violation


@pytest.mark.legacy
def test_auto_generate_never_schedules_illegal_night_to_morning_back_to_back() -> None:
    kwargs = portage_generate_kwargs(
        period_end=date(2026, 6, 7),
        weeks_in_period=1,
    )
    result = auto_generate_schedule(**kwargs)
    templates = kwargs["shift_templates"]
    by_employee_day: dict[tuple[str, date], str] = {}
    for assignment in result.assignments:
        key = (assignment.employee_id, assignment.assignment_date)
        assert key not in by_employee_day
        by_employee_day[key] = templates[assignment.shift_template_id].code

    for (employee_id, day), shift_code in by_employee_day.items():
        if shift_code != "MORNING":
            continue
        previous = day.fromordinal(day.toordinal() - 1)
        previous_shift = by_employee_day.get((employee_id, previous))
        assert previous_shift not in {"EVENING", "NIGHT"}
