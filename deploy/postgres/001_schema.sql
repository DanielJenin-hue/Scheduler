-- Postgres-compatible core schema for Lab Staffing Scheduler (multi-tenant SaaS).
-- Apply to a fresh database before data migration from SQLite.

CREATE TABLE IF NOT EXISTS tenants (
  id                  TEXT PRIMARY KEY,
  name                TEXT NOT NULL,
  slug                TEXT NOT NULL UNIQUE,
  status              TEXT NOT NULL,
  subscription_status TEXT NOT NULL DEFAULT 'trial',
  stripe_customer_id  TEXT,
  trial_ends_at       TIMESTAMPTZ,
  created_at          TIMESTAMPTZ NOT NULL,
  updated_at          TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS tenant_user_accounts (
  id             TEXT PRIMARY KEY,
  username       TEXT NOT NULL UNIQUE,
  password_salt  TEXT NOT NULL,
  password_hash  TEXT NOT NULL,
  tenant_id      TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  display_name   TEXT,
  is_active      BOOLEAN NOT NULL DEFAULT TRUE,
  created_at     TIMESTAMPTZ NOT NULL,
  updated_at     TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tenant_user_accounts_tenant
  ON tenant_user_accounts (tenant_id);

CREATE TABLE IF NOT EXISTS tenant_configurations (
  tenant_id    TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  config_key   TEXT NOT NULL,
  config_value TEXT,
  updated_at   TIMESTAMPTZ NOT NULL,
  PRIMARY KEY (tenant_id, config_key)
);

-- Remaining lab tables mirror sql/03_lab_core_tables.sql and sql/04_schedule_periods_and_assignments.sql
-- with TEXT date columns converted to DATE where noted in migrate_sqlite_to_postgres.py.
