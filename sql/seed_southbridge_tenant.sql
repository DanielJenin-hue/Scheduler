PRAGMA foreign_keys = ON;

-- Isolated Southbridge tenant shell (no roster, schedules, or Northstar data).
INSERT INTO tenants (id, name, slug, status, created_at, updated_at)
VALUES (
  'tenant-southbridge-health',
  'Southbridge Community Health Laboratory',
  'southbridge-health',
  'active',
  '2026-05-26T00:00:00Z',
  '2026-05-26T00:00:00Z'
);
