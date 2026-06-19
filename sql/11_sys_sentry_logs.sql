PRAGMA foreign_keys = ON;



-- Autonomous Sentry Agent telemetry for unhandled application exceptions.

CREATE TABLE IF NOT EXISTS sys_sentry_logs (

  log_id              INTEGER PRIMARY KEY AUTOINCREMENT,

  recorded_at_utc     TEXT NOT NULL,

  tenant_id           TEXT,

  username            TEXT,

  exception_type      TEXT NOT NULL,

  error_message       TEXT NOT NULL,

  target_file         TEXT,

  line_number         INTEGER,

  clean_traceback     TEXT NOT NULL,

  resolution_status   TEXT NOT NULL DEFAULT 'unresolved'

    CHECK (resolution_status IN (

      'unresolved', 'resolved', 'ignored', 'awaiting_review', 'patched', 'patch_failed'

    )),

  proposed_patch_code TEXT

);



CREATE INDEX IF NOT EXISTS idx_sys_sentry_logs_status_recorded

  ON sys_sentry_logs (resolution_status, recorded_at_utc DESC);

