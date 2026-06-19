#!/usr/bin/env python3
"""Render the 8-week Portage rotation grid (reference-style ASCII output)."""

from __future__ import annotations

import sys
from datetime import date, timedelta
from typing import List, Sequence

from lab_scheduler.compliance import MANITOBA
from lab_scheduler.policy.frame_bridge import schedule_frame_row_index_by_employee_id
from lab_scheduler.scheduling.preference_policy import FillMode
from lab_scheduler.scheduling.rotation_invariants import check_rotation_invariants
from lab_scheduler.scheduling.schedule_tallies import (
    calculate_daily_shift_tallies,
    shift_target_for_date,
)
from lab_scheduler.scheduling.weekend_placement_rules import get_grid_token
from lab_scheduler.simulation.load_test import build_portage_roster, portage_employee_target_hours
from tests.test_distribute_alternate_shifts import _period_dates
from tests.test_preference_fill import _fill_specs

_TOKEN_CHAR = {"D": "D", "E": "E", "N": "N", "": "·", "—": "·"}


def _render_line_row(label: str, frame, row_idx: int, dates: Sequence[date]) -> str:
    cells = [_TOKEN_CHAR.get(get_grid_token(frame, row_idx, day), "?") for day in dates]
    return f"{label:<28} " + "".join(f"{c:>2}" for c in cells)


def _render_footer_row(name: str, counts: dict[str, int], dates: Sequence[date], *, band: str) -> str:
    parts: List[str] = []
    for day in dates:
        count = counts.get(day.isoformat(), 0)
        if band == "D" and day.weekday() < 5:
            parts.append(f"{count:>2}")
        else:
            target = shift_target_for_date(day, band)
            parts.append(f"{count}/{target}"[:4].rjust(2))
    return f"{name:<28} " + "".join(f"{p:>2}" for p in parts)


def main() -> int:
    start = date(2026, 6, 1)
    dates = _period_dates(start)
    roster = build_portage_roster()
    targets = portage_employee_target_hours(roster, weeks_in_period=8, rules=MANITOBA)
    specs = [(e.id, e.full_name, e.contract_line_type or "D/E") for e in roster]

    frame, result = _fill_specs(
        dates,
        specs,
        targets=targets,
        mode=FillMode.ALTERNATE_SHIFTS,
    )
    row_lookup = schedule_frame_row_index_by_employee_id(frame)
    employees_by_id = {e.id: e for e in roster}
    qual_codes = {"qual-mlt": "MLT", "qual-mla": "MLA"}

    print("Portage reference rotation (ALTERNATE_SHIFTS fill)")
    print("Tier counts:", dict(result.tier_counts))
    print()

    dow = f"{'':<28} " + "".join(
        f"{['M','T','W','R','F','S','U'][day.weekday()]:>2}" for day in dates
    )
    print(dow)
    print("-" * (28 + 2 * len(dates)))

    current_group = ""
    for employee in roster:
        name = employee.full_name
        if "Vacant" not in name:
            continue
        group = name.split(" - ")[0].replace("Vacant ", "")
        if group != current_group:
            current_group = group
            print(f"\n=== {group} ===")
        row_idx = row_lookup.get(employee.id)
        if row_idx is None:
            continue
        short = name.replace("Vacant ", "")
        print(_render_line_row(short, frame, row_idx, dates))

    date_keys = [d.isoformat() for d in dates]
    tallies = calculate_daily_shift_tallies(frame, dates=date_keys)
    print("\n--- Footer ---")
    print(_render_footer_row("Days", tallies.days, dates, band="D"))
    print(_render_footer_row("Evenings", tallies.evenings, dates, band="E"))
    print(_render_footer_row("Nights", tallies.nights, dates, band="N"))

    report = check_rotation_invariants(
        frame,
        dates=dates,
        row_lookup=row_lookup,
        employees_by_id=employees_by_id,
        qual_codes=qual_codes,
        employee_target_hours=targets,
    )
    print(f"\nRotation invariants: {'PASS' if report.passed else 'FAIL'}")
    for violation in report.violations[:8]:
        print(f"  - {violation.message}")

    print("\n--- 7-day E blocks (reference shape) ---")
    for qual in ("MLT", "MLA"):
        for line_no in range(1, 9):
            emp = next(
                (
                    e
                    for e in roster
                    if qual in e.full_name
                    and "D/E" in (e.contract_line_type or "")
                    and f"Line {line_no:02}" in e.full_name
                ),
                None,
            )
            if emp is None:
                continue
            row_idx = row_lookup[emp.id]
            week_index = line_no - 1
            block_start = start + timedelta(weeks=week_index)
            block = [
                block_start + timedelta(days=i)
                for i in range(7)
                if block_start + timedelta(days=i) in dates
            ]
            block_tokens = [get_grid_token(frame, row_idx, d) for d in block]
            ok = all(t == "E" for t in block_tokens) and len(block) == 7
            visual = "".join(_TOKEN_CHAR.get(t, "?") for t in block_tokens)
            print(f"  {qual} L{line_no:02} W{week_index + 1}: {visual}  {'OK' if ok else 'MISS'}")

    return 0 if report.passed else 1


if __name__ == "__main__":
    sys.exit(main())
