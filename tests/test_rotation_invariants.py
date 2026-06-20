"""Rotation pattern invariants after ALTERNATE_SHIFTS on a clean grid."""

from __future__ import annotations

from datetime import date

from lab_scheduler.compliance import MANITOBA
from lab_scheduler.policy.frame_bridge import schedule_frame_row_index_by_employee_id
from lab_scheduler.scheduling.preference_policy import FillMode
from lab_scheduler.scheduling.profiles import EmployeeProfile
from lab_scheduler.scheduling.rotation_invariants import check_rotation_invariants
from lab_scheduler.scheduling.weekend_placement_rules import (
    can_place_daily_alt,
    weekend_sat_sun_tokens_mirrored,
)
from lab_scheduler.simulation.load_test import build_portage_roster, portage_employee_target_hours
from tests.test_distribute_alternate_shifts import _period_dates
from tests.test_preference_fill import _empty_frame, _fill_specs


def test_weekday_evening_cap_is_one_per_qual() -> None:
    """Weekday E cap is 1 per qual (1 MLT + 1 MLA), not 2 per qual."""
    start = date(2026, 6, 1)
    dates = _period_dates(start)
    specs = [
        ("mlt-de-01", "Vacant MLT D/E - Line 01", "D/E"),
        ("mlt-de-02", "Vacant MLT D/E - Line 02", "D/E"),
    ]
    frame = _empty_frame(dates, specs)
    row_lookup = schedule_frame_row_index_by_employee_id(frame)
    employees_by_id = {
        employee_id: EmployeeProfile(
            id=employee_id,
            full_name=name,
            fte=1.0,
            qualification_ids={"qual-mlt"},
            contract_line_type="D/E",
        )
        for employee_id, name, _contract in specs
    }
    qual_codes = {"qual-mlt": "MLT", "qual-mla": "MLA"}
    monday = date(2026, 6, 8)
    first_mlt, second_mlt = "mlt-de-01", "mlt-de-02"
    assert can_place_daily_alt(
        frame,
        row_lookup,
        employees_by_id,
        qual_codes,
        employee_id=first_mlt,
        day=monday,
        band="E",
    )
    frame.at[row_lookup[first_mlt], monday.isoformat()] = "E"
    assert not can_place_daily_alt(
        frame,
        row_lookup,
        employees_by_id,
        qual_codes,
        employee_id=second_mlt,
        day=monday,
        band="E",
    )


def test_weekend_sat_sun_mirror_helper() -> None:
    assert weekend_sat_sun_tokens_mirrored("", "")
    assert weekend_sat_sun_tokens_mirrored("N", "N")
    assert not weekend_sat_sun_tokens_mirrored("N", "")
    assert not weekend_sat_sun_tokens_mirrored("", "E")
    assert not weekend_sat_sun_tokens_mirrored("D", "E")


def test_weekend_sat_sun_mirror_invariant_flags_split_weekend() -> None:
    start = date(2026, 6, 1)
    dates = _period_dates(start)
    saturday = date(2026, 6, 6)
    sunday = date(2026, 6, 7)
    specs = [("mlt-dn-01", "Vacant MLT D/N - Line 01", "D/N")]
    frame = _empty_frame(dates, specs)
    row_lookup = schedule_frame_row_index_by_employee_id(frame)
    employees_by_id = {
        "mlt-dn-01": EmployeeProfile(
            id="mlt-dn-01",
            full_name="Vacant MLT D/N - Line 01",
            fte=1.0,
            qualification_ids={"qual-mlt"},
            contract_line_type="D/N",
        )
    }
    frame.at[row_lookup["mlt-dn-01"], saturday.isoformat()] = "N"
    report = check_rotation_invariants(
        frame,
        dates=dates,
        row_lookup=row_lookup,
        employees_by_id=employees_by_id,
        qual_codes={"qual-mlt": "MLT", "qual-mla": "MLA"},
        employee_target_hours={"mlt-dn-01": 320.0},
    )
    assert not report.passed
    assert any(v.invariant_id == "weekend_sat_sun_mirror" for v in report.violations)
    frame.at[row_lookup["mlt-dn-01"], sunday.isoformat()] = "N"
    report_ok = check_rotation_invariants(
        frame,
        dates=dates,
        row_lookup=row_lookup,
        employees_by_id=employees_by_id,
        qual_codes={"qual-mlt": "MLT", "qual-mla": "MLA"},
        employee_target_hours={"mlt-dn-01": 320.0},
    )
    assert report_ok.passed or not any(
        v.invariant_id == "weekend_sat_sun_mirror" for v in report_ok.violations
    )


def test_alternate_shifts_rotation_invariants_respect_night_streak_cap() -> None:
    """No vacant line may exceed the 4-night Manitoba cap after ALTERNATE_SHIFTS fill."""
    from lab_scheduler.scheduling.night_streak_corrector import PORTAGE_MAX_CONSECUTIVE_NIGHTS

    start = date(2026, 6, 1)
    dates = _period_dates(start)
    roster = build_portage_roster()
    targets = portage_employee_target_hours(roster, weeks_in_period=8, rules=MANITOBA)
    specs = [(e.id, e.full_name, e.contract_line_type or "D/E") for e in roster]
    frame, _ = _fill_specs(
        dates,
        specs,
        targets=targets,
        mode=FillMode.ALTERNATE_SHIFTS,
    )
    row_lookup = schedule_frame_row_index_by_employee_id(frame)
    from lab_scheduler.scheduling.weekend_placement_rules import get_grid_token

    for employee_id, row_idx in row_lookup.items():
        best = current = 0
        for day in dates:
            if get_grid_token(frame, row_idx, day) == "N":
                current += 1
                best = max(best, current)
            else:
                current = 0
        assert best <= PORTAGE_MAX_CONSECUTIVE_NIGHTS, (
            f"{employee_id}: {best} consecutive nights exceeds "
            f"{PORTAGE_MAX_CONSECUTIVE_NIGHTS}-night cap"
        )


def test_alternate_shifts_rotation_invariants_on_clean_grid() -> None:
    start = date(2026, 6, 1)
    dates = _period_dates(start)
    roster = build_portage_roster()
    targets = portage_employee_target_hours(roster, weeks_in_period=8, rules=MANITOBA)
    specs = [(e.id, e.full_name, e.contract_line_type or "D/E") for e in roster]
    frame, _ = _fill_specs(
        dates,
        specs,
        targets=targets,
        mode=FillMode.ALTERNATE_SHIFTS,
    )
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
    if not report.passed:
        messages = [v.message for v in report.violations[:15]]
        raise AssertionError(
            f"{len(report.violations)} invariant violations: {messages}"
        )
