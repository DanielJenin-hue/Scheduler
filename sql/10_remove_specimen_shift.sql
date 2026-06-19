PRAGMA foreign_keys = ON;

-- Retire the legacy Specimen (S) shift line in favor of Morning / Evening / Night only.
-- Existing assignments and audit codes are normalized to Morning (M).

INSERT OR IGNORE INTO shift_template_qualifications (tenant_id, shift_template_id, qualification_id, created_at)
SELECT tenant_id, 'shift-morning', qualification_id, datetime('now')
FROM shift_template_qualifications
WHERE shift_template_id = 'shift-specimen';

UPDATE shift_assignments
SET shift_template_id = 'shift-morning'
WHERE shift_template_id = 'shift-specimen';

DELETE FROM shift_assignments
WHERE shift_template_id = 'shift-specimen';

DELETE FROM shift_template_qualifications
WHERE shift_template_id = 'shift-specimen';

DELETE FROM shift_templates
WHERE id = 'shift-specimen' OR UPPER(code) = 'SPECIMEN';

UPDATE schedule_audit_logs
SET previous_shift_code = 'M'
WHERE previous_shift_code = 'S';

UPDATE schedule_audit_logs
SET new_shift_code = 'M'
WHERE new_shift_code = 'S';
