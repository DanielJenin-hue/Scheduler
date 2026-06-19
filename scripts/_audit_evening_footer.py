"""Audit evening footer tallies after clean ALTERNATE_SHIFTS fill."""
from __future__ import annotations

from datetime import date

from lab_scheduler.compliance import MANITOBA
from lab_scheduler.policy.frame_bridge import schedule_frame_row_index_by_employee_id
from lab_scheduler.scheduling.preference_policy import FillMode
from lab_scheduler.scheduling.schedule_tallies import (
    calculate_daily_shift_tallies,
    shift_target_for_date,
)
from lab_scheduler.scheduling.weekend_placement_rules import (
    daily_band_qual_count,
    get_grid_token,
    weekend_band_qual_count,
)
from lab_scheduler.simulation.load_test import build_portage_roster, portage_employee_target_hours
from tests.test_distribute_alternate_shifts import _period_dates
from tests.test_preference_fill import _fill_specs


def main() -> None:
    start = date(2026, 6, 1)
    dates = _period_dates(start)
    roster = build_portage_roster()
    targets = portage_employee_target_hours(roster, weeks_in_period=8, rules=MANITOBA)
    specs = [(e.id, e.full_name, e.contract_line_type or "D/E") for e in roster]
    updated, result = _fill_specs(
        dates,
        specs,
        targets=targets,
        mode=FillMode.ALTERNATE_SHIFTS,
    )
    row_lookup = schedule_frame_row_index_by_employee_id(updated)
    employees_by_id = {e.id: e for e in roster}
    qual_codes = {"qual-mlt": "MLT", "qual-mla": "MLA"}
    tallies = calculate_daily_shift_tallies(updated, dates=[d.isoformat() for d in dates])

    violations: list[tuple[str, int, int, int, int]] = []
    for day in dates:
        key = day.isoformat()
        e_count = tallies.evenings.get(key, 0)
        target = shift_target_for_date(day, "E")
        if e_count != target:
            if day.weekday() < 5:
                counts = daily_band_qual_count(
                    updated, row_lookup, employees_by_id, qual_codes, day, "E"
                )
            else:
                counts = weekend_band_qual_count(
                    updated, row_lookup, employees_by_id, qual_codes, day, "E"
                )
            violations.append(
                (key, e_count, target, counts.get("MLT", 0), counts.get("MLA", 0))
            )

    print("Tier counts:", dict(result.tier_counts))
    print("Evening footer violations:", len(violations))
    for row in violations[:25]:
        print(" ", row)

    for employee in roster:
        if "Line 05" in employee.full_name and (employee.contract_line_type or "").upper() == "D/E":
            row_idx = row_lookup[employee.id]
            e_days = [
                d.isoformat()
                for d in dates
                if get_grid_token(updated, row_idx, d) == "E"
            ]
            print(f"Line 05 E ({employee.full_name}): {e_days}")

    for employee in roster:
        if "Line 03" in employee.full_name:
            row_idx = row_lookup[employee.id]
            shift_days = [
                (d.isoformat(), get_grid_token(updated, row_idx, d))
                for d in dates
                if get_grid_token(updated, row_idx, d) in {"D", "E", "N"}
            ]
            print(f"Line 03 ({employee.full_name}): {len(shift_days)} shifts")
            print("  sample:", shift_days[:8])


if __name__ == "__main__":
    main()
