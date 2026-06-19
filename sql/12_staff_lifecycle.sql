PRAGMA foreign_keys = ON;

-- Administrative audit trail for staffing lifecycle events (deactivation, archival).
CREATE TABLE IF NOT EXISTS sys_audit_log (
  id                    INTEGER PRIMARY KEY AUTOINCREMENT,
  recorded_at_utc       TEXT NOT NULL,
  tenant_id             TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  manager_id            TEXT NOT NULL,
  action_type           TEXT NOT NULL,
  employee_id           TEXT,
  shifts_vacated_count  INTEGER NOT NULL DEFAULT 0,
  metadata_json         TEXT,
  CHECK (action_type IN (
    'employee_deactivation',
    'employee_reactivation',
    'audit_warning',
    'snapshot_restore'
  ))
);

CREATE INDEX IF NOT EXISTS idx_sys_audit_log_tenant_recorded
  ON sys_audit_log (tenant_id, recorded_at_utc DESC);

CREATE INDEX IF NOT EXISTS idx_sys_audit_log_tenant_employee
  ON sys_audit_log (tenant_id, employee_id);
