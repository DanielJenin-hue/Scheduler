from datetime import date, datetime

from lab_scheduler.time import workweek_for


def test_workweek_starts_on_monday_for_monday_date() -> None:
    ww = workweek_for(date(2026, 5, 25))  # Monday
    assert ww.start == date(2026, 5, 25)
    assert ww.end_exclusive == date(2026, 6, 1)


def test_workweek_starts_on_previous_monday_for_sunday_date() -> None:
    ww = workweek_for(date(2026, 5, 31))  # Sunday
    assert ww.start == date(2026, 5, 25)
    assert ww.end_exclusive == date(2026, 6, 1)


def test_workweek_accepts_datetime() -> None:
    ww = workweek_for(datetime(2026, 5, 31, 23, 59, 59))
    assert ww.start == date(2026, 5, 25)

