"""Assert the generated rotation matches the reference staggered 7+1 E grid."""

from __future__ import annotations

from datetime import date, timedelta

from lab_scheduler.compliance import MANITOBA
from lab_scheduler.policy.frame_bridge import schedule_frame_row_index_by_employee_id
from lab_scheduler.scheduling.preference_policy import FillMode
from lab_scheduler.scheduling.rotation_invariants import check_rotation_invariants
from lab_scheduler.scheduling.schedule_tallies import (
    calculate_daily_shift_tallies,
    shift_target_for_date,
    weekday_day_tally_status,
)
from lab_scheduler.scheduling.weekend_placement_rules import get_grid_token
from lab_scheduler.simulation.load_test import build_portage_roster, portage_employee_target_hours
from tests.test_distribute_alternate_shifts import _period_dates
from tests.test_preference_fill import _fill_specs


def _ft_de_employee(roster, *, qual: str, line_no: int):
    return next(
        e
        for e in roster
        if qual in e.full_name
        and "D/E" in (e.contract_line_type or "")
        and f"Line {line_no:02}" in e.full_name
    )


def test_reference_seven_day_e_blocks_staggered_by_line() -> None:
    """Each FT D/E line has one Mon–Sun E week offset by line number (W1..W8)."""
    start = date(2026, 6, 1)
    dates = _period_dates(start)
    roster = build_portage_roster()
    targets = portage_employee_target_hours(roster, weeks_in_period=8, rules=MANITOBA)
    specs = [(e.id, e.full_name, e.contract_line_type or "D/E") for e in roster]
    frame, _ = _fill_specs(
        dates, specs, targets=targets, mode=FillMode.ALTERNATE_SHIFTS
    )
    row_lookup = schedule_frame_row_index_by_employee_id(frame)

    ft_lines = {"MLT": range(1, 7), "MLA": range(1, 6)}
    for qual, line_range in ft_lines.items():
        for line_no in line_range:
            employee = _ft_de_employee(roster, qual=qual, line_no=line_no)
            row_idx = row_lookup[employee.id]
            week_index = line_no - 1
            monday = start + timedelta(weeks=week_index)
            block = [monday + timedelta(days=i) for i in range(7)]
            tokens = [get_grid_token(frame, row_idx, day) for day in block if day in dates]
            assert len(tokens) == 7, f"{qual} L{line_no:02} block length"
            assert all(
                token == "E" for token in tokens
            ), f"{qual} L{line_no:02} expected EEEEEEE, got {tokens}"


def test_reference_footer_and_weekday_day_balance() -> None:
    start = date(2026, 6, 1)
    dates = _period_dates(start)
    roster = build_portage_roster()
    targets = portage_employee_target_hours(roster, weeks_in_period=8, rules=MANITOBA)
    specs = [(e.id, e.full_name, e.contract_line_type or "D/E") for e in roster]
    frame, result = _fill_specs(
        dates, specs, targets=targets, mode=FillMode.ALTERNATE_SHIFTS
    )
    assert result.tier_counts.get("de_weekday_day_balanced", 0) > 0

    row_lookup = schedule_frame_row_index_by_employee_id(frame)
    employees_by_id = {e.id: e for e in roster}
    qual_codes = {"qual-mlt": "MLT", "qual-mla": "MLA"}
    report = check_rotation_invariants(
        frame,
        dates=dates,
        row_lookup=row_lookup,
        employees_by_id=employees_by_id,
        qual_codes=qual_codes,
        employee_target_hours=targets,
    )
    assert report.passed, [v.message for v in report.violations]

    date_keys = [d.isoformat() for d in dates]
    tallies = calculate_daily_shift_tallies(frame, dates=date_keys)
    weekday_counts = [
        tallies.days.get(day.isoformat(), 0)
        for day in dates
        if day.weekday() < 5
    ]
    for count in weekday_counts:
        assert (
            weekday_day_tally_status(count, weekday_counts) == "tally-ok"
        ), f"weekday D imbalance: {weekday_counts}"

    for day in dates:
        if day.weekday() < 5:
            continue
        key = day.isoformat()
        assert tallies.days.get(key, 0) == shift_target_for_date(day, "D")
