PRAGMA foreign_keys = ON;

-- CBA seniority tracking on employee records.
-- Applied idempotently at runtime via ensure_seniority_cba_schema().

-- schedule_audit_logs: seniority bypass grievance prevention columns are added
-- programmatically for existing databases (see schedule_log.ensure_audit_schema).
