-- Manager shift cell locks (per employee + date within a schedule period).
CREATE TABLE IF NOT EXISTS shift_cell_locks (
  tenant_id           TEXT NOT NULL,
  schedule_period_id  TEXT NOT NULL,
  employee_id         TEXT NOT NULL,
  assignment_date     TEXT NOT NULL,
  locked_at           TEXT NOT NULL,
  locked_by           TEXT NOT NULL,
  lock_band           TEXT NOT NULL DEFAULT 'ANY',
  PRIMARY KEY (tenant_id, schedule_period_id, employee_id, assignment_date),
  FOREIGN KEY (tenant_id, schedule_period_id)
    REFERENCES schedule_periods (tenant_id, id) ON DELETE CASCADE,
  FOREIGN KEY (tenant_id, employee_id)
    REFERENCES employees (tenant_id, id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_shift_cell_locks_tenant_period
  ON shift_cell_locks (tenant_id, schedule_period_id);
