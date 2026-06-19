PRAGMA foreign_keys = ON;

-- Durable, per-tenant key/value configuration store.
--
-- Replaces ephemeral Streamlit session state as the source of truth for
-- per-tenant settings (e.g. the default schedule archetype). Generic key/value
-- shape keeps future settings additive without further schema migrations.
CREATE TABLE IF NOT EXISTS tenant_configurations (
  tenant_id    TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  config_key   TEXT NOT NULL,
  config_value TEXT,
  updated_at   TEXT NOT NULL,
  PRIMARY KEY (tenant_id, config_key)
);

CREATE INDEX IF NOT EXISTS idx_tenant_configurations_tenant
  ON tenant_configurations (tenant_id);
