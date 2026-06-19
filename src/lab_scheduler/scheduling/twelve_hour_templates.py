"""Database helpers for twelve-hour schedule mode shift templates."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Sequence

from lab_scheduler.scheduling.auto_generate import PlannedAssignment
from lab_scheduler.scheduling.strategies.twelve_hour_7on7off_strategy import (
    FTE_TOPUP_TEMPLATE_CODE,
    FTE_TOPUP_TEMPLATE_ID,
)

# 6.125h structural top-up (375 minutes) closes the 313.875h → 320h FTE gap.
FTE_TOPUP_DURATION_MINUTES = 375


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def fte_topup_template_id_for_tenant(
    tenant_id: str,
    *,
    conn: sqlite3.Connection | None = None,
) -> str:
    """
    Resolve the persisted shift template id for a tenant's FTE top-up row.

    ``shift_templates.id`` is globally unique, so each tenant needs its own row.
    Legacy databases may still have the unscoped ``twelve-hour-fte-topup`` id for
    the first migrated tenant; keep using that row when present.
    """

    if conn is not None:
        legacy = conn.execute(
            """
            SELECT 1
            FROM shift_templates
            WHERE id = ? AND tenant_id = ?
            """,
            (FTE_TOPUP_TEMPLATE_ID, tenant_id),
        ).fetchone()
        if legacy:
            return FTE_TOPUP_TEMPLATE_ID
    return f"{tenant_id}::{FTE_TOPUP_TEMPLATE_ID}"


def remap_topup_assignments_for_persist(
    assignments: Sequence[PlannedAssignment],
    tenant_id: str,
    *,
    conn: sqlite3.Connection,
) -> tuple[PlannedAssignment, ...]:
    """Map strategy-level top-up ids to the tenant-owned ``shift_templates`` row."""

    persisted_topup_id = fte_topup_template_id_for_tenant(tenant_id, conn=conn)
    if persisted_topup_id == FTE_TOPUP_TEMPLATE_ID:
        return tuple(assignments)

    remapped: list[PlannedAssignment] = []
    for assignment in assignments:
        if assignment.shift_template_id != FTE_TOPUP_TEMPLATE_ID:
            remapped.append(assignment)
            continue
        remapped.append(
            PlannedAssignment(
                employee_id=assignment.employee_id,
                shift_template_id=persisted_topup_id,
                assignment_date=assignment.assignment_date,
                forced_clinical_ot=assignment.forced_clinical_ot,
                overtime_compliance_bypassed=assignment.overtime_compliance_bypassed,
                approved_stretch=assignment.approved_stretch,
                clinical_floor_stretch=assignment.clinical_floor_stretch,
                provisional_compliance=assignment.provisional_compliance,
                contract_line_exception=assignment.contract_line_exception,
                contract_line_exception_message=assignment.contract_line_exception_message,
            )
        )
    return tuple(remapped)


def _topup_qualification_ids(conn: sqlite3.Connection, tenant_id: str) -> list[str]:
    rows = conn.execute(
        """
        SELECT id
        FROM qualifications
        WHERE tenant_id = ?
          AND is_active = 1
          AND code IN ('MLT', 'MLA')
        ORDER BY code
        """,
        (tenant_id,),
    ).fetchall()
    return [str(row[0]) for row in rows]


def ensure_twelve_hour_shift_templates(conn: sqlite3.Connection, tenant_id: str) -> None:
    """
    Idempotently upsert the synthetic FTE top-up shift template required for TWELVE_HOUR persist.

    ``shift_assignments.shift_template_id`` references ``shift_templates``; without this row,
    Auto-Pilot persist rolls back and leaves stale assignments in the database.
    """

    conn.execute("PRAGMA foreign_keys = ON;")
    now = _utc_now_iso()
    template_id = fte_topup_template_id_for_tenant(tenant_id, conn=conn)
    conn.execute(
        """
        INSERT INTO shift_templates (
          id, tenant_id, code, name, start_time, end_time,
          duration_minutes, crosses_midnight, is_active, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, 0, 1, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
          tenant_id = excluded.tenant_id,
          code = excluded.code,
          name = excluded.name,
          duration_minutes = excluded.duration_minutes,
          updated_at = excluded.updated_at
        """,
        (
            template_id,
            tenant_id,
            FTE_TOPUP_TEMPLATE_CODE,
            "FTE Top-up Shift",
            "08:00",
            "14:07",
            FTE_TOPUP_DURATION_MINUTES,
            now,
            now,
        ),
    )
    for qualification_id in _topup_qualification_ids(conn, tenant_id):
        conn.execute(
            """
            INSERT OR IGNORE INTO shift_template_qualifications (
              tenant_id, shift_template_id, qualification_id, created_at
            ) VALUES (?, ?, ?, ?)
            """,
            (tenant_id, template_id, qualification_id, now),
        )
    conn.commit()
