"""Tests for operational shift slot gap detection."""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd

from lab_scheduler.compliance.engine import ScheduledShift, ShiftTemplateInfo
from lab_scheduler.policy.frame_bridge import count_open_shift_gaps_from_frame
from lab_scheduler.scheduling.open_shift_slots import is_operational_shift_template, list_open_shift_slots


def _morning_template() -> ShiftTemplateInfo:
    return ShiftTemplateInfo(
        id="shift-morning",
        code="MORNING",
        name="Morning",
        start_time="07:00",
        end_time="15:00",
        duration_minutes=480,
        crosses_midnight=False,
    )


def _evening_template() -> ShiftTemplateInfo:
    return ShiftTemplateInfo(
        id="shift-evening",
        code="EVENING",
        name="Evening",
        start_time="15:00",
        end_time="23:00",
        duration_minutes=480,
        crosses_midnight=False,
    )


def _night_template() -> ShiftTemplateInfo:
    return ShiftTemplateInfo(
        id="shift-night",
        code="NIGHT",
        name="Night",
        start_time="23:00",
        end_time="07:00",
        duration_minutes=480,
        crosses_midnight=True,
    )


def _topup_template() -> ShiftTemplateInfo:
    return ShiftTemplateInfo(
        id="tenant-a::twelve-hour-fte-topup",
        code="TOPUP",
        name="FTE Top-up Shift",
        start_time="08:00",
        end_time="14:07",
        duration_minutes=375,
        crosses_midnight=False,
    )


def test_topup_template_is_not_operational_shift_template() -> None:
    assert is_operational_shift_template(_morning_template()) is True
    assert is_operational_shift_template(_topup_template()) is False


def test_list_open_shift_slots_ignores_twelve_hour_topup_template() -> None:
    period_start = date(2026, 6, 1)
    period_end = period_start + timedelta(days=6)
    templates = {
        "shift-morning": _morning_template(),
        "tenant-a::twelve-hour-fte-topup": _topup_template(),
    }
    scheduled = [
        ScheduledShift(
            employee_id="emp-a",
            employee_name="Emp A",
            shift_template_id="shift-morning",
            assignment_date=period_start + timedelta(days=offset),
        )
        for offset in range(7)
    ]

    gaps = list_open_shift_slots(
        period_start=period_start,
        period_end=period_end,
        shift_templates=templates,
        assignments=scheduled,
    )

    assert gaps == []


def test_evening_is_a_gap_in_standard_mode() -> None:
    assert is_operational_shift_template(_evening_template()) is True


def test_evening_is_not_a_gap_in_twelve_hour_mode() -> None:
    assert (
        is_operational_shift_template(_evening_template(), schedule_archetype="TWELVE_HOUR")
        is False
    )
    # Day and Night seats are still staffed coverage requirements in 12-hour mode.
    assert (
        is_operational_shift_template(_morning_template(), schedule_archetype="TWELVE_HOUR")
        is True
    )
    assert (
        is_operational_shift_template(_night_template(), schedule_archetype="TWELVE_HOUR")
        is True
    )


def test_twelve_hour_mode_does_not_flag_evening_gaps() -> None:
    period_start = date(2026, 6, 1)
    period_end = period_start + timedelta(days=6)
    templates = {
        "shift-morning": _morning_template(),
        "shift-evening": _evening_template(),
        "shift-night": _night_template(),
    }
    scheduled = []
    for offset in range(7):
        day = period_start + timedelta(days=offset)
        scheduled.append(
            ScheduledShift(
                employee_id="emp-day",
                employee_name="Emp Day",
                shift_template_id="shift-morning",
                assignment_date=day,
            )
        )
        scheduled.append(
            ScheduledShift(
                employee_id="emp-night",
                employee_name="Emp Night",
                shift_template_id="shift-night",
                assignment_date=day,
            )
        )

    standard_gaps = list_open_shift_slots(
        period_start=period_start,
        period_end=period_end,
        shift_templates=templates,
        assignments=scheduled,
    )
    twelve_hour_gaps = list_open_shift_slots(
        period_start=period_start,
        period_end=period_end,
        shift_templates=templates,
        assignments=scheduled,
        schedule_archetype="TWELVE_HOUR",
    )

    # Standard mode treats every uncovered Evening seat as a gap (7 days).
    assert len(standard_gaps) == 7
    assert all(slot.shift_code == "EVENING" for slot in standard_gaps)
    # Twelve-hour mode runs Day + Night only, so there are no Evening gaps.
    assert twelve_hour_gaps == []


def test_count_open_shift_gaps_from_frame_tracks_draft_edits() -> None:
    day = date(2026, 6, 1)
    templates = {
        "shift-morning": {"id": "shift-morning", "code": "MORNING", "short": "D"},
        "shift-evening": {"id": "shift-evening", "code": "EVENING", "short": "E"},
        "shift-night": {"id": "shift-night", "code": "NIGHT", "short": "N"},
    }
    template_info = {
        "shift-morning": _morning_template(),
        "shift-evening": _evening_template(),
        "shift-night": _night_template(),
    }
    employees = [
        {"id": "emp-a", "full_name": "Emp A"},
        {"id": "emp-b", "full_name": "Emp B"},
    ]
    covered = pd.DataFrame(
        [
            {
                "employee_id": "emp-a",
                "Employee": "Emp A",
                day.isoformat(): "D",
            },
            {
                "employee_id": "emp-b",
                "Employee": "Emp B",
                day.isoformat(): "E",
            },
        ]
    )
    assert (
        count_open_shift_gaps_from_frame(
            covered,
            employees=employees,
            dates=[day],
            db_templates=templates,
            shift_templates=template_info,
            period_start=day,
            period_end=day,
        )
        == 1
    )

    swapped = covered.copy()
    swapped.at[0, day.isoformat()] = "E"
    swapped.at[1, day.isoformat()] = "D"
    assert (
        count_open_shift_gaps_from_frame(
            swapped,
            employees=employees,
            dates=[day],
            db_templates=templates,
            shift_templates=template_info,
            period_start=day,
            period_end=day,
        )
        == 1
    )

    cleared_evening = covered.copy()
    cleared_evening.at[1, day.isoformat()] = "—"
    assert (
        count_open_shift_gaps_from_frame(
            cleared_evening,
            employees=employees,
            dates=[day],
            db_templates=templates,
            shift_templates=template_info,
            period_start=day,
            period_end=day,
        )
        == 2
    )
