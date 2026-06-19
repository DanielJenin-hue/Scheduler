PRAGMA foreign_keys = ON;

-- Portage rotation contract line (D/N, D/E, M-F) on employee records.
ALTER TABLE employees ADD COLUMN contract_line_type TEXT
  CHECK (
    contract_line_type IS NULL
    OR contract_line_type IN ('D/N', 'D/E', 'M-F')
  );
