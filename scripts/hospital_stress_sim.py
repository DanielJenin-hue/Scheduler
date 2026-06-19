#!/usr/bin/env python3
"""
Hospital-scale scheduler stress simulation (CLI entry point).

Usage:
    python scripts/hospital_stress_sim.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from lab_scheduler.simulation.hospital_stress import (  # noqa: E402
    PERIOD_END,
    PERIOD_START,
    run_hospital_stress_simulation,
)


def _print_summary(result) -> None:
    print()
    print("=" * 60)
    print("  HOSPITAL-SCALE SCHEDULER STRESS SIMULATION")
    print("=" * 60)
    print(f"  Jurisdiction         : Manitoba (40h/wk, 8h/day OT)")
    print(f"  Period               : {PERIOD_START} -> {PERIOD_END}")
    print(f"  Roster               : 35 staff (22 MLT / 13 MLA)")
    print(f"  Crisis blocked days  : {result.blocked_day_count}")
    print("-" * 60)
    print(f"  Execution time       : {result.execution_seconds:>8.3f} s")
    print(f"  Shift fill rate      : {result.fill_rate_pct:>8.2f} %")
    print(f"  Slots filled         : {result.slots_filled} / {result.slots_total}")
    print(f"  Unfilled slots       : {result.unfilled_slots}")
    print(f"  Statutory OT (total) : {result.total_statutory_ot_hours:>8.1f} h")
    print("-" * 60)
    if result.exception_occurred:
        print(f"  Exceptions           : FAILED - {result.exception_message}")
    else:
        print("  Exceptions           : None (clean run)")
    print("=" * 60)
    print()


def main() -> int:
    result = run_hospital_stress_simulation()
    _print_summary(result)
    return 1 if result.exception_occurred else 0


if __name__ == "__main__":
    raise SystemExit(main())
