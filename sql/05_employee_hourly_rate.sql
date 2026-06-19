PRAGMA foreign_keys = ON;

-- Canadian credential tiers + hourly rates on employee profiles.
-- Safe to run on existing databases (SQLite ADD COLUMN).

ALTER TABLE employees ADD COLUMN base_hourly_rate REAL NOT NULL DEFAULT 40.0;
