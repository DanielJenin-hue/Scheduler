"""Guided onboarding helpers: demo roster, schedule periods, completion flags."""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, List, Optional

from lab_scheduler.data.import_manager import next_employee_code
from lab_scheduler.simulation.portage_blueprint import (
    PORTAGE_ROSTER_SIZE,
    build_portage_blueprint_roster,
)
from lab_scheduler.simulation.hospital_stress import QUAL_MLT
from lab_scheduler.tenant.configuration import (
    get_tenant_config_value,
    set_tenant_config_value,
)
from lab_scheduler.time import workweek_for

__all__ = [
    "DEFAULT_JURISDICTION",
    "ONBOARDING_COMPLETE_KEY",
    "TRIAL_PREVIEW_SNAPSHOT_KEY",
    "apply_trial_preview_snapshot",
    "count_active_employees",
    "create_schedule_period",
    "is_onboarding_complete",
    "load_portage_demo_roster",
    "mark_onboarding_complete",
    "save_trial_preview_snapshot",
    "seed_lab_infrastructure",
    "tenant_has_schedule_period",
    "try_apply_global_trial_preview",
]

ONBOARDING_COMPLETE_KEY = "onboarding_complete"
TRIAL_PREVIEW_SNAPSHOT_KEY = "trial_preview_snapshot_v1"
DEFAULT_JURISDICTION = "Manitoba"
DEFAULT_PERIOD_WEEKS = 8
_TRIAL_PREVIEW_PATH = (
    Path(__file__).resolve().parents[2] / "data" / "trial_preview_portage_standard_2w.json"
)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _tenant_suffix(tenant_id: str) -> str:
    return tenant_id.rsplit("-", 1)[-1][:10]


def is_onboarding_complete(conn: sqlite3.Connection, *, tenant_id: str) -> bool:
    raw = get_tenant_config_value(
        conn,
        tenant_id=tenant_id,
        config_key=ONBOARDING_COMPLETE_KEY,
        default="1",
    )
    return str(raw).strip().lower() in {"1", "true", "yes"}


def mark_onboarding_complete(conn: sqlite3.Connection, *, tenant_id: str) -> None:
    set_tenant_config_value(
        conn,
        tenant_id=tenant_id,
        config_key=ONBOARDING_COMPLETE_KEY,
        config_value="1",
    )
    conn.commit()


def count_active_employees(conn: sqlite3.Connection, *, tenant_id: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) FROM employees WHERE tenant_id = ? AND is_active = 1",
        (tenant_id,),
    ).fetchone()
    return int(row[0] if row else 0)


def tenant_has_schedule_period(conn: sqlite3.Connection, *, tenant_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM schedule_periods WHERE tenant_id = ? LIMIT 1",
        (tenant_id,),
    ).fetchone()
    return row is not None


def seed_lab_infrastructure(conn: sqlite3.Connection, *, tenant_id: str) -> None:
    """Seed Manitoba M/E/N shift templates and MLT/MLA qualifications for a tenant."""

    suffix = _tenant_suffix(tenant_id)
    now = _utc_now_iso()
    qual_mlt_id = f"qual-mlt-{suffix}"
    qual_mla_id = f"qual-mla-{suffix}"
    shift_morning_id = f"shift-morning-{suffix}"
    shift_evening_id = f"shift-evening-{suffix}"
    shift_night_id = f"shift-night-{suffix}"

    conn.execute(
        """
        INSERT INTO qualifications (id, tenant_id, code, display_name, description, is_active, created_at)
        VALUES
          (?, ?, 'MLT', 'Medical Laboratory Technologist',
           'Performs diagnostic laboratory testing and analysis.', 1, ?),
          (?, ?, 'MLA', 'Medical Laboratory Assistant',
           'Specimen processing and front-end laboratory support.', 1, ?)
        """,
        (
            qual_mlt_id,
            tenant_id,
            now,
            qual_mla_id,
            tenant_id,
            now,
        ),
    )
    conn.execute(
        """
        INSERT INTO shift_templates (
          id, tenant_id, code, name, start_time, end_time,
          duration_minutes, crosses_midnight, is_active, created_at, updated_at
        ) VALUES
          (?, ?, 'MORNING', 'Morning Shift', '07:00', '15:00', 480, 0, 1, ?, ?),
          (?, ?, 'EVENING', 'Evening Shift', '15:00', '23:00', 480, 0, 1, ?, ?),
          (?, ?, 'NIGHT', 'Night Shift', '23:00', '07:00', 480, 1, 1, ?, ?)
        """,
        (
            shift_morning_id,
            tenant_id,
            now,
            now,
            shift_evening_id,
            tenant_id,
            now,
            now,
            shift_night_id,
            tenant_id,
            now,
            now,
        ),
    )
    for shift_id, qual_id in (
        (shift_morning_id, qual_mlt_id),
        (shift_morning_id, qual_mla_id),
        (shift_evening_id, qual_mlt_id),
        (shift_evening_id, qual_mla_id),
        (shift_night_id, qual_mlt_id),
    ):
        conn.execute(
            """
            INSERT INTO shift_template_qualifications (
              tenant_id, shift_template_id, qualification_id, created_at
            ) VALUES (?, ?, ?, ?)
            """,
            (tenant_id, shift_id, qual_id, now),
        )


def _qualification_ids(conn: sqlite3.Connection, *, tenant_id: str) -> dict[str, str]:
    rows = conn.execute(
        """
        SELECT code, id FROM qualifications
        WHERE tenant_id = ? AND is_active = 1
        """,
        (tenant_id,),
    ).fetchall()
    return {str(code): str(qual_id) for code, qual_id in rows}


def load_portage_demo_roster(conn: sqlite3.Connection, *, tenant_id: str) -> int:
    """Insert the canonical 25-line Portage demo roster for trial Auto-Pilot."""

    existing = count_active_employees(conn, tenant_id=tenant_id)
    if existing > 0:
        raise ValueError("Roster already has employees. Clear the workspace or skip demo load.")

    qual_ids = _qualification_ids(conn, tenant_id=tenant_id)
    if "MLT" not in qual_ids or "MLA" not in qual_ids:
        raise ValueError("Lab qualifications are not provisioned for this tenant.")

    roster = build_portage_blueprint_roster()
    hire = date.today().isoformat()
    now = _utc_now_iso()
    inserted = 0

    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("BEGIN IMMEDIATE")
    try:
        for profile in roster:
            full_name = profile.full_name.strip()
            line_token = full_name.rsplit(" ", 1)[-1]
            first_name = full_name[: -(len(line_token) + 1)]
            last_name = line_token
            role_code = "MLT" if QUAL_MLT in profile.qualification_ids else "MLA"
            qual_id = qual_ids[role_code]
            employee_id = f"emp-{uuid.uuid4().hex[:10]}"
            employee_code = next_employee_code(conn, tenant_id)

            conn.execute(
                """
                INSERT INTO employees (
                  id, tenant_id, employee_code, first_name, last_name,
                  hire_date, fte, base_hourly_rate, seniority_hours, contract_line_type,
                  is_active, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                """,
                (
                    employee_id,
                    tenant_id,
                    employee_code,
                    first_name,
                    last_name,
                    hire,
                    float(profile.fte),
                    float(profile.base_hourly_rate),
                    float(profile.seniority_hours),
                    profile.contract_line_type,
                    now,
                    now,
                ),
            )
            conn.execute(
                """
                INSERT INTO employee_qualifications (
                  tenant_id, employee_id, qualification_id, awarded_on, expires_on, created_at
                ) VALUES (?, ?, ?, ?, NULL, ?)
                """,
                (tenant_id, employee_id, qual_id, hire, now),
            )
            inserted += 1

        conn.commit()
    except Exception:
        conn.rollback()
        raise

    if inserted != PORTAGE_ROSTER_SIZE:
        raise RuntimeError(f"Expected {PORTAGE_ROSTER_SIZE} demo lines, inserted {inserted}.")
    return inserted


def create_schedule_period(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    period_start: Optional[date] = None,
    week_count: int = DEFAULT_PERIOD_WEEKS,
    name: Optional[str] = None,
) -> str:
    """Create an 8-week Monday-start master rotation period."""

    if week_count < 1:
        raise ValueError("week_count must be positive")

    start = workweek_for(period_start or date.today()).start
    end = start + timedelta(days=week_count * 7 - 1)
    period_id = f"period-{uuid.uuid4().hex[:10]}"
    label = name or f"Master Rotation {start.isoformat()}"
    now = _utc_now_iso()

    conn.execute(
        """
        INSERT INTO schedule_periods (
          id, tenant_id, name, period_start, week_count, period_end_inclusive,
          status, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, 'draft', ?, ?)
        """,
        (
            period_id,
            tenant_id,
            label,
            start.isoformat(),
            week_count,
            end.isoformat(),
            now,
            now,
        ),
    )
    conn.commit()
    return period_id


def _shift_template_ids_by_code(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
) -> dict[str, str]:
    rows = conn.execute(
        """
        SELECT code, id FROM shift_templates
        WHERE tenant_id = ? AND is_active = 1
        """,
        (tenant_id,),
    ).fetchall()
    return {str(code): str(shift_id) for code, shift_id in rows}


def _ordered_employee_ids(conn: sqlite3.Connection, *, tenant_id: str) -> List[str]:
    rows = conn.execute(
        """
        SELECT id FROM employees
        WHERE tenant_id = ? AND is_active = 1
        ORDER BY employee_code
        """,
        (tenant_id,),
    ).fetchall()
    return [str(row[0]) for row in rows]


def save_trial_preview_snapshot(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    period_start: date,
    assignments: List[dict[str, Any]],
) -> None:
    """Persist a portable trial preview pattern for future onboarding replays."""

    employee_ids = _ordered_employee_ids(conn, tenant_id=tenant_id)
    employee_index = {employee_id: idx for idx, employee_id in enumerate(employee_ids)}
    payload = {
        "period_start": period_start.isoformat(),
        "entries": [
            {
                "employee_index": employee_index[item["employee_id"]],
                "shift_template_id": item["shift_template_id"],
                "day_offset": (
                    date.fromisoformat(str(item["assignment_date"])) - period_start
                ).days,
            }
            for item in assignments
            if item["employee_id"] in employee_index
        ],
    }
    set_tenant_config_value(
        conn,
        tenant_id=tenant_id,
        config_key=TRIAL_PREVIEW_SNAPSHOT_KEY,
        config_value=json.dumps(payload),
    )
    conn.commit()
    _TRIAL_PREVIEW_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not _TRIAL_PREVIEW_PATH.exists():
        portable = {
            "shift_codes": ["MORNING", "EVENING", "NIGHT"],
            "entries": [
                {
                    "employee_index": entry["employee_index"],
                    "shift_code": _shift_code_from_template_id(
                        conn,
                        tenant_id=tenant_id,
                        shift_template_id=entry["shift_template_id"],
                    ),
                    "day_offset": entry["day_offset"],
                }
                for entry in payload["entries"]
            ],
        }
        _TRIAL_PREVIEW_PATH.write_text(json.dumps(portable, indent=2), encoding="utf-8")


def _shift_code_from_template_id(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    shift_template_id: str,
) -> str:
    row = conn.execute(
        """
        SELECT code FROM shift_templates
        WHERE tenant_id = ? AND id = ?
        """,
        (tenant_id, shift_template_id),
    ).fetchone()
    return str(row[0]) if row else "MORNING"


def apply_trial_preview_snapshot(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    schedule_period_id: str,
    period_start: date,
    snapshot_json: str,
) -> int:
    payload = json.loads(snapshot_json)
    employee_ids = _ordered_employee_ids(conn, tenant_id=tenant_id)
    shift_ids = _shift_template_ids_by_code(conn, tenant_id=tenant_id)
    now = _utc_now_iso()
    inserted = 0
    conn.execute(
        """
        DELETE FROM shift_assignments
        WHERE tenant_id = ? AND schedule_period_id = ?
        """,
        (tenant_id, schedule_period_id),
    )
    for entry in payload.get("entries", []):
        employee_index = int(entry["employee_index"])
        if employee_index < 0 or employee_index >= len(employee_ids):
            continue
        shift_code = str(entry.get("shift_code") or "")
        if shift_code:
            shift_template_id = shift_ids.get(shift_code.upper())
        else:
            shift_template_id = str(entry.get("shift_template_id") or "")
        if not shift_template_id:
            continue
        assignment_date = period_start + timedelta(days=int(entry["day_offset"]))
        assignment_id = f"asg-{uuid.uuid4().hex[:10]}"
        conn.execute(
            """
            INSERT INTO shift_assignments (
              id, tenant_id, schedule_period_id, employee_id, shift_template_id,
              assignment_date, created_at, updated_at, assignment_status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'assigned')
            """,
            (
                assignment_id,
                tenant_id,
                schedule_period_id,
                employee_ids[employee_index],
                shift_template_id,
                assignment_date.isoformat(),
                now,
                now,
            ),
        )
        inserted += 1
    conn.commit()
    return inserted


def try_apply_global_trial_preview(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    schedule_period_id: str,
    period_start: date,
) -> int:
    tenant_raw = get_tenant_config_value(
        conn,
        tenant_id=tenant_id,
        config_key=TRIAL_PREVIEW_SNAPSHOT_KEY,
        default="",
    )
    if tenant_raw:
        return apply_trial_preview_snapshot(
            conn,
            tenant_id=tenant_id,
            schedule_period_id=schedule_period_id,
            period_start=period_start,
            snapshot_json=str(tenant_raw),
        )
    if not _TRIAL_PREVIEW_PATH.exists():
        return 0
    portable = json.loads(_TRIAL_PREVIEW_PATH.read_text(encoding="utf-8"))
    return apply_trial_preview_snapshot(
        conn,
        tenant_id=tenant_id,
        schedule_period_id=schedule_period_id,
        period_start=period_start,
        snapshot_json=json.dumps(portable),
    )
