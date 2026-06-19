PRAGMA foreign_keys = ON;

-- Approved time-off / availability blocks (tenant-scoped, per employee).
CREATE TABLE IF NOT EXISTS availability_exceptions (
  id                TEXT PRIMARY KEY,
  tenant_id         TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  employee_id       TEXT NOT NULL,
  start_date        TEXT NOT NULL,
  end_date          TEXT NOT NULL,
  reason            TEXT NOT NULL,
  status            TEXT NOT NULL DEFAULT 'approved',
  created_at        TEXT NOT NULL,
  updated_at        TEXT NOT NULL,
  CHECK (start_date <= end_date),
  FOREIGN KEY (tenant_id, employee_id) REFERENCES employees(tenant_id, id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_availability_exceptions_tenant_employee
  ON availability_exceptions (tenant_id, employee_id);

CREATE INDEX IF NOT EXISTS idx_availability_exceptions_tenant_dates
  ON availability_exceptions (tenant_id, start_date, end_date);
