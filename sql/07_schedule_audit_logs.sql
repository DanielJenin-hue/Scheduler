PRAGMA foreign_keys = ON;

-- Immutable schedule change history (union grievance / dispute protection).
CREATE TABLE IF NOT EXISTS schedule_audit_logs (
  id                  INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id           TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
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

CREATE INDEX IF NOT EXISTS idx_schedule_audit_logs_tenant_period
  ON schedule_audit_logs (tenant_id, schedule_period_id, id DESC);

CREATE INDEX IF NOT EXISTS idx_schedule_audit_logs_tenant_time
  ON schedule_audit_logs (tenant_id, recorded_at_utc DESC);
