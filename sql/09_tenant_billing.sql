PRAGMA foreign_keys = ON;

-- Subscription & billing state on tenant records.
ALTER TABLE tenants ADD COLUMN subscription_status TEXT NOT NULL DEFAULT 'trial';
ALTER TABLE tenants ADD COLUMN stripe_customer_id TEXT;
ALTER TABLE tenants ADD COLUMN trial_ends_at TEXT;
