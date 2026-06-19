PRAGMA foreign_keys = ON;

-- Tenant-scoped user accounts for application sign-in.
CREATE TABLE IF NOT EXISTS tenant_user_accounts (
  id                TEXT PRIMARY KEY,
  username          TEXT NOT NULL UNIQUE COLLATE NOCASE,
  password_salt     TEXT NOT NULL,
  password_hash     TEXT NOT NULL,
  tenant_id         TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  display_name      TEXT,
  is_active         INTEGER NOT NULL DEFAULT 1,
  created_at        TEXT NOT NULL,
  updated_at        TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tenant_user_accounts_tenant
  ON tenant_user_accounts (tenant_id);
