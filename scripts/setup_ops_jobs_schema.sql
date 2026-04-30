-- Phase 3 — manual override jobs.
-- Apply with:  sudo -u postgres psql -d osb_data -f setup_ops_jobs_schema.sql
-- Idempotent.

CREATE TABLE IF NOT EXISTS ops.jobs (
    id            UUID        PRIMARY KEY,
    kind          TEXT        NOT NULL,        -- scrape_state | scrape_tier | backfill_state | reload_postgres | send_notifications
    params        JSONB       NOT NULL DEFAULT '{}'::jsonb,
    status        TEXT        NOT NULL DEFAULT 'pending',  -- pending | running | succeeded | failed | cancelled
    actor         TEXT        NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at    TIMESTAMPTZ,
    finished_at   TIMESTAMPTZ,
    exit_code     INTEGER,
    systemd_unit  TEXT,
    output_tail   TEXT,
    error_text    TEXT,
    run_id        UUID        REFERENCES ops.scrape_runs(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS jobs_created_idx ON ops.jobs (created_at DESC);
CREATE INDEX IF NOT EXISTS jobs_status_idx  ON ops.jobs (status, created_at DESC);

GRANT ALL ON ops.jobs TO osb_writer;
