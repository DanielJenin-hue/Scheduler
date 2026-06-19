from datetime import date, timedelta

from lab_scheduler.compliance import (
    MANITOBA,
    ONTARIO,
    ScheduledShift,
    ShiftTemplateInfo,
    evaluate_schedule,
)


def _morning() -> ShiftTemplateInfo:
    return ShiftTemplateInfo(
        id="shift-morning",
        code="MORNING",
        name="Morning",
        start_time="07:00",
        end_time="15:00",
        duration_minutes=480,
        crosses_midnight=False,
    )


def test_manitoba_flags_daily_overtime() -> None:
    templates = {"shift-morning": _morning()}
    assignments = [
        ScheduledShift("e1", "Avery", date(2026, 6, 1), "shift-morning"),
        ScheduledShift("e1", "Avery", date(2026, 6, 1), "shift-morning"),  # won't happen in DB; test 10h via long shift
    ]
    long_day = ShiftTemplateInfo(
        id="long",
        code="LONG",
        name="Long",
        start_time="06:00",
        end_time="16:00",
        duration_minutes=600,
        crosses_midnight=False,
    )
    templates["long"] = long_day
    assignments = [ScheduledShift("e1", "Avery", date(2026, 6, 1), "long")]

    report = evaluate_schedule(
        MANITOBA,
        employees=[{"id": "e1", "full_name": "Avery", "fte": 1.0}],
        assignments=assignments,
        shift_templates=templates,
        period_start=date(2026, 6, 1),
        period_end=date(2026, 6, 28),
        weeks_in_period=4,
    )
    codes = {v.code for v in report.violations}
    assert "DAILY_OVERTIME" in codes


def test_ontario_has_no_daily_overtime_trigger() -> None:
    long_day = ShiftTemplateInfo(
        id="long",
        code="LONG",
        name="Long",
        start_time="06:00",
        end_time="16:00",
        duration_minutes=600,
        crosses_midnight=False,
    )
    templates = {"long": long_day}
    assignments = [ScheduledShift("e1", "Avery", date(2026, 6, 1), "long")]

    report = evaluate_schedule(
        ONTARIO,
        employees=[{"id": "e1", "full_name": "Avery", "fte": 1.0}],
        assignments=assignments,
        shift_templates=templates,
        period_start=date(2026, 6, 1),
        period_end=date(2026, 6, 28),
        weeks_in_period=4,
    )
    assert "DAILY_OVERTIME" not in {v.code for v in report.violations}


def test_ontario_weekly_overtime_threshold_is_44() -> None:
    templates = {"shift-morning": _morning()}
    # Five 8h shifts = 40h — no weekly OT in Ontario.
    assignments = [
        ScheduledShift("e1", "Avery", date(2026, 6, 1) + timedelta(days=i), "shift-morning")
        for i in range(5)
    ]
    report_ok = evaluate_schedule(
        ONTARIO,
        employees=[{"id": "e1", "full_name": "Avery", "fte": 1.0}],
        assignments=assignments,
        shift_templates=templates,
        period_start=date(2026, 6, 1),
        period_end=date(2026, 6, 28),
        weeks_in_period=4,
    )
    assert "WEEKLY_OVERTIME" not in {v.code for v in report_ok.violations}

    # Six 8h shifts = 48h — 4h weekly OT in Ontario, but not in Manitoba (under 40? 48>40 so MB would also flag)
    assignments_six = [
        ScheduledShift("e1", "Avery", date(2026, 6, 1) + timedelta(days=i), "shift-morning")
        for i in range(6)
    ]
    report_on = evaluate_schedule(
        ONTARIO,
        employees=[{"id": "e1", "full_name": "Avery", "fte": 1.0}],
        assignments=assignments_six,
        shift_templates=templates,
        period_start=date(2026, 6, 1),
        period_end=date(2026, 6, 28),
        weeks_in_period=4,
    )
    weekly = [v for v in report_on.violations if v.code == "WEEKLY_OVERTIME"]
    assert weekly
    assert "44" in weekly[0].message
