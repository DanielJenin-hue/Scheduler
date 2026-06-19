"""Export/import schedule periods as portable JSON week-token archives."""
from __future__ import annotations

import json
import re
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Mapping, Sequence

SCHEDULE_ARCHIVE_VERSION = 1
TOKEN_TO_TEMPLATE = {
    "D": "shift-morning",
    "E": "shift-evening",
    "N": "shift-night",
}
TEMPLATE_TO_TOKEN = {value: key for key, value in TOKEN_TO_TEMPLATE.items()}


class ScheduleArchiveError(Exception):
    """Raised when a schedule archive cannot be read or applied."""


def default_saved_schedules_dir(project_root: Path | None = None) -> Path:
    root = project_root or Path(__file__).resolve().parents[3]
    return root / "saved_schedules"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _safe_name(name: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9._-]+", "-", name.strip()).strip("-")
    return cleaned[:80] or "schedule"


def _week_strings_from_pattern(pattern: str) -> list[str]:
    if len(pattern) != 56:
        raise ScheduleArchiveError(f"Expected 56-day pattern, got {len(pattern)}")
    return [pattern[index * 7 : (index + 1) * 7] for index in range(8)]


def _pattern_from_week_strings(weeks: Sequence[str]) -> str:
    if len(weeks) != 8:
        raise ScheduleArchiveError(f"Expected 8 weeks, got {len(weeks)}")
    pattern = "".join(weeks)
    if len(pattern) != 56:
        raise ScheduleArchiveError("Combined week strings must be 56 characters")
    for char in pattern:
        if char not in TOKEN_TO_TEMPLATE and char != "-":
            raise ScheduleArchiveError(f"Invalid shift token {char!r}")
    return pattern


def export_period_schedule(
    conn,
    *,
    tenant_id: str,
    period_id: str,
    name: str = "",
    description: str = "",
) -> dict:
    period_row = conn.execute(
        """
        SELECT period_start, period_end_inclusive
        FROM schedule_periods
        WHERE id = ? AND tenant_id = ?
        """,
        (period_id, tenant_id),
    ).fetchone()
    if period_row is None:
        raise ScheduleArchiveError(f"Unknown schedule period: {period_id}")

    period_start = date.fromisoformat(str(period_row[0]))
    period_end = date.fromisoformat(str(period_row[1]))
    expected_days = (period_end - period_start).days + 1
    if expected_days != 56:
        raise ScheduleArchiveError(
            f"Schedule archives require 8-week (56-day) periods; got {expected_days} days"
        )

    rows = conn.execute(
        """
        SELECT employee_id, assignment_date, shift_template_id
        FROM shift_assignments
        WHERE tenant_id = ? AND schedule_period_id = ?
          AND COALESCE(assignment_status, 'assigned') = 'assigned'
        ORDER BY employee_id, assignment_date
        """,
        (tenant_id, period_id),
    ).fetchall()

    roster_ids = [
        str(row[0])
        for row in conn.execute(
            """
            SELECT id
            FROM employees
            WHERE tenant_id = ? AND COALESCE(is_active, 1) = 1
            ORDER BY id
            """,
            (tenant_id,),
        ).fetchall()
    ]

    by_employee: dict[str, dict[date, str]] = {employee_id: {} for employee_id in roster_ids}
    for employee_id, day_str, template_id in rows:
        token = TEMPLATE_TO_TOKEN.get(str(template_id))
        if token is None:
            continue
        employee_key = str(employee_id)
        if employee_key not in by_employee:
            by_employee[employee_key] = {}
        by_employee[employee_key][date.fromisoformat(str(day_str))] = token

    employees: dict[str, list[str]] = {}
    for employee_id in sorted(by_employee):
        day_tokens = by_employee[employee_id]
        pattern = "".join(
            day_tokens.get(period_start + timedelta(days=index), "-") for index in range(56)
        )
        employees[employee_id] = _week_strings_from_pattern(pattern)

    return {
        "archive_version": SCHEDULE_ARCHIVE_VERSION,
        "name": name.strip(),
        "description": description.strip(),
        "exported_at": _utc_now_iso(),
        "tenant_id": tenant_id,
        "period_id": period_id,
        "period_start": period_start.isoformat(),
        "period_end": period_end.isoformat(),
        "employees": employees,
    }


def import_period_schedule(
    conn,
    payload: Mapping[str, object],
    *,
    tenant_id: str,
    period_id: str | None = None,
) -> int:
    if int(payload.get("archive_version", 0) or 0) != SCHEDULE_ARCHIVE_VERSION:
        raise ScheduleArchiveError("Unsupported schedule archive version")

    target_period_id = str(period_id or payload.get("period_id") or "")
    if not target_period_id:
        raise ScheduleArchiveError("Schedule period id is required")

    period_start = date.fromisoformat(str(payload["period_start"]))
    employees = payload.get("employees")
    if not isinstance(employees, dict) or not employees:
        raise ScheduleArchiveError("Archive has no employee week data")

    period_row = conn.execute(
        """
        SELECT period_start, period_end_inclusive
        FROM schedule_periods
        WHERE id = ? AND tenant_id = ?
        """,
        (target_period_id, tenant_id),
    ).fetchone()
    if period_row is None:
        raise ScheduleArchiveError(f"Unknown schedule period: {target_period_id}")

    if date.fromisoformat(str(period_row[0])) != period_start:
        raise ScheduleArchiveError(
            "Archive period_start does not match the active schedule period"
        )

    now = _utc_now_iso()
    inserted = 0
    conn.execute(
        """
        DELETE FROM shift_assignments
        WHERE tenant_id = ? AND schedule_period_id = ?
        """,
        (tenant_id, target_period_id),
    )

    for employee_id, weeks in employees.items():
        if not isinstance(weeks, list):
            raise ScheduleArchiveError(f"{employee_id}: expected a list of week strings")
        pattern = _pattern_from_week_strings([str(week) for week in weeks])
        exists = conn.execute(
            "SELECT 1 FROM employees WHERE id = ? AND tenant_id = ?",
            (employee_id, tenant_id),
        ).fetchone()
        if not exists:
            raise ScheduleArchiveError(f"Unknown employee id: {employee_id}")

        for index, token in enumerate(pattern):
            if token not in TOKEN_TO_TEMPLATE:
                continue
            assignment_date = period_start + timedelta(days=index)
            conn.execute(
                """
                INSERT INTO shift_assignments (
                  id, tenant_id, schedule_period_id, employee_id,
                  shift_template_id, assignment_date, assignment_status,
                  created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'assigned', ?, ?)
                """,
                (
                    f"asg-{uuid.uuid4().hex[:12]}",
                    tenant_id,
                    target_period_id,
                    employee_id,
                    TOKEN_TO_TEMPLATE[token],
                    assignment_date.isoformat(),
                    now,
                    now,
                ),
            )
            inserted += 1

    conn.commit()
    return inserted


def save_named_archive(
    payload: Mapping[str, object],
    *,
    name: str,
    saved_dir: Path | None = None,
) -> Path:
    directory = saved_dir or default_saved_schedules_dir()
    directory.mkdir(parents=True, exist_ok=True)
    safe = _safe_name(name)
    destination = directory / f"{safe}.json"
    counter = 1
    while destination.exists():
        destination = directory / f"{safe}-{counter}.json"
        counter += 1
    destination.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return destination


def list_named_archives(saved_dir: Path | None = None) -> list[Path]:
    directory = saved_dir or default_saved_schedules_dir()
    if not directory.is_dir():
        return []
    return sorted(directory.glob("*.json"), key=lambda path: path.stat().st_mtime, reverse=True)


def load_named_archive(path: Path) -> dict:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ScheduleArchiveError("Archive file must contain a JSON object")
    return payload


def touch_schedule_reload_stamp(project_root: Path | None = None) -> Path:
    stamp = (project_root or Path(__file__).resolve().parents[3]) / ".last_schedule_import"
    stamp.write_text(_utc_now_iso(), encoding="utf-8")
    return stamp
