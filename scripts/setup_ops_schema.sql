-- ops schema — backend monitoring + override surface for the OSB tracker.
-- Apply with:  sudo -u postgres psql -d osb_data -f setup_ops_schema.sql
-- Idempotent: safe to re-apply.

CREATE SCHEMA IF NOT EXISTS ops;

-- One row per tier wrapper invocation (or manual scrape via the ops API).
CREATE TABLE IF NOT EXISTS ops.scrape_runs (
    id              UUID        PRIMARY KEY,
    run_type        TEXT        NOT NULL,        -- tier1 | tier23 | tier45 | full | manual | qa
    started_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at     TIMESTAMPTZ,
    exit_code       INTEGER,
    states          TEXT[],
    commit_sha      TEXT,
    summary         JSONB,
    triggered_by    TEXT,                        -- systemd | manual:<user> | api
    host            TEXT
);
CREATE INDEX IF NOT EXISTS scrape_runs_started_at_idx ON ops.scrape_runs (started_at DESC);
CREATE INDEX IF NOT EXISTS scrape_runs_run_type_idx   ON ops.scrape_runs (run_type, started_at DESC);

-- Per-state outcome inside a run.
CREATE TABLE IF NOT EXISTS ops.scrape_state_results (
    id              BIGSERIAL   PRIMARY KEY,
    run_id          UUID        NOT NULL REFERENCES ops.scrape_runs(id) ON DELETE CASCADE,
    state           TEXT        NOT NULL,
    started_at      TIMESTAMPTZ,
    finished_at     TIMESTAMPTZ,
    status          TEXT        NOT NULL,       -- ok | no_new_data | failed | skipped | timeout
    rows_total      INTEGER,
    rows_new        INTEGER,
    period_latest   DATE,
    period_type     TEXT,
    elapsed_sec     NUMERIC,
    error_text      TEXT,
    metadata        JSONB
);
CREATE INDEX IF NOT EXISTS ssr_run_idx        ON ops.scrape_state_results (run_id);
CREATE INDEX IF NOT EXISTS ssr_state_time_idx ON ops.scrape_state_results (state, finished_at DESC);
CREATE INDEX IF NOT EXISTS ssr_status_idx     ON ops.scrape_state_results (status, finished_at DESC);

-- Suppression rules first so anomalies can FK to it.
CREATE TABLE IF NOT EXISTS ops.suppression_rules (
    id                BIGSERIAL   PRIMARY KEY,
    state             TEXT,                     -- NULL = all states
    check_name        TEXT,                     -- NULL = all checks
    pattern           TEXT,                     -- substring match on message
    operator_pattern  TEXT,                     -- substring match on operator
    period_before     DATE,                     -- only suppress anomalies <= this period
    reason            TEXT        NOT NULL,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by        TEXT
);

-- Anomalies surfaced during scrapes (HIGH/MEDIUM/LOW).
CREATE TABLE IF NOT EXISTS ops.anomalies (
    id                    BIGSERIAL   PRIMARY KEY,
    run_id                UUID        REFERENCES ops.scrape_runs(id) ON DELETE SET NULL,
    state                 TEXT        NOT NULL,
    check_name            TEXT        NOT NULL,
    severity              TEXT        NOT NULL,  -- high | medium | low
    period                DATE,
    message               TEXT,
    details               JSONB,
    detected_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    status                TEXT        NOT NULL DEFAULT 'open',  -- open | acked | resolved | suppressed
    suppressed_by_rule_id BIGINT      REFERENCES ops.suppression_rules(id) ON DELETE SET NULL,
    acked_by              TEXT,
    acked_at              TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS anomalies_state_idx  ON ops.anomalies (state, detected_at DESC);
CREATE INDEX IF NOT EXISTS anomalies_status_idx ON ops.anomalies (status, severity, detected_at DESC);

-- Source-page health probes (separate cadence from scrapes — early warning).
CREATE TABLE IF NOT EXISTS ops.source_health (
    id            BIGSERIAL   PRIMARY KEY,
    state         TEXT        NOT NULL,
    url           TEXT        NOT NULL,
    checked_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    http_code     INTEGER,
    content_hash  TEXT,
    content_bytes INTEGER,
    error_text    TEXT
);
CREATE INDEX IF NOT EXISTS source_health_state_idx ON ops.source_health (state, checked_at DESC);

-- Per-state overrides (e.g., temporarily skip in tier runs).
CREATE TABLE IF NOT EXISTS ops.state_overrides (
    state    TEXT        PRIMARY KEY,
    disabled BOOLEAN     NOT NULL DEFAULT FALSE,
    reason   TEXT,
    set_by   TEXT,
    set_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Audit log for every privileged action via the ops API.
CREATE TABLE IF NOT EXISTS ops.audit_log (
    id      BIGSERIAL   PRIMARY KEY,
    actor   TEXT        NOT NULL,
    action  TEXT        NOT NULL,
    params  JSONB,
    result  JSONB,
    ts      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS audit_log_ts_idx ON ops.audit_log (ts DESC);

-- Email send log — every send_notifications + welcome + manual send.
CREATE TABLE IF NOT EXISTS ops.email_log (
    id           BIGSERIAL   PRIMARY KEY,
    recipient    TEXT        NOT NULL,
    subject      TEXT        NOT NULL,
    sent_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    status       TEXT        NOT NULL,          -- sent | failed
    error_text   TEXT,
    run_id       UUID        REFERENCES ops.scrape_runs(id) ON DELETE SET NULL,
    body_snippet TEXT
);
CREATE INDEX IF NOT EXISTS email_log_sent_idx ON ops.email_log (sent_at DESC);

-- Permissions: osb_writer manages everything in ops for now.
-- Phase 1.2 introduces an osb_ops role with narrower grants.
GRANT USAGE ON SCHEMA ops TO osb_writer;
GRANT ALL  ON ALL TABLES   IN SCHEMA ops TO osb_writer;
GRANT ALL  ON ALL SEQUENCES IN SCHEMA ops TO osb_writer;
ALTER DEFAULT PRIVILEGES IN SCHEMA ops GRANT ALL ON TABLES    TO osb_writer;
ALTER DEFAULT PRIVILEGES IN SCHEMA ops GRANT ALL ON SEQUENCES TO osb_writer;
