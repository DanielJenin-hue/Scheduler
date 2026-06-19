from datetime import date, datetime, timedelta

from lab_scheduler.policy.union_rules_portage import (
    UNION_RULES_PORTAGE,
    is_in_weekend_rest_window,
    is_portage_weekend,
    portage_weekend_window_end,
    portage_weekend_window_start,
    shift_target_for_portage_date,
)


def test_is_portage_weekend() -> None:
    assert is_portage_weekend(date(2026, 6, 6)) is True  # Saturday
    assert is_portage_weekend(date(2026, 6, 7)) is True  # Sunday
    assert is_portage_weekend(date(2026, 6, 8)) is False  # Monday


def test_weekend_window_boundaries() -> None:
    saturday = date(2026, 6, 6)
    start = portage_weekend_window_start(saturday)
    end = portage_weekend_window_end(saturday)
    assert start == datetime(2026, 6, 6, 0, 1)
    assert end == datetime(2026, 6, 8, 0, 0)

    assert is_in_weekend_rest_window(datetime(2026, 6, 6, 0, 1)) is True
    assert is_in_weekend_rest_window(datetime(2026, 6, 7, 23, 59)) is True
    assert is_in_weekend_rest_window(datetime(2026, 6, 8, 0, 0)) is False
    assert is_in_weekend_rest_window(datetime(2026, 6, 5, 23, 59)) is False


def test_shift_target_for_portage_date_weekday_vs_weekend() -> None:
    monday = date(2026, 6, 1)
    saturday = date(2026, 6, 6)
    assert shift_target_for_portage_date(monday, "D") == 16
    assert shift_target_for_portage_date(monday, "E") == 2
    assert shift_target_for_portage_date(saturday, "D") == 2
    assert shift_target_for_portage_date(saturday, "N") == 2


def test_union_rules_portage_constants() -> None:
    assert UNION_RULES_PORTAGE.hours_per_shift == 8.0
    assert UNION_RULES_PORTAGE.biweekly_normal_hours == 80.0
    assert UNION_RULES_PORTAGE.allow_autonomous_contract_line_changes is False
