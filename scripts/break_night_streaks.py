#!/usr/bin/env python3
"""
Post-processing utility: break 8+ consecutive night streaks on Portage D/N lines.

Runs the Layer-3 micro-shuffle corrector against a saved schedule export JSON or by
re-generating the active Portage period. Swaps preserve 320h integrity (8h for 8h).

Usage:
    python scripts/break_night_streaks.py --dry-run
    python scripts/break_night_streaks.py --export exports/Schedule_Export_2026-05-28.json
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from lab_scheduler.compliance import MANITOBA  # noqa: E402
from lab_scheduler.scheduling.auto_generate import PlannedAssignment  # noqa: E402
from lab_scheduler.scheduling.night_streak_corrector import (  # noqa: E402
    PORTAGE_NIGHT_STREAK_TARGETS,
    correct_portage_night_streaks,
    find_consecutive_night_streaks,
)
from lab_scheduler.simulation.hospital_stress import (  # noqa: E402
    shift_required_qualifications,
    shift_templates,
)
from lab_scheduler.simulation.load_test import build_portage_roster  # noqa: E402


def _load_assignments_from_export(export_path: Path) -> tuple[list[PlannedAssignment], date, date]:
    payload = json.loads(export_path.read_text(encoding="utf-8"))
    rows = payload.get("assignments") or payload
    if not isinstance(rows, list):
        raise ValueError(f"Unexpected export shape in {export_path}")

    assignments: list[PlannedAssignment] = []
    dates: list[date] = []
    for row in rows:
        assignment_date = date.fromisoformat(str(row["assignment_date"]))
        dates.append(assignment_date)
        assignments.append(
            PlannedAssignment(
                employee_id=str(row["employee_id"]),
                shift_template_id=str(row["shift_template_id"]),
                assignment_date=assignment_date,
            )
        )
    if not dates:
        raise ValueError(f"No assignments found in {export_path}")
    return assignments, min(dates), max(dates)


def main() -> int:
    parser = argparse.ArgumentParser(description="Break Portage 8-night streaks via peer swaps")
    parser.add_argument(
        "--export",
        type=Path,
        help="Schedule export JSON path (default: run auto-generate dry audit only)",
    )
    parser.add_argument(
        "--weeks",
        type=int,
        default=8,
        help="Weeks in period when inferring from export (default: 8)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report streaks and proposed corrections without writing output",
    )
    args = parser.parse_args()

    employees = build_portage_roster()
    templates = shift_templates()
    required = shift_required_qualifications()

    if args.export:
        assignments, period_start, period_end = _load_assignments_from_export(args.export.resolve())
    else:
        from portage_fixtures import portage_generate_kwargs
        from lab_scheduler.scheduling.auto_generate import auto_generate_schedule

        kwargs = portage_generate_kwargs(weeks=args.weeks)
        result = auto_generate_schedule(**kwargs)
        assignments = list(result.assignments)
        period_start = kwargs["period_start"]
        period_end = kwargs["period_end"]
        print(
            f"Generated schedule: required coverage gaps={result.coverage_gap_count}, "
            f"night streak swaps already applied={result.night_streak_swaps_applied}"
        )

    before: list[str] = []
    for target_id in PORTAGE_NIGHT_STREAK_TARGETS:
        streaks = find_consecutive_night_streaks(
            employee_id=target_id,
            period_start=period_start,
            period_end=period_end,
            assignments=assignments,
            shift_templates=templates,
        )
        for streak in streaks:
            before.append(
                f"{target_id}: {streak.length} nights "
                f"({streak.start_date.isoformat()}..{streak.end_date.isoformat()})"
            )

    if before:
        print("Night streaks before correction:")
        for line in before:
            print(f"  - {line}")
    else:
        print("No 8+ night streaks detected on target lines.")

    correction = correct_portage_night_streaks(
        assignments,
        employees=employees,
        shift_templates=templates,
        shift_required_qualifications=required,
        rules=MANITOBA,
        period_start=period_start,
        period_end=period_end,
        weeks_in_period=args.weeks,
    )

    if correction.swaps_applied:
        print(f"\nApplied {len(correction.swaps_applied)} peer swap(s):")
        for swap in correction.swaps_applied:
            print(
                f"  - {swap.assignment_date.isoformat()}: "
                f"{swap.target_employee_id} <-> {swap.peer_employee_id}"
            )
    else:
        print("\nNo legal peer swaps found.")

    if correction.remaining_streaks:
        print("\nRemaining streaks:")
        for streak in correction.remaining_streaks:
            print(
                f"  - {streak.employee_id}: {streak.length} nights "
                f"({streak.start_date.isoformat()}..{streak.end_date.isoformat()})"
            )

    if args.dry_run or not args.export:
        return 0

    output_path = args.export.with_name(args.export.stem + "_night_corrected.json")
    payload = {
        "assignments": [
            {
                "employee_id": assignment.employee_id,
                "shift_template_id": assignment.shift_template_id,
                "assignment_date": assignment.assignment_date.isoformat(),
            }
            for assignment in assignments
        ],
        "night_streak_swaps": [
            {
                "assignment_date": swap.assignment_date.isoformat(),
                "target_employee_id": swap.target_employee_id,
                "peer_employee_id": swap.peer_employee_id,
                "rationale": swap.rationale,
            }
            for swap in correction.swaps_applied
        ],
    }
    output_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"\nWrote corrected assignments to {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
