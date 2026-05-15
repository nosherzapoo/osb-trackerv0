-- Notification preferences keyed by Supabase user_id, plus a pending-digest
-- queue for daily/weekly subscribers. Idempotent — safe to re-run.

CREATE SCHEMA IF NOT EXISTS auth_ext;
-- We can't write to Supabase-managed auth.* — keep our own schema. The
-- user_id column references Supabase users by UUID but we don't have a
-- foreign-key relationship (Supabase's auth.users lives in a different
-- database/managed service). Application code validates via JWT.

CREATE TABLE IF NOT EXISTS auth_ext.notification_prefs (
  user_id     UUID PRIMARY KEY,
  email       TEXT NOT NULL,
  name        TEXT,
  company     TEXT,
  states      TEXT[],                       -- null = every state we cover
  frequency   TEXT NOT NULL DEFAULT 'immediate'
                CHECK (frequency IN ('immediate', 'daily', 'weekly')),
  enabled     BOOLEAN NOT NULL DEFAULT TRUE,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS notif_prefs_freq_enabled
  ON auth_ext.notification_prefs (frequency, enabled);

CREATE INDEX IF NOT EXISTS notif_prefs_email
  ON auth_ext.notification_prefs (lower(email));

-- Queue for daily/weekly subscribers. send_notifications.py appends here when
-- new data lands; the digest scripts drain it on their schedule.
CREATE TABLE IF NOT EXISTS auth_ext.pending_digest_items (
  id            BIGSERIAL PRIMARY KEY,
  user_id       UUID NOT NULL REFERENCES auth_ext.notification_prefs(user_id) ON DELETE CASCADE,
  state_code    TEXT NOT NULL,
  period_end    DATE,
  period_type   TEXT,
  handle        DOUBLE PRECISION,
  standard_ggr  DOUBLE PRECISION,
  hold_pct      DOUBLE PRECISION,
  yoy_handle    DOUBLE PRECISION,
  enqueued_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS pending_digest_user
  ON auth_ext.pending_digest_items (user_id, enqueued_at);

-- Auto-update updated_at on prefs change.
CREATE OR REPLACE FUNCTION auth_ext.touch_updated_at() RETURNS trigger AS $$
BEGIN
  NEW.updated_at = now();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS notif_prefs_touch ON auth_ext.notification_prefs;
CREATE TRIGGER notif_prefs_touch
  BEFORE UPDATE ON auth_ext.notification_prefs
  FOR EACH ROW EXECUTE FUNCTION auth_ext.touch_updated_at();

GRANT USAGE ON SCHEMA auth_ext TO osb_writer;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA auth_ext TO osb_writer;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA auth_ext TO osb_writer;
