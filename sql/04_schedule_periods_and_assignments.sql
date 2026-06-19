PRAGMA foreign_keys = ON;

-- Multi-week schedule windows and daily shift assignments.
-- Assumes lab core tables from 03_lab_core_tables.sql exist.

-- Composite uniqueness enables tenant-safe composite foreign keys.
CREATE UNIQUE INDEX IF NOT EXISTS uq_employees_tenant_id
  ON employees (tenant_id, id);

CREATE UNIQUE INDEX IF NOT EXISTS uq_qualifications_tenant_id
  ON qualifications (tenant_id, id);

CREATE UNIQUE INDEX IF NOT EXISTS uq_shift_templates_tenant_id
  ON shift_templates (tenant_id, id);


-- Master rotation / scheduling windows (e.g. 4-week blocks).
-- period_start MUST be a Monday (logic-engine standard work week).
CREATE TABLE IF NOT EXISTS schedule_periods (
  id                    TEXT PRIMARY KEY,
  tenant_id             TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  name                  TEXT NOT NULL,
  period_start          TEXT NOT NULL, -- YYYY-MM-DD, must be Monday
  week_count            INTEGER NOT NULL,
  period_end_inclusive  TEXT NOT NULL, -- last day of final week (Sunday)
  status                TEXT NOT NULL DEFAULT 'draft', -- draft | published | archived
  created_at            TEXT NOT NULL,
  updated_at            TEXT NOT NULL,
  UNIQUE (tenant_id, name),
  CHECK (week_count > 0),
  -- SQLite: strftime('%w') -> Sunday=0, Monday=1
  CHECK (CAST(strftime('%w', period_start) AS INTEGER) = 1),
  CHECK (period_end_inclusive = date(period_start, printf('+%d days', week_count * 7 - 1)))
);

CREATE INDEX IF NOT EXISTS idx_schedule_periods_tenant
  ON schedule_periods (tenant_id);

CREATE INDEX IF NOT EXISTS idx_schedule_periods_tenant_start
  ON schedule_periods (tenant_id, period_start);

CREATE UNIQUE INDEX IF NOT EXISTS uq_schedule_periods_tenant_id
  ON schedule_periods (tenant_id, id);


-- Required qualifications per shift template (employee must hold at least one listed tier).
CREATE TABLE IF NOT EXISTS shift_template_qualifications (
  tenant_id             TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  shift_template_id     TEXT NOT NULL,
  qualification_id      TEXT NOT NULL,
  created_at            TEXT NOT NULL,
  PRIMARY KEY (tenant_id, shift_template_id, qualification_id),
  FOREIGN KEY (tenant_id, shift_template_id)
    REFERENCES shift_templates (tenant_id, id) ON DELETE CASCADE,
  FOREIGN KEY (tenant_id, qualification_id)
    REFERENCES qualifications (tenant_id, id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_shift_template_quals_tenant_shift
  ON shift_template_qualifications (tenant_id, shift_template_id);


-- Daily assignment of an employee to a shift template on a calendar date.
CREATE TABLE IF NOT EXISTS shift_assignments (
  id                    TEXT PRIMARY KEY,
  tenant_id             TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  schedule_period_id    TEXT NOT NULL,
  employee_id           TEXT NOT NULL,
  shift_template_id     TEXT NOT NULL,
  assignment_date       TEXT NOT NULL, -- YYYY-MM-DD
  created_at            TEXT NOT NULL,
  updated_at            TEXT NOT NULL,
  UNIQUE (tenant_id, employee_id, assignment_date),
  FOREIGN KEY (tenant_id, schedule_period_id)
    REFERENCES schedule_periods (tenant_id, id) ON DELETE CASCADE,
  FOREIGN KEY (tenant_id, employee_id)
    REFERENCES employees (tenant_id, id) ON DELETE CASCADE,
  FOREIGN KEY (tenant_id, shift_template_id)
    REFERENCES shift_templates (tenant_id, id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_shift_assignments_tenant_date
  ON shift_assignments (tenant_id, assignment_date);

CREATE INDEX IF NOT EXISTS idx_shift_assignments_tenant_period
  ON shift_assignments (tenant_id, schedule_period_id);

CREATE INDEX IF NOT EXISTS idx_shift_assignments_tenant_employee
  ON shift_assignments (tenant_id, employee_id);


-- Reject assignments outside the linked schedule period window.
CREATE TRIGGER IF NOT EXISTS trg_shift_assignments_period_window
BEFORE INSERT ON shift_assignments
FOR EACH ROW
BEGIN
  SELECT
    CASE
      WHEN NOT EXISTS (
        SELECT 1
        FROM schedule_periods sp
        WHERE sp.tenant_id = NEW.tenant_id
          AND sp.id = NEW.schedule_period_id
          AND NEW.assignment_date >= sp.period_start
          AND NEW.assignment_date <= sp.period_end_inclusive
      )
      THEN RAISE(ABORT, 'assignment_date must fall within the schedule period')
    END;
END;

CREATE TRIGGER IF NOT EXISTS trg_shift_assignments_period_window_update
BEFORE UPDATE ON shift_assignments
FOR EACH ROW
BEGIN
  SELECT
    CASE
      WHEN NOT EXISTS (
        SELECT 1
        FROM schedule_periods sp
        WHERE sp.tenant_id = NEW.tenant_id
          AND sp.id = NEW.schedule_period_id
          AND NEW.assignment_date >= sp.period_start
          AND NEW.assignment_date <= sp.period_end_inclusive
      )
      THEN RAISE(ABORT, 'assignment_date must fall within the schedule period')
    END;
END;


-- Reject assignments when the employee lacks required shift qualifications.
CREATE TRIGGER IF NOT EXISTS trg_shift_assignments_required_qualifications
BEFORE INSERT ON shift_assignments
FOR EACH ROW
BEGIN
  SELECT
    CASE
      WHEN EXISTS (
        SELECT 1
        FROM shift_template_qualifications stq
        WHERE stq.tenant_id = NEW.tenant_id
          AND stq.shift_template_id = NEW.shift_template_id
      )
      AND EXISTS (
        SELECT 1
        FROM shift_template_qualifications stq
        WHERE stq.tenant_id = NEW.tenant_id
          AND stq.shift_template_id = NEW.shift_template_id
      )
      AND NOT EXISTS (
        SELECT 1
        FROM shift_template_qualifications stq
        INNER JOIN employee_qualifications eq
          ON eq.tenant_id = stq.tenant_id
         AND eq.employee_id = NEW.employee_id
         AND eq.qualification_id = stq.qualification_id
         AND (eq.expires_on IS NULL OR eq.expires_on >= NEW.assignment_date)
         AND (eq.awarded_on IS NULL OR eq.awarded_on <= NEW.assignment_date)
        WHERE stq.tenant_id = NEW.tenant_id
          AND stq.shift_template_id = NEW.shift_template_id
      )
      THEN RAISE(ABORT, 'employee lacks required qualification for this shift')
    END;
END;

CREATE TRIGGER IF NOT EXISTS trg_shift_assignments_required_qualifications_update
BEFORE UPDATE ON shift_assignments
FOR EACH ROW
BEGIN
  SELECT
    CASE
      WHEN EXISTS (
        SELECT 1
        FROM shift_template_qualifications stq
        WHERE stq.tenant_id = NEW.tenant_id
          AND stq.shift_template_id = NEW.shift_template_id
      )
      AND EXISTS (
        SELECT 1
        FROM shift_template_qualifications stq
        WHERE stq.tenant_id = NEW.tenant_id
          AND stq.shift_template_id = NEW.shift_template_id
      )
      AND NOT EXISTS (
        SELECT 1
        FROM shift_template_qualifications stq
        INNER JOIN employee_qualifications eq
          ON eq.tenant_id = stq.tenant_id
         AND eq.employee_id = NEW.employee_id
         AND eq.qualification_id = stq.qualification_id
         AND (eq.expires_on IS NULL OR eq.expires_on >= NEW.assignment_date)
         AND (eq.awarded_on IS NULL OR eq.awarded_on <= NEW.assignment_date)
        WHERE stq.tenant_id = NEW.tenant_id
          AND stq.shift_template_id = NEW.shift_template_id
      )
      THEN RAISE(ABORT, 'employee lacks required qualification for this shift')
    END;
END;
