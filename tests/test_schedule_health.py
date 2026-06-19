"""Tests for live draft schedule health snapshot."""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd

from lab_scheduler.compliance import MANITOBA
from lab_scheduler.compliance.engine import ShiftTemplateInfo
from lab_scheduler.scheduling.schedule_health import (
    ScheduleHealthSnapshot,
    build_schedule_health_snapshot,
    chunk_index_for_date,
    format_tally_issue_message,
)

def _templates() -> dict[str, dict]:
    return {
        "shift-morning": {
            "id": "shift-morning",
            "code": "MORNING",
            "name": "Day",
            "short": "D",
            "start_time": "07:00",
            "end_time": "15:00",
            "duration_minutes": 480,
            "crosses_midnight": False,
        },
        "shift-evening": {
            "id": "shift-evening",
            "code": "EVENING",
            "name": "Evening",
            "short": "E",
            "start_time": "15:00",
            "end_time": "23:00",
            "duration_minutes": 480,
            "crosses_midnight": False,
        },
        "shift-night": {
            "id": "shift-night",
            "code": "NIGHT",
            "name": "Night",
            "short": "N",
            "start_time": "23:00",
            "end_time": "07:00",
            "duration_minutes": 480,
            "crosses_midnight": True,
        },
    }


def _template_info(templates: dict[str, dict]) -> dict[str, ShiftTemplateInfo]:
    return {
        template_id: ShiftTemplateInfo(
            id=template_id,
            code=template["code"],
            name=template["name"],
            start_time=template["start_time"],
            end_time=template["end_time"],
            duration_minutes=template["duration_minutes"],
            crosses_midnight=template["crosses_midnight"],
        )
        for template_id, template in templates.items()
    }


def _employees(count: int) -> list[dict]:
    return [
        {
            "id": f"line-{index}",
            "full_name": f"Vacant MLT D/E - Line {index:02d}",
            "fte": 1.0,
            "contract_line_type": "D/E",
        }
        for index in range(1, count + 1)
    ]


def _build_snapshot(
    *,
    frame: pd.DataFrame,
    employees: list[dict],
    day: date,
    pending_mutations: int = 0,
) -> ScheduleHealthSnapshot:
    templates = _templates()
    return build_schedule_health_snapshot(
        schedule_frame=frame,
        employees=employees,
        dates=[day],
        templates=templates,
        template_info=_template_info(templates),
        period_start=day,
        period_end=day,
        qual_codes={},
        pending_mutations=pending_mutations,
        hours_delta=0.0,
        rules=MANITOBA,
        weeks_in_period=1,
        employee_target_hours={employee["id"]: 320.0 for employee in employees},
        emp_quals={},
    )


def test_health_snapshot_detects_evening_overfill() -> None:
    day = date(2026, 6, 8)
    day_key = day.isoformat()
    employees = _employees(4)
    frame = pd.DataFrame(
        [
            {
                "employee_id": employee["id"],
                "Employee": employee["full_name"],
                "contract_line_type": "D/E",
                day_key: "E",
            }
            for employee in employees
        ]
    )

    snapshot = _build_snapshot(frame=frame, employees=employees, day=day)

    assert snapshot.evening_violation_days == 1
    assert snapshot.night_violation_days == 1
    assert snapshot.is_operational_floor_ok is False
    assert len(snapshot.tally_issues) == 2
    evening_issue = next(item for item in snapshot.tally_issues if item.band == "E")
    assert evening_issue.actual == 4
    assert evening_issue.target == 2
    assert evening_issue.severity == "over"
    assert "you have 4" in format_tally_issue_message(evening_issue)


def test_health_snapshot_detects_night_underfill() -> None:
    day = date(2026, 6, 12)
    day_key = day.isoformat()
    employees = _employees(2)
    frame = pd.DataFrame(
        [
            {
                "employee_id": employees[0]["id"],
                "Employee": employees[0]["full_name"],
                "contract_line_type": "D/E",
                day_key: "E",
            },
            {
                "employee_id": employees[1]["id"],
                "Employee": employees[1]["full_name"],
                "contract_line_type": "D/E",
                day_key: "N",
            },
        ]
    )

    snapshot = _build_snapshot(frame=frame, employees=employees, day=day)

    assert snapshot.night_violation_days == 1
    night_issue = next(item for item in snapshot.tally_issues if item.band == "N")
    assert night_issue.actual == 1
    assert night_issue.target == 2
    assert night_issue.severity == "under"
    assert "short 1 night seat" in format_tally_issue_message(night_issue)


def test_health_snapshot_clean_period() -> None:
    day = date(2026, 6, 1)
    day_key = day.isoformat()
    employees = _employees(4)
    frame = pd.DataFrame(
        [
            {
                "employee_id": employees[0]["id"],
                "Employee": employees[0]["full_name"],
                "contract_line_type": "D/E",
                day_key: "E",
            },
            {
                "employee_id": employees[1]["id"],
                "Employee": employees[1]["full_name"],
                "contract_line_type": "D/E",
                day_key: "E",
            },
            {
                "employee_id": employees[2]["id"],
                "Employee": employees[2]["full_name"],
                "contract_line_type": "D/E",
                day_key: "N",
            },
            {
                "employee_id": employees[3]["id"],
                "Employee": employees[3]["full_name"],
                "contract_line_type": "D/E",
                day_key: "N",
            },
        ]
    )

    snapshot = _build_snapshot(frame=frame, employees=employees, day=day)

    assert snapshot.is_operational_floor_ok is True
    assert snapshot.tally_issues == ()
    assert snapshot.evening_violation_days == 0
    assert snapshot.night_violation_days == 0


def test_health_pending_mutations_passed_through() -> None:
    day = date(2026, 6, 1)
    day_key = day.isoformat()
    employees = _employees(4)
    frame = pd.DataFrame(
        [
            {
                "employee_id": employee["id"],
                "Employee": employee["full_name"],
                "contract_line_type": "D/E",
                day_key: "D",
            }
            for employee in employees
        ]
    )

    snapshot = _build_snapshot(
        frame=frame,
        employees=employees,
        day=day,
        pending_mutations=47,
    )

    assert snapshot.pending_mutations == 47


def test_chunk_index_for_date() -> None:
    start = date(2026, 6, 1)
    dates = [start + timedelta(days=offset) for offset in range(16 * 7)]
    target = date(2026, 6, 8)

    assert chunk_index_for_date(dates, target) == 0

    later_target = date(2026, 8, 10)
    assert chunk_index_for_date(dates, later_target) == 1
