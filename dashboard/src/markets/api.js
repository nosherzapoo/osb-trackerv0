// Thin PostgREST client for prediction-markets data. No auth required —
// pm.* views are exposed read-only via the existing api.osbdata.com surface.

const API_BASE =
  import.meta.env.VITE_PM_API_BASE || 'https://api.osbdata.com';

async function _get(path) {
  const resp = await fetch(`${API_BASE}${path}`);
  if (!resp.ok) {
    throw new Error(`PostgREST ${path}: HTTP ${resp.status}`);
  }
  return resp.json();
}

export const pm = {
  /** Top markets ranked by 24h volume — used by the overview table. */
  topMarkets: ({ platform = 'kalshi', category = null, limit = 50 } = {}) => {
    const qs = new URLSearchParams({
      platform: `eq.${platform}`,
      status: 'eq.active',
      order: 'volume_24h_usd.desc.nullslast',
      limit: String(limit),
    });
    if (category) qs.set('category', `eq.${category}`);
    return _get(`/pm_markets?${qs}`);
  },

  /** Headline counters for the strip above the top-markets table. */
  overviewCounters: ({ platform = 'kalshi' } = {}) =>
    _get(
      `/pm_markets?platform=eq.${platform}&status=eq.active` +
        '&select=volume_24h_usd,category,yes_price,yes_bid,yes_ask',
    ),

  /** Volume per day stacked by category, last 90 days. */
  platformDaily: ({ platform = 'kalshi', days = 90 } = {}) => {
    const since = new Date(Date.now() - days * 86400 * 1000)
      .toISOString()
      .slice(0, 10);
    return _get(
      `/pm_platform_daily?platform=eq.${platform}&date=gte.${since}` +
        '&order=date.asc&limit=10000',
    );
  },

  /** Single market record (full row, including last snapshot fields). */
  marketDetail: (id) =>
    _get(`/pm_markets?id=eq.${encodeURIComponent(id)}&limit=1`),

  /** Per-market intraday snapshots (≤7d). */
  marketSnapshots: ({ id, hours = 24 } = {}) => {
    const since = new Date(Date.now() - hours * 3600 * 1000).toISOString();
    return _get(
      `/pm_market_snapshots?market_id=eq.${encodeURIComponent(id)}` +
        `&taken_at=gte.${since}` +
        '&order=taken_at.asc&limit=2000',
    );
  },

  /** Per-market daily history (for the price chart). */
  marketDaily: ({ id, days = 90 } = {}) => {
    const since = new Date(Date.now() - days * 86400 * 1000)
      .toISOString()
      .slice(0, 10);
    return _get(
      `/pm_market_daily?market_id=eq.${encodeURIComponent(id)}` +
        `&date=gte.${since}&order=date.asc&limit=1000`,
    );
  },
};
