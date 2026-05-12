// PostgREST client for the Kalshi-business views.

const API_BASE =
  import.meta.env.VITE_PM_API_BASE || 'https://api.osbdata.com';

async function _get(path) {
  const resp = await fetch(`${API_BASE}${path}`);
  if (!resp.ok) {
    throw new Error(`PostgREST ${path}: HTTP ${resp.status}`);
  }
  return resp.json();
}

export const biz = {
  /** All monthly rows for a platform — pivoted client-side into wide shape. */
  monthly: ({ platform = 'kalshi' } = {}) =>
    _get(
      `/pm_platform_monthly?platform=eq.${platform}` +
        '&order=month_start.asc&limit=10000',
    ),

  /** Kalshi sports volume joined with US sportsbook handle by month. */
  vsSportsbook: () =>
    _get('/pm_vs_sportsbook_monthly?order=month.asc&limit=200'),
};
