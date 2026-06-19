#!/usr/bin/env python3
"""
Portage-scale batch Auto-Pilot load test (CLI entry point).

Usage:
    python scripts/load_test.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from lab_scheduler.simulation.load_test import (  # noqa: E402
    format_load_test_summary,
    run_portage_load_test,
)


def main() -> int:
    summary = run_portage_load_test()
    print(format_load_test_summary(summary))
    return 0 if summary.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
