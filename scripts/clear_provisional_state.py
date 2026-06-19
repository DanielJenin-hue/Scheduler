#!/usr/bin/env python3
"""Clear stale PROVISIONAL_STRETCH / APPROVED_STRETCH markers before a fresh solve."""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "demo.sqlite3"

if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from lab_scheduler.scheduling.provisional_state_cleanup import clear_provisional_stretch_state


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Remove PROVISIONAL_STRETCH and APPROVED_STRETCH system_note markers "
            "and any cached provisional session JSON sidecars."
        )
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_DB,
        help=f"SQLite database path (default: {DEFAULT_DB.name})",
    )
    parser.add_argument(
        "--tenant-id",
        default=None,
        help="Optional tenant filter (e.g. tenant-northstar-lab)",
    )
    parser.add_argument(
        "--period-id",
        default=None,
        help="Optional schedule period filter (e.g. period-2026-summer)",
    )
    parser.add_argument(
        "--project-root",
        type=Path,
        default=ROOT,
        help="Project root used to locate exports/.streamlit provisional sidecars",
    )
    return parser


def main() -> int:
    args = _build_parser().parse_args()
    if not args.db.is_file():
        print(f"Database not found: {args.db}", file=sys.stderr)
        return 1

    conn = sqlite3.connect(args.db)
    try:
        result = clear_provisional_stretch_state(
            conn,
            tenant_id=args.tenant_id,
            schedule_period_id=args.period_id,
            project_root=args.project_root,
        )
    finally:
        conn.close()

    print(
        "Cleared provisional stretch state: "
        f"{result.db_notes_cleared} database note(s), "
        f"{len(result.session_files_removed)} session file(s)."
    )
    for path in result.session_files_removed:
        print(f"  removed file: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
