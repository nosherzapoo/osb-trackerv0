-- api.state_monthly
--   Server-side per-state aggregate. Each row = one (state, channel, period)
--   triple, summing handle/GGR/etc across all reporting operators.
--
-- Exposed automatically by PostgREST at /state_monthly.
--
-- Why this exists:
--   Only ~16 of 35 states publish an explicit TOTAL row in monthly_data.
--   For the remaining 19, you'd have to client-side sum operators. This view
--   gives a single endpoint that works uniformly across all 35.
--
-- Idempotent — safe to re-run.

DROP VIEW IF EXISTS api.state_monthly CASCADE;

CREATE VIEW api.state_monthly AS
WITH per_operator AS (
  -- Sum per-operator rows for states that publish operator-level detail.
  SELECT
    state_code,
    period_end,
    period_start,
    period_type,
    channel,
    SUM(handle)         AS handle,
    SUM(standard_ggr)   AS standard_ggr,
    SUM(gross_revenue)  AS gross_revenue,
    SUM(promo_credits)  AS promo_credits,
    SUM(net_revenue)    AS net_revenue,
    SUM(payouts)        AS payouts,
    SUM(tax_paid)       AS tax_paid,
    CASE
      WHEN SUM(handle) > 0
        THEN SUM(standard_ggr)::numeric / SUM(handle)::numeric
      ELSE NULL
    END AS hold_pct,
    COUNT(*)            AS operator_count,
    'sum_of_operators'::text AS source,
    -- Provenance: distinct file URLs and stable report URLs that fed this
    -- aggregate. STRING_AGG(DISTINCT, ' | ') captures every unique source —
    -- in most states a single PDF covers every operator, so the value
    -- collapses to one URL. Multi-source states (e.g. NJ tax-return +
    -- press-release) keep both visible.
    STRING_AGG(DISTINCT source_url, ' | ') FILTER (WHERE source_url IS NOT NULL)         AS source_url,
    STRING_AGG(DISTINCT source_report_url, ' | ') FILTER (WHERE source_report_url IS NOT NULL) AS source_report_url
  FROM api.monthly_data
  WHERE operator_standard IS NOT NULL
    AND operator_standard NOT IN ('TOTAL', 'ALL', 'UNKNOWN')
    AND sport_category IS NULL
  GROUP BY state_code, period_end, period_start, period_type, channel
),
totals AS (
  -- Verbatim TOTAL/ALL rows for states that only publish aggregate.
  SELECT
    state_code,
    period_end,
    period_start,
    period_type,
    channel,
    handle,
    standard_ggr,
    gross_revenue,
    promo_credits,
    net_revenue,
    payouts,
    tax_paid,
    hold_pct,
    NULL::bigint AS operator_count,
    'regulator_total'::text AS source,
    source_url,
    source_report_url
  FROM api.monthly_data
  WHERE operator_standard IN ('TOTAL', 'ALL')
    AND sport_category IS NULL
)
-- Use per-operator sums when available, fall back to TOTAL rows otherwise.
SELECT * FROM per_operator
UNION ALL
SELECT t.* FROM totals t
LEFT JOIN per_operator p
  ON p.state_code   = t.state_code
 AND p.period_end   = t.period_end
 AND p.period_type  = t.period_type
 AND p.channel      IS NOT DISTINCT FROM t.channel
WHERE p.state_code IS NULL;

GRANT SELECT ON api.state_monthly TO web_anon;
GRANT SELECT ON api.state_monthly TO authenticator;

NOTIFY pgrst, 'reload schema';
