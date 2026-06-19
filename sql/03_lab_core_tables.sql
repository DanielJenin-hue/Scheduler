PRAGMA foreign_keys = ON;

-- Core laboratory scheduling tables.
-- Assumes `tenants` already exists from your base SaaS schema.

CREATE TABLE IF NOT EXISTS employees (
  id                TEXT PRIMARY KEY, -- UUID
  tenant_id         TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  employee_code     TEXT,
  first_name        TEXT NOT NULL,
  last_name         TEXT NOT NULL,
  hire_date         TEXT NOT NULL, -- ISO-8601 date (YYYY-MM-DD)
  fte               REAL NOT NULL, -- ex: 1.0, 0.8
  base_hourly_rate  REAL NOT NULL DEFAULT 40.0, -- CAD; MLT default $40, MLA $26
  seniority_hours   REAL NOT NULL DEFAULT 0.0, -- CBA seniority bank (hours)
  contract_line_type TEXT CHECK (
    contract_line_type IS NULL OR contract_line_type IN ('D/N', 'D/E', 'M-F')
  ),
  is_active         INTEGER NOT NULL DEFAULT 1, -- SQLite boolean
  created_at        TEXT NOT NULL,
  updated_at        TEXT NOT NULL,
  UNIQUE (tenant_id, employee_code),
  CHECK (fte > 0.0 AND fte <= 1.0)
);

CREATE INDEX IF NOT EXISTS idx_employees_tenant
  ON employees (tenant_id);

CREATE INDEX IF NOT EXISTS idx_employees_tenant_active
  ON employees (tenant_id, is_active);


-- Qualification catalog per tenant (Canadian tiers: MLT, MLA)
CREATE TABLE IF NOT EXISTS qualifications (
  id                TEXT PRIMARY KEY, -- UUID
  tenant_id         TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  code              TEXT NOT NULL, -- MLT, MLA
  display_name      TEXT NOT NULL, -- Medical Laboratory Technologist, ...
  description       TEXT,
  is_active         INTEGER NOT NULL DEFAULT 1,
  created_at        TEXT NOT NULL,
  UNIQUE (tenant_id, code)
);

CREATE INDEX IF NOT EXISTS idx_qualifications_tenant
  ON qualifications (tenant_id);


-- Employee-to-qualification mapping.
CREATE TABLE IF NOT EXISTS employee_qualifications (
  tenant_id         TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  employee_id       TEXT NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
  qualification_id  TEXT NOT NULL REFERENCES qualifications(id) ON DELETE CASCADE,
  awarded_on        TEXT,
  expires_on        TEXT,
  created_at        TEXT NOT NULL,
  PRIMARY KEY (tenant_id, employee_id, qualification_id)
);

CREATE INDEX IF NOT EXISTS idx_emp_quals_tenant_employee
  ON employee_qualifications (tenant_id, employee_id);

CREATE INDEX IF NOT EXISTS idx_emp_quals_tenant_qualification
  ON employee_qualifications (tenant_id, qualification_id);


-- Shift templates that can later be used by rotation/schedule generators.
CREATE TABLE IF NOT EXISTS shift_templates (
  id                TEXT PRIMARY KEY, -- UUID
  tenant_id         TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  code              TEXT NOT NULL, -- MORNING / EVENING / NIGHT
  name              TEXT NOT NULL,
  start_time        TEXT NOT NULL, -- HH:MM (24h)
  end_time          TEXT NOT NULL, -- HH:MM (24h)
  duration_minutes  INTEGER NOT NULL,
  crosses_midnight  INTEGER NOT NULL DEFAULT 0,
  is_active         INTEGER NOT NULL DEFAULT 1,
  created_at        TEXT NOT NULL,
  updated_at        TEXT NOT NULL,
  UNIQUE (tenant_id, code),
  CHECK (duration_minutes > 0),
  CHECK (start_time <> end_time)
);

CREATE INDEX IF NOT EXISTS idx_shift_templates_tenant
  ON shift_templates (tenant_id);

