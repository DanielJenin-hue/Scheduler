"""Durable per-tenant configuration store.

This module is the persistent source of truth for per-tenant settings that were
previously held only in ephemeral Streamlit session state. It is backed by the
``tenant_configurations`` key/value table (see ``sql/16_tenant_configurations.sql``)
so that settings such as the default schedule archetype survive new sessions and
can be read by headless / batch generation paths.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Optional

from lab_scheduler.scheduling.strategies import ScheduleArchetype, normalize_archetype

__all__ = [
    "SCHEDULE_ARCHETYPE_KEY",
    "MANAGER_MODE_KEY",
    "SCHEDULING_PREFERENCE_POLICY_KEY",
    "ensure_tenant_configuration_schema",
    "get_tenant_config_value",
    "set_tenant_config_value",
    "get_tenant_schedule_archetype",
    "set_tenant_schedule_archetype",
]

SCHEDULE_ARCHETYPE_KEY = "schedule_archetype"
MANAGER_MODE_KEY = "manager_mode"
SCHEDULING_PREFERENCE_POLICY_KEY = "scheduling_preference_policy"


def ensure_tenant_configuration_schema(conn: sqlite3.Connection) -> None:
    """Create the ``tenant_configurations`` table if it does not yet exist.

    Idempotent; safe to call alongside the SQL migration so that in-memory
    connections (tests) and partially-migrated databases both get the table.
    """

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tenant_configurations (
          tenant_id    TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
          config_key   TEXT NOT NULL,
          config_value TEXT,
          updated_at   TEXT NOT NULL,
          PRIMARY KEY (tenant_id, config_key)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_tenant_configurations_tenant "
        "ON tenant_configurations (tenant_id)"
    )


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_tenant_config_value(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    config_key: str,
    default: Optional[str] = None,
) -> Optional[str]:
    """Return the stored value for ``config_key``, or ``default`` if unset."""

    row = conn.execute(
        "SELECT config_value FROM tenant_configurations "
        "WHERE tenant_id = ? AND config_key = ?",
        (tenant_id, config_key),
    ).fetchone()
    if row is None or row[0] is None:
        return default
    return str(row[0])


def set_tenant_config_value(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    config_key: str,
    config_value: Optional[str],
) -> None:
    """Upsert a single tenant configuration key/value pair."""

    conn.execute(
        """
        INSERT INTO tenant_configurations (tenant_id, config_key, config_value, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(tenant_id, config_key) DO UPDATE SET
          config_value = excluded.config_value,
          updated_at = excluded.updated_at
        """,
        (tenant_id, config_key, config_value, _now_iso()),
    )


def get_tenant_schedule_archetype(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    default: ScheduleArchetype | str = ScheduleArchetype.STANDARD,
) -> str:
    """Return the tenant's persisted default schedule archetype value.

    Falls back to ``default`` when no value is stored. Any stored value is run
    through ``normalize_archetype`` so legacy/aliased values resolve to a canonical
    archetype string, and an unrecognized value degrades to the default.
    """

    raw = get_tenant_config_value(
        conn, tenant_id=tenant_id, config_key=SCHEDULE_ARCHETYPE_KEY, default=None
    )
    if raw is None:
        return normalize_archetype(default).value
    try:
        return normalize_archetype(raw).value
    except ValueError:
        return normalize_archetype(default).value


def set_tenant_schedule_archetype(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    archetype: ScheduleArchetype | str,
) -> None:
    """Persist the tenant's default schedule archetype (normalized to canonical)."""

    value = normalize_archetype(archetype).value
    set_tenant_config_value(
        conn, tenant_id=tenant_id, config_key=SCHEDULE_ARCHETYPE_KEY, config_value=value
    )
