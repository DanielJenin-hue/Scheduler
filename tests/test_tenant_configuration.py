"""Tests for the durable per-tenant configuration store."""

from __future__ import annotations

import sqlite3

import pytest

from lab_scheduler.scheduling.strategies import ScheduleArchetype
from lab_scheduler.tenant.configuration import (
    SCHEDULE_ARCHETYPE_KEY,
    ensure_tenant_configuration_schema,
    get_tenant_config_value,
    get_tenant_schedule_archetype,
    set_tenant_config_value,
    set_tenant_schedule_archetype,
)


@pytest.fixture()
def conn() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.execute("PRAGMA foreign_keys = ON;")
    connection.execute(
        """
        CREATE TABLE tenants (
          id TEXT PRIMARY KEY,
          name TEXT NOT NULL,
          slug TEXT NOT NULL UNIQUE,
          status TEXT NOT NULL,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        "INSERT INTO tenants (id, name, slug, status, created_at, updated_at) "
        "VALUES ('tenant-a', 'A', 'a', 'active', 'now', 'now')"
    )
    connection.execute(
        "INSERT INTO tenants (id, name, slug, status, created_at, updated_at) "
        "VALUES ('tenant-b', 'B', 'b', 'active', 'now', 'now')"
    )
    ensure_tenant_configuration_schema(connection)
    try:
        yield connection
    finally:
        connection.close()


def test_get_returns_default_when_unset(conn: sqlite3.Connection) -> None:
    assert get_tenant_config_value(conn, tenant_id="tenant-a", config_key="missing") is None
    assert (
        get_tenant_config_value(
            conn, tenant_id="tenant-a", config_key="missing", default="fallback"
        )
        == "fallback"
    )


def test_set_then_get_roundtrip(conn: sqlite3.Connection) -> None:
    set_tenant_config_value(
        conn, tenant_id="tenant-a", config_key="color", config_value="blue"
    )
    assert get_tenant_config_value(conn, tenant_id="tenant-a", config_key="color") == "blue"


def test_set_is_idempotent_upsert(conn: sqlite3.Connection) -> None:
    set_tenant_config_value(conn, tenant_id="tenant-a", config_key="k", config_value="v1")
    set_tenant_config_value(conn, tenant_id="tenant-a", config_key="k", config_value="v2")
    assert get_tenant_config_value(conn, tenant_id="tenant-a", config_key="k") == "v2"
    count = conn.execute(
        "SELECT COUNT(*) FROM tenant_configurations WHERE tenant_id = ? AND config_key = ?",
        ("tenant-a", "k"),
    ).fetchone()[0]
    assert count == 1


def test_schedule_archetype_defaults_to_standard(conn: sqlite3.Connection) -> None:
    assert (
        get_tenant_schedule_archetype(conn, tenant_id="tenant-a")
        == ScheduleArchetype.STANDARD.value
    )


def test_schedule_archetype_persists_and_normalizes(conn: sqlite3.Connection) -> None:
    set_tenant_schedule_archetype(conn, tenant_id="tenant-a", archetype="7on7off")
    # Stored value is canonical, not the alias that was passed in.
    assert (
        get_tenant_config_value(
            conn, tenant_id="tenant-a", config_key=SCHEDULE_ARCHETYPE_KEY
        )
        == ScheduleArchetype.TWELVE_HOUR.value
    )
    assert (
        get_tenant_schedule_archetype(conn, tenant_id="tenant-a")
        == ScheduleArchetype.TWELVE_HOUR.value
    )


def test_schedule_archetype_is_tenant_scoped(conn: sqlite3.Connection) -> None:
    set_tenant_schedule_archetype(
        conn, tenant_id="tenant-a", archetype=ScheduleArchetype.TWELVE_HOUR
    )
    # tenant-b must be unaffected and keep the default.
    assert (
        get_tenant_schedule_archetype(conn, tenant_id="tenant-a")
        == ScheduleArchetype.TWELVE_HOUR.value
    )
    assert (
        get_tenant_schedule_archetype(conn, tenant_id="tenant-b")
        == ScheduleArchetype.STANDARD.value
    )


def test_corrupt_stored_value_degrades_to_default(conn: sqlite3.Connection) -> None:
    set_tenant_config_value(
        conn,
        tenant_id="tenant-a",
        config_key=SCHEDULE_ARCHETYPE_KEY,
        config_value="not-a-real-archetype",
    )
    assert (
        get_tenant_schedule_archetype(conn, tenant_id="tenant-a")
        == ScheduleArchetype.STANDARD.value
    )


def test_cascade_delete_removes_config(conn: sqlite3.Connection) -> None:
    set_tenant_schedule_archetype(
        conn, tenant_id="tenant-a", archetype=ScheduleArchetype.TWELVE_HOUR
    )
    conn.execute("DELETE FROM tenants WHERE id = 'tenant-a'")
    remaining = conn.execute(
        "SELECT COUNT(*) FROM tenant_configurations WHERE tenant_id = 'tenant-a'"
    ).fetchone()[0]
    assert remaining == 0
