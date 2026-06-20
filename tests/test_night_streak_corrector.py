
import pytest

pytestmark = pytest.mark.legacy

from datetime import date, timedelta

from lab_scheduler.compliance import MANITOBA
from lab_scheduler.scheduling.auto_generate import PlannedAssignment
from lab_scheduler.scheduling.night_streak_corrector import (
    NIGHT_STREAK_CORRECTION_MIN_LENGTH,
    PORTAGE_MAX_CONSECUTIVE_NIGHTS,
    correct_night_streaks,
    find_consecutive_night_streaks,
    validate_night_streak_sequences,
    validate_night_streaks_from_schedule_rows,
)
from lab_scheduler.scheduling.profiles import EmployeeProfile
from lab_scheduler.simulation.hospital_stress import (
    QUAL_MLT,
    shift_required_qualifications,
    shift_templates,
)


def _dn_mlt(id_suffix: str, *, line: int) -> EmployeeProfile:
    return EmployeeProfile(
        f"portage-mlt-{id_suffix}",
        f"Vacant MLT D/N - Line {line:02d}",
        1.0,
        {QUAL_MLT},
        contract_line_type="D/N",
    )


def test_find_consecutive_night_streaks_detects_eight_day_run() -> None:
    start = date(2026, 6, 10)
    assignments = [
        PlannedAssignment("portage-mlt-01", "shift-night", start + timedelta(days=offset))
        for offset in range(8)
    ]

    streaks = find_consecutive_night_streaks(
        employee_id="portage-mlt-01",
        period_start=start,
        period_end=start + timedelta(days=20),
        assignments=assignments,
        shift_templates=shift_templates(),
    )

    assert len(streaks) == 1
    assert streaks[0].length == 8
    assert streaks[0].start_date == start
    assert streaks[0].end_date == start + timedelta(days=7)


def test_correct_night_streaks_swaps_day_from_peer_to_break_chain() -> None:
    start = date(2026, 6, 10)
    employees = [
        _dn_mlt("01", line=1),
        _dn_mlt("02", line=2),
    ]
    templates = shift_templates()
    assignments: list[PlannedAssignment] = []
    for offset in range(8):
        day = start + timedelta(days=offset)
        assignments.append(PlannedAssignment("portage-mlt-01", "shift-night", day))
        if offset < 6:
            assignments.append(PlannedAssignment("portage-mlt-02", "shift-morning", day))
    # Peer holds a night immediately before the swap date so taking the streak
    # night preserves legal [N, N] / [N, D] transitions for both lines.
    assignments.append(
        PlannedAssignment("portage-mlt-02", "shift-night", start + timedelta(days=6))
    )
    assignments.append(
        PlannedAssignment("portage-mlt-02", "shift-morning", start + timedelta(days=7))
    )

    result = correct_night_streaks(
        assignments,
        employees=employees,
        shift_templates=templates,
        shift_required_qualifications=shift_required_qualifications(),
        rules=MANITOBA,
        period_start=start,
        period_end=start + timedelta(days=20),
        weeks_in_period=4,
        target_employee_ids=("portage-mlt-01",),
    )

    assert result.swaps_applied
    assert all(streak.length < 8 for streak in result.remaining_streaks)
    remaining = find_consecutive_night_streaks(
        employee_id="portage-mlt-01",
        period_start=start,
        period_end=start + timedelta(days=20),
        assignments=assignments,
        shift_templates=templates,
        min_length=NIGHT_STREAK_CORRECTION_MIN_LENGTH,
    )
    assert len(remaining) == 1
    assert remaining[0].length == 7
    break_day = result.swaps_applied[0].assignment_date
    swapped_target = next(
        assignment
        for assignment in assignments
        if assignment.employee_id == "portage-mlt-01"
        and assignment.assignment_date == break_day
    )
    assert swapped_target.shift_template_id == "shift-morning"


def test_catalog_dn_streak_exemption_is_disabled() -> None:
    from datetime import timedelta

    from lab_scheduler.scheduling.night_streak_corrector import (
        NightStreak,
        catalog_dn_weekday_night_streak_allowed,
    )

    employee = _dn_mlt("01", line=1)
    streak = NightStreak(
        employee_id=employee.id,
        start_date=date(2026, 6, 1),
        end_date=date(2026, 6, 14),
        length=14,
    )
    assert not catalog_dn_weekday_night_streak_allowed(
        employee,
        streak,
        date(2026, 6, 1),
    )


def test_validate_night_streak_sequences_flags_runs_longer_than_four() -> None:
    start = date(2026, 6, 10)
    employee = _dn_mlt("01", line=1)
    assignments = [
        PlannedAssignment("portage-mlt-01", "shift-night", start + timedelta(days=offset))
        for offset in range(5)
    ]

    violations = validate_night_streak_sequences(
        assignments,
        employees=[employee],
        shift_templates=shift_templates(),
        period_start=start,
        period_end=start + timedelta(days=20),
    )

    assert len(violations) == 1
    assert violations[0].length == 5
    assert str(PORTAGE_MAX_CONSECUTIVE_NIGHTS) in violations[0].message


def test_validate_night_streak_sequences_allows_four_nights() -> None:
    start = date(2026, 6, 10)
    employee = _dn_mlt("01", line=1)
    assignments = [
        PlannedAssignment("portage-mlt-01", "shift-night", start + timedelta(days=offset))
        for offset in range(4)
    ]

    violations = validate_night_streak_sequences(
        assignments,
        employees=[employee],
        shift_templates=shift_templates(),
        period_start=start,
        period_end=start + timedelta(days=20),
    )

    assert not violations


def test_validate_night_streaks_from_schedule_rows() -> None:
    start = date(2026, 6, 10)
    dates = [start + timedelta(days=offset) for offset in range(7)]
    row = {
        "Employee": "Vacant MLT D/N - Line 01",
        "employee_id": "portage-mlt-01",
    }
    for index, day in enumerate(dates):
        row[day.isoformat()] = "N" if index < 5 else "D"

    violations = validate_night_streaks_from_schedule_rows(
        [row],
        employees=[{"id": "portage-mlt-01", "full_name": "Vacant MLT D/N - Line 01"}],
        dates=dates,
    )

    assert len(violations) == 1
    assert violations[0].employee_id == "portage-mlt-01"
