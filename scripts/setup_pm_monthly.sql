-- Kalshi-business: monthly rollups + settled-market enrichment.
-- Apply with:  sudo -u postgres psql -d osb_data -f setup_pm_monthly.sql
-- Idempotent.

CREATE TABLE IF NOT EXISTS pm.platform_monthly (
    platform        TEXT NOT NULL,
    month_start     DATE NOT NULL,                 -- first day of the month
    category        TEXT NOT NULL DEFAULT '_all',  -- '_all' = platform total
    volume_usd      NUMERIC,
    n_settled       INTEGER,                       -- markets settled in this month
    n_new_listings  INTEGER,                       -- markets opened in this month
    top_market_vol  NUMERIC,                       -- single-event-dependence proxy
    top10_share     NUMERIC,                       -- 0-1; top-10 markets' share of vol
    hhi             NUMERIC,                       -- Herfindahl-Hirschman index, 0-1
    PRIMARY KEY (platform, month_start, category)
);
CREATE INDEX IF NOT EXISTS pm_monthly_month_idx ON pm.platform_monthly (month_start DESC);

GRANT SELECT ON pm.platform_monthly TO web_anon;
GRANT ALL    ON pm.platform_monthly TO osb_writer;

-- PostgREST-exposed view
CREATE OR REPLACE VIEW api.pm_platform_monthly AS
SELECT platform, month_start, category, volume_usd,
       n_settled, n_new_listings, top_market_vol, top10_share, hhi
  FROM pm.platform_monthly;

GRANT SELECT ON api.pm_platform_monthly TO web_anon;

-- A view that joins Kalshi's sports-category monthly volume with the existing
-- US sportsbook handle (sum of operator rows across all states). This is the
-- "Kalshi as 36th operator" comparison the analyst dashboard needs.
CREATE OR REPLACE VIEW api.pm_vs_sportsbook_monthly AS
WITH kalshi_sports AS (
    SELECT month_start AS month, volume_usd AS kalshi_sports_volume
      FROM pm.platform_monthly
     WHERE platform = 'kalshi' AND category = 'sports'
),
osb_handle AS (
    SELECT date_trunc('month', period_end)::date AS month,
           SUM(handle)        AS osb_handle,
           SUM(standard_ggr)  AS osb_ggr
      FROM api.monthly_data
     WHERE period_type = 'monthly'
       AND (operator_standard IS NULL OR operator_standard NOT IN ('TOTAL', 'ALL'))
       AND sport_category IS NULL
     GROUP BY 1
)
SELECT COALESCE(k.month, o.month)   AS month,
       k.kalshi_sports_volume,
       o.osb_handle,
       o.osb_ggr,
       CASE WHEN o.osb_handle > 0
            THEN k.kalshi_sports_volume / o.osb_handle
       END AS kalshi_share_of_handle
  FROM kalshi_sports k
  FULL OUTER JOIN osb_handle o ON o.month = k.month
 ORDER BY month;

GRANT SELECT ON api.pm_vs_sportsbook_monthly TO web_anon;
