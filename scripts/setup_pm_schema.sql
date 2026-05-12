-- Prediction Markets schema — Kalshi (phase 1), Polymarket (phase 2 will share this).
-- Apply with:  sudo -u postgres psql -d osb_data -f setup_pm_schema.sql
-- Idempotent.

CREATE SCHEMA IF NOT EXISTS pm;

-- One row per market — upserted on every ingest cycle.
-- id is "<platform>:<external_id>" so cross-platform analytics can union safely.
CREATE TABLE IF NOT EXISTS pm.markets (
    id              TEXT        PRIMARY KEY,
    platform        TEXT        NOT NULL,        -- 'kalshi' | 'polymarket'
    external_id     TEXT        NOT NULL,        -- Kalshi ticker / Polymarket id
    slug            TEXT,
    title           TEXT        NOT NULL,
    category        TEXT,                        -- politics|sports|crypto|weather|economics|entertainment|multivariate|other
    series_ticker   TEXT,                        -- Kalshi series grouping
    event_ticker    TEXT,
    status          TEXT,                        -- active|closed|settled|finalized
    open_at          TIMESTAMPTZ,
    close_at         TIMESTAMPTZ,
    settled_at       TIMESTAMPTZ,
    settled_outcome  TEXT,
    metadata        JSONB       NOT NULL DEFAULT '{}'::jsonb,
    first_seen_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS pm_markets_platform_status_idx ON pm.markets (platform, status);
CREATE INDEX IF NOT EXISTS pm_markets_category_idx        ON pm.markets (category);
CREATE INDEX IF NOT EXISTS pm_markets_close_at_idx        ON pm.markets (close_at)
    WHERE status = 'active';

-- High-frequency snapshots — top-N markets by 24h volume, every 15 min.
-- Long-tail markets are skipped at ingest time to keep snapshot volume bounded.
CREATE TABLE IF NOT EXISTS pm.market_snapshots (
    market_id        TEXT        NOT NULL REFERENCES pm.markets(id) ON DELETE CASCADE,
    taken_at         TIMESTAMPTZ NOT NULL,
    yes_price        NUMERIC,                    -- last trade price
    yes_bid          NUMERIC,
    yes_ask          NUMERIC,
    no_bid           NUMERIC,
    no_ask           NUMERIC,
    volume_total_usd NUMERIC,                    -- lifetime volume
    volume_24h_usd   NUMERIC,                    -- last 24h
    liquidity_usd    NUMERIC,
    open_interest    NUMERIC,
    PRIMARY KEY (market_id, taken_at)
);
CREATE INDEX IF NOT EXISTS pm_snap_taken_idx     ON pm.market_snapshots (taken_at DESC);
CREATE INDEX IF NOT EXISTS pm_snap_market_idx    ON pm.market_snapshots (market_id, taken_at DESC);

-- Per-market daily aggregates — rolled up from snapshots each night.
CREATE TABLE IF NOT EXISTS pm.market_daily (
    market_id    TEXT NOT NULL REFERENCES pm.markets(id) ON DELETE CASCADE,
    date         DATE NOT NULL,
    volume_usd   NUMERIC,
    open_yes     NUMERIC,
    high_yes     NUMERIC,
    low_yes      NUMERIC,
    close_yes    NUMERIC,
    PRIMARY KEY (market_id, date)
);
CREATE INDEX IF NOT EXISTS pm_daily_date_idx ON pm.market_daily (date DESC);

-- Platform-wide daily roll-up (for the volume time-series chart).
CREATE TABLE IF NOT EXISTS pm.platform_daily (
    platform        TEXT NOT NULL,
    date            DATE NOT NULL,
    category        TEXT NOT NULL DEFAULT '_all',
    volume_usd      NUMERIC,
    active_markets  INTEGER,
    new_markets     INTEGER,
    PRIMARY KEY (platform, date, category)
);
CREATE INDEX IF NOT EXISTS pm_platform_daily_date_idx ON pm.platform_daily (date DESC);

-- Permissions: osb_writer writes (ingest), web_anon reads (PostgREST).
GRANT USAGE ON SCHEMA pm TO osb_writer, web_anon;
GRANT ALL  ON ALL TABLES   IN SCHEMA pm TO osb_writer;
GRANT ALL  ON ALL SEQUENCES IN SCHEMA pm TO osb_writer;
GRANT SELECT ON ALL TABLES IN SCHEMA pm TO web_anon;
ALTER DEFAULT PRIVILEGES IN SCHEMA pm GRANT ALL    ON TABLES    TO osb_writer;
ALTER DEFAULT PRIVILEGES IN SCHEMA pm GRANT ALL    ON SEQUENCES TO osb_writer;
ALTER DEFAULT PRIVILEGES IN SCHEMA pm GRANT SELECT ON TABLES    TO web_anon;

-- --------------------------------------------------------------------------
-- PostgREST-exposed views in `api` schema. PostgREST already serves `api.*`
-- at api.osbdata.com; adding views here keeps the consumer URL shape
-- consistent without touching postgrest.conf.
-- --------------------------------------------------------------------------

-- The full catalog joined with the latest snapshot. One row per market.
CREATE OR REPLACE VIEW api.pm_markets AS
SELECT m.id,
       m.platform,
       m.external_id,
       m.slug,
       m.title,
       m.category,
       m.series_ticker,
       m.event_ticker,
       m.status,
       m.open_at,
       m.close_at,
       m.settled_at,
       m.settled_outcome,
       m.metadata,
       m.first_seen_at,
       m.last_seen_at,
       s.taken_at        AS snapshot_at,
       s.yes_price,
       s.yes_bid,
       s.yes_ask,
       s.no_bid,
       s.no_ask,
       s.volume_total_usd,
       s.volume_24h_usd,
       s.liquidity_usd,
       s.open_interest
  FROM pm.markets m
  LEFT JOIN LATERAL (
        SELECT *
          FROM pm.market_snapshots s
         WHERE s.market_id = m.id
         ORDER BY s.taken_at DESC
         LIMIT 1
  ) s ON TRUE;

-- Daily roll-up exposure for the time-series chart.
CREATE OR REPLACE VIEW api.pm_platform_daily AS
SELECT platform, date, category, volume_usd, active_markets, new_markets
  FROM pm.platform_daily;

-- Per-market daily history (used by market-detail price chart).
CREATE OR REPLACE VIEW api.pm_market_daily AS
SELECT market_id, date, volume_usd, open_yes, high_yes, low_yes, close_yes
  FROM pm.market_daily;

-- Per-market snapshot history (used by market-detail intraday chart).
CREATE OR REPLACE VIEW api.pm_market_snapshots AS
SELECT market_id, taken_at, yes_price, yes_bid, yes_ask, no_bid, no_ask,
       volume_24h_usd, liquidity_usd
  FROM pm.market_snapshots;

GRANT SELECT ON api.pm_markets, api.pm_platform_daily,
                api.pm_market_daily, api.pm_market_snapshots
  TO web_anon;
