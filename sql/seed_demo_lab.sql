PRAGMA foreign_keys = ON;



-- Demo tenant for local simulation.

INSERT INTO tenants (id, name, slug, status, created_at, updated_at)

VALUES (

  'tenant-northstar-lab',

  'Northstar Medical Laboratory',

  'northstar-lab',

  'active',

  '2026-05-26T00:00:00Z',

  '2026-05-26T00:00:00Z'

);



-- Employees (all tenant-scoped).

INSERT INTO employees (

  id, tenant_id, employee_code, first_name, last_name, hire_date, fte, base_hourly_rate, is_active, created_at, updated_at

)

VALUES

  ('emp-a1', 'tenant-northstar-lab', 'E1001', 'Avery', 'Miller', '2022-04-11', 1.0, 40.0, 1, '2026-05-26T00:00:00Z', '2026-05-26T00:00:00Z'),

  ('emp-b1', 'tenant-northstar-lab', 'E1002', 'Jordan', 'Patel', '2021-09-07', 0.8, 40.0, 1, '2026-05-26T00:00:00Z', '2026-05-26T00:00:00Z'),

  ('emp-c1', 'tenant-northstar-lab', 'E1003', 'Riley', 'Chen', '2024-01-15', 0.6, 26.0, 1, '2026-05-26T00:00:00Z', '2026-05-26T00:00:00Z');



-- Qualification catalog for this tenant (Canadian healthcare tiers).

INSERT INTO qualifications (

  id, tenant_id, code, display_name, description, is_active, created_at

)

VALUES

  ('qual-mlt', 'tenant-northstar-lab', 'MLT', 'Medical Laboratory Technologist', 'Performs diagnostic laboratory testing and analysis.', 1, '2026-05-26T00:00:00Z'),

  ('qual-mla', 'tenant-northstar-lab', 'MLA', 'Medical Laboratory Assistant', 'Specimen processing and front-end laboratory support.', 1, '2026-05-26T00:00:00Z');



-- Employee-to-qualification mappings.

INSERT INTO employee_qualifications (

  tenant_id, employee_id, qualification_id, awarded_on, expires_on, created_at

)

VALUES

  ('tenant-northstar-lab', 'emp-a1', 'qual-mlt', '2022-04-11', NULL, '2026-05-26T00:00:00Z'),

  ('tenant-northstar-lab', 'emp-b1', 'qual-mlt', '2021-09-07', NULL, '2026-05-26T00:00:00Z'),

  ('tenant-northstar-lab', 'emp-c1', 'qual-mla',  '2024-01-15', NULL, '2026-05-26T00:00:00Z');



-- Shift templates.

INSERT INTO shift_templates (

  id, tenant_id, code, name, start_time, end_time, duration_minutes, crosses_midnight, is_active, created_at, updated_at

)

VALUES

  ('shift-morning', 'tenant-northstar-lab', 'MORNING', 'Morning Shift', '07:00', '15:00', 480, 0, 1, '2026-05-26T00:00:00Z', '2026-05-26T00:00:00Z'),

  ('shift-evening', 'tenant-northstar-lab', 'EVENING', 'Evening Shift', '15:00', '23:00', 480, 0, 1, '2026-05-26T00:00:00Z', '2026-05-26T00:00:00Z'),

  ('shift-night',   'tenant-northstar-lab', 'NIGHT',   'Night Shift',   '23:00', '07:00', 480, 1, 1, '2026-05-26T00:00:00Z', '2026-05-26T00:00:00Z');



-- Required qualifications per shift (AND logic across rows for the same shift).

INSERT INTO shift_template_qualifications (

  tenant_id, shift_template_id, qualification_id, created_at

)

VALUES

  ('tenant-northstar-lab', 'shift-morning',  'qual-mlt', '2026-05-26T00:00:00Z'),

  ('tenant-northstar-lab', 'shift-morning',  'qual-mla', '2026-05-26T00:00:00Z'),

  ('tenant-northstar-lab', 'shift-evening',  'qual-mlt', '2026-05-26T00:00:00Z'),

  ('tenant-northstar-lab', 'shift-night',    'qual-mlt', '2026-05-26T00:00:00Z');



-- 8-week master rotation window (Monday start: 2026-06-01).

INSERT INTO schedule_periods (

  id, tenant_id, name, period_start, week_count, period_end_inclusive, status, created_at, updated_at

)

VALUES (

  'period-2026-summer',

  'tenant-northstar-lab',

  'Summer 2026 Master Rotation',

  '2026-06-01',

  8,

  '2026-07-26',

  'published',

  '2026-05-26T00:00:00Z',

  '2026-05-26T00:00:00Z'

);



-- Sample assignments across the 4-week period (one per employee per day max).

INSERT INTO shift_assignments (

  id, tenant_id, schedule_period_id, employee_id, shift_template_id, assignment_date, created_at, updated_at

)

VALUES

  -- Week 1 (Mon Jun 1 - Sun Jun 7)

  ('asg-001', 'tenant-northstar-lab', 'period-2026-summer', 'emp-a1', 'shift-morning',  '2026-06-01', '2026-05-26T00:00:00Z', '2026-05-26T00:00:00Z'),

  ('asg-002', 'tenant-northstar-lab', 'period-2026-summer', 'emp-b1', 'shift-evening',  '2026-06-01', '2026-05-26T00:00:00Z', '2026-05-26T00:00:00Z'),

  ('asg-003', 'tenant-northstar-lab', 'period-2026-summer', 'emp-c1', 'shift-morning',  '2026-06-02', '2026-05-26T00:00:00Z', '2026-05-26T00:00:00Z'),

  ('asg-004', 'tenant-northstar-lab', 'period-2026-summer', 'emp-a1', 'shift-night',    '2026-06-05', '2026-05-26T00:00:00Z', '2026-05-26T00:00:00Z'),

  -- Week 2

  ('asg-005', 'tenant-northstar-lab', 'period-2026-summer', 'emp-b1', 'shift-evening',  '2026-06-08', '2026-05-26T00:00:00Z', '2026-05-26T00:00:00Z'),

  ('asg-006', 'tenant-northstar-lab', 'period-2026-summer', 'emp-c1', 'shift-morning',  '2026-06-10', '2026-05-26T00:00:00Z', '2026-05-26T00:00:00Z'),

  -- Week 3

  ('asg-007', 'tenant-northstar-lab', 'period-2026-summer', 'emp-a1', 'shift-morning',  '2026-06-15', '2026-05-26T00:00:00Z', '2026-05-26T00:00:00Z'),

  ('asg-008', 'tenant-northstar-lab', 'period-2026-summer', 'emp-b1', 'shift-evening',  '2026-06-17', '2026-05-26T00:00:00Z', '2026-05-26T00:00:00Z'),

  -- Week 4

  ('asg-009', 'tenant-northstar-lab', 'period-2026-summer', 'emp-a1', 'shift-morning',  '2026-06-22', '2026-05-26T00:00:00Z', '2026-05-26T00:00:00Z'),

  ('asg-010', 'tenant-northstar-lab', 'period-2026-summer', 'emp-c1', 'shift-morning',  '2026-06-26', '2026-05-26T00:00:00Z', '2026-05-26T00:00:00Z');


