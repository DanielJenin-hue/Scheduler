from datetime import date, timedelta

from lab_scheduler.compliance import MANITOBA, ScheduledShift, ShiftTemplateInfo
from lab_scheduler.scheduling.auto_generate import EmployeeProfile
from lab_scheduler.validation.overtime_savings import compute_overtime_savings_report


def _templates() -> dict[str, ShiftTemplateInfo]:
    return {
        "shift-morning": ShiftTemplateInfo(
            "shift-morning", "MORNING", "Morning", "07:00", "15:00", 480, False
        ),
        "shift-evening": ShiftTemplateInfo(
            "shift-evening", "EVENING", "Evening", "15:00", "23:00", 480, False
        ),
    }


def _employees() -> list[EmployeeProfile]:
    return [
        EmployeeProfile("emp-a1", "Avery Miller", 1.0, {"qual-mlt"}),
        EmployeeProfile("emp-b1", "Jordan Patel", 0.8, {"qual-mlt"}),
    ]


def _rates() -> dict[str, float]:
    return {"emp-a1": 40.0, "emp-b1": 40.0}


def test_overtime_savings_no_overtime_zero_prevented() -> None:
    assignments = [
        ScheduledShift("emp-a1", "Avery Miller", date(2026, 6, 1), "shift-morning"),
        ScheduledShift("emp-a1", "Avery Miller", date(2026, 6, 2), "shift-morning"),
    ]
    report = compute_overtime_savings_report(
        rules=MANITOBA,
        period_start=date(2026, 6, 1),
        period_end=date(2026, 6, 7),
        weeks_in_period=1,
        assignments=assignments,
        shift_templates=_templates(),
        shift_required_qualifications={
            "shift-morning": {"qual-mlt"},
            "shift-evening": {"qual-mlt"},
        },
        employees=_employees(),
        employee_hourly_rates=_rates(),
    )
    assert report.current_ot_premium == 0.0
    assert report.estimated_overtime_prevented >= 0.0
    assert report.worst_case_ot_premium >= report.current_ot_premium
    assert "information-only" in report.methodology.lower()


def test_overtime_savings_worst_case_exceeds_current_with_open_slots() -> None:
    assignments = [
        ScheduledShift("emp-a1", "Avery Miller", date(2026, 6, 1) + timedelta(days=i), "shift-morning")
        for i in range(6)
    ]
    report = compute_overtime_savings_report(
        rules=MANITOBA,
        period_start=date(2026, 6, 1),
        period_end=date(2026, 6, 7),
        weeks_in_period=1,
        assignments=assignments,
        shift_templates=_templates(),
        shift_required_qualifications={
            "shift-morning": {"qual-mlt"},
            "shift-evening": {"qual-mlt"},
        },
        employees=_employees(),
        employee_hourly_rates=_rates(),
    )
    assert report.current_ot_premium > 0.0
    assert report.worst_case_ot_premium > report.current_ot_premium
    assert report.estimated_overtime_prevented > 0.0
    assert report.open_shift_count > 0
    assert report.savings_pct > 0.0
