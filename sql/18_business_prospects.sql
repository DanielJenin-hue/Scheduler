PRAGMA foreign_keys = ON;

-- Outbound sales prospects / leads for Manitoba hospital lab GTM.
CREATE TABLE IF NOT EXISTS business_prospects (
  id                   TEXT PRIMARY KEY,
  facility_id          TEXT,
  facility             TEXT NOT NULL,
  contact_name         TEXT,
  email                TEXT,
  phone                TEXT,
  province             TEXT NOT NULL DEFAULT 'MB',
  icp_score            INTEGER NOT NULL DEFAULT 0
                         CHECK (icp_score >= 0 AND icp_score <= 100),
  pain_signals_json    TEXT NOT NULL DEFAULT '[]',
  status               TEXT NOT NULL DEFAULT 'discovered'
                         CHECK (status IN (
                           'discovered',
                           'previewed',
                           'contacted',
                           'active_client',
                           'declined'
                         )),
  notes                TEXT,
  email_draft_subject  TEXT,
  email_draft_body     TEXT,
  tenant_id            TEXT REFERENCES tenants(id) ON DELETE SET NULL,
  created_at           TEXT NOT NULL,
  updated_at           TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_business_prospects_facility_id
  ON business_prospects (facility_id)
  WHERE facility_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_business_prospects_status
  ON business_prospects (status, icp_score DESC);

CREATE INDEX IF NOT EXISTS idx_business_prospects_province
  ON business_prospects (province);
