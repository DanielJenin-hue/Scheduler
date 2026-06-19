from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import List, Literal, Optional

ChangeType = Literal["manual_edit", "auto_generation", "seniority_bypass", "constraint_violation"]

DEFAULT_AUDIT_ACTOR = "manager"


@dataclass(frozen=True, slots=True)
class ScheduleAuditEntry:
    id: int
    tenant_id: str
    schedule_period_id: Optional[str]
    recorded_at_utc: str
    actor: str
    employee_id: Optional[str]
    employee_name: Optional[str]
    shift_date: Optional[date]
    previous_shift_code: Optional[str]
    new_shift_code: Optional[str]
    change_type: ChangeType
    seniority_bypass_flag: bool = False
    seniority_bypass_justification: Optional[str] = None


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def ensure_audit_schema(conn: sqlite3.Connection) -> None:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(schedule_audit_logs)")}
    if not cols:
        return

    if "seniority_bypass_flag" not in cols:
        conn.execute(
            """
            ALTER TABLE schedule_audit_logs
            ADD COLUMN seniority_bypass_flag INTEGER NOT NULL DEFAULT 0
            """
        )
    if "seniority_bypass_justification" not in cols:
        conn.execute(
            """
            ALTER TABLE schedule_audit_logs
            ADD COLUMN seniority_bypass_justification TEXT
            """
        )

    table_sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'schedule_audit_logs'"
    ).fetchone()
    ddl = (table_sql[0] or "") if table_sql else ""
    if ddl and "'constraint_violation'" not in ddl:
        _rebuild_schedule_audit_logs_change_type_check(conn)

    conn.commit()


def _rebuild_schedule_audit_logs_change_type_check(conn: sqlite3.Connection) -> None:
    """Expand change_type CHECK to allow seniority_bypass audit entries."""

    conn.executescript(
        """
        CREATE TABLE schedule_audit_logs__new (
          id                  INTEGER PRIMARY KEY AUTOINCREMENT,
          tenant_id           TEXT NOT NULL,
          schedule_period_id  TEXT,
          recorded_at_utc     TEXT NOT NULL,
          actor               TEXT NOT NULL DEFAULT 'manager',
          employee_id         TEXT,
          shift_date          TEXT,
          previous_shift_code TEXT,
          new_shift_code      TEXT,
          change_type         TEXT NOT NULL,
          seniority_bypass_flag INTEGER NOT NULL DEFAULT 0,
          seniority_bypass_justification TEXT,
          CHECK (change_type IN ('manual_edit', 'auto_generation', 'seniority_bypass', 'constraint_violation'))
        );

        INSERT INTO schedule_audit_logs__new (
          id, tenant_id, schedule_period_id, recorded_at_utc, actor,
          employee_id, shift_date, previous_shift_code, new_shift_code, change_type,
          seniority_bypass_flag, seniority_bypass_justification
        )
        SELECT
          id, tenant_id, schedule_period_id, recorded_at_utc, actor,
          employee_id, shift_date, previous_shift_code, new_shift_code, change_type,
          COALESCE(seniority_bypass_flag, 0),
          seniority_bypass_justification
        FROM schedule_audit_logs;

        DROP TABLE schedule_audit_logs;
        ALTER TABLE schedule_audit_logs__new RENAME TO schedule_audit_logs;

        CREATE INDEX IF NOT EXISTS idx_schedule_audit_logs_tenant_period
          ON schedule_audit_logs (tenant_id, schedule_period_id, id DESC);

        CREATE INDEX IF NOT EXISTS idx_schedule_audit_logs_tenant_time
          ON schedule_audit_logs (tenant_id, recorded_at_utc DESC);
        """
    )


def ensure_seniority_cba_schema(conn: sqlite3.Connection) -> None:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(employees)")}
    if "seniority_hours" not in cols:
        conn.execute(
            """
            ALTER TABLE employees
            ADD COLUMN seniority_hours REAL NOT NULL DEFAULT 0.0
            """
        )
    conn.execute(
        """
        UPDATE employees
        SET seniority_hours = ROUND(
          MAX(0.0, (julianday('now') - julianday(hire_date)) / 365.25) * 2080.0,
          1
        )
        WHERE seniority_hours = 0.0 OR seniority_hours IS NULL
        """
    )
    ensure_audit_schema(conn)
    conn.commit()


def _normalize_code(code: Optional[str]) -> str:
    if code is None or not str(code).strip():
        return ""
    return str(code).strip().upper()[:1] if len(str(code).strip()) == 1 else str(code).strip()


def log_manual_edit(
    conn,
    *,
    tenant_id: str,
    schedule_period_id: str,
    employee_id: str,
    shift_date: date,
    previous_shift_code: str,
    new_shift_code: str,
    actor: str = DEFAULT_AUDIT_ACTOR,
    seniority_bypass_flag: bool = False,
    seniority_bypass_justification: Optional[str] = None,
) -> int:
    """Append an immutable record for a successful inline grid edit."""

    ensure_audit_schema(conn)
    cur = conn.execute(
        """
        INSERT INTO schedule_audit_logs (
          tenant_id, schedule_period_id, recorded_at_utc, actor,
          employee_id, shift_date, previous_shift_code, new_shift_code, change_type,
          seniority_bypass_flag, seniority_bypass_justification
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'manual_edit', ?, ?)
        """,
        (
            tenant_id,
            schedule_period_id,
            utc_now_iso(),
            actor,
            employee_id,
            shift_date.isoformat(),
            _normalize_code(previous_shift_code) or None,
            _normalize_code(new_shift_code) or None,
            1 if seniority_bypass_flag else 0,
            seniority_bypass_justification,
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def log_shift_cell_lock(
    conn,
    *,
    tenant_id: str,
    schedule_period_id: str,
    employee_id: str,
    assignment_date: date,
    locked: bool,
    actor: str = DEFAULT_AUDIT_ACTOR,
) -> int:
    """Audit trail entry for manager shift cell lock / unlock."""

    ensure_audit_schema(conn)
    cur = conn.execute(
        """
        INSERT INTO schedule_audit_logs (
          tenant_id, schedule_period_id, recorded_at_utc, actor,
          employee_id, shift_date, previous_shift_code, new_shift_code, change_type,
          seniority_bypass_flag, seniority_bypass_justification
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'manual_edit', 0, ?)
        """,
        (
            tenant_id,
            schedule_period_id,
            utc_now_iso(),
            actor,
            employee_id,
            assignment_date.isoformat(),
            "LOCK" if not locked else None,
            "LOCK" if locked else None,
            "shift_cell_lock" if locked else "shift_cell_unlock",
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def log_reactive_swap(
    conn,
    *,
    tenant_id: str,
    schedule_period_id: str,
    shift_id: str,
    old_employee_id: str,
    new_employee_id: str,
    shift_date: date,
    previous_shift_code: str,
    actor: str = DEFAULT_AUDIT_ACTOR,
    summary: str = "",
    seniority_bypass_flag: bool = False,
    seniority_bypass_justification: Optional[str] = None,
) -> int:
    """Append an immutable audit record for a reactive shift swap."""

    ensure_audit_schema(conn)
    cur = conn.execute(
        """
        INSERT INTO schedule_audit_logs (
          tenant_id, schedule_period_id, recorded_at_utc, actor,
          employee_id, shift_date, previous_shift_code, new_shift_code, change_type,
          seniority_bypass_flag, seniority_bypass_justification
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'manual_edit', ?, ?)
        """,
        (
            tenant_id,
            schedule_period_id,
            utc_now_iso(),
            f"{actor} [reactive-swap]",
            new_employee_id,
            shift_date.isoformat(),
            f"{previous_shift_code}:{old_employee_id}",
            f"SWAP:{shift_id}->{new_employee_id}",
            1 if seniority_bypass_flag else 0,
            seniority_bypass_justification or summary or None,
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def log_seniority_bypass(
    conn,
    *,
    tenant_id: str,
    schedule_period_id: str,
    employee_id: str,
    shift_date: date,
    shift_code: str,
    justification: str,
    actor: str = DEFAULT_AUDIT_ACTOR,
) -> int:
    """Record a CBA seniority bypass with mandatory justification."""

    ensure_audit_schema(conn)
    cur = conn.execute(
        """
        INSERT INTO schedule_audit_logs (
          tenant_id, schedule_period_id, recorded_at_utc, actor,
          employee_id, shift_date, previous_shift_code, new_shift_code, change_type,
          seniority_bypass_flag, seniority_bypass_justification
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'seniority_bypass', 1, ?)
        """,
        (
            tenant_id,
            schedule_period_id,
            utc_now_iso(),
            actor,
            employee_id,
            shift_date.isoformat(),
            "SENIORITY_BYPASS",
            _normalize_code(shift_code) or shift_code,
            justification,
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def log_constraint_violation(
    conn,
    *,
    tenant_id: str,
    schedule_period_id: str,
    shift_date: date,
    shift_code: str,
    violation_code: str,
    message: str,
    actor: str = DEFAULT_AUDIT_ACTOR,
) -> int:
    """Record an unfilled shift left open to preserve labor-law compliance."""

    ensure_audit_schema(conn)
    cur = conn.execute(
        """
        INSERT INTO schedule_audit_logs (
          tenant_id, schedule_period_id, recorded_at_utc, actor,
          employee_id, shift_date, previous_shift_code, new_shift_code, change_type,
          seniority_bypass_flag, seniority_bypass_justification
        ) VALUES (?, ?, ?, ?, NULL, ?, ?, ?, 'constraint_violation', 0, ?)
        """,
        (
            tenant_id,
            schedule_period_id,
            utc_now_iso(),
            actor,
            shift_date.isoformat(),
            "UNASSIGNED",
            _normalize_code(shift_code) or shift_code,
            f"{violation_code}: {message}",
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def log_auto_generation(
    conn,
    *,
    tenant_id: str,
    schedule_period_id: str,
    assignments_written: int,
    slots_filled: int,
    slots_total: int,
    actor: str = DEFAULT_AUDIT_ACTOR,
) -> int:
    """Append a single macro audit event after algorithmic schedule rebuild."""

    ensure_audit_schema(conn)
    summary = (
        f"GENERATED:{assignments_written} assignments "
        f"({slots_filled}/{slots_total} slots filled)"
    )
    cur = conn.execute(
        """
        INSERT INTO schedule_audit_logs (
          tenant_id, schedule_period_id, recorded_at_utc, actor,
          employee_id, shift_date, previous_shift_code, new_shift_code, change_type,
          seniority_bypass_flag, seniority_bypass_justification
        ) VALUES (?, ?, ?, ?, NULL, NULL, ?, ?, 'auto_generation', 0, NULL)
        """,
        (
            tenant_id,
            schedule_period_id,
            utc_now_iso(),
            actor,
            "FULL_REBUILD",
            summary,
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def fetch_audit_logs(
    conn,
    *,
    tenant_id: str,
    schedule_period_id: str,
    limit: int = 200,
) -> List[ScheduleAuditEntry]:
    ensure_audit_schema(conn)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(schedule_audit_logs)")}
    has_bypass_cols = "seniority_bypass_flag" in cols

    if has_bypass_cols:
        rows = conn.execute(
            """
            SELECT
              l.id,
              l.tenant_id,
              l.schedule_period_id,
              l.recorded_at_utc,
              l.actor,
              l.employee_id,
              TRIM(e.first_name || ' ' || e.last_name) AS employee_name,
              l.shift_date,
              l.previous_shift_code,
              l.new_shift_code,
              l.change_type,
              l.seniority_bypass_flag,
              l.seniority_bypass_justification
            FROM schedule_audit_logs l
            LEFT JOIN employees e
              ON e.tenant_id = l.tenant_id AND e.id = l.employee_id
            WHERE l.tenant_id = ? AND l.schedule_period_id = ?
            ORDER BY l.id DESC
            LIMIT ?
            """,
            (tenant_id, schedule_period_id, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT
              l.id,
              l.tenant_id,
              l.schedule_period_id,
              l.recorded_at_utc,
              l.actor,
              l.employee_id,
              TRIM(e.first_name || ' ' || e.last_name) AS employee_name,
              l.shift_date,
              l.previous_shift_code,
              l.new_shift_code,
              l.change_type
            FROM schedule_audit_logs l
            LEFT JOIN employees e
              ON e.tenant_id = l.tenant_id AND e.id = l.employee_id
            WHERE l.tenant_id = ? AND l.schedule_period_id = ?
            ORDER BY l.id DESC
            LIMIT ?
            """,
            (tenant_id, schedule_period_id, limit),
        ).fetchall()

    entries: List[ScheduleAuditEntry] = []
    for r in rows:
        shift_date = date.fromisoformat(r[7]) if r[7] else None
        if has_bypass_cols:
            entries.append(
                ScheduleAuditEntry(
                    id=int(r[0]),
                    tenant_id=r[1],
                    schedule_period_id=r[2],
                    recorded_at_utc=r[3],
                    actor=r[4],
                    employee_id=r[5],
                    employee_name=r[6] or None,
                    shift_date=shift_date,
                    previous_shift_code=r[8],
                    new_shift_code=r[9],
                    change_type=r[10],
                    seniority_bypass_flag=bool(r[11]),
                    seniority_bypass_justification=r[12],
                )
            )
        else:
            entries.append(
                ScheduleAuditEntry(
                    id=int(r[0]),
                    tenant_id=r[1],
                    schedule_period_id=r[2],
                    recorded_at_utc=r[3],
                    actor=r[4],
                    employee_id=r[5],
                    employee_name=r[6] or None,
                    shift_date=shift_date,
                    previous_shift_code=r[8],
                    new_shift_code=r[9],
                    change_type=r[10],
                )
            )
    return entries


def format_shift_code_display(code: Optional[str]) -> str:
    if code is None or not str(code).strip():
        return "—"
    text = str(code).strip()
    if text == "FULL_REBUILD":
        return "Full rebuild"
    if text == "SENIORITY_BYPASS":
        return "Seniority bypass"
    if text == "UNASSIGNED":
        return "Unassigned"
    return text
