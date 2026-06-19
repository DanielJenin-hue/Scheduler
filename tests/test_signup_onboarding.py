import sqlite3
from datetime import date, timedelta

import pytest

from lab_scheduler.auth.onboarding import (
    create_schedule_period,
    is_onboarding_complete,
    load_portage_demo_roster,
    mark_onboarding_complete,
    seed_lab_infrastructure,
)
from lab_scheduler.auth.signup import SignupError, register_tenant, slugify_facility_name
from lab_scheduler.billing.feature_gates import ensure_billing_schema, fetch_tenant_billing
from lab_scheduler.tenant.configuration import ensure_tenant_configuration_schema


def _memory_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE tenants (
          id TEXT PRIMARY KEY,
          name TEXT NOT NULL,
          slug TEXT NOT NULL UNIQUE,
          status TEXT NOT NULL,
          subscription_status TEXT NOT NULL DEFAULT 'trial',
          stripe_customer_id TEXT,
          trial_ends_at TEXT,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        CREATE TABLE tenant_user_accounts (
          id TEXT PRIMARY KEY,
          username TEXT NOT NULL UNIQUE,
          password_salt TEXT NOT NULL,
          password_hash TEXT NOT NULL,
          tenant_id TEXT NOT NULL,
          display_name TEXT,
          is_active INTEGER NOT NULL DEFAULT 1,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        CREATE TABLE qualifications (
          id TEXT PRIMARY KEY,
          tenant_id TEXT NOT NULL,
          code TEXT NOT NULL,
          display_name TEXT NOT NULL,
          description TEXT,
          is_active INTEGER NOT NULL DEFAULT 1,
          created_at TEXT NOT NULL
        );
        CREATE TABLE shift_templates (
          id TEXT PRIMARY KEY,
          tenant_id TEXT NOT NULL,
          code TEXT NOT NULL,
          name TEXT NOT NULL,
          start_time TEXT NOT NULL,
          end_time TEXT NOT NULL,
          duration_minutes INTEGER NOT NULL,
          crosses_midnight INTEGER NOT NULL DEFAULT 0,
          is_active INTEGER NOT NULL DEFAULT 1,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        CREATE TABLE shift_template_qualifications (
          tenant_id TEXT NOT NULL,
          shift_template_id TEXT NOT NULL,
          qualification_id TEXT NOT NULL,
          created_at TEXT NOT NULL,
          PRIMARY KEY (tenant_id, shift_template_id, qualification_id)
        );
        CREATE TABLE employees (
          id TEXT PRIMARY KEY,
          tenant_id TEXT NOT NULL,
          employee_code TEXT,
          first_name TEXT NOT NULL,
          last_name TEXT NOT NULL,
          hire_date TEXT NOT NULL,
          fte REAL NOT NULL,
          base_hourly_rate REAL NOT NULL DEFAULT 40.0,
          seniority_hours REAL NOT NULL DEFAULT 0.0,
          contract_line_type TEXT,
          is_active INTEGER NOT NULL DEFAULT 1,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        CREATE TABLE employee_qualifications (
          tenant_id TEXT NOT NULL,
          employee_id TEXT NOT NULL,
          qualification_id TEXT NOT NULL,
          awarded_on TEXT,
          expires_on TEXT,
          created_at TEXT NOT NULL,
          PRIMARY KEY (tenant_id, employee_id, qualification_id)
        );
        CREATE TABLE schedule_periods (
          id TEXT PRIMARY KEY,
          tenant_id TEXT NOT NULL,
          name TEXT NOT NULL,
          period_start TEXT NOT NULL,
          week_count INTEGER NOT NULL,
          period_end_inclusive TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'draft',
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        """
    )
    ensure_billing_schema(conn)
    ensure_tenant_configuration_schema(conn)
    return conn


def test_slugify_facility_name() -> None:
    assert slugify_facility_name("Portage Regional Lab") == "portage-regional-lab"


def test_register_tenant_creates_trial_workspace() -> None:
    conn = _memory_db()
    session = register_tenant(
        conn,
        facility_name="Portage Regional Lab",
        email="manager@example.com",
        password="securepass1",
    )
    assert session.tenant_slug == "portage-regional-lab"
    assert session.username == "manager@example.com"
    billing = fetch_tenant_billing(conn, session.tenant_id)
    assert billing.subscription_status == "trial"
    assert billing.trial_ends_at == date.today() + timedelta(days=14)
    assert not is_onboarding_complete(conn, tenant_id=session.tenant_id)


def test_register_tenant_rejects_duplicate_email() -> None:
    conn = _memory_db()
    register_tenant(
        conn,
        facility_name="Alpha Lab",
        email="dup@example.com",
        password="securepass1",
    )
    with pytest.raises(SignupError):
        register_tenant(
            conn,
            facility_name="Beta Lab",
            email="dup@example.com",
            password="securepass2",
        )


def test_load_portage_demo_roster_and_period() -> None:
    conn = _memory_db()
    tenant_id = "tenant-test-001"
    now = "2026-05-30T00:00:00Z"
    conn.execute(
        """
        INSERT INTO tenants (
          id, name, slug, status, subscription_status, created_at, updated_at
        ) VALUES (?, 'Test Lab', 'test-lab', 'active', 'trial', ?, ?)
        """,
        (tenant_id, now, now),
    )
    seed_lab_infrastructure(conn, tenant_id=tenant_id)
    conn.commit()

    inserted = load_portage_demo_roster(conn, tenant_id=tenant_id)
    assert inserted == 25
    period_id = create_schedule_period(conn, tenant_id=tenant_id)
    assert period_id.startswith("period-")
    mark_onboarding_complete(conn, tenant_id=tenant_id)
    assert is_onboarding_complete(conn, tenant_id=tenant_id)
