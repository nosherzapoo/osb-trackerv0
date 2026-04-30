// Thin fetch wrapper for the osb-ops-api. Bearer token from localStorage,
// auto-clears on 401 so the UI bounces back to login.

const TOKEN_KEY = 'osb_ops_token';
const TOKEN_EXP_KEY = 'osb_ops_token_exp';

export const API_BASE =
  import.meta.env.VITE_OPS_API_BASE || 'https://api.osbdata.com/ops';

export function getToken() {
  const tok = localStorage.getItem(TOKEN_KEY);
  const exp = Number(localStorage.getItem(TOKEN_EXP_KEY) || 0);
  if (!tok || !exp || Date.now() >= exp) {
    return null;
  }
  return tok;
}

export function setToken(token, ttlSec) {
  localStorage.setItem(TOKEN_KEY, token);
  localStorage.setItem(TOKEN_EXP_KEY, String(Date.now() + ttlSec * 1000));
}

export function clearToken() {
  localStorage.removeItem(TOKEN_KEY);
  localStorage.removeItem(TOKEN_EXP_KEY);
}

async function request(path, { method = 'GET', body, auth = true } = {}) {
  const headers = { 'Content-Type': 'application/json' };
  if (auth) {
    const tok = getToken();
    if (!tok) {
      const err = new Error('not authenticated');
      err.status = 401;
      throw err;
    }
    headers['Authorization'] = `Bearer ${tok}`;
  }
  const resp = await fetch(`${API_BASE}${path}`, {
    method,
    headers,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  if (resp.status === 401) {
    clearToken();
  }
  if (!resp.ok) {
    let detail = resp.statusText;
    try {
      const j = await resp.json();
      detail = j.detail || detail;
    } catch {}
    const err = new Error(detail);
    err.status = resp.status;
    throw err;
  }
  return resp.json();
}

export const api = {
  login: (username, password) =>
    request('/auth/login', { method: 'POST', body: { username, password }, auth: false }),
  me:               () => request('/auth/me'),
  systemHealth:     () => request('/system/health'),
  systemTimers:     () => request('/system/timers'),
  states:           () => request('/states'),
  state:        (code) => request(`/states/${code}`),
  runs: ({ limit = 50, offset = 0, run_type } = {}) => {
    const qs = new URLSearchParams({ limit, offset });
    if (run_type) qs.set('run_type', run_type);
    return request(`/runs?${qs}`);
  },
  run:        (runId) => request(`/runs/${runId}`),
  anomalies: ({ severity, status = 'open', state, limit = 100 } = {}) => {
    const qs = new URLSearchParams({ status, limit });
    if (severity) qs.set('severity', severity);
    if (state) qs.set('state', state);
    return request(`/anomalies?${qs}`);
  },
  audit: (limit = 100) => request(`/audit?limit=${limit}`),
  ackAnomaly: (id, note) =>
    request(`/anomalies/${id}/ack`, { method: 'POST', body: { note } }),
  resolveAnomaly: (id, note) =>
    request(`/anomalies/${id}/resolve`, { method: 'POST', body: { note } }),
  listSuppressionRules: () => request('/suppression-rules'),
  createSuppressionRule: (rule) =>
    request('/suppression-rules', { method: 'POST', body: rule }),
  deleteSuppressionRule: (id) =>
    request(`/suppression-rules/${id}`, { method: 'DELETE' }),
  sources:           () => request('/sources'),
  sourceHistory: (code) => request(`/sources/${code}`),
  scrapeState: (states, backfill = false) =>
    request('/actions/scrape-state', { method: 'POST', body: { states, backfill } }),
  scrapeTier: (tier) =>
    request('/actions/scrape-tier', { method: 'POST', body: { tier } }),
  setStateOverride: (code, disabled, reason) =>
    request(`/states/${code}/override`, { method: 'POST', body: { disabled, reason } }),
  jobs: ({ limit = 50, status } = {}) => {
    const qs = new URLSearchParams({ limit });
    if (status) qs.set('status', status);
    return request(`/jobs?${qs}`);
  },
  job: (id) => request(`/jobs/${id}`),
};
