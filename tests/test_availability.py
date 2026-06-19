from datetime import date

from lab_scheduler.availability import (
    AvailabilityException,
    adjusted_target_hours,
    expand_blocked_dates,
    reason_to_off_code,
)
from lab_scheduler.compliance import MANITOBA
from lab_scheduler.scheduling.auto_generate import EmployeeProfile, auto_generate_schedule
from portage_fixtures import portage_generate_kwargs


def test_adjusted_target_hours_one_week_off() -> None:
    blocked = {date(2026, 6, d) for d in range(8, 15)}
    target = adjusted_target_hours(
        rules=MANITOBA,
        fte=1.0,
        period_start=date(2026, 6, 1),
        period_end=date(2026, 6, 28),
        weeks_in_period=4,
        blocked_dates=blocked,
    )
    assert target == 120.0


def test_reason_to_off_code_mapping() -> None:
    assert reason_to_off_code("Vacation") == "V"
    assert reason_to_off_code("Sick Leave") == "I"


def test_expand_blocked_dates_respects_period_window() -> None:
    exc = AvailabilityException(
        id="x1",
        tenant_id="t1",
        employee_id="emp-a1",
        start_date=date(2026, 6, 8),
        end_date=date(2026, 6, 10),
        reason="Vacation",
    )
    blocked = expand_blocked_dates(
        [exc],
        period_start=date(2026, 6, 1),
        period_end=date(2026, 6, 28),
    )
    assert len(blocked["emp-a1"]) == 3


def test_auto_generate_skips_blocked_employee_dates() -> None:
    kwargs = portage_generate_kwargs(
        period_end=date(2026, 6, 7),
        weeks_in_period=1,
    )
    employee = kwargs["employees"][0]
    blocked = {employee.id: {date(2026, 6, 1)}}
    result = auto_generate_schedule(**kwargs, availability_blocked=blocked)
    assert all(
        assignment.employee_id != employee.id or assignment.assignment_date != date(2026, 6, 1)
        for assignment in result.assignments
    )
