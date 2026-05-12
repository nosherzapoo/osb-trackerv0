-- Phase 4 — probe-driven scrape triggers.
-- Apply with:  sudo -u postgres psql -d osb_data -f setup_ops_probe_trigger.sql
-- Idempotent.

-- Record the source-page content_hash that was current when a successful
-- scrape ran. Probe-driven triggers compare against this to decide whether
-- the regulator has moved since we last got data.
ALTER TABLE ops.scrape_state_results
    ADD COLUMN IF NOT EXISTS source_hash_at_scrape TEXT;

CREATE INDEX IF NOT EXISTS ssr_state_hash_idx
    ON ops.scrape_state_results (state, source_hash_at_scrape);
