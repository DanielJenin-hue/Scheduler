PRAGMA foreign_keys = ON;

-- Demo availability blocks for Summer 2026 period (2026-06-01 .. 2026-06-28).
INSERT INTO availability_exceptions (
  id, tenant_id, employee_id, start_date, end_date, reason, status, created_at, updated_at
)
VALUES
  (
    'avail-avery-vacation',
    'tenant-northstar-lab',
    'emp-a1',
    '2026-06-08',
    '2026-06-14',
    'Vacation',
    'approved',
    '2026-05-26T00:00:00Z',
    '2026-05-26T00:00:00Z'
  ),
  (
    'avail-jordan-vacation',
    'tenant-northstar-lab',
    'emp-b1',
    '2026-06-15',
    '2026-06-17',
    'Vacation',
    'approved',
    '2026-05-26T00:00:00Z',
    '2026-05-26T00:00:00Z'
  ),
  (
    'avail-riley-sick',
    'tenant-northstar-lab',
    'emp-c1',
    '2026-06-03',
    '2026-06-04',
    'Sick Leave',
    'approved',
    '2026-05-26T00:00:00Z',
    '2026-05-26T00:00:00Z'
  );
