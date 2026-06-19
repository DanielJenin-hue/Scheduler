"""Audit D/E/N daily tallies in a breakroom HTML export.

Post-Auto-Pilot QA (summer 2026):
  1. Restart Streamlit and run Advanced -> Auto-Pilot once for period-2026-summer.
  2. Export breakroom HTML from Print tab (or ops Export popover).
  3. Run this script on the exported file:

     python scripts/_audit_tallies_html.py path/to/breakroom_schedule_period-2026-summer.html

  4. Optional headless comparison against a fresh generate (no DB):

     python scripts/_audit_tallies_html.py --compare path/to/export.html

Expected for Portage summer 2026: weekday 16D/2E/2N, weekend 2D/2E/2N, zero off-target days.
"""
from __future__ import annotations

import argparse
import re
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

PERIOD_START = date(2026, 6, 1)
TARGETS = {
    "d": lambda weekend: 2 if weekend else 16,
    "e": lambda _weekend: 2,
    "n": lambda _weekend: 2,
}
LABELS = {
    "d": "Total Days",
    "e": "Total Evenings",
    "n": "Total Nights",
}


def _parse_dates(html: str) -> list[date]:
    dates_raw = re.findall(
        r"<th class='day-col[^']*'>(\d+/\d+)<br>(Mon|Tue|Wed|Thu|Fri|Sat|Sun)</th>",
        html,
    )
    return [PERIOD_START + timedelta(days=i) for i in range(len(dates_raw))]


def audit_html_tallies(html: str) -> dict[str, list[tuple[date, int, int]]]:
    dates = _parse_dates(html)
    results: dict[str, list[tuple[date, int, int]]] = {}
    for band in ("d", "e", "n"):
        label = LABELS[band]
        match = re.search(
            rf"<tr><td class='emp-col'>{re.escape(label)}[^<]*</td>(.*?)</tr>",
            html,
            re.S,
        )
        if not match:
            results[band] = []
            print(f"missing row: {label}")
            continue
        cells = re.findall(rf"print-token-{band}'[^>]*>(\d+)</", match.group(1))
        bad: list[tuple[date, int, int]] = []
        for index, count_text in enumerate(cells):
            if index >= len(dates):
                break
            day = dates[index]
            expected = TARGETS[band](day.weekday() >= 5)
            count = int(count_text)
            if count != expected:
                bad.append((day, count, expected))
        results[band] = bad
    return results


def print_audit_report(path: Path, html: str) -> int:
    print(f"file: {path}")
    dates = _parse_dates(html)
    all_bad = audit_html_tallies(html)
    bad_total = 0
    for band in ("d", "e", "n"):
        bad = all_bad[band]
        bad_total += len(bad)
        print(f"{LABELS[band]}: {len(bad)} off-target days (of {len(dates)})")
        for day, count, expected in bad[:20]:
            print(f"  {day.isoformat()} ({day.strftime('%a')}): {count} vs {expected}")
    badge = re.search(r"breakroom-compliance-badge[^>]*>([^<]+)", html)
    print("compliance badge:", badge.group(1).strip() if badge else "none")
    build = re.search(r"Generator build[^<]*</[^>]+>\s*([^<]+)", html)
    if build:
        print("generator:", build.group(1).strip())
    return bad_total


def headless_summer_tallies() -> dict[str, list[tuple[date, int, int]]]:
    from collections import defaultdict

    from lab_scheduler.compliance import MANITOBA
    from lab_scheduler.compliance.engine import ShiftTemplateInfo
    from lab_scheduler.engine.constraints import portage_coverage_targets, portage_employee_target_hours
    from lab_scheduler.scheduling.adaptive_auto_pilot import run_adaptive_auto_pilot_ladder
    from lab_scheduler.scheduling.auto_pilot import dedupe_planned_assignments, run_auto_pilot_full_block
    from lab_scheduler.scheduling.auto_generate import PlannedAssignment
    from lab_scheduler.scheduling.persist_validation import count_subfloor_evening_night_days
    from lab_scheduler.scheduling.portage_equity_policy import resolve_portage_scheduling_policy
    from lab_scheduler.scheduling.profiles import EmployeeProfile
    from lab_scheduler.scheduling.schedule_families import resolve_schedule_family
    from lab_scheduler.scheduling.schedule_tallies import shift_band_from_template_code
    from lab_scheduler.scheduling.strategies import ScheduleArchetype
    from lab_scheduler.simulation.hospital_stress import shift_templates as stress_templates

    period_start = PERIOD_START
    period_end = date(2026, 7, 26)
    weeks = 8
    shift_templates = stress_templates()
    employees = [
        EmployeeProfile(
            f"emp-{index}",
            name,
            1.0,
            {"qual-mlt"} if "MLT" in name else {"qual-mla"},
            contract_line_type="D/N" if "D/N" in name else "D/E",
        )
        for index, name in enumerate(
            [
                "Vacant MLT D/N - Line 01",
                "Vacant MLT D/N - Line 02",
                "Vacant MLT D/N - Line 03",
                "Vacant MLT D/N - Line 04",
                "Vacant MLA D/N - Line 01",
                "Vacant MLA D/N - Line 02",
                "Vacant MLA D/N - Line 03",
                "Vacant MLA D/N - Line 04",
            ]
        )
    ]
    target_hours = portage_employee_target_hours(employees, rules=MANITOBA, weeks_in_period=weeks)
    family = resolve_schedule_family(
        archetype=ScheduleArchetype.STANDARD.value,
        has_portage_coverage_targets=True,
        is_self_serve_trial=False,
    )
    pilot, _, _ = run_adaptive_auto_pilot_ladder(
        run_auto_pilot_full_block,
        allow_preview_tier=family.allow_preview_tier,
        require_complete_for_success=family.require_complete_for_success,
        family=family.family,
        rules=MANITOBA,
        period_start=period_start,
        period_end=period_end,
        weeks_in_period=weeks,
        employees=employees,
        shift_templates=shift_templates,
        shift_required_qualifications={},
        employee_target_hours=target_hours,
        coverage_targets=portage_coverage_targets(employees),
        availability_blocked={},
        emit_triage=False,
        enable_fairness_rerun=False,
        archetype=ScheduleArchetype.STANDARD.value,
        portage_scheduling_policy=resolve_portage_scheduling_policy(),
    )
    bands = {tid: shift_band_from_template_code(info.code) for tid, info in shift_templates.items()}
    assignments = dedupe_planned_assignments(pilot.generate.assignments, template_id_to_band=bands)
    below_e, below_n = count_subfloor_evening_night_days(
        assignments=assignments,
        shift_templates=shift_templates,
        period_start=period_start,
        period_end=period_end,
    )
    day_counts: dict[str, dict[date, int]] = {"d": defaultdict(int), "e": defaultdict(int), "n": defaultdict(int)}
    code_map = {"MORNING": "d", "EVENING": "e", "NIGHT": "n"}
    for assignment in assignments:
        info = shift_templates.get(assignment.shift_template_id)
        if info is None:
            continue
        band = code_map.get(info.code)
        if band:
            day_counts[band][assignment.assignment_date] += 1
    bad: dict[str, list[tuple[date, int, int]]] = {"d": [], "e": [], "n": []}
    current = period_start
    while current <= period_end:
        weekend = current.weekday() >= 5
        for band in ("d", "e", "n"):
            expected = TARGETS[band](weekend)
            count = day_counts[band].get(current, 0)
            if count != expected:
                bad[band].append((current, count, expected))
        current += timedelta(days=1)
    print("\nheadless generate:")
    print(f"  below evening floor days: {below_e}")
    print(f"  below night floor days: {below_n}")
    for band in ("d", "e", "n"):
        print(f"  {LABELS[band]}: {len(bad[band])} off-target days")
    return bad


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "html_path",
        nargs="?",
        default=str(Path.home() / "Downloads" / "breakroom_schedule_period-2026-summer.html"),
        help="Breakroom HTML export to audit",
    )
    parser.add_argument(
        "--compare",
        action="store_true",
        help="Also print headless summer-2026 generate tallies for comparison",
    )
    args = parser.parse_args()
    path = Path(args.html_path)
    if not path.is_file():
        print(f"missing file: {path}", file=sys.stderr)
        return 1
    html = path.read_text(encoding="utf-8")
    bad_total = print_audit_report(path, html)
    if args.compare:
        headless_summer_tallies()
    return 1 if bad_total else 0


if __name__ == "__main__":
    raise SystemExit(main())
