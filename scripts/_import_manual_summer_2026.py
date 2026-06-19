"""Restore manager manual summer-2026 schedule from screenshot fixture JSON."""
from __future__ import annotations

import json
import sqlite3
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "portage_manual_screenshot_summer_2026.json"
TENANT = "tenant-northstar-lab"
PERIOD_ID = "period-2026-summer"


def _load_fixture(path: Path = FIXTURE) -> tuple[dict[str, str], date]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    period_start = date.fromisoformat(payload["period_start"])
    patterns: dict[str, str] = {}
    for employee_id, weeks in payload["employees"].items():
        if len(weeks) != 8:
            raise ValueError(f"{employee_id}: expected 8 weeks, got {len(weeks)}")
        for week_index, week in enumerate(weeks):
            if len(week) != 7:
                raise ValueError(f"{employee_id} W{week_index + 1}: expected 7 days, got {week!r}")
        patterns[employee_id] = "".join(weeks)
    return patterns, period_start


def import_manual_schedule(db_path: Path, *, fixture_path: Path = FIXTURE) -> int:
    from lab_scheduler.data.schedule_archive import import_period_schedule, touch_schedule_reload_stamp
    from lab_scheduler.data.snapshots import create_snapshot

    payload = json.loads(fixture_path.read_text(encoding="utf-8"))
    create_snapshot(db_path, label="pre-schedule-import")

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON;")
    try:
        inserted = import_period_schedule(
            conn,
            payload,
            tenant_id=TENANT,
            period_id=PERIOD_ID,
        )
    finally:
        conn.close()

    touch_schedule_reload_stamp(ROOT)
    return inserted


def _print_line(conn: sqlite3.Connection, employee_id: str, period_start: date, weeks: int = 8) -> None:
    mapping = {"shift-morning": "D", "shift-evening": "E", "shift-night": "N"}
    rows = conn.execute(
        """
        SELECT assignment_date, shift_template_id
        FROM shift_assignments
        WHERE employee_id=? AND schedule_period_id=?
        ORDER BY assignment_date
        """,
        (employee_id, PERIOD_ID),
    ).fetchall()
    by_date = {date.fromisoformat(day): mapping.get(tid, "?") for day, tid in rows}
    print(employee_id)
    for week in range(weeks):
        chunk = [
            by_date.get(period_start + timedelta(days=week * 7 + day), "-")
            for day in range(7)
        ]
        print(f"  W{week + 1}: {' '.join(chunk)}")


def main() -> None:
    db_path = ROOT / "demo.sqlite3"
    count = import_manual_schedule(db_path)
    print(f"Imported {count} assignments from {FIXTURE.name}")

    conn = sqlite3.connect(db_path)
    _, period_start = _load_fixture()
    for employee_id in ("portage-mlt-01", "portage-mla-06", "portage-mlt-05"):
        _print_line(conn, employee_id, period_start, weeks=4)
    conn.close()


if __name__ == "__main__":
    main()
