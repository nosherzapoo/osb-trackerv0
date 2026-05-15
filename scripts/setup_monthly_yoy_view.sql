-- api.monthly_data_yoy
--   Same rows as api.monthly_data plus YoY columns (same month one year prior).
--   Exposed automatically by PostgREST at /monthly_data_yoy.
--
-- Idempotent — safe to re-run.
--
-- YoY semantics:
--   yoy_handle / yoy_standard_ggr / yoy_hold_pct = the prior-year row's values
--   yoy_handle_growth = (current - prior) / prior  (NULL when prior is 0/missing)
--   yoy_ggr_growth    = (current - prior) / prior
--   yoy_hold_diff     = current_hold - prior_hold (percentage-point delta as decimal)
--
-- Match key: (state_code, operator_standard, channel, sport_category,
-- period_type, period_end - 1 year). Weekly rows will rarely match because
-- the exact date one year prior is usually a different day-of-week — that's
-- expected; downstream consumers can filter to period_type=eq.monthly for
-- a complete picture.

DROP VIEW IF EXISTS api.monthly_data_yoy CASCADE;

CREATE VIEW api.monthly_data_yoy AS
SELECT
  m.*,
  py.handle        AS yoy_handle,
  py.standard_ggr  AS yoy_standard_ggr,
  py.hold_pct      AS yoy_hold_pct,
  CASE
    WHEN py.handle IS NULL OR py.handle = 0 THEN NULL
    ELSE (m.handle - py.handle)::numeric / py.handle::numeric
  END AS yoy_handle_growth,
  CASE
    WHEN py.standard_ggr IS NULL OR py.standard_ggr = 0 THEN NULL
    ELSE (m.standard_ggr - py.standard_ggr)::numeric / py.standard_ggr::numeric
  END AS yoy_ggr_growth,
  CASE
    WHEN py.hold_pct IS NULL OR m.hold_pct IS NULL THEN NULL
    ELSE (m.hold_pct - py.hold_pct)
  END AS yoy_hold_diff
FROM api.monthly_data m
LEFT JOIN api.monthly_data py
  ON py.state_code         = m.state_code
 AND py.operator_standard  = m.operator_standard
 AND py.channel            IS NOT DISTINCT FROM m.channel
 AND py.sport_category     IS NOT DISTINCT FROM m.sport_category
 AND py.period_type        = m.period_type
 AND py.period_end         = (m.period_end - INTERVAL '1 year')::date;

GRANT SELECT ON api.monthly_data_yoy TO web_anon;
GRANT SELECT ON api.monthly_data_yoy TO authenticator;

NOTIFY pgrst, 'reload schema';
