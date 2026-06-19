from datetime import date, timedelta

from lab_scheduler.compliance import MANITOBA, ONTARIO, ScheduledShift, ShiftTemplateInfo
from lab_scheduler.finance.forecast import build_full_forecast, compute_labor_forecast
from lab_scheduler.scheduling.auto_generate import EmployeeProfile


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


def _employees() -> list[EmployeeProfile]:
    return [
        EmployeeProfile("emp-a1", "Avery Miller", 1.0, {"qual-mlt"}),
        EmployeeProfile("emp-b1", "Jordan Patel", 0.8, {"qual-mlt"}),
        EmployeeProfile("emp-c1", "Riley Chen", 0.6, {"qual-mla"}),
    ]


def _rates() -> dict[str, float]:
    return {"emp-a1": 40.0, "emp-b1": 40.0, "emp-c1": 26.0}


def test_labor_forecast_regular_hours_only() -> None:
    assignments = [
        ScheduledShift("emp-a1", "Avery Miller", date(2026, 6, 1), "shift-morning"),
        ScheduledShift("emp-a1", "Avery Miller", date(2026, 6, 2), "shift-morning"),
    ]
    forecast = compute_labor_forecast(
        rules=MANITOBA,
        period_start=date(2026, 6, 1),
        period_end=date(2026, 6, 28),
        assignments=assignments,
        shift_templates=_templates(),
        employee_hourly_rates=_rates(),
    )
    assert forecast.regular_hours == 16.0
    assert forecast.overtime_hours == 0.0
    assert forecast.total_cost == 640.0


def test_labor_forecast_weekly_overtime_manitoba() -> None:
    assignments = [
        ScheduledShift("emp-a1", "Avery Miller", date(2026, 6, 1) + timedelta(days=i), "shift-morning")
        for i in range(6)
    ]
    forecast = compute_labor_forecast(
        rules=MANITOBA,
        period_start=date(2026, 6, 1),
        period_end=date(2026, 6, 28),
        assignments=assignments,
        shift_templates=_templates(),
        employee_hourly_rates=_rates(),
    )
    assert forecast.regular_hours == 40.0
    assert forecast.overtime_hours == 8.0
    assert forecast.overtime_cost == 8.0 * 40.0 * 1.5
    assert forecast.total_cost == forecast.regular_cost + forecast.overtime_cost


def test_labor_forecast_weekly_overtime_ontario() -> None:
    assignments = [
        ScheduledShift("emp-a1", "Avery Miller", date(2026, 6, 1) + timedelta(days=i), "shift-morning")
        for i in range(6)
    ]
    forecast = compute_labor_forecast(
        rules=ONTARIO,
        period_start=date(2026, 6, 1),
        period_end=date(2026, 6, 28),
        assignments=assignments,
        shift_templates=_templates(),
        employee_hourly_rates=_rates(),
    )
    assert forecast.regular_hours == 44.0
    assert forecast.overtime_hours == 4.0


def test_build_full_forecast_includes_flagged_violations() -> None:
    assignments = [
        ScheduledShift("emp-a1", "Avery Miller", date(2026, 6, 1), "shift-morning"),
    ]
    flagged = [
        (
            "emp-a1",
            "WEEKLY_OVERTIME",
            "48.0h in work week starting 2026-06-01 → 8.0h statutory overtime (threshold 40h).",
        )
    ]
    forecast = build_full_forecast(
        rules=MANITOBA,
        period_start=date(2026, 6, 1),
        period_end=date(2026, 6, 28),
        weeks_in_period=4,
        assignments=assignments,
        shift_templates=_templates(),
        shift_required_qualifications={
            "shift-morning": {"qual-mlt", "qual-mla"},
            "shift-evening": {"qual-mlt"},
            "shift-night": {"qual-mlt"},
        },
        employees=_employees(),
        employee_hourly_rates=_rates(),
        flagged_violations=flagged,
    )
    assert forecast.prevented_leakage == round(8.0 * 40.0 * 0.5, 2)
