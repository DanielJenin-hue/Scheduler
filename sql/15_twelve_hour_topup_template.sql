-- Synthetic FTE top-up shift template for 12-hour 7-on/7-off contract reconciliation.
-- Required so shift_assignments FK accepts twelve-hour FTE top-up rows.
-- shift_templates.id is globally unique, so each tenant gets a tenant-scoped id when none exists yet.

INSERT OR IGNORE INTO shift_templates (
  id, tenant_id, code, name, start_time, end_time,
  duration_minutes, crosses_midnight, is_active, created_at, updated_at
)
SELECT
  t.id || '::twelve-hour-fte-topup',
  t.id,
  'TOPUP',
  'FTE Top-up Shift',
  '08:00',
  '14:07',
  375,
  0,
  1,
  '2026-05-26T00:00:00Z',
  '2026-05-26T00:00:00Z'
FROM tenants t
WHERE NOT EXISTS (
  SELECT 1
  FROM shift_templates st
  WHERE st.tenant_id = t.id
    AND st.code = 'TOPUP'
);

-- Attach MLT/MLA quals to whichever TOPUP template the tenant already owns (legacy or scoped).
INSERT OR IGNORE INTO shift_template_qualifications (
  tenant_id, shift_template_id, qualification_id, created_at
)
SELECT
  t.id,
  st.id,
  q.id,
  '2026-05-26T00:00:00Z'
FROM tenants t
INNER JOIN shift_templates st
  ON st.tenant_id = t.id
 AND st.code = 'TOPUP'
INNER JOIN qualifications q
  ON q.tenant_id = t.id
 AND q.code IN ('MLT', 'MLA');
