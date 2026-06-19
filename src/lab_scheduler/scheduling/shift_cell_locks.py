"""Manager shift cell locks — per employee+date within a schedule period."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Dict, Iterable, List, Mapping, Set, Tuple

CellLockKey = Tuple[str, date]
LockBand = str  # D, E, N, ANY


@dataclass(frozen=True, slots=True)
class ShiftCellLock:
    employee_id: str
    assignment_date: date
    lock_band: LockBand = "ANY"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def ensure_shift_cell_locks_schema(conn: sqlite3.Connection) -> None:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='shift_cell_locks'"
    ).fetchone()
    if row is None:
        conn.executescript(
            """
            CREATE TABLE shift_cell_locks (
              tenant_id           TEXT NOT NULL,
              schedule_period_id  TEXT NOT NULL,
              employee_id         TEXT NOT NULL,
              assignment_date     TEXT NOT NULL,
              locked_at           TEXT NOT NULL,
              locked_by           TEXT NOT NULL,
              lock_band           TEXT NOT NULL DEFAULT 'ANY',
              PRIMARY KEY (tenant_id, schedule_period_id, employee_id, assignment_date),
              FOREIGN KEY (tenant_id, schedule_period_id)
                REFERENCES schedule_periods (tenant_id, id) ON DELETE CASCADE,
              FOREIGN KEY (tenant_id, employee_id)
                REFERENCES employees (tenant_id, id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_shift_cell_locks_tenant_period
              ON shift_cell_locks (tenant_id, schedule_period_id);
            """
        )
        conn.commit()
        return
    columns = {
        str(row[1])
        for row in conn.execute("PRAGMA table_info(shift_cell_locks)").fetchall()
    }
    if "lock_band" not in columns:
        conn.execute(
            "ALTER TABLE shift_cell_locks ADD COLUMN lock_band TEXT NOT NULL DEFAULT 'ANY'"
        )
        conn.commit()


def fetch_shift_cell_locks(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    schedule_period_id: str,
) -> Set[CellLockKey]:
    return {
        (lock.employee_id, lock.assignment_date)
        for lock in fetch_shift_cell_lock_records(
            conn,
            tenant_id=tenant_id,
            schedule_period_id=schedule_period_id,
        )
    }


def fetch_shift_cell_lock_records(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    schedule_period_id: str,
) -> Tuple[ShiftCellLock, ...]:
    ensure_shift_cell_locks_schema(conn)
    rows = conn.execute(
        """
        SELECT employee_id, assignment_date, lock_band
        FROM shift_cell_locks
        WHERE tenant_id = ? AND schedule_period_id = ?
        """,
        (tenant_id, schedule_period_id),
    ).fetchall()
    locks: list[ShiftCellLock] = []
    for employee_id, assignment_date, lock_band in rows:
        locks.append(
            ShiftCellLock(
                employee_id=str(employee_id),
                assignment_date=date.fromisoformat(str(assignment_date)),
                lock_band=str(lock_band or "ANY").upper(),
            )
        )
    return tuple(locks)


def fetch_shift_cell_lock_bands(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    schedule_period_id: str,
) -> Dict[CellLockKey, LockBand]:
    return {
        (lock.employee_id, lock.assignment_date): lock.lock_band
        for lock in fetch_shift_cell_lock_records(
            conn,
            tenant_id=tenant_id,
            schedule_period_id=schedule_period_id,
        )
    }


def is_shift_cell_locked(
    locks: Set[CellLockKey],
    *,
    employee_id: str,
    assignment_date: date,
) -> bool:
    return (employee_id, assignment_date) in locks


def lock_band_for_cell(
    lock_bands: Mapping[CellLockKey, LockBand],
    *,
    employee_id: str,
    assignment_date: date,
    shift_band: str | None = None,
) -> LockBand | None:
    band = lock_bands.get((employee_id, assignment_date))
    if band is None:
        return None
    if band == "ANY":
        return band
    if shift_band is None:
        return band
    if band == shift_band.upper():
        return band
    return band


def monday_on_or_before(anchor_date: date) -> date:
    """Monday-start week containing ``anchor_date`` (Portage schedule weeks)."""

    return anchor_date - timedelta(days=anchor_date.weekday())


def week_dates_for_lock(
    anchor_date: date,
    *,
    period_start: date,
    period_end: date,
) -> List[date]:
    """All calendar days in the anchor's Mon–Sun week that fall inside the period."""

    week_start = monday_on_or_before(anchor_date)
    return [
        week_start + timedelta(days=offset)
        for offset in range(7)
        if period_start <= week_start + timedelta(days=offset) <= period_end
    ]


def expand_lock_toggle(
    toggle: dict[str, object],
    *,
    period_start: date | None = None,
    period_end: date | None = None,
) -> List[dict[str, object]]:
    """Expand a week-level toggle to per-day payloads (default scope is ``week``)."""

    employee_id = str(toggle.get("employee_id", "") or "")
    if not employee_id:
        return []
    locked = bool(toggle.get("locked", True))
    lock_band = str(toggle.get("lock_band", "ANY") or "ANY").upper()
    scope = str(toggle.get("scope", "week") or "week").strip().lower()
    date_raw = str(toggle.get("week_start") or toggle.get("date") or "")
    if not date_raw:
        return []
    if scope == "day" or period_start is None or period_end is None:
        return [
            {
                "employee_id": employee_id,
                "date": date_raw,
                "locked": locked,
                "lock_band": lock_band,
            }
        ]
    anchor = date.fromisoformat(date_raw)
    return [
        {
            "employee_id": employee_id,
            "date": assignment_date.isoformat(),
            "locked": locked,
            "lock_band": lock_band,
        }
        for assignment_date in week_dates_for_lock(
            anchor,
            period_start=period_start,
            period_end=period_end,
        )
    ]


def set_shift_cell_lock(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    schedule_period_id: str,
    employee_id: str,
    assignment_date: date,
    locked: bool,
    actor: str,
    lock_band: LockBand = "ANY",
) -> None:
    ensure_shift_cell_locks_schema(conn)
    if locked:
        conn.execute(
            """
            INSERT INTO shift_cell_locks (
              tenant_id, schedule_period_id, employee_id,
              assignment_date, locked_at, locked_by, lock_band
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(tenant_id, schedule_period_id, employee_id, assignment_date)
            DO UPDATE SET
              locked_at = excluded.locked_at,
              locked_by = excluded.locked_by,
              lock_band = excluded.lock_band
            """,
            (
                tenant_id,
                schedule_period_id,
                employee_id,
                assignment_date.isoformat(),
                utc_now_iso(),
                actor,
                lock_band.upper(),
            ),
        )
    else:
        conn.execute(
            """
            DELETE FROM shift_cell_locks
            WHERE tenant_id = ? AND schedule_period_id = ?
              AND employee_id = ? AND assignment_date = ?
            """,
            (
                tenant_id,
                schedule_period_id,
                employee_id,
                assignment_date.isoformat(),
            ),
        )
    conn.commit()


def apply_shift_cell_lock_toggles(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    schedule_period_id: str,
    toggles: Iterable[dict[str, object]],
    actor: str,
    period_start: date | None = None,
    period_end: date | None = None,
) -> int:
    """Apply lock/unlock toggles; week scope expands to all days in Mon–Sun week."""

    from lab_scheduler.audit.schedule_log import log_shift_cell_lock

    changed = 0
    for toggle in toggles:
        for day_toggle in expand_lock_toggle(
            toggle,
            period_start=period_start,
            period_end=period_end,
        ):
            employee_id = str(day_toggle.get("employee_id", "") or "")
            date_raw = str(day_toggle.get("date", "") or "")
            if not employee_id or not date_raw:
                continue
            assignment_date = date.fromisoformat(date_raw)
            locked = bool(day_toggle.get("locked", True))
            lock_band = str(day_toggle.get("lock_band", "ANY") or "ANY").upper()
            set_shift_cell_lock(
                conn,
                tenant_id=tenant_id,
                schedule_period_id=schedule_period_id,
                employee_id=employee_id,
                assignment_date=assignment_date,
                locked=locked,
                actor=actor,
                lock_band=lock_band,
            )
            log_shift_cell_lock(
                conn,
                tenant_id=tenant_id,
                schedule_period_id=schedule_period_id,
                employee_id=employee_id,
                assignment_date=assignment_date,
                locked=locked,
                actor=actor,
            )
            changed += 1
    return changed
