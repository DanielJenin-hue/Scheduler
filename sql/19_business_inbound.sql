PRAGMA foreign_keys = ON;

-- Inbound email replies synced from IMAP or logged manually.
CREATE TABLE IF NOT EXISTS business_inbound_messages (
  id                   TEXT PRIMARY KEY,
  prospect_id          TEXT REFERENCES business_prospects(id) ON DELETE SET NULL,
  from_email           TEXT NOT NULL,
  to_email             TEXT,
  subject              TEXT,
  body_text            TEXT,
  body_html            TEXT,
  received_at          TEXT NOT NULL,
  read_at              TEXT,
  thread_id            TEXT,
  external_message_id  TEXT,
  status               TEXT NOT NULL DEFAULT 'unread'
                         CHECK (status IN ('unread', 'read', 'archived')),
  created_at           TEXT NOT NULL,
  updated_at           TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_business_inbound_external_message_id
  ON business_inbound_messages (external_message_id)
  WHERE external_message_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_business_inbound_prospect
  ON business_inbound_messages (prospect_id, received_at DESC);

CREATE INDEX IF NOT EXISTS idx_business_inbound_status
  ON business_inbound_messages (status, received_at DESC);

CREATE INDEX IF NOT EXISTS idx_business_inbound_thread
  ON business_inbound_messages (thread_id, received_at DESC);
