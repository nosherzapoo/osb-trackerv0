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
-- period_type) plus period_end within a ±4-day window centered on
-- "exactly one year ago". For monthly data this catches the same calendar
-- month a year prior; for weekly data it catches the closest-aligned week
-- even when the day-of-week shifts by 1-2 days year-over-year.
--
-- We use LATERAL + ORDER BY ABS + LIMIT 1 to pick exactly one match per
-- row, avoiding the row multiplication that a plain LEFT JOIN with a
-- range would produce when two weekly periods both fall in the window.

DROP VIEW IF EXISTS api.monthly_data_yoy CASCADE;

CREATE VIEW api.monthly_data_yoy AS
SELECT
  m.*,
  py.handle        AS yoy_handle,
  py.standard_ggr  AS yoy_standard_ggr,
  py.hold_pct      AS yoy_hold_pct,
  py.period_end    AS yoy_period_end,
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
LEFT JOIN LATERAL (
  SELECT
    candidate.handle,
    candidate.standard_ggr,
    candidate.hold_pct,
    candidate.period_end
  FROM api.monthly_data candidate
  WHERE candidate.state_code        = m.state_code
    AND candidate.operator_standard = m.operator_standard
    AND candidate.channel           IS NOT DISTINCT FROM m.channel
    AND candidate.sport_category    IS NOT DISTINCT FROM m.sport_category
    AND candidate.period_type       = m.period_type
    AND candidate.period_end BETWEEN
            (m.period_end - INTERVAL '1 year' - INTERVAL '4 days')::date
        AND (m.period_end - INTERVAL '1 year' + INTERVAL '4 days')::date
  ORDER BY ABS(candidate.period_end - (m.period_end - INTERVAL '1 year')::date)
  LIMIT 1
) py ON TRUE;

GRANT SELECT ON api.monthly_data_yoy TO web_anon;
GRANT SELECT ON api.monthly_data_yoy TO authenticator;

NOTIFY pgrst, 'reload schema';
